"""端到端场景演示：风电 + 分布式光伏聚合商 + 储能站接入同一馈线。

运行：
    PYTHONPATH=src python3 demo.py

演示内容：
1. 资源按计量点申报可用区间/爬坡/合同优先级，约束与检修按版本生效；
2. 日前冻结清算 -> 日内预测迟到、约束收紧触发修订与重新分配；
3. 限发按约定顺序 + 累计公平，储能充放电互斥；
4. 重复请求幂等、已结算时段不可改写、计量更正只追加调整；
5. explain 给出可解释的获配/限发/补偿依据。
"""

from __future__ import annotations

import json
from datetime import datetime

from grid_commitment import (
    ConstraintVersion,
    Declaration,
    Forecast,
    GridCommitmentService,
    MeterReading,
    Repository,
    Stage,
    StorageMode,
)
from grid_commitment.service import LoggingDelivery

P1 = "2026-10-06T00:00"
P2 = "2026-10-06T01:00"


def show(title: str, payload: object) -> None:
    print(f"\n===== {title} =====")
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def main() -> None:
    svc = GridCommitmentService(
        Repository(":memory:"),
        LoggingDelivery(),
        clock=lambda: datetime(2026, 10, 5, 12, 0),
    )

    # 1) 主数据：合同优先级 rank 越小越优先保电
    from grid_commitment import Resource, MaintenanceWindow
    svc.register_resource(Resource("WIND-1", "WIND", "MP-W1", "FDR-A", 1, 120.0))
    svc.register_resource(Resource("PV-AGG", "SOLAR", "MP-PV1", "FDR-A", 2, 90.0))
    svc.register_resource(Resource("BESS-1", "STORAGE", "MP-B1", "FDR-A", 1, 0.0,
                                   storage_charge_max_mw=20.0, storage_discharge_max_mw=20.0))

    svc.publish_constraint(ConstraintVersion(
        "FDR-A", 1, 100.0, "2026-01-01T00:00", "2026-10-01T00:00"))

    # 2) 申报：可用区间、爬坡；储能申报充电（与放电互斥）
    svc.submit_declaration(Declaration(
        "WIND-1", P1, 1, 10.0, 70.0, 0.2, 0.2, "2026-10-05T08:00"))
    svc.submit_declaration(Declaration(
        "PV-AGG", P1, 1, 0.0, 60.0, 0.3, 0.3, "2026-10-05T08:00"))
    svc.submit_declaration(Declaration(
        "BESS-1", P1, 1, 0.0, 20.0, 1.0, 1.0, "2026-10-05T08:00",
        storage_mode=StorageMode.CHARGE.value, charge_request_mw=20.0))
    for code, hi in (("WIND-1", 70.0), ("PV-AGG", 40.0)):
        svc.submit_declaration(Declaration(
            code, P2, 1, 0.0, hi, 0.3, 0.3, "2026-10-05T08:00"))
    svc.publish_maintenance(MaintenanceWindow(
        "WIN-77", "FDR-A", 1, P2, "2026-10-06T02:00", 70.0, "2026-10-04T00:00"))

    # 3) 日前清算（冻结输入；总愿望 70+60-20=110 > 100，光伏先被限）
    day_ahead = svc.run_clearing(Stage.DAY_AHEAD, (P1, P2), request_id="da-1")
    show("日前清算结果（P1 光伏被限；P2 检修残余容量生效）", {
        "run_id": day_ahead["run_id"],
        "freeze": day_ahead["freeze"]["fingerprint"],
        "lines": [
            {k: l[k] for k in ("period_code", "resource_code", "mode",
                               "requested_mw", "allocated_mw", "curtailed_mw",
                               "feeder_flow_mw", "feeder_capacity_mw")}
            for l in day_ahead["lines"]
        ],
    })

    # 4) 重复请求：同 request_id 重放；同冻结输入自动去重
    again = svc.run_clearing(Stage.DAY_AHEAD, (P1, P2), request_id="da-1")
    dedup = svc.run_clearing(Stage.DAY_AHEAD, (P1, P2), request_id="da-1b")
    print(f"\n重复请求 run_id 一致: {again['run_id'] == day_ahead['run_id']}; "
          f"相同输入去重: {dedup['deduped']}")

    # 5) 日内：预测迟到 + 安全边界收紧到 60 MW（版本生效）
    svc.submit_forecast(Forecast("PV-AGG", P1, 1, 30.0, "2026-10-05T20:00"))
    svc.publish_constraint(ConstraintVersion(
        "FDR-A", 2, 60.0, "2026-10-05T00:00", "2026-10-05T21:00"))
    svc.clock = lambda: datetime(2026, 10, 5, 21, 30)
    intraday = svc.run_clearing(Stage.INTRADAY, (P1,), request_id="id-1")
    show("日内修订（P1）：迟到预测只进新版本；收紧后按优先级限发", {
        "run_id": intraday["run_id"],
        "lines": [
            {k: l[k] for k in ("resource_code", "mode", "requested_mw",
                               "allocated_mw", "curtailed_mw",
                               "cumulative_curtailment_mwh_after")}
            for l in intraday["lines"]
        ],
        "traces": [
            {"resource": l["resource_code"],
             "why": [t["message"] for t in l["traces"]]}
            for l in intraday["lines"]
        ],
    })

    # 6) 实时阶段再次冻结（输入未变也独立留痕，版本替代日内结论）
    realtime = svc.run_clearing(Stage.REALTIME, (P1,), request_id="rt-1")
    print(f"\n实时清算 run_id={realtime['run_id']}，PV-AGG 最终获配 "
          f"{next(l for l in realtime['lines'] if l['resource_code'] == 'PV-AGG')['allocated_mw']} MW")

    # 7) 场站确认、撤回（撤回未开始时段；已结算时段撤回会被拒绝并留痕）
    svc.confirm_commitment("WIND-1", P1)

    # 8) 实时执行后计量上报与结算
    svc.clock = lambda: datetime(2026, 10, 6, 2, 0)
    svc.submit_meter_reading(MeterReading("WIND-1", P1, 1, 40.0, "2026-10-06T01:30"))
    svc.submit_meter_reading(MeterReading("PV-AGG", P1, 1, 0.0, "2026-10-06T01:30"))
    svc.submit_meter_reading(MeterReading("BESS-1", P1, 1, -20.0, "2026-10-06T01:30"))
    settled = svc.settle_period(P1, request_id="settle-1")
    show("P1 结算：最终电量与限发补偿依据", [
        {k: s[k] for k in ("resource_code", "scheduled_energy_mwh",
                           "metered_energy_mwh", "directed_curtailment_mwh",
                           "compensable_curtailment_mwh", "compensation_amount")}
        for s in settled["settlements"]
    ])

    # 9) 结算后计量更正：原结算不动，追加调整与争议
    corrected = svc.submit_meter_reading(
        MeterReading("WIND-1", P1, 2, 35.0, "2026-10-06T05:00"))
    show("结算后计量更正（WIND-1 40 -> 35 MWh）", corrected["adjustment"])

    # 10) 已结算时段不可重新清算 / 撤回被拒
    blocked = []
    try:
        svc.run_clearing(Stage.REALTIME, (P1,))
    except Exception as exc:  # noqa: BLE001
        blocked.append(f"重新清算被拒绝: {getattr(exc, 'code', exc)}")
    blocked.append(f"撤回结果: {svc.withdraw_plan('WIND-1', P1)}")
    print("\n".join(blocked))

    # 11) 场站可解释查询
    show("explain PV-AGG@P1：为何被限发 + 补偿依据", {
        "why": svc.explain("PV-AGG", P1)["why"],
        "traces": svc.explain("PV-AGG", P1)["traces"],
        "settlement": svc.explain("PV-AGG", P1)["settlement"],
        "disputes": svc.explain("PV-AGG", P1)["disputes"],
    })

    # 12) 修订台账
    show("修订台账（全部可追溯）", [
        {"reason": r.reason, "resource": r.resource_code, "period": r.period_code,
         "detail": r.detail}
        for r in svc.repo.list_revisions()
    ])


if __name__ == "__main__":
    main()
