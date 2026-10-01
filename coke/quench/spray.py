"""熄焦喷淋。

喷淋强度按焦炭温度回读动态调节：流量 = 满强度流量 × 当前强度比例。完成判据
是"浇透"（焦炭温度降到目标值，由编排回写 ``mark_quenched``），最长喷淋时长
只是防止温度回读长期异常的安全帽。低水位时编排可挂起喷淋（``hold``），恢复
凭令牌：同一令牌的重复恢复是幂等重放，不会重复开水阀、重复计量。
"""

from __future__ import annotations

from ..errors import InterlockError


class Spray:
    """熄焦喷淋控制器（动态强度 + 挂起/恢复）。"""

    def __init__(self, duration_s: float, water_flow_m3_h: float) -> None:
        self._duration_s = float(duration_s)
        self._water_flow_m3_h = float(water_flow_m3_h)
        self._elapsed_s = 0.0
        self._water_m3 = 0.0
        self._open = False
        self._intensity = 1.0
        self._quenched = False
        self._degraded = False
        self._hold_token: str | None = None
        self._hold_seq = 0

    @property
    def duration_s(self) -> float:
        return self._duration_s

    @property
    def elapsed_s(self) -> float:
        """累计喷淋时长。"""

        return self._elapsed_s

    @property
    def water_flow_m3_h(self) -> float:
        return self._water_flow_m3_h

    @property
    def open(self) -> bool:
        return self._open

    @property
    def intensity(self) -> float:
        """当前喷淋强度比例（0..1）。"""

        return self._intensity

    @property
    def degraded(self) -> bool:
        """是否处于读数丢失的保守降级模式。"""

        return self._degraded

    @property
    def held(self) -> bool:
        """喷淋是否被低水位策略挂起。"""

        return self._hold_token is not None

    @property
    def hold_token(self) -> str | None:
        return self._hold_token

    def set_intensity(self, intensity: float) -> float:
        """设定下一步的喷淋强度比例。"""

        if not 0.0 <= intensity <= 1.0:
            raise ValueError("喷淋强度必须落在 [0, 1] 区间")
        self._intensity = float(intensity)
        return self._intensity

    def set_degraded(self, degraded: bool) -> None:
        """标记/解除读数丢失的降级模式。"""

        self._degraded = bool(degraded)

    def open_valve(self) -> None:
        """开喷淋阀。"""

        self._open = True

    def close_valve(self) -> None:
        """关喷淋阀。"""

        self._open = False

    def mark_quenched(self) -> None:
        """回写"已浇透"：焦炭温度已降到目标值，喷淋完成。"""

        self._quenched = True
        self.close_valve()

    def hold(self, reason: str) -> str:
        """挂起喷淋（低水位保水位），返回恢复令牌；重复挂起返回同一令牌。"""

        if self._hold_token is None:
            self._hold_seq += 1
            self._hold_token = f"hold-{self._hold_seq}"
            self.close_valve()
        return self._hold_token

    def resume(self, token: str | None = None) -> dict:
        """凭令牌恢复喷淋；重复恢复是幂等重放，不会重复开阀。

        令牌缺省时取当前挂起令牌。令牌与当前挂起不符且喷淋并未挂起时，视为
        上一轮恢复的重复触发，按幂等重放处理。
        """

        if self._hold_token is None:
            return {"resumed": False, "idempotent_replay": True, "token": token}
        expected = token if token is not None else self._hold_token
        if expected != self._hold_token:
            raise InterlockError("恢复令牌与当前挂起不符，拒绝恢复", component="quench")
        used = self._hold_token
        self._hold_token = None
        self.open_valve()
        return {"resumed": True, "idempotent_replay": False, "token": used}

    def step(self, dt: float) -> float:
        """按当前强度累加喷淋量与时长，返回本次喷水量。"""

        if not self._open:
            raise InterlockError("喷淋阀未开，无法喷淋", component="quench")
        if self.held:
            raise InterlockError("喷淋已挂起，恢复后才能继续喷淋", component="quench")
        if dt <= 0:
            raise ValueError("步长必须为正")
        amount = self._water_flow_m3_h * self._intensity * dt / 3600.0
        self._elapsed_s += dt
        self._water_m3 += amount
        if self._elapsed_s >= self._duration_s:
            self.close_valve()
        return amount

    def complete(self) -> bool:
        """喷淋是否结束（已浇透或触到最长时长帽）。"""

        return self._quenched or self._elapsed_s >= self._duration_s

    def quenched(self) -> bool:
        """是否以"浇透"方式完成（区别于时长帽兜底）。"""

        return self._quenched

    def water_m3(self) -> float:
        """返回累计喷水量。"""

        return self._water_m3
