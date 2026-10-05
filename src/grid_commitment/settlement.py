"""时段结算。

只能对存在权威计划的 (时段, 资源) 执行一次；结算后该时段冻结，
之后的计量更正只追加 :class:`SettlementAdjustment` 与争议，原结论不变。
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime

from .domain import (
    Dispute,
    DisputeKind,
    MeterReading,
    Settlement,
    SettlementAdjustment,
    Trace,
    new_code,
    q4,
)

# 计量与计划允许偏差（MWh）；带内偏差视为正常波动，不削减补偿资格。
DEFAULT_TOLERANCE_MWH = 0.05


def build_settlement(
    line: dict,
    hours: float,
    meter: MeterReading | None,
    compensation_rate: float,
    settled_at: datetime,
    tolerance_mwh: float = DEFAULT_TOLERANCE_MWH,
) -> tuple[Settlement, list[Dispute]]:
    """依据权威计划明细行与最新计量构造结算与偏差争议。"""

    desired_energy = q4(float(line["requested_mw"]) * hours)
    scheduled_net = q4(float(line["allocated_mw"]) * hours)
    directed = q4(float(line["curtailed_energy_mwh"]))
    metered = q4(meter.energy_mwh if meter is not None else 0.0)
    meter_seq = meter.sequence if meter is not None else 0

    disputes: list[Dispute] = []
    # 容忍带内的计量偏差视为正常波动，全额保留补偿资格；
    # 一旦超出容忍带，按全额欠发扣减可补偿限发电量（不按带内部分豁免）。
    deviation = max(0.0, scheduled_net - metered)
    outside_tolerance = deviation > tolerance_mwh
    shortfall = deviation if outside_tolerance else 0.0
    compensable = q4(max(0.0, directed - shortfall))
    amount = q4(compensable * compensation_rate)

    basis: list[Trace] = [
        Trace(
            "SETTLEMENT_BASIS",
            "以权威阶段计划明细与最新计量结算",
            {
                "stage": line["stage"],
                "requested_mw": line["requested_mw"],
                "allocated_mw": line["allocated_mw"],
                "curtailed_energy_mwh": directed,
                "meter_sequence": meter_seq,
                "tolerance_mwh": tolerance_mwh,
            },
        )
    ]
    if shortfall > 0:
        basis.append(
            Trace(
                "UNDERDELIVERY_OFFSET",
                "欠发超容忍带部分从可补偿限发电量中扣减",
                {"shortfall_mwh": q4(shortfall), "offset_mwh": q4(min(shortfall, directed))},
            )
        )
    if meter is None:
        basis.append(Trace("METER_MISSING", "暂无计量上报，按零电量结算，待计量更正修订", {}))
    elif metered + tolerance_mwh < scheduled_net:
        disputes.append(
            Dispute(
                code=new_code("disp"),
                resource_code=line["resource_code"],
                period_code=line["period_code"],
                kind=DisputeKind.METER_DEVIATION.value,
                status="OPEN",
                detail=(
                    f"计量 {metered} MWh 低于计划 {scheduled_net} MWh 超过容忍带 "
                    f"{tolerance_mwh} MWh，欠发 {q4(deviation)} MWh，补偿按全额欠发扣减"
                ),
                created_at=settled_at.isoformat(timespec="seconds"),
            )
        )

    settlement = Settlement(
        period_code=line["period_code"],
        resource_code=line["resource_code"],
        stage=line["stage"],
        desired_energy_mwh=desired_energy,
        scheduled_energy_mwh=scheduled_net,
        metered_energy_mwh=metered,
        directed_curtailment_mwh=directed,
        compensable_curtailment_mwh=compensable,
        compensation_rate=compensation_rate,
        compensation_amount=amount,
        meter_sequence=meter_seq,
        final_energy_mwh=metered,
        settled_at=settled_at.isoformat(timespec="seconds"),
        basis=tuple(basis),
    )
    return settlement, disputes


def build_meter_correction_adjustment(
    settlement: Settlement,
    corrected: MeterReading,
    created_at: datetime,
    tolerance_mwh: float = DEFAULT_TOLERANCE_MWH,
) -> tuple[SettlementAdjustment, Dispute]:
    """已结算时段收到计量更正：原结算冻结，追加补偿差额调整与争议。"""

    new_energy = q4(corrected.energy_mwh)
    deviation = max(0.0, settlement.scheduled_energy_mwh - new_energy)
    shortfall_new = deviation if deviation > tolerance_mwh else 0.0
    compensable_new = max(0.0, settlement.directed_curtailment_mwh - shortfall_new)
    amount_new = q4(compensable_new * settlement.compensation_rate)
    delta_energy = q4(compensable_new - settlement.compensable_curtailment_mwh)
    delta_amount = q4(amount_new - settlement.compensation_amount)

    adj = SettlementAdjustment(
        code=new_code("adj"),
        period_code=settlement.period_code,
        resource_code=settlement.resource_code,
        original_meter_sequence=settlement.meter_sequence,
        corrected_meter_sequence=corrected.sequence,
        original_energy_mwh=settlement.metered_energy_mwh,
        corrected_energy_mwh=new_energy,
        delta_compensable_mwh=delta_energy,
        delta_compensation=delta_amount,
        created_at=created_at.isoformat(timespec="seconds"),
        note="原结算保持不变，补偿差额按更正后计量单列调整",
    )
    dispute = Dispute(
        code=new_code("disp"),
        resource_code=settlement.resource_code,
        period_code=settlement.period_code,
        kind=DisputeKind.METER_CORRECTION_AFTER_SETTLE.value,
        status="OPEN",
        detail=(
            f"结算后计量更正 seq {corrected.sequence}：{settlement.metered_energy_mwh} -> "
            f"{new_energy} MWh，补偿差额 {delta_amount}"
        ),
        created_at=created_at.isoformat(timespec="seconds"),
    )
    return adj, dispute
