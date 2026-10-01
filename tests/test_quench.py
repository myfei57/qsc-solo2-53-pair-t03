"""熄焦动态喷淋与回用水池联动验证。

覆盖：按温度回读动态调强度、浇透判据、水位下限（按池容定死）、低水位策略按
炉次（throttle 降喷淋保水位 / maintain 维持喷淋报警）、回读丢失的保守处理、
喷淋恢复幂等、水量结算与审计一致、控制台展示字段。
"""

from __future__ import annotations

import dataclasses

import pytest

from coke.app import build_platform
from coke.config import ConfigError, QuenchLimits, default_config
from coke.errors import CokeError
from coke.quench.machine import QuenchCar
from coke.sequence import quench_oven


def _platform(tmp_path, clock, **quench_overrides):
    config = default_config()
    if quench_overrides:
        config = dataclasses.replace(
            config, quench=dataclasses.replace(config.quench, **quench_overrides)
        )
    return build_platform(config, clock=clock, storage_root=str(tmp_path / "state"))


def test_dynamic_intensity_follows_temperature(tmp_path, clock):
    """强度随温度回读从满强度逐渐下调，浇透判据是温度而不是时长。"""

    platform = _platform(tmp_path, clock)
    report = platform.quench(oven="oven01")
    assert report["ok"] is True
    assert report["settlement"]["matched"] is True

    trace = report["trace"]
    assert trace[0]["intensity"] == pytest.approx(1.0)
    assert trace[-1]["intensity"] < trace[0]["intensity"]
    assert all(record["intensity"] >= platform.config.quench.min_intensity - 1e-9 for record in trace)
    # 温度降到目标即完成，而不是喷满时长帽。
    assert platform.quench_car.spray.quenched() is True
    assert platform.quench_car.spray.elapsed_s < platform.config.quench.spray_duration_s
    assert trace[-1]["temp_c"] <= platform.config.quench.quench_target_c


def test_hotter_coke_takes_more_water(tmp_path, clock):
    """焦炭越热（天冷天热、焦炭多少的代理），动态喷淋用水越多。"""

    cool = _platform(tmp_path / "cool", clock)
    cool_report = cool.quench(oven="oven01")

    hot = _platform(tmp_path / "hot", clock, coke_initial_c=1150.0)
    hot_report = hot.quench(oven="oven01")

    assert hot_report["settlement"]["water_m3"] > cool_report["settlement"]["water_m3"]


def test_low_limit_is_capacity_based(tmp_path, clock):
    """水位下限定死按池容比例计算，与班次用量无关。"""

    platform = _platform(tmp_path, clock)
    limits = platform.config.quench
    assert platform.quench_car.pool.low_limit_m3 == pytest.approx(
        limits.pool_capacity_m3 * limits.pool_low_ratio
    )
    assert limits.pool_low_limit_m3 == pytest.approx(limits.pool_capacity_m3 * limits.pool_low_ratio)


def test_pool_ratio_validation():
    """下限比例必须小于上限比例，装配期就拦下。"""

    with pytest.raises(ConfigError):
        QuenchLimits.from_mapping(
            {
                "travel_distance_m": 42.0,
                "travel_speed_m_s": 1.6,
                "spray_duration_s": 240.0,
                "water_flow_m3_h": 320.0,
                "quench_target_c": 80.0,
                "coke_initial_c": 1050.0,
                "min_intensity": 0.35,
                "cool_rate_c_per_m3": 100.0,
                "pool_capacity_m3": 60.0,
                "pool_low_ratio": 0.9,
                "pool_high_ratio": 0.5,
                "pool_recharge_m3_h": 120.0,
                "pool_initial_m3": 45.0,
            }
        )


