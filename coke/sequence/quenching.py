"""熄焦顺序：接焦 → 走行 → 温度反馈喷淋。"""

from __future__ import annotations

import math

from ..quench.machine import QuenchCar

MAX_STEPS = 2400
VALID_POLICIES = ("reduce", "alarm")


def _finite(value: float | None, default: float) -> float:
    if value is None:
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def quench_oven(
    quench: QuenchCar,
    oven: str,
    dt: float = 1.0,
    *,
    coke_t: float | None = None,
    initial_temperature_c: float | None = None,
    temperatures_c: list[float | None] | None = None,
    levels_m3: list[float | None] | None = None,
    low_level_policy: str = "reduce",
) -> dict:
    """执行一炉熄焦，返回带步骤、降级项、违约项与水量结算的报告。"""

    if low_level_policy not in VALID_POLICIES:
        raise ValueError("低水位策略必须是 reduce 或 alarm")
    if dt <= 0:
        raise ValueError("步长必须为正")
    if coke_t is not None and (not math.isfinite(_finite(coke_t, float("nan"))) or coke_t <= 0):
        raise ValueError("焦炭量必须为正数")

    steps: list[dict] = []
    violations: list[str] = []
    degraded: list[str] = []
    warnings: list[dict] = []

    quench.receive(oven)
    steps.append({"component": "quench", "action": "receive", "detail": {"oven": oven}})

    for _ in range(MAX_STEPS):
        if quench.at_tower():
            break
        quench.travel(dt)
    steps.append(
        {
            "component": "quench",
            "action": "travel",
            "detail": {"travelled_m": round(quench.snapshot().travelled_m, 2)},
        }
    )
    if not quench.at_tower():
        violations.append("travel_incomplete")
        return {"oven": oven, "ok": False, "steps": steps, "violations": violations, "degraded": degraded}

    initial_temp = _finite(initial_temperature_c, float("nan"))
    start_temp = initial_temp if math.isfinite(initial_temp) else None
    quench.start_spray(coke_t=coke_t, temperature_c=start_temp, low_level_policy=low_level_policy)
    start_snapshot = quench.snapshot()
    steps.append(
        {
            "component": "quench",
            "action": "start_spray",
            "detail": {
                "coke_t": round(quench.spray.coke_t, 3),
                "temperature_c": start_snapshot.temperature_c,
                "temperature_valid": start_snapshot.temperature_valid,
                "low_level_policy": low_level_policy,
                "spray_intensity_m3_h": start_snapshot.spray_intensity_m3_h,
            },
        }
    )
    if not start_snapshot.temperature_valid:
        degraded.append("temperature_reading_lost")
        warnings.append(
            {
                "action": "temperature_reading_lost",
                "severity": "warning",
                "message": "焦炭温度回读丢失，启用保守喷淋流量且不得按温度确认完成",
            }
        )

    low_events: set[str] = set()
    was_hard_blocked = False
    final_step: dict = {}
    for index in range(MAX_STEPS):
        if quench.spray.complete() or quench.spray.timed_out():
            break

        temperature_lost_before = "temperature_reading_lost" in degraded
        level_lost_before = "level_reading_lost" in degraded
        if temperatures_c is not None:
            temperature_raw = temperatures_c[min(index, len(temperatures_c) - 1)]
            temperature = _finite(temperature_raw, float("nan"))
            if math.isfinite(temperature):
                quench.spray.update_temperature(temperature)
            else:
                quench.spray.update_temperature(None)
        if levels_m3 is not None:
            level_raw = levels_m3[min(index, len(levels_m3) - 1)]
            level = _finite(level_raw, float("nan"))
            quench.tank.measurement(level if math.isfinite(level) else None)

        before_state = quench.snapshot()
        was_suspended = before_state.spray_suspended
        final_step = quench.spray_step(dt)
        after_state = quench.snapshot()

        if not after_state.temperature_valid and "temperature_reading_lost" not in degraded:
            degraded.append("temperature_reading_lost")
            warnings.append(
                {
                    "action": "temperature_reading_lost",
                    "severity": "warning",
                    "message": "焦炭温度回读丢失，按保守流量处理",
                }
            )
        elif after_state.temperature_valid and temperature_lost_before:
            warnings.append(
                {
                    "action": "temperature_reading_restored",
                    "severity": "notice",
                    "message": "焦炭温度回读恢复，恢复温度反馈控制",
                }
            )
            degraded = [item for item in degraded if item != "temperature_reading_lost"]

        if not after_state.tank_level_valid and "level_reading_lost" not in degraded:
            degraded.append("level_reading_lost")
            warnings.append(
                {
                    "action": "level_reading_lost",
                    "severity": "alarm",
                    "message": "回用水池水位丢失，保守停喷淋并报警",
                }
            )
        elif after_state.tank_level_valid and level_lost_before:
            warnings.append(
                {
                    "action": "level_reading_restored",
                    "severity": "notice",
                    "message": "回用水池水位回读恢复，允许联锁判断喷淋恢复",
                }
            )
            degraded = [item for item in degraded if item != "level_reading_lost"]

        if after_state.spray_suspended and not was_suspended:
            warnings.append(
                {
                    "action": "spray_suspended",
                    "severity": "alarm",
                    "message": f"喷淋暂停：{after_state.suspended_reason}",
                    "reason": after_state.suspended_reason,
                }
            )
        elif was_suspended and not after_state.spray_suspended and final_step.get("resumed"):
            warnings.append(
                {
                    "action": "spray_resumed",
                    "severity": "notice",
                    "message": "喷淋条件恢复，自动恢复喷淋",
                    "recovery_id": f"{oven}:auto",
                    "idempotency_key": f"{oven}:auto",
                }
            )

        if after_state.tank_state == "low":
            severity = "alarm" if low_level_policy == "alarm" else "warning"
            key = f"tank_low:{low_level_policy}"
            if key not in low_events:
                low_events.add(key)
                warnings.append(
                    {
                        "action": "tank_low_level",
                        "severity": severity,
                        "message": "回用水池接近下限"
                        + (
                            "，按本炉策略维持既定喷淋并报警；硬余量仍受联锁保护"
                            if low_level_policy == "alarm"
                            else "，按本炉策略降低喷淋保水位"
                        ),
                        "low_level_policy": low_level_policy,
                    }
                )
        elif final_step.get("blocked") and after_state.tank_state in ("low_low", "high_high", "unknown"):
            if not was_hard_blocked:
                warnings.append(
                    {
                        "action": f"tank_{after_state.tank_state}",
                        "severity": "alarm",
                        "message": f"回用水池硬联锁状态：{after_state.tank_state}",
                    }
                )
            was_hard_blocked = True
        else:
            was_hard_blocked = False

    settlement = quench.tank.settle()
    completion = quench.snapshot()
    steps.extend(
        {
            "component": "quench",
            "action": warning["action"],
            "detail": {
                key: value
                for key, value in warning.items()
                if key not in ("action", "message")
            },
            "severity": warning["severity"],
            "message": warning["message"],
        }
        for warning in warnings
    )
    steps.append(
        {
            "component": "quench",
            "action": "spray",
            "detail": {
                **settlement,
                "complete": completion.spray_complete,
                "timed_out": completion.spray_timed_out,
                "temperature_c": completion.temperature_c,
                "temperature_valid": completion.temperature_valid,
                "spray_intensity_m3_h": completion.spray_intensity_m3_h,
                "spray_requested_m3_h": completion.spray_requested_m3_h,
                "elapsed_s": completion.elapsed_s,
            },
        }
    )

    if not quench.spray.complete():
        violations.append("spray_incomplete")
        if quench.spray.timed_out():
            violations.append("spray_timeout")
    if abs(quench.spray.water_m3() - settlement["water_withdrawn_m3"]) > 0.01:
        violations.append("water_settlement_mismatch")

    return {
        "oven": oven,
        "ok": not violations,
        "steps": steps,
        "violations": violations,
        "degraded": degraded,
        "water_settlement": settlement,
        "low_level_policy": low_level_policy,
    }
