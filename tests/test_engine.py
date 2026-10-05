"""清算引擎与输入冻结测试。"""

from __future__ import annotations

import unittest
from datetime import datetime

from grid_commitment.domain import (
    AllocationLine,
    ConstraintVersion,
    Declaration,
    Forecast,
    InputFreeze,
    MaintenanceWindow,
    Resource,
    Stage,
    StorageMode,
)
from grid_commitment.engine import clear
from grid_commitment.freeze import build_freeze


P1 = "2026-10-06T00:00"
P2 = "2026-10-06T01:00"


def resource(code, feeder="F1", rank=2, rate=100.0, **kw):
    return Resource(
        resource_code=code, resource_type=kw.pop("rtype", "WIND"),
        meter_point_code=f"M-{code}", feeder_code=feeder, priority_rank=rank,
        compensation_rate=rate, **kw,
    )


def decl(code, period=P1, lo=0.0, hi=10.0, ramp=1000.0, seq=1, **kw):
    return Declaration(
        resource_code=code, period_code=period, revision_seq=seq,
        available_min_mw=lo, available_max_mw=hi,
        ramp_up_mw_per_min=ramp, ramp_down_mw_per_min=ramp,
        issued_at="2026-10-05T08:00", **kw,
    )


class _Catalog:
    def __init__(self, resources, declarations, constraints=None, maintenance=None, forecasts=None):
        self._resources = resources
        self._declarations = {(d.resource_code, d.period_code): d for d in declarations}
        self._constraints = constraints or {}
        self._maintenance = maintenance or []
        self._forecasts = forecasts or {}

    def list_resources(self):
        return list(self._resources)

    def latest_declaration(self, resource_code, period_code):
        return self._declarations.get((resource_code, period_code))

    def latest_forecast(self, resource_code, period_code):
        return self._forecasts.get((resource_code, period_code))

    def active_constraint(self, feeder_code, at):
        return self._constraints.get(feeder_code)

    def active_maintenance(self, feeder_code, at):
        best = None
        for win in self._maintenance:
            if win.feeder_code == feeder_code and win.starts_at <= at.isoformat() < win.ends_at:
                if best is None or win.revision_seq > best.revision_seq:
                    best = win
        return best


def freeze(resources, declarations, stage=Stage.DAY_AHEAD, periods=(P1,),
           constraints=None, maintenance=None, forecasts=None):
    cat = _Catalog(resources, declarations, constraints, maintenance, forecasts)
    return build_freeze(cat, stage, tuple(periods), datetime(2026, 10, 5, 12, 0))


def line_of(result, code, period=P1) -> AllocationLine:
    return next(l for l in result.lines if l.resource_code == code and l.period_code == period)