def test_throttle_policy_protects_level(tmp_path, clock):
    """throttle：水位临近下限时降到净零强度保水位，水位始终不低于下限。"""

    platform = _platform(tmp_path, clock, pool_initial_m3=20.0)
    report = platform.quench(oven="oven01", low_level_policy="throttle")
    actions = [step["action"] for step in report["steps"]]
    assert "spray_throttled" in actions
    low_limit = platform.quench_car.pool.low_limit_m3
    levels = [record["level_m3"] for record in report["trace"] if record["level_m3"] is not None]
    assert min(levels) >= low_limit - 1e-6
    assert report["settlement"]["matched"] is True


def test_throttle_holds_and_resumes_at_floor(tmp_path, clock):
    """水位触及下限时挂起喷淋，回水到位后自动恢复，恢复动作幂等。"""

    platform = _platform(tmp_path, clock, pool_initial_m3=18.0)
    report = platform.quench(oven="oven01", low_level_policy="throttle")
    actions = [step["action"] for step in report["steps"]]
    assert "spray_held" in actions
    assert "spray_resumed" in actions
    assert "spray_held_at_end" not in report["violations"]
    # 喷淋已结束，再次恢复是幂等重放，不重复开阀、不重复计量。
    result = platform.resume_quench()
    assert result["resumed"] is False
    assert result["idempotent_replay"] is True
    assert report["settlement"]["matched"] is True


def test_maintain_policy_keeps_spraying_and_alarms(tmp_path, clock):
    """maintain：水位临近下限时维持喷淋并报警，报警落审计。"""

    platform = _platform(tmp_path, clock, pool_initial_m3=20.0)
    report = platform.quench(oven="oven01", low_level_policy="maintain")
    actions = [step["action"] for step in report["steps"]]
    assert "pool_low_alarm" in actions
    assert "spray_throttled" not in actions
    alarms = platform.audit.alarms()
    assert any(entry["action"] == "pool_low_alarm" for entry in alarms)
    assert report["settlement"]["matched"] is True


def test_maintain_starves_pool_but_books_stay_consistent(tmp_path, clock):
    """maintain 到底把池子抽空时记 pool_starved，两本账仍然一致。"""

    platform = _platform(tmp_path, clock, pool_initial_m3=5.0, pool_recharge_m3_h=0.0)
    report = platform.quench(oven="oven01", low_level_policy="maintain")
    assert "pool_starved" in report["violations"]
    assert report["ok"] is False
    assert report["settlement"]["matched"] is True
    assert platform.quench_car.pool.level_m3 == pytest.approx(0.0)


def test_temp_reading_lost_sprays_conservatively(tmp_path, clock):
    """温度回读丢失：拉满强度、退化为时长帽，标记 degraded 并落审计。"""

    platform = _platform(tmp_path, clock)
    platform.quench_sensor_fault("coke_temp", True)
    report = platform.quench(oven="oven01")
    assert report["degraded"] is True
    assert all(record["intensity"] == pytest.approx(1.0) for record in report["trace"])
    assert platform.quench_car.spray.quenched() is False
    assert "not_quenched" in report["violations"]
    actions = [step["action"] for step in report["steps"]]
    assert "sensor_lost" in actions
    assert any(entry["action"] == "sensor_lost" for entry in platform.audit.entries(50))
    assert report["settlement"]["matched"] is True


def test_level_reading_lost_throttles_conservatively(tmp_path, clock):
    """水位回读丢失视同到下限：throttle 直接压到净零强度。"""

    platform = _platform(tmp_path, clock)
    platform.quench_sensor_fault("pool_level", True)
    report = platform.quench(oven="oven01", low_level_policy="throttle")
    assert report["degraded"] is True
    net_zero = platform.config.quench.pool_recharge_m3_h / platform.config.quench.water_flow_m3_h
    assert all(record["intensity"] <= net_zero + 1e-9 for record in report["trace"])
    assert report["settlement"]["matched"] is True


def test_level_reading_lost_maintain_alarms(tmp_path, clock):
    """水位回读丢失 + maintain：维持喷淋并报警。"""

    platform = _platform(tmp_path, clock)
    platform.quench_sensor_fault("pool_level", True)
    report = platform.quench(oven="oven01", low_level_policy="maintain")
    actions = [step["action"] for step in report["steps"]]
    assert "pool_low_alarm" in actions
    assert report["degraded"] is True


