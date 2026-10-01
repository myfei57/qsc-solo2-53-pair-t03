"""熄焦顺序：接焦 → 走行 → 按温度回读动态喷淋 → 水量结算。

喷淋强度由焦炭温度回读决定，回用水池水位每一步都参与决策；温度或水位回读
丢失属于部分失败，按保守方式处理（温度丢失拉满强度、水位丢失视同到下限），
并在报告里标记 ``degraded``。低水位策略按炉次传入：``throttle`` 降喷淋保水
位，``maintain`` 维持喷淋并报警。喷淋恢复凭令牌幂等；结算时喷淋累计水量与
水池累计取水量两本账必须一致，不一致记违约。
"""

from __future__ import annotations

from ..quench.machine import QuenchCar

MAX_STEPS = 2400


def quench_oven(
    quench: QuenchCar,
    oven: str,
    dt: float = 1.0,
    low_level_policy: str = "throttle",
) -> dict:
    """执行一炉熄焦，返回带步骤、轨迹与违约项的报告。"""

    steps: list[dict] = []
    violations: list[str] = []
    trace: list[dict] = []

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
        return {"oven": oven, "ok": False, "steps": steps, "violations": violations, "trace": trace}

    quench.start_spray(low_level_policy)
    steps.append(
        {
            "component": "quench",
            "action": "start_spray",
            "detail": {"duration_s": quench.spray.duration_s, "low_level_policy": low_level_policy},
        }
    )

    prev = {"held": False, "degraded": False, "low_level": False}
    for _ in range(MAX_STEPS):
        if quench.spray.complete():
            break
        record = quench.spray_step(dt)
        trace.append(record)
        if record["degraded"] and not prev["degraded"]:
            lost = []
            if record["temp_c"] is None:
                lost.append("coke_temp")
            if record["level_m3"] is None:
                lost.append("pool_level")
            steps.append(
                {
                    "component": "quench",
                    "action": "sensor_lost",
                    "detail": {"severity": "warning", "message": f"回读丢失：{','.join(lost)}，按保守方式喷淋"},
                }
            )
        if record["alarm_raised"]:
            steps.append(
                {
                    "component": "quench",
                    "action": "pool_low_alarm",
                    "detail": {"severity": "alarm", "message": "水位临近下限，按本炉策略维持喷淋"},
                }
            )
        if record["low_level"] and not prev["low_level"] and low_level_policy == "throttle":
            steps.append(
                {
                    "component": "quench",
                    "action": "spray_throttled",
                    "detail": {"intensity": record["intensity"]},
                }
            )
        if record["held"] and not prev["held"]:
            steps.append(
                {
                    "component": "quench",
                    "action": "spray_held",
                    "detail": {"message": "水位触及下限，挂起喷淋保水位", "token": quench.spray.hold_token},
                }
            )
        if not record["held"] and prev["held"]:
            steps.append(
                {
                    "component": "quench",
                    "action": "spray_resumed",
                    "detail": {"level_m3": record["level_m3"]},
                }
            )
        if record.get("starved"):
            violations.append("pool_starved")
        prev = {key: record[key] for key in prev}

    settlement = quench.settlement()
    steps.append(
        {
            "component": "quench",
            "action": "spray",
            "detail": {
                "water_m3": settlement["water_m3"],
                "complete": quench.spray.complete(),
                "quenched": quench.spray.quenched(),
            },
        }
    )
    steps.append(
        {
            "component": "quench",
            "action": "quench_settled",
            "detail": {
                **settlement,
                "policy": low_level_policy,
                "pool_level_end_m3": round(quench.pool.level_m3, 2),
                "spray_elapsed_s": round(quench.spray.elapsed_s, 1),
            },
        }
    )
    if not settlement["matched"]:
        violations.append("settlement_mismatch")
    if not quench.spray.complete():
        violations.append("spray_incomplete")
    if quench.spray.complete() and not quench.spray.quenched():
        # 触到时长帽却没浇透（温度没回读到目标）：必须显式暴露，不能静默放走。
        violations.append("not_quenched")
    if quench.spray.held:
        violations.append("spray_held_at_end")

    degraded = any(record["degraded"] for record in trace)
    report = {
        "oven": oven,
        "ok": not violations,
        "steps": steps,
        "violations": violations,
        "trace": trace,
        "degraded": degraded,
        "low_level_policy": low_level_policy,
        "settlement": settlement,
    }
    return report
