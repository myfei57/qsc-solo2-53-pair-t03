"""焦炭温度模型与回读。

红焦温度随喷水量下降；温度回读可能丢失（``read_c`` 返回 ``None``），控制侧
按保守方式处理：按最热处理拉满喷淋强度，完成判据退化为最长喷淋时长帽。
"""

from __future__ import annotations

from ..config import QuenchLimits

AMBIENT_C = 40.0


class CokeBed:
    """焦炭床温度与回读状态。"""

    def __init__(self, limits: QuenchLimits) -> None:
        self._limits = limits
        self._temp_c = float(limits.coke_initial_c)
        self._reading_lost = False

    @property
    def temp_c(self) -> float:
        """真实温度（模型值，仅供验证读取）。"""

        return self._temp_c

    @property
    def reading_lost(self) -> bool:
        return self._reading_lost

    def read_c(self) -> float | None:
        """温度回读；读数丢失时返回 ``None``。"""

        return None if self._reading_lost else self._temp_c

    def lose_reading(self) -> None:
        """模拟温度回读丢失。"""

        self._reading_lost = True

    def restore_reading(self) -> None:
        """恢复温度回读。"""

        self._reading_lost = False

    def cool(self, water_m3: float) -> float:
        """按本次喷水量降温，返回新的温度。"""

        if water_m3 < 0:
            raise ValueError("喷水量不能为负")
        self._temp_c = max(AMBIENT_C, self._temp_c - water_m3 * self._limits.cool_rate_c_per_m3)
        return self._temp_c

    def quenched(self) -> bool:
        """是否已浇透（温度降到目标值以下）。"""

        return self._temp_c <= self._limits.quench_target_c

    def reload(self) -> None:
        """接新一炉红焦，温度回到初始值。"""

        self._temp_c = float(self._limits.coke_initial_c)
