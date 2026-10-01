"""熄焦喷淋强度控制。

喷淋不再以固定时长作为完成条件，而是根据焦炭回读温度计算流量：温度越高，
流量越大；温度进入目标带后维持最小流量并持续确认。温度读数丢失是部分失败，
系统改用保守流量且不得按温度确认完成。喷淋暂停、恢复和本炉累计水量都在
控制器内保持同一口径。
"""

from __future__ import annotations

from ..config import QuenchLimits
from ..errors import InterlockError


class Spray:
    """温度反馈的熄焦喷淋控制器。"""

    def __init__(self, limits: QuenchLimits) -> None:
        self._limits = limits
        self._coke_t = limits.default_coke_t
        self._temperature_c: float | None = None
        self._temperature_valid = False
        self._requested_flow_m3_h = limits.water_flow_fallback_m3_h
        self._actual_flow_m3_h = 0.0
        self._elapsed_s = 0.0
        self._spraying_s = 0.0
        self._soak_s = 0.0
        self._water_m3 = 0.0
        self._open = False
        self._started = False
        self._finished = False
        self._suspended_reason = ""
        self._last_recovery_id = ""
        self._last_recovery_result: dict | None = None

    @property
    def coke_t(self) -> float:
        return self._coke_t

    @property
    def open(self) -> bool:
        return self._open

    @property
    def started(self) -> bool:
        return self._started

    @property
    def suspended(self) -> bool:
        return bool(self._suspended_reason)

    @property
    def suspended_reason(self) -> str:
        return self._suspended_reason

    @property
    def temperature_valid(self) -> bool:
        return self._temperature_valid

    @property
    def temperature_c(self) -> float | None:
        return self._temperature_c

    @property
    def requested_flow_m3_h(self) -> float:
        return self._requested_flow_m3_h

    def begin(self, coke_t: float, temperature_c: float | None = None) -> None:
        """开始一炉喷淋，记录焦炭量与初始温度回读。"""

        if coke_t <= 0:
            raise ValueError("焦炭量必须为正")
        self._coke_t = float(coke_t)
        self._elapsed_s = 0.0
        self._spraying_s = 0.0
        self._soak_s = 0.0
        self._water_m3 = 0.0
        self._open = True
        self._started = True
        self._finished = False
        self._suspended_reason = ""
        self._actual_flow_m3_h = self._limits.water_flow_fallback_m3_h
        self._last_recovery_id = ""
        self._last_recovery_result = None
        self.update_temperature(temperature_c)

    def update_temperature(self, temperature_c: float | None) -> None:
        """更新焦炭温度回读；``None``、NaN 或负值视为读数丢失。"""

        if temperature_c is None or not isinstance(temperature_c, (int, float)) or isinstance(temperature_c, bool):
            self._temperature_valid = False
            self._temperature_c = None
            self._requested_flow_m3_h = self._limits.water_flow_fallback_m3_h
            return
        value = float(temperature_c)
        if value != value or value < 0.0:
            self._temperature_valid = False
            self._temperature_c = None
            self._requested_flow_m3_h = self._limits.water_flow_fallback_m3_h
            return

        self._temperature_valid = True
        self._temperature_c = value
        if not self._started or self.complete():
            self._requested_flow_m3_h = 0.0
            return
        excess_c = max(0.0, value - self._limits.temp_target_c)
        # 热系数是 m3/(t·°C)；除以 60 秒形成温和的温度反馈流量，再由设备流量上下限截断。
        thermal_flow = self._limits.thermal_m3_per_t_c * self._coke_t * excess_c * 60.0
        self._requested_flow_m3_h = min(
            self._limits.water_flow_max_m3_h,
            max(self._limits.water_flow_min_m3_h, thermal_flow),
        )

    def suspend(self, reason: str) -> dict:
        """暂停喷淋；已暂停时重复调用不产生新的状态变化。"""

        if not reason:
            raise ValueError("暂停原因不能为空")
        changed = False
        if self._open:
            self._open = False
            changed = True
        if self._suspended_reason != reason:
            self._suspended_reason = reason
            changed = True
        self._last_recovery_id = ""
        self._last_recovery_result = None
        return {"suspended": changed, "reason": reason, "already_suspended": not changed}

    def resume(self, recovery_id: str, *, allowed: bool = True, blocked_state: str = "") -> dict:
        """按幂等键恢复喷淋，重复请求返回第一次结果。"""

        if not recovery_id:
            raise ValueError("恢复请求标识不能为空")
        if recovery_id == self._last_recovery_id:
            return dict(self._last_recovery_result or {"resumed": False})

        result: dict
        if not self._started:
            raise InterlockError("喷淋尚未开始，不能恢复", component="quench")
        if self.complete():
            result = {"resumed": False, "already_complete": True}
        elif not allowed:
            result = {"resumed": False, "blocked_state": blocked_state}
        elif self.suspended:
            self._open = True
            self._suspended_reason = ""
            result = {"resumed": True}
        else:
            result = {"resumed": False, "already_active": True}
        self._last_recovery_id = recovery_id
        self._last_recovery_result = dict(result)
        return result

    def apply_water(self, dt: float, water_m3: float, actual_flow_m3_h: float | None = None) -> float:
        """按水池实抽水量推进温度模型，返回新的温度。"""

        if not self._started:
            raise InterlockError("喷淋未开始，无法结算水量", component="quench")
        if dt <= 0:
            raise ValueError("步长必须为正")
        if water_m3 < 0:
            raise ValueError("喷水量不得为负")

        self._actual_flow_m3_h = self._requested_flow_m3_h if actual_flow_m3_h is None else float(actual_flow_m3_h)
        self._elapsed_s += dt
        completed_before = self.complete()
        if self._open and water_m3 > 0:
            self._spraying_s += dt
            self._water_m3 += water_m3
            heat_capacity = self._limits.thermal_m3_per_t_c * self._coke_t
            if self._temperature_valid and self._temperature_c is not None:
                cooled = water_m3 / heat_capacity
                self._temperature_c = max(0.0, self._temperature_c - cooled)
                if self._temperature_c <= self._limits.temp_target_c:
                    self._soak_s += dt
                else:
                    self._soak_s = 0.0
        if not completed_before and self.complete():
            self._finished = True
            self._open = False
            self._suspended_reason = ""
            self._actual_flow_m3_h = 0.0
        elif not self.complete() and self._elapsed_s >= self._limits.spray_max_s:
            self._finished = True
            self._open = False
            self._actual_flow_m3_h = 0.0
        if self._temperature_valid and not self.complete() and self._open:
            self.update_temperature(self._temperature_c)
        elif self.complete():
            self._requested_flow_m3_h = 0.0
        return self._temperature_c if self._temperature_c is not None else float("nan")

    def complete(self) -> bool:
        """温度有效、达标并持续确认后才算浇透。"""

        return (
            self._started
            and self._temperature_valid
            and self._temperature_c is not None
            and self._temperature_c <= self._limits.temp_target_c
            and self._soak_s >= self._limits.temp_soak_s
        )

    def timed_out(self) -> bool:
        """是否达到最大喷淋保护时间但未能确认浇透。"""

        return self._finished and not self.complete()

    def water_m3(self) -> float:
        """返回控制器侧累计喷水量，应与水池取水量一致。"""

        return self._water_m3

    def snapshot(self) -> dict:
        """返回喷淋强度与确认状态。"""

        return {
            "spraying": self._open,
            "spray_started": self._started,
            "spray_suspended": self.suspended,
            "suspended_reason": self._suspended_reason,
            "spray_complete": self.complete(),
            "spray_timed_out": self.timed_out(),
            "temperature_c": round(self._temperature_c, 3) if self._temperature_c is not None else None,
            "temperature_valid": self._temperature_valid,
            "coke_t": round(self._coke_t, 3),
            "spray_intensity_m3_h": round(self._actual_flow_m3_h if self._open else 0.0, 3),
            "spray_requested_m3_h": round(self._requested_flow_m3_h, 3),
            "elapsed_s": round(self._elapsed_s, 2),
            "spraying_s": round(self._spraying_s, 2),
            "soak_s": round(self._soak_s, 2),
            "water_m3": round(self._water_m3, 4),
        }
