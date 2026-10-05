"""应用服务与 SQLite 持久化集成测试。"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from grid_commitment.domain import (
    CommitmentState,
    ConstraintVersion,
    Declaration,
    DomainError,
    Forecast,
    MaintenanceWindow,
    MeterReading,
    Resource,
    Stage,
    StorageMode,
    commitment_code,
)
from grid_commitment.repository import Repository
from grid_commitment.service import GridCommitmentService, LoggingDelivery


P1 = "2026-10-06T00:00"
P2 = "2026-10-06T01:00"
FUTURE = "2026-10-06T00:00"


def make_service(path: str = ":memory:", now: datetime | None = None) -> GridCommitmentService:
    clock = (lambda: now) if now else datetime.now
    return GridCommitmentService(Repository(path), LoggingDelivery(), clock=clock)


def seed_two_wind(svc, cap=80.0, hi_a=60.0, hi_b=60.0, ranks=(1, 3)):
    svc.register_resource(Resource("A", "WIND", "M-A", "F1", ranks[0], 100.0))
    svc.register_resource(Resource("B", "WIND", "M-B", "F1", ranks[1], 100.0))
    svc.publish_constraint(ConstraintVersion("F1", 1, cap, "2026-01-01T00:00", "2026-10-01T00:00"))
    for code, hi in (("A", hi_a), ("B", hi_b)):
        svc.submit_declaration(Declaration(
            code, P1, 1, 0.0, hi, 1000.0, 1000.0, "2026-10-05T08:00"))


class IdempotencyTests(unittest.TestCase):
    def test_duplicate_declaration_replays_response(self):
        svc = make_service()
        svc.register_resource(Resource("A", "WIND", "M-A", "F1", 1, 100.0))
        d1 = Declaration("A", P1, 1, 0, 50, 1000, 1000, "2026-10-05T08:00")
        first = svc.submit_declaration(d1, request_id="req-1")
        second = svc.submit_declaration(d1, request_id="req-1")
        self.assertEqual(first, second)

    def test_same_request_id_different_payload_conflicts(self):
        svc = make_service()
        svc.register_resource(Resource("A", "WIND", "M-A", "F1", 1, 100.0))
        svc.submit_declaration(Declaration("A", P1, 1, 0, 50, 1000, 1000, "2026-10-05T08:00"),
                               request_id="req-1")
        with self.assertRaises(DomainError) as ctx:
            svc.submit_declaration(Declaration("A", P1, 2, 0, 40, 1000, 1000, "2026-10-05T09:00"),
                                   request_id="req-1")
        self.assertEqual(ctx.exception.code, "IDEMPOTENCY_CONFLICT")

    def test_clearing_signature_dedup(self):
        svc = make_service()
        seed_two_wind(svc)
        r1 = svc.run_clearing(Stage.DAY_AHEAD, (P1,))
        r2 = svc.run_clearing(Stage.DAY_AHEAD, (P1,))
        self.assertEqual(r1["run_id"], r2["run_id"])
        self.assertTrue(r2["deduped"])


class RevisionAndStageTests(unittest.TestCase):
    def test_late_forecast_creates_revision_and_next_stage_sees_it(self):
        svc = make_service()
        seed_two_wind(svc)
        svc.run_clearing(Stage.DAY_AHEAD, (P1,))
        out = svc.submit_forecast(Forecast("B", P1, 1, 10.0, "2026-10-05T20:00"))
        self.assertEqual(out["reason"], "FORECAST_LATE")
        # 日前结果不变
        day_ahead = svc.repo.latest_run_for_periods((P1,))[P1]
        b_line = next(l for l in day_ahead["lines"] if l["resource_code"] == "B")
        self.assertEqual(b_line["allocated_mw"], 20.0)
        # 日内重算看到新预测
        intra = svc.run_clearing(Stage.INTRADAY, (P1,))
        b_line = next(l for l in intra["lines"] if l["resource_code"] == "B")
        self.assertEqual(b_line["allocated_mw"], 10.0)

    def test_stage_cannot_go_backwards(self):
        svc = make_service()
        seed_two_wind(svc)
        svc.run_clearing(Stage.INTRADAY, (P1,))
        with self.assertRaises(DomainError) as ctx:
            svc.run_clearing(Stage.DAY_AHEAD, (P1,))
        self.assertEqual(ctx.exception.code, "STAGE_ORDER_VIOLATION")

    def test_constraint_tightening_intraday_curtails_more(self):
        svc = make_service()
        seed_two_wind(svc, cap=120.0)
        da = svc.run_clearing(Stage.DAY_AHEAD, (P1,))
        self.assertEqual(sum(l["allocated_mw"] for l in da["lines"]), 120.0)
        svc.publish_constraint(ConstraintVersion("F1", 2, 60.0, "2026-10-05T00:00", "2026-10-05T22:00"))
        intra = svc.run_clearing(Stage.INTRADAY, (P1,))
        a = next(l for l in intra["lines"] if l["resource_code"] == "A")
        b = next(l for l in intra["lines"] if l["resource_code"] == "B")
        self.assertEqual(a["allocated_mw"], 60.0)   # 高优先级保住
        self.assertEqual(b["allocated_mw"], 0.0)    # 低优先级被限完
        versions = svc.repo.list_commitment_versions(commitment_code("B", P1))
        self.assertEqual([v.state for v in versions][-2:],
                         [CommitmentState.SUPERSEDED.value, CommitmentState.ISSUED.value])


class WithdrawTests(unittest.TestCase):
    def test_withdraw_before_period(self):
        svc = make_service(now=datetime(2026, 10, 5, 12, 0))
        seed_two_wind(svc)
        svc.run_clearing(Stage.DAY_AHEAD, (P1,))
        out = svc.withdraw_plan("B", P1)
        self.assertTrue(out["accepted"])
        self.assertEqual(svc.repo.current_commitment(commitment_code("B", P1)).state,
                         CommitmentState.WITHDRAWN.value)

    def test_withdraw_after_settle_rejected_and_traced(self):
        svc = make_service(now=datetime(2026, 10, 6, 2, 0))
        seed_two_wind(svc)
        svc.run_clearing(Stage.DAY_AHEAD, (P1,))
        svc.settle_period(P1)
        out = svc.withdraw_plan("B", P1)
        self.assertFalse(out["accepted"])
        disputes = svc.repo.list_disputes("B", P1)
        self.assertTrue(any(d.kind == "WITHDRAWAL_REJECTED" for d in disputes))
        reasons = [r.reason for r in svc.repo.list_revisions("B", P1)]
        self.assertIn("WITHDRAWAL_REJECTED_SETTLED", reasons)


class SettlementTests(unittest.TestCase):
    def test_settlement_locks_period(self):
        svc = make_service(now=datetime(2026, 10, 6, 2, 0))
        seed_two_wind(svc)
        svc.run_clearing(Stage.DAY_AHEAD, (P1,))
        svc.submit_meter_reading(MeterReading("B", P1, 1, 20.0, "2026-10-06T01:30"))
        result = svc.settle_period(P1)
        b = next(s for s in result["settlements"] if s["resource_code"] == "B")
        # B 愿望 60、获配 20、被限 40 MWh，计量=计划 20 => 40 MWh 全部可补偿
        self.assertEqual(b["compensable_curtailment_mwh"], 40.0)
        self.assertEqual(b["compensation_amount"], 4000.0)
        self.assertEqual(b["final_energy_mwh"], 20.0)
        with self.assertRaises(DomainError) as ctx:
            svc.run_clearing(Stage.REALTIME, (P1,))
        self.assertEqual(ctx.exception.code, "PERIOD_SETTLED")

    def test_meter_correction_after_settle_appends_adjustment_only(self):
        svc = make_service(now=datetime(2026, 10, 6, 2, 0))
        seed_two_wind(svc)
        svc.run_clearing(Stage.DAY_AHEAD, (P1,))
        svc.submit_meter_reading(MeterReading("B", P1, 1, 20.0, "2026-10-06T01:30"))
        svc.settle_period(P1)
        before = svc.repo.get_settlement(P1, "B")
        # 更正为 15 MWh：欠发 5 MWh 超容忍带，可补偿限发从 40 降到 35
        out = svc.submit_meter_reading(MeterReading("B", P1, 2, 15.0, "2026-10-06T03:00"))
        self.assertTrue(out["settled"])
        self.assertEqual(out["adjustment"]["delta_compensable_mwh"], -5.0)
        self.assertEqual(out["adjustment"]["delta_compensation"], -500.0)
        after = svc.repo.get_settlement(P1, "B")
        self.assertEqual(before.final_energy_mwh, after.final_energy_mwh)  # 原结算不变
        self.assertEqual(after.metered_energy_mwh, 20.0)

    def test_underdelivery_within_tolerance_still_compensable(self):
        svc = make_service(now=datetime(2026, 10, 6, 2, 0))
        seed_two_wind(svc)
        svc.run_clearing(Stage.DAY_AHEAD, (P1,))
        svc.submit_meter_reading(MeterReading("B", P1, 1, 19.97, "2026-10-06T01:30"))
        result = svc.settle_period(P1)
        b = next(s for s in result["settlements"] if s["resource_code"] == "B")
        self.assertEqual(b["compensable_curtailment_mwh"], 40.0)

    def test_settled_period_rejects_new_clearing_and_declaration_is_traced(self):
        svc = make_service(now=datetime(2026, 10, 6, 2, 0))
        seed_two_wind(svc)
        svc.run_clearing(Stage.DAY_AHEAD, (P1,))
        svc.settle_period(P1)
        out = svc.submit_declaration(Declaration(
            "A", P1, 2, 0, 30, 1000, 1000, "2026-10-06T03:00"))
        self.assertFalse(out["accepted"])
        self.assertEqual(out["reason"], "SETTLEMENT_LOCKED")


class RecoveryTests(unittest.TestCase):
    def test_pending_outbox_redelivered_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "g.db")
            svc = make_service(db, now=datetime(2026, 10, 5, 12, 0))
            seed_two_wind(svc)
            svc.run_clearing(Stage.DAY_AHEAD, (P1,))
            self.assertEqual(len(svc.delivery.sent), 2)
            first_run_ids = {p["commitment_code"] for p in svc.delivery.sent}

            # 模拟进程重启：新服务实例、新投递器，重新打开同一数据库
            svc2 = make_service(db, now=datetime(2026, 10, 5, 12, 1))
            stats = svc2.recover_pending()
            self.assertEqual(stats["pending_found"], 0)
            # 已投递的不再重投；人为制造失败后重启可继续
            svc3 = make_service(db, now=datetime(2026, 10, 5, 12, 2))

            class FailOnce:
                def __init__(self):
                    self.calls = 0
                def deliver(self, payload):
                    self.calls += 1
                    return False

            failing = FailOnce()
            svc3.delivery = failing
            # 将一条记录重置为 PENDING 模拟投递崩溃
            svc3.repo.conn.execute("UPDATE outbox SET status='PENDING' WHERE id=1")
            svc3.repo.commit()
            stats = svc3.recover_pending()
            self.assertEqual(stats["pending_found"], 1)
            self.assertEqual(stats["failed"], 1)
            self.assertGreaterEqual(failing.calls, 1)
            # 再次恢复：记录仍在，重启后继续处理未确认承诺
            svc4 = make_service(db, now=datetime(2026, 10, 5, 12, 3))
            stats = svc4.recover_pending()
            self.assertEqual(stats["pending_found"], 1)
            self.assertEqual(stats["delivered"], 1)
            reasons = [r.reason for r in svc4.repo.list_revisions()]
            self.assertIn("RECOVERED_ON_STARTUP", reasons)


class ExplainTests(unittest.TestCase):
    def test_explain_curtailment_and_settlement_basis(self):
        svc = make_service(now=datetime(2026, 10, 6, 2, 0))
        seed_two_wind(svc)
        svc.run_clearing(Stage.DAY_AHEAD, (P1,))
        svc.submit_meter_reading(MeterReading("B", P1, 1, 20.0, "2026-10-06T01:30"))
        svc.settle_period(P1)
        view = svc.explain("B", P1)
        self.assertEqual(view["why"], "CURTAILED")
        trace_codes = {t["code"] for t in view["traces"]}
        self.assertIn("CURTAILED_BY_SECURITY", trace_codes)
        self.assertIn("CONSTRAINT_VERSION_ACTIVE", trace_codes)
        self.assertEqual(view["settlement"]["compensation_amount"], 4000.0)
        basis_codes = {t["code"] for t in view["settlement_basis"]}
        self.assertIn("SETTLEMENT_BASIS", basis_codes)
        self.assertIsNotNone(view["freeze_fingerprint"])
        self.assertGreaterEqual(len(view["version_history"]), 2)

    def test_explain_storage_charge_allocation(self):
        svc = make_service()
        svc.register_resource(Resource("BAT", "STORAGE", "M-BAT", "F1", 1, 0.0,
                                       storage_charge_max_mw=20.0, storage_discharge_max_mw=20.0))
        svc.publish_constraint(ConstraintVersion("F1", 1, 50.0, "2026-01-01T00:00", "2026-10-01T00:00"))
        svc.submit_declaration(Declaration(
            "BAT", P1, 1, 0, 20, 1000, 1000, "2026-10-05T08:00",
            storage_mode=StorageMode.CHARGE.value, charge_request_mw=20.0))
        svc.run_clearing(Stage.DAY_AHEAD, (P1,))
        view = svc.explain("BAT", P1)
        self.assertEqual(view["allocation"]["allocated_charge_mw"], 20.0)
        self.assertEqual(view["allocation"]["allocated_discharge_mw"], 0.0)


class ReplayTests(unittest.TestCase):
    def test_replay_from_persisted_freeze_matches(self):
        svc = make_service()
        seed_two_wind(svc)
        run = svc.run_clearing(Stage.DAY_AHEAD, (P1,))
        verdict = svc.replay_run(run["run_id"])
        self.assertTrue(verdict["matches"])
        self.assertEqual(verdict["stored_fingerprint"], verdict["recomputed_fingerprint"])


class MaintenanceServiceTests(unittest.TestCase):
    def test_maintenance_window_curtails_during_window_only(self):
        svc = make_service()
        seed_two_wind(svc, cap=200.0)
        for code in ("A", "B"):
            svc.submit_declaration(Declaration(
                code, P2, 1, 0, 60, 1000, 1000, "2026-10-05T08:00"))
        svc.publish_maintenance(MaintenanceWindow(
            "WIN-1", "F1", 1, P1, P2, 60.0, "2026-10-04T00:00"))
        result = svc.run_clearing(Stage.DAY_AHEAD, (P1, P2))
        p1 = [l for l in result["lines"] if l["period_code"] == P1]
        p2 = [l for l in result["lines"] if l["period_code"] == P2]
        self.assertEqual(sum(l["allocated_mw"] for l in p1), 60.0)
        self.assertEqual(sum(l["allocated_mw"] for l in p2), 120.0)


if __name__ == "__main__":
    unittest.main()
