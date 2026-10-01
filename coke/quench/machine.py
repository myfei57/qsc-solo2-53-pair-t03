"""熄焦车状态机。

接焦只在焦侧进行；接焦之后才能走行，走行到位之后才能开喷淋。喷淋阈前检查
避免了"没走行到位就喷淋"这类会把红焦留在轨道上的顺序错误。动态喷淋期间由
温度控制器决定强度，由回用水池硬联锁决定实际可取水量。
"""

from __future__ import annotations

from ..config import QuenchLimits
from ..errors import InterlockError
from .snapshot import QuenchSnapshot
from .spray import Spray
from .tank import ReuseTank
from .travel import TravelPlan


class QuenchCar:
    """熄焦车状态机。"""

    def __init__(self, limits: QuenchLimits) -> None:
        self._limits = limits
        self._plan = TravelPlan(limits.travel_distance_m, limits.travel_speed_m_s)
        self._tank = ReuseTank(limits)
        self._spray = Spray(limits)
        self._oven = ""
        self._loaded = False
        self._travelled_m = 0.0
        self._low_level_policy = "reduce"

    @property
    def plan(self) -> TravelPlan:
        return self._plan

    @property
    def spray(self) -> Spray:
        return self._spray

    @property
    def tank(self) -> ReuseTank:
        return self._tank

    @property
    def low_level_policy(self) -> str:
        return self._low_level_policy

    def set_low_level_policy(self, policy: str) -> None:
        """按炉次固定低水位策略：``reduce`` 降喷淋或 ``alarm`` 维持报警。"""

        if policy not in ("reduce", "alarm"):
            raise ValueError("低水位策略必须是 reduce 或 alarm")
        self._low_level_policy = policy

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

    def start_spray(
        self,
        coke_t: float | None = None,
        temperature_c: float | None = None,
        low_level_policy: str | None = None,
    ) -> None:
        """开喷淋；未走行到位或已喷淋过即拒绝。"""

        if not self.at_tower():
            raise InterlockError("熄焦车未走行到位，禁止喷淋", component="quench")
        if self._spray.started:
            raise InterlockError("本炉已喷淋开始，禁止重复开喷淋", component="quench")
        if low_level_policy is not None:
            self.set_low_level_policy(low_level_policy)
        if self._tank.state() in ("unknown", "low_low", "high_high"):
            raise InterlockError(f"回用水池状态 {self._tank.state()} 不允许开喷淋", component="quench")
        self._tank.reset()
        self._spray.begin(self._limits.default_coke_t if coke_t is None else coke_t, temperature_c)

    def spray_step(self, dt: float) -> dict:
        """按步长推进动态喷淋与水池结算，返回本周期结果。"""

        if not self._spray.started:
            raise InterlockError("喷淋未开始，无法推进", component="quench")
        if self._spray.complete():
            return {"withdrawn_m3": 0.0}

        if self._spray.suspended:
            tank_step = self._tank.step(0.0, dt, low_policy=self._low_level_policy)
            temperature_c = self._spray.apply_water(dt, 0.0, 0.0)
            if not tank_step.blocked and self._tank.state() not in ("unknown", "low_low", "high_high"):
                self._spray.resume(f"{self._oven}:auto")
            return {
                "requested_m3_h": 0.0,
                "withdrawn_m3": 0.0,
                "returned_m3": tank_step.returned_m3,
                "makeup_m3": tank_step.makeup_m3,
                "temperature_c": temperature_c,
                "tank_state": tank_step.state,
                "blocked": tank_step.blocked,
                "reduced": tank_step.reduced,
                "resumed": not self._spray.suspended,
            }

        requested = self._spray.requested_flow_m3_h
        tank_step = self._tank.step(requested, dt, low_policy=self._low_level_policy)
        temperature_c = self._spray.apply_water(
            dt,
            tank_step.withdrawn_m3,
            tank_step.withdrawn_m3 * 3600.0 / dt,
        )
        if tank_step.blocked:
            self._spray.suspend(f"tank_{tank_step.state}")
        return {
            "requested_m3_h": tank_step.requested_m3_h,
            "withdrawn_m3": tank_step.withdrawn_m3,
            "returned_m3": tank_step.returned_m3,
            "makeup_m3": tank_step.makeup_m3,
            "temperature_c": temperature_c,
            "tank_state": tank_step.state,
            "blocked": tank_step.blocked,
            "reduced": tank_step.reduced,
            "resumed": False,
        }

    def recover_spray(self, recovery_id: str) -> dict:
        """控制台手动恢复喷淋；相同恢复标识重复调用保持幂等。"""

        state = self._tank.state()
        blocked = state in ("unknown", "low_low", "high_high")
        return self._spray.resume(recovery_id, allowed=not blocked, blocked_state=state)

    def reset(self) -> None:
        """复位走行与喷淋，准备下一炉（接焦时自动调用）。"""

        self._loaded = False
        self._travelled_m = 0.0
        self._spray = Spray(self._limits)
        self._tank.reset()
        self._low_level_policy = "reduce"

    def snapshot(self) -> QuenchSnapshot:
        """返回熄焦车状态快照。"""

        spray = self._spray.snapshot()
        tank = self._tank.snapshot()
        return QuenchSnapshot(
            oven=self._oven,
            loaded=self._loaded,
            travelled_m=self._travelled_m,
            distance_m=self._plan.distance_m,
            at_tower=self.at_tower(),
            spraying=spray["spraying"],
            spray_complete=spray["spray_complete"],
            spray_timed_out=spray["spray_timed_out"],
            water_m3=spray["water_m3"],
            temperature_c=spray["temperature_c"],
            temperature_valid=spray["temperature_valid"],
            spray_intensity_m3_h=spray["spray_intensity_m3_h"],
            spray_requested_m3_h=spray["spray_requested_m3_h"],
            spray_suspended=spray["spray_suspended"],
            suspended_reason=spray["suspended_reason"],
            elapsed_s=spray["elapsed_s"],
            spraying_s=spray["spraying_s"],
            soak_s=spray["soak_s"],
            tank_level_m3=tank["tank_level_m3"],
            tank_level_fraction=tank["tank_level_fraction"],
            tank_level_valid=tank["tank_level_valid"],
            tank_state=tank["tank_state"],
        )
