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
    spray_timed_out: bool
    water_m3: float
    temperature_c: float | None
    temperature_valid: bool
    spray_intensity_m3_h: float
    spray_requested_m3_h: float
    spray_suspended: bool
    suspended_reason: str
    elapsed_s: float
    spraying_s: float
    soak_s: float
    tank_level_m3: float | None
    tank_level_fraction: float | None
    tank_level_valid: bool
    tank_state: str

    def as_dict(self) -> dict:
        return {
            "oven": self.oven,
            "loaded": self.loaded,
            "travelled_m": round(self.travelled_m, 2),
            "distance_m": round(self.distance_m, 2),
            "at_tower": self.at_tower,
            "spraying": self.spraying,
            "spray_complete": self.spray_complete,
            "spray_timed_out": self.spray_timed_out,
            "water_m3": round(self.water_m3, 3),
            "temperature_c": round(self.temperature_c, 3) if self.temperature_c is not None else None,
            "temperature_valid": self.temperature_valid,
            "spray_intensity_m3_h": round(self.spray_intensity_m3_h, 3),
            "spray_requested_m3_h": round(self.spray_requested_m3_h, 3),
            "spray_suspended": self.spray_suspended,
            "suspended_reason": self.suspended_reason,
            "elapsed_s": round(self.elapsed_s, 2),
            "spraying_s": round(self.spraying_s, 2),
            "soak_s": round(self.soak_s, 2),
            "tank_level_m3": round(self.tank_level_m3, 3) if self.tank_level_m3 is not None else None,
            "tank_level_fraction": round(self.tank_level_fraction, 4)
            if self.tank_level_fraction is not None
            else None,
            "tank_level_valid": self.tank_level_valid,
            "tank_state": self.tank_state,
        }