class PriorityOrderTests(unittest.TestCase):
    def test_lower_priority_curtailed_first(self):
        # rank 1 = 高优先级（先保），rank 3 = 低优先级（先限）
        resources = [resource("A", rank=1), resource("B", rank=3)]
        declarations = [decl("A", hi=60), decl("B", hi=60)]
        constraints = {"F1": ConstraintVersion("F1", 1, 80.0, "2026-01-01T00:00", "2026-10-01T00:00")}
        result = clear(freeze(resources, declarations, constraints=constraints))
        self.assertEqual(line_of(result, "A").allocated_mw, 60.0)
        self.assertEqual(line_of(result, "B").allocated_mw, 20.0)
        self.assertEqual(line_of(result, "B").curtailed_mw, 40.0)
        self.assertTrue(any(t.code == "CURTAILED_BY_SECURITY" for t in line_of(result, "B").traces))
        self.assertTrue(any(t.code == "FULL_ALLOCATION" for t in line_of(result, "A").traces))

    def test_cumulative_fairness_waterfill(self):
        # 同优先级；B 历史已被限 2 MWh，新越限 4 MW/h 应由 A 多承担，最终累计拉平为 3/3
        resources = [resource("A", rank=3), resource("B", rank=3)]
        declarations = [decl("A", hi=100), decl("B", hi=100)]
        constraints = {"F1": ConstraintVersion("F1", 1, 196.0, "2026-01-01T00:00", "2026-10-01T00:00")}
        result = clear(freeze(resources, declarations, constraints=constraints),
                       initial_cumulative={"A": 0.0, "B": 2.0})
        a, b = line_of(result, "A"), line_of(result, "B")
        self.assertAlmostEqual(a.curtailed_mw, 3.0, places=5)
        self.assertAlmostEqual(b.curtailed_mw, 1.0, places=5)
        self.assertAlmostEqual(a.cumulative_curtailment_mwh_after, 3.0, places=5)
        self.assertAlmostEqual(b.cumulative_curtailment_mwh_after, 3.0, places=5)
        self.assertLessEqual(a.feeder_flow_mw, 196.0 + 1e-5)

    def test_fairness_across_periods_uses_running_ledger(self):
        # 两个时段各越限 2MW/h；无历史基数时两期均摊，累计各 2 MWh
        resources = [resource("A", rank=3), resource("B", rank=3)]
        declarations = [decl("A", P1, hi=100), decl("B", P1, hi=100),
                        decl("A", P2, hi=100), decl("B", P2, hi=100)]
        constraints = {"F1": ConstraintVersion("F1", 1, 198.0, "2026-01-01T00:00", "2026-10-01T00:00")}
        result = clear(freeze(resources, declarations, periods=(P1, P2), constraints=constraints))
        for period in (P1, P2):
            self.assertAlmostEqual(line_of(result, "A", period).curtailed_mw, 1.0, places=5)
            self.assertAlmostEqual(line_of(result, "B", period).curtailed_mw, 1.0, places=5)


class StorageTests(unittest.TestCase):
    def test_charge_and_discharge_never_both(self):
        bat = resource("BAT", rtype="STORAGE", rank=1,
                       storage_charge_max_mw=30.0, storage_discharge_max_mw=30.0)
        d = decl("BAT", hi=50, storage_mode=StorageMode.CHARGE.value,
                 discharge_request_mw=40.0, charge_request_mw=30.0)
        result = clear(freeze([bat], [d]))
        line = line_of(result, "BAT")
        self.assertEqual(line.mode, StorageMode.CHARGE.value)
        self.assertEqual(line.allocated_discharge_mw, 0.0)
        self.assertEqual(line.allocated_charge_mw, 30.0)
        self.assertEqual(line.allocated_mw, -30.0)

    def test_storage_discharge_counts_toward_feeder_flow(self):
        bat = resource("BAT", rtype="STORAGE", rank=1, storage_discharge_max_mw=50.0)
        wind = resource("W", rank=2)
        d_bat = decl("BAT", hi=50, storage_mode=StorageMode.DISCHARGE.value, discharge_request_mw=50.0)
        d_wind = decl("W", hi=60.0)
        constraints = {"F1": ConstraintVersion("F1", 1, 80.0, "2026-01-01T00:00", "2026-10-01T00:00")}
        result = clear(freeze([bat, wind], [d_bat, d_wind], constraints=constraints))
        self.assertEqual(line_of(result, "BAT").allocated_mw, 50.0)
        self.assertEqual(line_of(result, "W").allocated_mw, 30.0)
        self.assertAlmostEqual(line_of(result, "W").feeder_flow_mw, 80.0, places=5)

    def test_charge_reduced_when_reverse_overloaded(self):
        bat = resource("BAT", rtype="STORAGE", rank=1, storage_charge_max_mw=80.0)
        d = decl("BAT", storage_mode=StorageMode.CHARGE.value, charge_request_mw=80.0)
        constraints = {"F1": ConstraintVersion("F1", 1, 50.0, "2026-01-01T00:00", "2026-10-01T00:00")}
        result = clear(freeze([bat], [d], constraints=constraints))
        self.assertAlmostEqual(line_of(result, "BAT").allocated_charge_mw, 50.0, places=5)
        self.assertTrue(any(t.code == "CHARGE_REDUCED_BY_SECURITY"
                            for t in line_of(result, "BAT").traces))


