"""熄焦车状态机。

接焦只在焦侧进行；接焦之后才能走行，走行到位之后才能开喷淋。喷淋阈前检查
避免了"没走行到位就喷淋"这类会把红焦留在轨道上的顺序错误。

喷淋强度按焦炭温度回读动态调节：越接近浇透目标强度越低；温度回读丢失时按
最热处理拉满强度并退化为时长帽兜底。回用水池水位参与每一步决策：水位临近
下限（水位 − 本炉剩余预计需水 ≤ 下限）时按本炉策略处理——``throttle`` 降
喷淋保水位，``maintain`` 维持喷淋并报警；水位回读丢失视同已到下限，按保守
方式处理。水位下限按池容比例定死，不随班次用量漂移。
"""

from __future__ import annotations

from ..config import QuenchLimits
from ..errors import CokeError, InterlockError
from .bed import CokeBed
from .pool import WaterPool
from .snapshot import QuenchSnapshot
from .spray import Spray
from .travel import TravelPlan

LOW_LEVEL_POLICIES = ("throttle", "maintain")


class QuenchCar:
    """熄焦车状态机。"""

    def __init__(self, limits: QuenchLimits) -> None:
        self._limits = limits
        self._plan = TravelPlan(limits.travel_distance_m, limits.travel_speed_m_s)
        self._spray = Spray(limits.spray_duration_s, limits.water_flow_m3_h)
        self._pool = WaterPool(limits)
        self._bed = CokeBed(limits)
        self._oven = ""
        self._loaded = False
        self._travelled_m = 0.0
        self._low_level_policy = "throttle"
        self._batch_withdrawn_start_m3 = 0.0
        self._alarm_raised = False

    @property
    def plan(self) -> TravelPlan:
        return self._plan

    @property
    def spray(self) -> Spray:
        return self._spray

    @property
    def pool(self) -> WaterPool:
        return self._pool

    @property
    def bed(self) -> CokeBed:
        return self._bed

    @property
    def low_level_policy(self) -> str:
        return self._low_level_policy

    def receive(self, oven: str) -> None:
        """在焦侧接焦。"""

        self.reset()
        self._oven = oven
        self._loaded = True

    def travel(self, dt: float) -> float:
        """走行到熄焦塔，返回新的已走行距离。"""

        if not self._loaded:
            raise InterlockError("熄焦车未接焦，禁止走行", component="quench")
        self._travelled_m = self._plan.step(self._travelled_m, dt)
        if self.at_tower():
            self._loaded = False
        return self._travelled_m

    def at_tower(self) -> bool:
        """是否已走行到熄焦塔。"""

        return self._travelled_m >= self._plan.distance_m

    def start_spray(self, low_level_policy: str = "throttle") -> None:
        """开喷淋；未走行到位或已喷淋过即拒绝。低水位策略按炉次指定。"""

        if low_level_policy not in LOW_LEVEL_POLICIES:
            raise CokeError(f"低水位策略必须是 {LOW_LEVEL_POLICIES} 之一", component="quench")
        if not self.at_tower():
            raise InterlockError("熄焦车未走行到位，禁止喷淋", component="quench")
        if self._spray.complete():
            raise InterlockError("本炉已喷淋完成，禁止重复喷淋", component="quench")
        self._low_level_policy = low_level_policy
        self._batch_withdrawn_start_m3 = self._pool.withdrawn_m3
        self._alarm_raised = False
        self._spray.open_valve()

    def _base_intensity(self, temp_c: float | None) -> float:
        """按温度回读计算强度：温度丢失按最热处理拉满（保守）。"""

        if temp_c is None:
            return 1.0
        span = self._limits.coke_initial_c - self._limits.quench_target_c
        ratio = (temp_c - self._limits.quench_target_c) / span
        ratio = min(1.0, max(0.0, ratio))
        return self._limits.min_intensity + (1.0 - self._limits.min_intensity) * ratio

    def _remaining_need_m3(self, temp_c: float | None) -> float:
        """估算本炉剩余需水量（临近下限判断的提前量）。"""

        if temp_c is None:
            # 温度丢失：按最坏情况（满强度喷到时长帽）估算。
            remaining_s = max(0.0, self._spray.duration_s - self._spray.elapsed_s)
            return self._limits.water_flow_m3_h * remaining_s / 3600.0
        over_c = max(0.0, temp_c - self._limits.quench_target_c)
        return over_c / self._limits.cool_rate_c_per_m3

    def spray_step(self, dt: float) -> dict:
        """按步长推进喷淋，返回本步轨迹（强度、回读、挂起与告警标志）。"""

        if dt <= 0:
            raise ValueError("步长必须为正")
        temp_c = self._bed.read_c()
        level_m3 = self._pool.read_level_m3()
        degraded = temp_c is None or level_m3 is None
        self._spray.set_degraded(degraded)

        intensity = self._base_intensity(temp_c)
        low_level = False
        alarm = False

        if level_m3 is None:
            # 水位回读丢失：视同已到下限，按本炉策略保守处理。
            low_level = True
            if self._low_level_policy == "throttle":
                intensity = min(intensity, self._net_zero_intensity())
            else:
                alarm = True
        else:
            need = self._remaining_need_m3(temp_c)
            if level_m3 - need <= self._pool.low_limit_m3:
                low_level = True
                if self._low_level_policy == "throttle":
                    intensity = min(intensity, self._net_zero_intensity())
                    if self._pool.headroom_m3() <= 0.0 and not self._spray.held:
                        self._spray.hold("pool_low")
                else:
                    alarm = True

        if self._spray.held:
            # 水位恢复到下限加回差以上才允许恢复；恢复动作幂等。
            resume_level = self._pool.low_limit_m3 + self._limits.pool_capacity_m3 * 0.02
            recovered = level_m3 is not None and level_m3 >= resume_level
            if recovered:
                self._spray.resume()
            else:
                self._pool.recharge(dt)
                return self._trace(
                    dt, 0.0, self._bed.read_c(), self._pool.read_level_m3(), degraded, low_level, bool(alarm), False
                )

        alarm_raised_now = False
        if alarm and not self._alarm_raised:
            self._alarm_raised = True
            alarm_raised_now = True

        self._spray.set_intensity(intensity)
        intended = self._limits.water_flow_m3_h * intensity * dt / 3600.0
        actual = self._pool.withdraw(intended)
        starved = actual < intended - 1e-9
        if starved:
            # 池子被抽空：只按实际取到的水计量，保证喷淋账与水池账一致。
            self._spray.set_intensity(actual * 3600.0 / (self._limits.water_flow_m3_h * dt))
        self._pool.recharge(dt)
        amount = self._spray.step(dt)
        self._bed.cool(amount)

        if temp_c is not None and self._bed.read_c() <= self._limits.quench_target_c:
            self._spray.mark_quenched()
        trace = self._trace(
            dt, amount, self._bed.read_c(), self._pool.read_level_m3(), degraded, low_level, bool(alarm), alarm_raised_now
        )
        trace["starved"] = starved
        return trace

    def _net_zero_intensity(self) -> float:
        """净取水为零的强度（喷多少回多少，保水位）。"""

        if self._limits.water_flow_m3_h <= 0:
            return 0.0
        return min(1.0, self._limits.pool_recharge_m3_h / self._limits.water_flow_m3_h)

    def _trace(
        self,
        dt: float,
        amount: float,
        temp_c: float | None,
        level_m3: float | None,
        degraded: bool,
        low_level: bool,
        alarm: bool,
        alarm_raised: bool,
    ) -> dict:
        return {
            "dt_s": dt,
            "intensity": round(self._spray.intensity, 4),
            "amount_m3": round(amount, 4),
            "temp_c": None if temp_c is None else round(temp_c, 1),
            "level_m3": None if level_m3 is None else round(level_m3, 2),
            "held": self._spray.held,
            "degraded": degraded,
            "low_level": low_level,
            "alarm": alarm,
            "alarm_raised": alarm_raised,
        }

    def resume_spray(self, token: str | None = None) -> dict:
        """恢复被挂起的喷淋；重复触发按幂等重放处理。"""

        return self._spray.resume(token)

    def settlement(self) -> dict:
        """本炉水量结算：喷淋累计与水池取水两本账必须一致。"""

        sprayed = self._spray.water_m3()
        drawn = self._pool.withdrawn_m3 - self._batch_withdrawn_start_m3
        return {
            "water_m3": round(sprayed, 3),
            "pool_drawn_m3": round(drawn, 3),
            "matched": abs(sprayed - drawn) < 1e-6,
        }

    def reset(self) -> None:
        """复位走行与喷淋，准备下一炉（接焦时自动调用）。"""

        self._loaded = False
        self._travelled_m = 0.0
        self._spray = Spray(self._limits.spray_duration_s, self._limits.water_flow_m3_h)
        self._bed.reload()
        self._low_level_policy = "throttle"
        self._alarm_raised = False

    def snapshot(self) -> QuenchSnapshot:
        """返回熄焦车状态快照。"""

        return QuenchSnapshot(
            oven=self._oven,
            loaded=self._loaded,
            travelled_m=self._travelled_m,
            distance_m=self._plan.distance_m,
            at_tower=self.at_tower(),
            spraying=self._spray.open,
            spray_complete=self._spray.complete(),
            water_m3=self._spray.water_m3(),
            intensity=self._spray.intensity,
            coke_temp_c=self._bed.read_c(),
            temp_lost=self._bed.reading_lost,
            pool_level_m3=self._pool.read_level_m3(),
            pool_low_limit_m3=self._pool.low_limit_m3,
            pool_high_limit_m3=self._pool.high_limit_m3,
            pool_low=self._pool.below_low(),
            pool_overflow=self._pool.above_high(),
            level_lost=self._pool.reading_lost,
            held=self._spray.held,
            degraded=self._spray.degraded,
            low_level_policy=self._low_level_policy,
        )
