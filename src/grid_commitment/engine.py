"""清算引擎。

在一份已冻结输入上执行确定性分配，规则：

1. 储能在一个时段只能处于 ``DISCHARGE`` / ``CHARGE`` / ``IDLE`` 之一，
   充放电不会同时占用馈线容量；
2. 出力愿望先受爬坡上下界约束，再参与馈线安全分配；
3. 安全边界收紧时，先限 ``priority_rank`` 数字大（约定顺序靠后）的资源；
   同一优先级内按累计被限发电量公平注水——历史被限越多，本次越晚被限；
4. 所有资源压到有效最小出力仍越界时，不编造可行解，输出 SECURITY_INFEASIBLE 争议；
5. 检修窗口命中时段，馈线容量取约束版本与残余容量的较小值。

引擎对相同输入必然产生相同结果，不接触持久化，便于单测与回放。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .domain import (
    AllocationLine,
    ClearingResult,
    DisputeKind,
    InputFreeze,
    Stage,
    StorageMode,
    Trace,
    new_code,
    period_start,
    q6,
)

_EPS = 1e-6


def _period_hours(period_codes: tuple[str, ...]) -> dict[str, float]:
    """根据相邻时段起点推断每个时段长度（末时段沿用前一间隔，缺省 1 小时）。"""
    starts = [period_start(p) for p in period_codes]
    gap = timedelta(hours=1)
    if len(starts) >= 2:
        gap = starts[1] - starts[0]
    hours: dict[str, float] = {}
    for idx, code in enumerate(period_codes):
        delta = (starts[idx + 1] - starts[idx]) if idx + 1 < len(period_codes) else gap
        hours[code] = max(delta.total_seconds() / 3600.0, 1e-9)
    return hours


class _View:
    """冻结快照的类型化读取器。"""

    def __init__(self, freeze: InputFreeze) -> None:
        p = freeze.payload
        self.stage = Stage.from_value(p["stage"])
        self.periods: tuple[str, ...] = tuple(p["period_codes"])
        self.resources = {r["resource_code"]: r for r in p["resources"]}
        self.declarations = p["declarations"]
        self.forecasts = p["forecasts"]
        self.constraints = p["constraints"]
        self.maintenance = p["maintenance"]

    def declaration(self, resource_code: str, period: str) -> dict[str, Any] | None:
        return self.declarations.get(resource_code, {}).get(period)

    def forecast_cap(self, resource_code: str, period: str) -> float | None:
        fc = self.forecasts.get(resource_code, {}).get(period)
        return None if fc is None else float(fc["max_mw"])


def clear(
    freeze: InputFreeze,
    initial_cumulative: dict[str, float] | None = None,
    run_id: str | None = None,
    created_at: datetime | None = None,
) -> ClearingResult:
    """对冻结输入执行确定性清算。

    ``initial_cumulative`` 携带本次运行之前各资源累计被限电量（MWh），
    由应用层从历史承诺加载，实现跨日/跨修订的累计公平。
    """

    view = _View(freeze)
    created_at = created_at or datetime.now()
    run_id = run_id or new_code("run")
    hours_map = _period_hours(view.periods)

    cumulative = dict(initial_cumulative or {})
    prev_net: dict[str, float] = {}
    lines: list[AllocationLine] = []
    disputes: list[dict[str, Any]] = []

    for period in view.periods:
        hours = hours_map[period]
        present: list[dict[str, Any]] = []
        generators: list[dict[str, Any]] = []
        chargers: list[dict[str, Any]] = []

        for code, resource in sorted(view.resources.items()):
            decl = view.declaration(code, period)
            if decl is None:
                continue
            mode, desired, requested, ramp_traces = _desired(
                resource, decl, view.forecast_cap(code, period), prev_net.get(code), hours
            )
            member = {
                "resource": resource,
                "decl": decl,
                "mode": mode,
                "requested_mw": requested,
                "desired_mw": desired,
                "allocated_mw": desired,
                "eff_min_mw": 0.0,
                "cut_mw": 0.0,
                "cum_mwh": cumulative.get(code, 0.0),
                "traces": list(ramp_traces),
            }
            if desired > _EPS:
                member["eff_min_mw"] = _effective_min(resource, decl, prev_net.get(code), hours)
                generators.append(member)
            elif desired < -_EPS:
                chargers.append(member)
            present.append(member)

        feeder_of = {m["resource"]["resource_code"]: m["resource"]["feeder_code"] for m in present}
        feeders = sorted(set(feeder_of.values()))
        cap_of, feeder_traces = _feeder_caps(view, feeders, period)

        # ---- 正向越限：约定顺序——priority_rank 大者先限；同级累计公平注水 ----
        for rank in sorted({m["resource"]["priority_rank"] for m in generators}, reverse=True):
            overload = _overload(generators, chargers, cap_of, feeder_of)
            if not overload:
                break
            for feeder, excess in overload.items():
                group = [
                    m
                    for m in generators
                    if feeder_of[m["resource"]["resource_code"]] == feeder
                    and m["resource"]["priority_rank"] == rank
                ]
                group = [m for m in group if _room(m) > _EPS]
                if not group:
                    continue
                leftover = _waterfill_curtail(group, excess, hours)
                for m in group:
                    m["allocated_mw"] = q6(m["desired_mw"] - m["cut_mw"])
                    if m["cut_mw"] > _EPS:
                        m["traces"].append(
                            Trace(
                                "CURTAILED_BY_SECURITY",
                                f"安全边界收紧，按优先级 rank={rank} 与累计公平限发",
                                {
                                    "feeder": feeder,
                                    "curtail_mw": q6(m["cut_mw"]),
                                    "priority_rank": rank,
                                    "cumulative_mwh_after": q6(m["cum_mwh"]),
                                },
                            )
                        )
                # leftover 不在此判定争议：更高优先级层级可能继续吸收，
                # 所有层级处理完后由统一越限检查决定。
                _ = leftover

        for m in generators:
            m["allocated_mw"] = q6(max(m["eff_min_mw"], m["desired_mw"] - m["cut_mw"]))

        # 所有优先级压到有效最小出力后仍越限：登记争议（不覆盖更高优先级的争议）
        for feeder, excess in _overload(generators, chargers, cap_of, feeder_of).items():
            stuck = [m for m in generators if feeder_of[m["resource"]["resource_code"]] == feeder]
            _mark_infeasible(disputes, stuck, feeder, period, excess, cap_of[feeder])

        # ---- 反向越限（充电过多）：按比例压减充电请求 ----
        for feeder, excess in _reverse_overload(generators, chargers, cap_of, feeder_of).items():
            group = [m for m in chargers if feeder_of[m["resource"]["resource_code"]] == feeder]
            total_charge = sum(-m["allocated_mw"] for m in group)
            for m in group:
                share = ((-m["allocated_mw"]) / total_charge) * excess if total_charge > _EPS else 0.0
                before = m["allocated_mw"]
                m["allocated_mw"] = q6(min(0.0, before + share))
                reduced = (-before) - (-m["allocated_mw"])
                if reduced > _EPS:
                    m["traces"].append(
                        Trace(
                            "CHARGE_REDUCED_BY_SECURITY",
                            "反向负载越限，按比例压减储能充电请求",
                            {"feeder": feeder, "reduced_mw": q6(reduced)},
                        )
                    )

        # ---- 馈线潮流、累计公平台账与明细行 ----
        flow_of = {feeder: 0.0 for feeder in feeders}
        for m in present:
            flow_of[feeder_of[m["resource"]["resource_code"]]] += m["allocated_mw"]

        for m in sorted(present, key=lambda x: x["resource"]["resource_code"]):
            code = m["resource"]["resource_code"]
            cut = max(0.0, m["desired_mw"] - m["allocated_mw"]) if m["desired_mw"] > 0 else 0.0
            cut_energy = cut * hours
            # cum_mwh 是本次运行前的历史台账，加上本次被限电量得到最新累计
            cumulative[code] = m["cum_mwh"] + cut_energy
            lines.append(
                _line(
                    period,
                    m,
                    cut,
                    cut_energy,
                    cumulative[code],
                    cap_of.get(m["resource"]["feeder_code"], float("inf")),
                    flow_of.get(m["resource"]["feeder_code"], 0.0),
                    view,
                    feeder_traces.get(m["resource"]["feeder_code"], []),
                )
            )
            prev_net[code] = m["allocated_mw"]

    return ClearingResult(
        run_id=run_id,
        stage=freeze.stage,
        period_codes=view.periods,
        freeze=freeze,
        lines=tuple(lines),
        disputes=tuple(disputes),
        created_at=created_at.isoformat(timespec="seconds"),
    )


def _room(member: dict[str, Any]) -> float:
    return max(0.0, (member["desired_mw"] - member["eff_min_mw"]) - member["cut_mw"])


def _waterfill_curtail(members: list[dict[str, Any]], need_mw: float, hours: float) -> float:
    """同优先级内按累计被限电量公平注水削减。

    成员携带历史累计 ``cum_mwh``（注水期间保持不变）与本次已削减 ``cut_mw``，
    注水目标是让 ``cum_mwh + cut_mw*hours`` 尽量拉平：
    历史被限少的成员先承担，拉平后共同承担。
    就地更新 ``cut_mw``，返回无法消化的剩余 MW。
    """
    remaining = need_mw
    while remaining > _EPS:
        active = [m for m in members if _room(m) > _EPS]
        if not active:
            return remaining
        level = {id(m): m["cum_mwh"] + m["cut_mw"] * hours for m in active}
        bottom = min(level.values())
        pool = [m for m in active if level[id(m)] <= bottom + _EPS]
        # 最近的两个拐点：池中成员到限，或池外成员的历史累计被追平
        cap_level = min(m["cum_mwh"] + (m["cut_mw"] + _room(m)) * hours for m in pool)
        outside = [level[id(m)] for m in active if m not in pool]
        next_join = min(outside) if outside else float("inf")
        target = min(cap_level, next_join)
        energy_to_target = sum(max(0.0, target - level[id(m)]) for m in pool)
        energy_need = remaining * hours
        if energy_need + _EPS < energy_to_target:
            add_mw = energy_need / hours / len(pool)
            for m in pool:
                m["cut_mw"] += add_mw
            return 0.0
        for m in pool:
            used = max(0.0, (target - level[id(m)])) / hours
            m["cut_mw"] += used
            remaining -= used
    return 0.0


def _desired(
    resource: dict[str, Any],
    decl: dict[str, Any],
    forecast_cap: float | None,
    prev_net: float | None,
    hours: float,
) -> tuple[str, float, float, list[Trace]]:
    """返回 (模式, 净期望出力[充电为负], 申报请求量[非负], 爬坡追踪)。"""
    traces: list[Trace] = []
    if resource["resource_type"] == "STORAGE":
        mode = decl.get("storage_mode") or StorageMode.IDLE.value
        if mode == StorageMode.DISCHARGE.value:
            raw = min(
                float(decl["discharge_request_mw"]),
                float(resource["storage_discharge_max_mw"]),
                float(decl["available_max_mw"]),
            )
        elif mode == StorageMode.CHARGE.value:
            raw = -min(
                float(decl["charge_request_mw"]),
                float(resource["storage_charge_max_mw"]),
            )
        else:
            return StorageMode.IDLE.value, 0.0, 0.0, traces
    else:
        mode = "GEN"
        raw = float(decl["available_max_mw"])
        if forecast_cap is not None:
            raw = min(raw, forecast_cap)
    requested = abs(raw)

    desired = raw
    if prev_net is not None:
        up_room = float(decl["ramp_up_mw_per_min"]) * hours * 60.0
        down_room = float(decl["ramp_down_mw_per_min"]) * hours * 60.0
        upper, lower = prev_net + up_room, prev_net - down_room
        if desired > upper + _EPS:
            desired = upper
            traces.append(Trace("RAMP_UP_CLAMPED", "受爬坡速率限制下调愿望出力", {"upper_mw": q6(upper)}))
        elif desired < lower - _EPS:
            desired = lower
            traces.append(Trace("RAMP_DOWN_CLAMPED", "受爬坡速率限制上调愿望出力", {"lower_mw": q6(lower)}))
    if resource["resource_type"] == "STORAGE":
        desired = max(desired, -float(resource["storage_charge_max_mw"]))
    else:
        desired = max(desired, 0.0)
    return mode, q6(desired), q6(requested), traces


def _effective_min(
    resource: dict[str, Any], decl: dict[str, Any], prev_net: float | None, hours: float
) -> float:
    """有效最小出力：合同最小出力与爬坡可达下界取大值。"""
    floor = 0.0 if resource["resource_type"] == "STORAGE" else float(decl["available_min_mw"])
    if prev_net is not None:
        ramp_floor = prev_net - float(decl["ramp_down_mw_per_min"]) * hours * 60.0
        floor = max(floor, min(ramp_floor, float(decl["available_max_mw"])))
    return q6(max(0.0, floor))


def _feeder_caps(
    view: _View, feeders: list[str], period: str
) -> tuple[dict[str, float], dict[str, list[Trace]]]:
    cap_of: dict[str, float] = {}
    traces: dict[str, list[Trace]] = {}
    for feeder in feeders:
        cap = float("inf")
        feeder_traces: list[Trace] = []
        cons = view.constraints.get(feeder)
        if cons is not None:
            cap = float(cons["capacity_mw"])
            feeder_traces.append(
                Trace(
                    "CONSTRAINT_VERSION_ACTIVE",
                    f"适用馈线约束版本 v{cons['version']}",
                    {"feeder": feeder, "capacity_mw": cap, "version": cons["version"]},
                )
            )
        win = view.maintenance.get(feeder, {}).get(period)
        if win is not None:
            residual = float(win["residual_capacity_mw"])
            cap = min(cap, residual)
            feeder_traces.append(
                Trace(
                    "MAINTENANCE_APPLIED",
                    f"命中检修窗口 {win['window_code']}（修订 {win['revision_seq']}）",
                    {
                        "feeder": feeder,
                        "window": win["window_code"],
                        "residual_capacity_mw": residual,
                        "revision_seq": win["revision_seq"],
                    },
                )
            )
        cap_of[feeder] = cap
        traces[feeder] = feeder_traces
    return cap_of, traces


def _net_flow(
    generators: list[dict[str, Any]],
    chargers: list[dict[str, Any]],
    feeder_of: dict[str, str],
) -> dict[str, float]:
    flow: dict[str, float] = {}
    for m in generators + chargers:
        feeder = feeder_of[m["resource"]["resource_code"]]
        flow[feeder] = flow.get(feeder, 0.0) + m["allocated_mw"]
    return flow


def _overload(
    generators: list[dict[str, Any]],
    chargers: list[dict[str, Any]],
    cap_of: dict[str, float],
    feeder_of: dict[str, str],
) -> dict[str, float]:
    flow = _net_flow(generators, chargers, feeder_of)
    return {f: v - cap_of[f] for f, v in flow.items() if v > cap_of[f] + _EPS}


def _reverse_overload(
    generators: list[dict[str, Any]],
    chargers: list[dict[str, Any]],
    cap_of: dict[str, float],
    feeder_of: dict[str, str],
) -> dict[str, float]:
    flow = _net_flow(generators, chargers, feeder_of)
    return {f: -cap_of[f] - v for f, v in flow.items() if v < -cap_of[f] - _EPS}


def _mark_infeasible(
    disputes: list[dict[str, Any]],
    group: list[dict[str, Any]],
    feeder: str,
    period: str,
    excess: float,
    cap: float,
) -> None:
    for m in group:
        code = m["resource"]["resource_code"]
        if any(d["resource_code"] == code and d["period_code"] == period for d in disputes):
            continue
        detail = f"馈线 {feeder} 在最小出力/爬坡下仍越限 {q6(excess)} MW（容量 {q6(cap)} MW）"
        m["traces"].append(
            Trace(
                "SECURITY_INFEASIBLE",
                detail,
                {"feeder": feeder, "excess_mw": q6(excess), "capacity_mw": q6(cap)},
            )
        )
        disputes.append(
            {
                "kind": DisputeKind.SECURITY_INFEASIBLE.value,
                "resource_code": code,
                "period_code": period,
                "feeder_code": feeder,
                "detail": detail,
            }
        )


def _line(
    period: str,
    member: dict[str, Any],
    cut_mw: float,
    cut_energy: float,
    cumulative_after: float,
    feeder_cap: float,
    feeder_flow: float,
    view: _View,
    extra_traces: list[Trace],
) -> AllocationLine:
    resource = member["resource"]
    allocated = member["allocated_mw"]
    mode = member["mode"]
    if mode == StorageMode.CHARGE.value:
        discharge, charge = 0.0, -allocated
    elif mode == StorageMode.IDLE.value:
        discharge = charge = 0.0
    else:
        discharge, charge = max(0.0, allocated), 0.0

    own: list[Trace]
    if member["traces"]:
        own = list(member["traces"])
    elif cut_mw > _EPS:
        own = []
    else:
        own = [Trace("FULL_ALLOCATION", "愿望出力全部获配", {"allocated_mw": q6(allocated)})]

    return AllocationLine(
        period_code=period,
        resource_code=resource["resource_code"],
        feeder_code=resource["feeder_code"],
        stage=view.stage.value,
        resource_type=resource["resource_type"],
        mode=mode,
        requested_mw=q6(member["requested_mw"]),
        allocated_mw=q6(allocated),
        allocated_discharge_mw=q6(discharge),
        allocated_charge_mw=q6(charge),
        curtailed_mw=q6(cut_mw),
        curtailed_energy_mwh=q6(cut_energy),
        cumulative_curtailment_mwh_after=q6(cumulative_after),
        feeder_flow_mw=q6(feeder_flow),
        feeder_capacity_mw=(q6(feeder_cap) if feeder_cap != float("inf") else None),
        declaration_revision_seq=int(member["decl"]["revision_seq"]),
        traces=tuple(own + extra_traces),
    )