class RampTests(unittest.TestCase):
    def test_ramp_up_clamped_between_periods(self):
        resources = [resource("A")]
        # 爬坡 0.05 MW/min => 每小时最多 +3 MW；P1 出 2，P2 愿望 10 被夹到 5
        declarations = [decl("A", P1, hi=2.0, ramp=0.05), decl("A", P2, hi=10.0, ramp=0.05)]
        result = clear(freeze(resources, declarations, periods=(P1, P2)))
        self.assertEqual(line_of(result, "A", P1).allocated_mw, 2.0)
        self.assertEqual(line_of(result, "A", P2).allocated_mw, 5.0)
        self.assertTrue(any(t.code == "RAMP_UP_CLAMPED" for t in line_of(result, "A", P2).traces))


class MaintenanceTests(unittest.TestCase):
    def test_maintenance_residual_capacity_applies(self):
        resources = [resource("A"), resource("B")]
        declarations = [decl("A", hi=80), decl("B", hi=80)]
        constraints = {"F1": ConstraintVersion("F1", 1, 150.0, "2026-01-01T00:00", "2026-10-01T00:00")}
        win = MaintenanceWindow("WIN-1", "F1", 1, P1, P2, 60.0, "2026-10-04T00:00")
        result = clear(freeze(resources, declarations, constraints=constraints, maintenance=[win]))
        self.assertEqual(sum(l.allocated_mw for l in result.lines), 60.0)
        self.assertTrue(any(t.code == "MAINTENANCE_APPLIED" for t in result.lines[0].traces))

    def test_maintenance_revision_picked_up_by_freeze(self):
        win1 = MaintenanceWindow("WIN-1", "F1", 1, P1, P2, 60.0, "2026-10-04T00:00")
        win2 = MaintenanceWindow("WIN-1", "F1", 2, P1, P2, 40.0, "2026-10-04T12:00")
        cat = _Catalog([resource("A"), resource("B")], [decl("A", hi=80), decl("B", hi=80)],
                       maintenance=[win1, win2])
        fz = build_freeze(cat, Stage.DAY_AHEAD, (P1,), datetime(2026, 10, 5, 12, 0))
        self.assertEqual(fz.payload["maintenance"]["F1"][P1]["residual_capacity_mw"], 40.0)


class InfeasibleTests(unittest.TestCase):
    def test_min_output_above_capacity_becomes_dispute(self):
        resources = [resource("A", rank=1), resource("B", rank=1)]
        declarations = [decl("A", lo=40, hi=80), decl("B", lo=40, hi=80)]
        constraints = {"F1": ConstraintVersion("F1", 1, 50.0, "2026-01-01T00:00", "2026-10-01T00:00")}
        result = clear(freeze(resources, declarations, constraints=constraints))
        kinds = {d["kind"] for d in result.disputes}
        self.assertIn("SECURITY_INFEASIBLE", kinds)
        self.assertEqual(sum(l.allocated_mw for l in result.lines), 80.0)


class DeterminismTests(unittest.TestCase):
    def test_same_freeze_same_result(self):
        resources = [resource("A"), resource("B")]
        declarations = [decl("A", hi=80), decl("B", hi=80)]
        constraints = {"F1": ConstraintVersion("F1", 1, 100.0, "2026-01-01T00:00", "2026-10-01T00:00")}
        fz1 = freeze(resources, declarations, constraints=constraints)
        fz2 = freeze(resources, declarations, constraints=constraints)
        self.assertEqual(fz1.fingerprint, fz2.fingerprint)
        r1, r2 = clear(fz1, run_id="r1"), clear(fz2, run_id="r2")
        self.assertEqual(
            [(l.resource_code, l.allocated_mw, l.curtailed_mw) for l in r1.lines],
            [(l.resource_code, l.allocated_mw, l.curtailed_mw) for l in r2.lines],
        )

    def test_forecast_lowers_desired_output(self):
        resources = [resource("A")]
        declarations = [decl("A", hi=100)]
        forecasts = {("A", P1): Forecast("A", P1, 1, 55.0, "2026-10-05T10:00")}
        result = clear(freeze(resources, declarations, forecasts=forecasts))
        self.assertEqual(line_of(result, "A").allocated_mw, 55.0)


if __name__ == "__main__":
    unittest.main()