def test_sensor_fault_rejects_unknown_sensor(tmp_path, clock):
    platform = _platform(tmp_path, clock)
    with pytest.raises(CokeError):
        platform.quench_sensor_fault("bogus", True)


def test_invalid_policy_rejected(tmp_path, clock):
    platform = _platform(tmp_path, clock)
    with pytest.raises(CokeError):
        platform.quench(oven="oven01", low_level_policy="guess")


def test_resume_is_idempotent_under_repeated_triggers(tmp_path, clock):
    """恢复动作重复触发：第一次生效，后续都是幂等重放，不重复计量。"""

    car = QuenchCar(default_config().quench)
    car.receive("oven01")
    while not car.at_tower():
        car.travel(1.0)
    car.start_spray("throttle")
    token = car.spray.hold("pool_low")
    first = car.resume_spray(token)
    assert first["resumed"] is True
    again = car.resume_spray(token)
    assert again["resumed"] is False
    assert again["idempotent_replay"] is True
    third = car.resume_spray()
    assert third["idempotent_replay"] is True
    assert car.spray.water_m3() == pytest.approx(0.0)


def test_settlement_matches_pool_ledger(tmp_path, clock):
    """喷淋累计水量与水池累计取水量两本账一致，并落审计。"""

    platform = _platform(tmp_path, clock)
    report = platform.quench(oven="oven01")
    settlement = report["settlement"]
    assert settlement["matched"] is True
    assert settlement["water_m3"] == pytest.approx(platform.quench_car.spray.water_m3(), abs=1e-3)
    entries = platform.audit.entries(100)
    settled = [entry for entry in entries if entry["action"] == "quench_settled"]
    assert settled and settled[0]["payload"]["water_m3"] == pytest.approx(settlement["water_m3"])


def test_status_and_trend_expose_spray_and_pool(tmp_path, clock):
    """控制台状态与趋势能看到喷淋强度与水池水位。"""

    platform = _platform(tmp_path, clock)
    platform.quench(oven="oven01")
    status = platform.status()
    quench = status["quench"]
    for key in ("intensity", "coke_temp_c", "pool_level_m3", "pool_low_limit_m3", "low_level_policy"):
        assert key in quench
    trend = platform.trend.snapshot()
    assert trend["spray_intensity"]["latest"] is not None
    assert trend["pool_level_m3"]["latest"] is not None
    assert trend["coke_temp_c"]["latest"] is not None


def test_console_quench_passes_policy(tmp_path, clock):
    """控制台接口按炉次透传低水位策略，并暴露恢复与传感器演练接口。"""

    from coke.console.api import Api

    platform = _platform(tmp_path, clock)
    api = Api(platform)
    code, payload = api.handle(
        "POST", "/api/quench", {}, {"oven": "oven01", "low_level_policy": "maintain"}
    )
    assert code == 200
    assert payload["low_level_policy"] == "maintain"

    code, payload = api.handle("POST", "/api/quench/sensor", {}, {"sensor": "coke_temp", "lost": True})
    assert code == 200 and payload["lost"] is True

    code, payload = api.handle("POST", "/api/quench/resume", {}, {})
    assert code == 200
    assert payload["idempotent_replay"] is True

    code, payload = api.handle("POST", "/api/quench", {}, {"low_level_policy": "bogus"})
    assert code == 409


def test_pool_recharge_never_overflows(tmp_path, clock):
    """回水补水不越过池容，不会把池子灌漫。"""

    platform = _platform(tmp_path, clock, pool_initial_m3=59.9)
    pool = platform.quench_car.pool
    pool.recharge(3600.0)
    assert pool.level_m3 <= pool.capacity_m3
    assert pool.level_m3 == pytest.approx(pool.capacity_m3)
