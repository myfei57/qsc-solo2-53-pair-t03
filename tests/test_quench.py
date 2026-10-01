"""动态熄焦喷淋、回用水池联锁与审计。"""

from __future__ import annotations

from coke.app import build_platform
from coke.config import default_config
from coke.quench.machine import QuenchCar
from coke.quench.tank import ReuseTank
from coke.sequence import quench_oven


def test_temperature_feedback_completes_quench_without_fixed_duration(tmp_path):
    platform = build_platform(default_config(), storage_root=str(tmp_path / "state"))
    report = platform.quench(oven="oven01", initial_temperature_c=1100.0, levels_m3=[170.0])
    assert report["ok"] is True
    status = platform.status()["quench"]
    assert status["spray_complete"] is True
    assert status["temperature_c"] <= default_config().quench.temp_target_c
    assert status["spray_intensity_m3_h"] == 0.0
    assert status["spray_requested_m3_h"] == 0.0
    assert status["tank_level_m3"] is not None
    assert report["water_settlement"]["water_withdrawn_m3"] > 0.0
    assert abs(
        report["water_settlement"]["water_withdrawn_m3"] - status["water_m3"]
    ) <= 0.01


def test_low_level_policy_is_fixed_per_heat_and_reduces_flow(tmp_path):
    config = default_config()
    quench = QuenchCar(config.quench)
    limits = config.quench
    level = limits.tank_capacity_m3 * (limits.tank_low_fraction - 0.01)
    report = quench_oven(
        quench,
        "oven01",
        coke_t=limits.default_coke_t,
        initial_temperature_c=1100.0,
        levels_m3=[level],
        low_level_policy="reduce",
    )
    assert report["low_level_policy"] == "reduce"
    warning = next(step for step in report["steps"] if step["action"] == "tank_low_level")
    assert warning["severity"] == "warning"
    assert warning["detail"]["low_level_policy"] == "reduce"
    assert quench.snapshot().tank_level_m3 >= limits.tank_capacity_m3 * limits.tank_low_low_fraction


def test_low_level_alarm_policy_keeps_requested_flow(tmp_path):
    config = default_config()
    quench = QuenchCar(config.quench)
    limits = config.quench
    level = limits.tank_capacity_m3 * (limits.tank_low_fraction - 0.01)
    report = quench_oven(
        quench,
        "oven01",
        initial_temperature_c=1100.0,
        levels_m3=[level],
        low_level_policy="alarm",
    )
    warning = next(step for step in report["steps"] if step["action"] == "tank_low_level")
    assert warning["severity"] == "alarm"
    assert report["low_level_policy"] == "alarm"


def test_lost_level_is_partial_failure_and_conservatively_stops_spray(tmp_path):
    platform = build_platform(default_config(), storage_root=str(tmp_path / "state"))
    report = platform.quench(
        oven="oven01",
        initial_temperature_c=1100.0,
        levels_m3=[170.0, *([None] * 500)],
        low_level_policy="reduce",
    )
    assert report["ok"] is False
    assert "level_reading_lost" in report["degraded"]
    assert "spray_incomplete" in report["violations"]
    assert platform.status()["quench"]["spray_suspended"] is True
    alarms = platform.audit.alarms()
    assert any(item["action"] == "level_reading_lost" for item in alarms)
    assert any(item["action"] == "spray_suspended" for item in alarms)


def test_lost_temperature_uses_fallback_and_cannot_complete(tmp_path):
    platform = build_platform(default_config(), storage_root=str(tmp_path / "state"))
    report = platform.quench(oven="oven01", initial_temperature_c=None, levels_m3=[170.0])
    assert report["ok"] is False
    assert "temperature_reading_lost" in report["degraded"]
    assert "spray_timeout" in report["violations"]
    status = platform.status()["quench"]
    assert status["temperature_valid"] is False
    assert status["spray_requested_m3_h"] == default_config().quench.water_flow_fallback_m3_h
    assert status["spray_intensity_m3_h"] == 0.0


def test_spray_recovery_request_is_idempotent(tmp_path):
    platform = build_platform(default_config(), storage_root=str(tmp_path / "state"))
    platform.quench(
        oven="oven01",
        initial_temperature_c=1100.0,
        levels_m3=[170.0, *([None] * 500)],
        low_level_policy="reduce",
        dt=1.0,
    )
    first = platform.recover_quench("manual-1")
    second = platform.recover_quench("manual-1")
    assert first == second
    assert first["resumed"] is False
    platform.quench_car.tank.measurement(170.0)
    recovered = platform.recover_quench("manual-2")
    repeated = platform.recover_quench("manual-2")
    assert recovered["resumed"] is True
    assert repeated == recovered


def test_water_settlement_balances_and_is_audited(tmp_path):
    platform = build_platform(default_config(), storage_root=str(tmp_path / "state"))
    report = platform.quench(oven="oven01", initial_temperature_c=1100.0, levels_m3=[170.0])
    settlement = report["water_settlement"]
    assert "water_settlement_mismatch" not in report["violations"]
    assert (
        abs(
            settlement["water_withdrawn_m3"]
            - settlement["water_returned_m3"]
            - settlement["water_consumed_m3"]
        )
        <= 0.01
    )
    entries = platform.audit.entries(limit=20)
    assert any(item["action"] == "spray" for item in entries)
    detail = next(item["payload"] for item in entries if item["action"] == "spray")
    assert detail["water_withdrawn_m3"] == settlement["water_withdrawn_m3"]
    assert detail["tank_level_fraction"] == settlement["tank_level_fraction"]


def test_hard_limits_are_based_on_tank_capacity_not_shift_usage():
    config = default_config()
    limits = config.quench
    tank = ReuseTank(limits)
    low_low_m3 = limits.tank_capacity_m3 * limits.tank_low_low_fraction
    tank.measurement(low_low_m3)
    assert tank.state() == "low_low"
    step = tank.step(limits.water_flow_max_m3_h, 1.0)
    assert step.withdrawn_m3 == 0.0
    assert step.blocked is True
