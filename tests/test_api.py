"""HTTP API 端到端测试。"""

from __future__ import annotations

import json
import unittest
import urllib.error
import urllib.request

from grid_commitment.api import serve


def _request(url: str, payload: dict | None = None, method: str = "POST",
             headers: dict[str, str] | None = None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


P1 = "2026-10-06T00:00"


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server, cls.service = serve("127.0.0.1", 0, ":memory:")
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.service.repo.close()

    def _seed(self) -> None:
        for code, rank in (("A", 1), ("B", 3)):
            _request(f"{self.base}/resources", {
                "resource_code": code, "resource_type": "WIND",
                "meter_point_code": f"M-{code}", "feeder_code": "F1",
                "priority_rank": rank, "compensation_rate": 100.0})
        _request(f"{self.base}/constraints", {
            "feeder_code": "F1", "version": 1, "capacity_mw": 80.0,
            "effective_from": "2026-01-01T00:00", "published_at": "2026-10-01T00:00"})
        for code in ("A", "B"):
            _request(f"{self.base}/declarations", {
                "resource_code": code, "period_code": P1, "revision_seq": 1,
                "available_min_mw": 0.0, "available_max_mw": 60.0,
                "ramp_up_mw_per_min": 1000.0, "ramp_down_mw_per_min": 1000.0,
                "issued_at": "2026-10-05T08:00"})

    def test_health(self) -> None:
        status, body = _request(f"{self.base}/health", method="GET")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_full_flow_with_idempotency_and_explain(self) -> None:
        self._seed()
        status, body = _request(
            f"{self.base}/clearings",
            {"stage": "DAY_AHEAD", "period_codes": [P1]},
            headers={"Idempotency-Key": "clear-1"},
        )
        self.assertEqual(status, 201)
        run_id = body["run_id"]
        b_line = next(l for l in body["lines"] if l["resource_code"] == "B")
        self.assertEqual(b_line["allocated_mw"], 20.0)

        # 相同幂等键重放
        status, body2 = _request(
            f"{self.base}/clearings",
            {"stage": "DAY_AHEAD", "period_codes": [P1]},
            headers={"Idempotency-Key": "clear-1"},
        )
        self.assertEqual(status, 201)
        self.assertEqual(body2["run_id"], run_id)

        # 冻结输入回放核对
        status, body = _request(f"{self.base}/runs/{run_id}/replay", method="GET")
        self.assertEqual(status, 200)
        self.assertTrue(body["matches"])

        # 场站查询解释
        status, body = _request(
            f"{self.base}/explain?resource_code=B&period_code={P1}", method="GET")
        self.assertEqual(status, 200)
        self.assertEqual(body["why"], "CURTAILED")
        self.assertIsNotNone(body["freeze_fingerprint"])

        # 计量 + 结算
        _request(f"{self.base}/meters", {
            "resource_code": "B", "period_code": P1, "sequence": 1,
            "energy_mwh": 20.0, "recorded_at": "2026-10-06T01:30"})
        status, body = _request(f"{self.base}/settlements", {"period_code": P1})
        self.assertEqual(status, 201)
        settlement_b = next(s for s in body["settlements"] if s["resource_code"] == "B")
        self.assertEqual(settlement_b["compensation_amount"], 4000.0)

        # 已结算时段再清算 -> 409
        status, body = _request(
            f"{self.base}/clearings", {"stage": "REALTIME", "period_codes": [P1]})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "PERIOD_SETTLED")

    def test_validation_error_shape(self) -> None:
        status, body = _request(f"{self.base}/resources", {
            "resource_code": "", "resource_type": "WIND",
            "meter_point_code": "M", "feeder_code": "F",
            "priority_rank": 1, "compensation_rate": 0.0})
        self.assertEqual(status, 400)
        self.assertIn("code", body["error"])

    def test_revisions_listed(self) -> None:
        self._seed()
        _request(f"{self.base}/clearings", {"stage": "DAY_AHEAD", "period_codes": [P1]})
        _request(f"{self.base}/forecasts", {
            "resource_code": "B", "period_code": P1, "sequence": 1,
            "max_mw": 5.0, "issued_at": "2026-10-05T20:00"})
        status, body = _request(
            f"{self.base}/revisions?resource_code=B&period_code={P1}", method="GET")
        self.assertEqual(status, 200)
        reasons = {r["reason"] for r in body["revisions"]}
        self.assertIn("FORECAST_LATE", reasons)

    def test_recovery_endpoint(self) -> None:
        status, body = _request(f"{self.base}/recover", {})
        self.assertEqual(status, 200)
        self.assertIn("pending_found", body)


if __name__ == "__main__":
    unittest.main()
