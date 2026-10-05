"""清算输入冻结。

每次日前/日内/实时清算都先构造一份只读快照并计算指纹；
快照内容决定分配结果，事后任何主数据修订都不会改变该次运行的结论，
只能通过更高阶段的新运行形成新版本。
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime

from .domain import (
    ConstraintVersion,
    Declaration,
    Forecast,
    InputFreeze,
    MaintenanceWindow,
    Resource,
    Stage,
    canonical,
    fingerprint_of,
    parse_ts,
    period_start,
)


class InputCatalog:
    """冻结前的只读主数据视图（由仓库适配器实现）。"""

    def list_resources(self) -> list[Resource]:  # pragma: no cover - 接口
        raise NotImplementedError

    def latest_declaration(self, resource_code: str, period_code: str) -> Declaration | None:
        raise NotImplementedError

    def latest_forecast(self, resource_code: str, period_code: str) -> Forecast | None:
        raise NotImplementedError

    def active_constraint(self, feeder_code: str, at: datetime) -> ConstraintVersion | None:
        raise NotImplementedError

    def active_maintenance(self, feeder_code: str, at: datetime) -> MaintenanceWindow | None:
        raise NotImplementedError


def build_freeze(
    catalog: InputCatalog,
    stage: Stage | str,
    period_codes: tuple[str, ...],
    run_at: datetime,
) -> InputFreeze:
    """收集各时段实际生效的申报、预测、约束版本与检修窗口。"""

    stage = Stage.from_value(stage)
    resources = sorted(catalog.list_resources(), key=lambda r: r.resource_code)
    periods = tuple(sorted(period_codes, key=period_start))

    declarations: dict[str, dict[str, dict]] = {}
    forecasts: dict[str, dict[str, dict]] = {}
    for resource in resources:
        for period in periods:
            decl = catalog.latest_declaration(resource.resource_code, period)
            if decl is not None:
                declarations.setdefault(resource.resource_code, {})[period] = asdict(decl)
            fc = catalog.latest_forecast(resource.resource_code, period)
            if fc is not None:
                forecasts.setdefault(resource.resource_code, {})[period] = asdict(fc)

    constraints: dict[str, dict[str, dict]] = {}
    maintenance: dict[str, dict[str, dict]] = {}
    feeders = sorted({r.feeder_code for r in resources})
    for feeder in feeders:
        cons = catalog.active_constraint(feeder, run_at)
        if cons is not None:
            constraints[feeder] = asdict(cons)
        for period in periods:
            win = catalog.active_maintenance(feeder, period_start(period))
            if win is not None:
                maintenance.setdefault(feeder, {})[period] = asdict(win)

    payload = {
        "stage": stage.value,
        "run_at": run_at.isoformat(timespec="seconds"),
        "period_codes": list(periods),
        "resources": [asdict(r) for r in resources],
        "declarations": declarations,
        "forecasts": forecasts,
        "constraints": constraints,
        "maintenance": maintenance,
    }
    return InputFreeze(
        stage=stage.value,
        period_codes=periods,
        fingerprint=fingerprint_of(payload),
        payload=payload,
        created_at=run_at.isoformat(timespec="seconds"),
    )


def freeze_signature(freeze: InputFreeze) -> str:
    """用于清算幂等：同阶段+同时段+同输入指纹即重复请求。"""
    return canonical(
        {
            "stage": freeze.stage,
            "period_codes": list(freeze.period_codes),
            "fingerprint": freeze.fingerprint,
        }
    )
