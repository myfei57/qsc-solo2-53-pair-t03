"""熄焦车状态快照。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class QuenchSnapshot:
    """熄焦车某一时刻的状态。"""

    oven: str
    loaded: bool
    travelled_m: float
    distance_m: float
    at_tower: bool
    spraying: bool
    spray_complete: bool
    water_m3: float
    intensity: float
    coke_temp_c: float | None
    temp_lost: bool
    pool_level_m3: float | None
    pool_low_limit_m3: float
    pool_high_limit_m3: float
    pool_low: bool
    pool_overflow: bool
    level_lost: bool
    held: bool
    degraded: bool
    low_level_policy: str

    def as_dict(self) -> dict:
        return {
            "oven": self.oven,
            "loaded": self.loaded,
            "travelled_m": round(self.travelled_m, 2),
            "distance_m": round(self.distance_m, 2),
            "at_tower": self.at_tower,
            "spraying": self.spraying,
            "spray_complete": self.spray_complete,
            "water_m3": round(self.water_m3, 3),
            "intensity": round(self.intensity, 4),
            "coke_temp_c": None if self.coke_temp_c is None else round(self.coke_temp_c, 1),
            "temp_lost": self.temp_lost,
            "pool_level_m3": None if self.pool_level_m3 is None else round(self.pool_level_m3, 2),
            "pool_low_limit_m3": round(self.pool_low_limit_m3, 2),
            "pool_high_limit_m3": round(self.pool_high_limit_m3, 2),
            "pool_low": self.pool_low,
            "pool_overflow": self.pool_overflow,
            "level_lost": self.level_lost,
            "held": self.held,
            "degraded": self.degraded,
            "low_level_policy": self.low_level_policy,
        }
