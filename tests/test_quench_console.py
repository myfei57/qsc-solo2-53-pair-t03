"""动态熄焦喷淋控制台接口。"""

from __future__ import annotations

from coke.app import build_platform
from coke.config import default_config
from coke.console.api import Api


def test_quench_console_reports_dynamic_status(tmp_path):
    api = Api(build_platform(default_config(), storage_root=str(tmp_path / "state")))
    status, payload = api.handle(
        "POST",
        "/api/quench",
        {},
        {"initial_temperature_c": 1100.0, "levels_m3": [170.0], "low_level_policy": "alarm"},
    )
    assert status == 200
    assert payload["ok"] is True
    assert payload["low_level_policy"] == "alarm"
    assert "water_settlement" in payload

    status, status_payload = api.handle("GET", "/api/status", {}, {})
    assert status == 200
    assert "quench_tank" in status_payload
    assert status_payload["quench"]["spray_complete"] is True


def test_quench_recovery_endpoint_is_idempotent(tmp_path):
    platform = build_platform(default_config(), storage_root=str(tmp_path / "state"))
    api = Api(platform)
    api.handle(
        "POST",
        "/api/quench",
        {},
        {"initial_temperature_c": 1100.0, "levels_m3": [170.0, *([None] * 500)]},
    )
    status, first = api.handle("POST", "/api/quench/recover", {}, {"recovery_id": "console-1"})
    status_again, second = api.handle("POST", "/api/quench/recover", {}, {"recovery_id": "console-1"})
    assert status == 200 and status_again == 200
    assert first == second
