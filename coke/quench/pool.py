"""回用水池。

喷淋取水与回水补水都过这一个账本：水位下限按池容比例定死（见
:class:`coke.config.QuenchLimits`），不随班次用量漂移；累计取水量由取水动作
积分得出，与水位传感器无关——水位读数丢失时结算口径不变。水位回读可能丢失，
``read_level_m3`` 返回 ``None``，控制侧按保守方式（视同已到下限）处理。
"""

from __future__ import annotations

from ..config import QuenchLimits


class WaterPool:
    """回用水池水位与水量账本。"""

    def __init__(self, limits: QuenchLimits) -> None:
        self._limits = limits
        self._level_m3 = float(limits.pool_initial_m3)
        self._withdrawn_m3 = 0.0
        self._reading_lost = False

    @property
    def capacity_m3(self) -> float:
        return self._limits.pool_capacity_m3

    @property
    def low_limit_m3(self) -> float:
        """水位下限（按池容比例，定死）。"""

        return self._limits.pool_low_limit_m3

    @property
    def high_limit_m3(self) -> float:
        """水位上限（漫溢预警线）。"""

        return self._limits.pool_high_limit_m3

    @property
    def level_m3(self) -> float:
        """真实水位（账本值，仅供对账与验证读取）。"""

        return self._level_m3

    @property
    def withdrawn_m3(self) -> float:
        """累计取水量（流量计口径，结算依据）。"""

        return self._withdrawn_m3

    @property
    def reading_lost(self) -> bool:
        return self._reading_lost

    def read_level_m3(self) -> float | None:
        """水位回读；读数丢失时返回 ``None``。"""

        return None if self._reading_lost else self._level_m3

    def lose_reading(self) -> None:
        """模拟水位回读丢失。"""

        self._reading_lost = True

    def restore_reading(self) -> None:
        """恢复水位回读。"""

        self._reading_lost = False

    def headroom_m3(self) -> float:
        """距水位下限的可取水量。"""

        return max(0.0, self._level_m3 - self.low_limit_m3)

    def below_low(self) -> bool:
        """水位是否已触及下限。"""

        return self._level_m3 <= self.low_limit_m3

    def above_high(self) -> bool:
        """水位是否越过漫溢预警线。"""

        return self._level_m3 >= self.high_limit_m3

    def withdraw(self, amount_m3: float) -> float:
        """取水喷淋，返回实际取到的水量（不会把池子抽成负值）。"""

        if amount_m3 < 0:
            raise ValueError("取水量不能为负")
        actual = min(amount_m3, self._level_m3)
        self._level_m3 -= actual
        self._withdrawn_m3 += actual
        return actual

    def recharge(self, dt: float) -> float:
        """按回水速率补水，返回实际补入水量（不越过池容，防漫溢）。"""

        if dt <= 0:
            raise ValueError("步长必须为正")
        amount = self._limits.pool_recharge_m3_h * dt / 3600.0
        actual = min(amount, self.capacity_m3 - self._level_m3)
        self._level_m3 += actual
        return actual

    def recharge(self, dt: float) -> float:
        """按回水速率补水，返回实际补入水量（不越过池容，防漫溢）。"""

        if dt <= 0:
            raise ValueError("步长必须为正")
        amount = self._limits.pool_recharge_m3_h * dt / 3600.0
        actual = min(amount, self.capacity_m3 - self._level_m3)
        self._level_m3 += actual
        return actual
