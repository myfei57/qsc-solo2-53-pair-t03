"""熄焦回用水池。

水位下限固定按水池物理容积计算：low-low 是硬联锁余量，low 是按炉次策略
处理的预警线，不随班次用量漂移。水池同时记录喷淋取水量、喷淋回水量和补水
量，保证控制台展示、本炉结算和审计使用同一份水量账。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import QuenchLimits


@dataclass(frozen=True)
class TankStep:
    """一个控制周期内的水池水量变化。"""

    requested_m3_h: float
    withdrawn_m3: float
    returned_m3: float
    makeup_m3: float
    level_valid: bool
    state: str
    blocked: bool
    reduced: bool

    def as_dict(self) -> dict:
        return {
            "requested_m3_h": round(self.requested_m3_h, 3),
            "withdrawn_m3": round(self.withdrawn_m3, 4),
            "returned_m3": round(self.returned_m3, 4),
            "makeup_m3": round(self.makeup_m3, 4),
            "level_valid": self.level_valid,
            "state": self.state,
            "blocked": self.blocked,
            "reduced": self.reduced,
        }


class ReuseTank:
    """回用水池与取水泵联锁。"""

    def __init__(self, limits: QuenchLimits) -> None:
        self._limits = limits
        self._level_m3 = limits.tank_initial_m3
        self._level_valid = True
        self._withdrawn_m3 = 0.0
        self._returned_m3 = 0.0
        self._makeup_m3 = 0.0
        self._return_flow_m3_h = 0.0
        self._last_result: TankStep | None = None

    @property
    def limits(self) -> QuenchLimits:
        return self._limits

    @property
    def level_m3(self) -> float:
        return self._level_m3

    @property
    def level_valid(self) -> bool:
        return self._level_valid

    @property
    def withdrawn_m3(self) -> float:
        return self._withdrawn_m3

    @property
    def returned_m3(self) -> float:
        return self._returned_m3

    @property
    def makeup_m3(self) -> float:
        return self._makeup_m3

    def level_fraction(self) -> float | None:
        """返回当前水位占池容比例；读数丢失时返回 ``None``。"""

        if not self._level_valid:
            return None
        return self._level_m3 / self._limits.tank_capacity_m3

    def state(self) -> str:
        """返回水池联锁状态。"""

        if not self._level_valid:
            return "unknown"
        fraction = self.level_fraction() or 0.0
        if self._level_m3 <= self._limits.tank_low_low_fraction * self._limits.tank_capacity_m3:
            return "low_low"
        if self._level_m3 <= self._limits.tank_low_fraction * self._limits.tank_capacity_m3:
            return "low"
        if self._level_m3 >= self._limits.tank_high_high_fraction * self._limits.tank_capacity_m3:
            return "high_high"
        if self._level_m3 >= self._limits.tank_high_fraction * self._limits.tank_capacity_m3:
            return "high"
        return "normal"

    def measurement(self, level_m3: float | None) -> None:
        """更新水位回读；``None``、NaN 或越界均视为读数丢失。"""

        if level_m3 is None or not isinstance(level_m3, (int, float)) or isinstance(level_m3, bool):
            self._level_valid = False
            return
        value = float(level_m3)
        if value != value or value < 0.0 or value > self._limits.tank_capacity_m3:
            self._level_valid = False
            return
        self._level_valid = True
        self._level_m3 = value

    def _safe_withdraw_flow(
        self, requested_m3_h: float, low_policy: str, dt: float
    ) -> tuple[float, bool, bool]:
        state_before = self.state()
        hard_blocked = state_before in ("unknown", "low_low", "high_high")
        blocked = hard_blocked
        reduced = False
        flow = max(0.0, float(requested_m3_h))
        if state_before == "high":
            flow = min(flow, self._limits.water_flow_min_m3_h)
            reduced = True
        if state_before == "low":
            if low_policy == "alarm":
                reduced = False
            else:
                flow = min(flow, self._limits.water_flow_min_m3_h)
                reduced = True
        elif state_before == "low_low":
            flow = 0.0
        if blocked:
            flow = 0.0
        flow = min(flow, self._limits.water_flow_max_m3_h)
        if self._level_valid and state_before not in ("high", "high_high", "low_low"):
            reserve_m3 = self._limits.tank_low_low_fraction * self._limits.tank_capacity_m3
            available_m3 = max(0.0, self._level_m3 - reserve_m3)
            flow = min(flow, available_m3 * 3600.0 / dt)
            if requested_m3_h > 0.0 and flow <= 0.0:
                blocked = True
        return flow, blocked, reduced

    def step(self, requested_m3_h: float, dt: float, *, low_policy: str = "reduce") -> TankStep:
        """结算一个周期的取水、回水和补水。"""

        if dt <= 0:
            raise ValueError("步长必须为正")
        if low_policy not in ("reduce", "alarm"):
            raise ValueError("低水位策略必须是 reduce 或 alarm")

        state_before = self.state()
        flow, blocked, reduced = self._safe_withdraw_flow(requested_m3_h, low_policy, dt)
        withdrawn = min(flow * dt / 3600.0, self._level_m3)
        if state_before in ("unknown", "high_high"):
            withdrawn = 0.0
        self._level_m3 -= withdrawn
        self._withdrawn_m3 += withdrawn

        returned = 0.0
        if self.state() != "high_high" and self._level_valid:
            delayed = min(self._return_flow_m3_h * dt / 3600.0, self._limits.tank_capacity_m3)
            capacity = max(0.0, self._limits.tank_capacity_m3 - self._level_m3)
            returned = min(delayed, capacity)
            self._level_m3 += returned
            self._returned_m3 += returned
        self._return_flow_m3_h = flow * self._limits.tank_return_fraction

        makeup = 0.0
        if self._level_valid and self.state() in ("normal", "low"):
            makeup_capacity = max(0.0, self._limits.tank_capacity_m3 - self._level_m3)
            target_m3 = self._limits.tank_low_fraction * self._limits.tank_capacity_m3
            makeup = min(self._limits.tank_makeup_m3_h * dt / 3600.0, makeup_capacity)
            if self.state() == "low":
                makeup = min(makeup, max(0.0, target_m3 - self._level_m3))
            self._level_m3 += makeup
            self._makeup_m3 += makeup

        result = TankStep(
            requested_m3_h=requested_m3_h,
            withdrawn_m3=withdrawn,
            returned_m3=returned,
            makeup_m3=makeup,
            level_valid=self._level_valid,
            state=self.state(),
            blocked=self.state() in ("unknown", "low_low", "high_high"),
            reduced=reduced or (0.0 < flow < requested_m3_h),
        )
        self._last_result = result
        return result

    def settle(self) -> dict:
        """返回本炉水量结算。"""

        consumed = self._withdrawn_m3 - self._returned_m3
        net_demand = consumed + self._makeup_m3
        fraction = self.level_fraction()
        return {
            "tank_capacity_m3": self._limits.tank_capacity_m3,
            "tank_level_m3": round(self._level_m3, 4),
            "tank_level_fraction": round(fraction, 4) if fraction is not None else None,
            "tank_level_valid": self._level_valid,
            "tank_state": self.state(),
            "water_withdrawn_m3": round(self._withdrawn_m3, 4),
            "water_returned_m3": round(self._returned_m3, 4),
            "water_makeup_m3": round(self._makeup_m3, 4),
            "water_consumed_m3": round(consumed, 4),
            "water_net_demand_m3": round(net_demand, 4),
        }

    def reset(self) -> None:
        """复位到本炉初始水位与累计水量。"""

        self._withdrawn_m3 = 0.0
        self._returned_m3 = 0.0
        self._makeup_m3 = 0.0
        self._return_flow_m3_h = 0.0
        self._last_result = None
        self._level_m3 = self._limits.tank_initial_m3
        self._level_valid = True

    def snapshot(self) -> dict:
        """返回控制台和审计共用的水位快照。"""

        fraction = self.level_fraction()
        return {
            **self.settle(),
            "last_step": self._last_result.as_dict() if self._last_result else None,
            "tank_level_fraction": round(fraction, 4) if fraction is not None else None,
        }
