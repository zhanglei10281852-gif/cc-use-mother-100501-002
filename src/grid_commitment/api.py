"""HTTP API（标准库实现，无第三方依赖）。

所有写接口接受 JSON；可通过请求头 ``Idempotency-Key`` 实现重复请求重放。
错误统一返回 ``{"error": {"code", "message", "details"}}``。
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .domain import (
    ConstraintVersion,
    Declaration,
    DomainError,
    Forecast,
    MaintenanceWindow,
    MeterReading,
    Resource,
)
from .repository import Repository, dumps
from .service import GridCommitmentService


def _build_service(db_path: str = ":memory:") -> GridCommitmentService:
    return GridCommitmentService(Repository(db_path))


def create_app(service: GridCommitmentService) -> type[BaseHTTPRequestHandler]:
    request_lock = threading.RLock()

    class _Handler(BaseHTTPRequestHandler):
        server_version = "GridCommitment/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:  # 静默访问日志
            return

        # ------------------------------------------------------------ helpers

        def _send(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise DomainError("BAD_JSON", f"请求体不是合法 JSON: {exc}") from exc
            if not isinstance(data, dict):
                raise DomainError("BAD_JSON", "请求体必须是 JSON 对象")
            return data

        def _handle_error(self, exc: Exception) -> None:
            if isinstance(exc, DomainError):
                status = 409 if exc.code in {
                    "IDEMPOTENCY_CONFLICT", "STAGE_ORDER_VIOLATION", "PERIOD_SETTLED",
                    "REVISION_SEQ_MISMATCH", "FORECAST_SEQ_MISMATCH", "METER_SEQ_MISMATCH",
                } else 400
                self._send(status, {"error": {"code": exc.code, "message": exc.message,
                                              "details": exc.details}})
            else:
                self._send(500, {"error": {"code": "INTERNAL", "message": str(exc)}})

        def _idempotency_key(self) -> str | None:
            return self.headers.get("Idempotency-Key")

        def handle_one_request(self) -> None:
            # 共享单条 SQLite 连接，请求级串行化（服务方法内部另有锁）
            with request_lock:
                super().handle_one_request()

        # ------------------------------------------------------------ routing

        def do_GET(self) -> None:  # noqa: N802
            try:
                parsed = urlparse(self.path)
                qs = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                if parsed.path == "/health":
                    self._send(200, {"status": "ok"})
                elif parsed.path == "/explain":
                    self._require(qs, "resource_code", "period_code")
                    self._send(200, service.explain(qs["resource_code"], qs["period_code"]))
                elif parsed.path == "/disputes":
                    disputes = service.repo.list_disputes(
                        resource_code=qs.get("resource_code"),
                        period_code=qs.get("period_code"),
                        status=qs.get("status"),
                    )
                    self._send(200, {"disputes": [json.loads(dumps(d)) for d in disputes]})
                elif parsed.path == "/revisions":
                    revisions = service.repo.list_revisions(
                        resource_code=qs.get("resource_code"), period_code=qs.get("period_code")
                    )
                    self._send(200, {"revisions": [json.loads(dumps(r)) for r in revisions]})
                elif parsed.path == "/settlements":
                    self._require(qs, "period_code")
                    self._send(200, {"settlements": [asdict(s) for s in service.repo.list_settlements(qs["period_code"])]})
                elif parsed.path.startswith("/runs/") and parsed.path.endswith("/replay"):
                    run_id = parsed.path[len("/runs/"):-len("/replay")]
                    self._send(200, service.replay_run(run_id))
                else:
                    self._send(404, {"error": {"code": "NOT_FOUND", "message": parsed.path}})
            except Exception as exc:  # noqa: BLE001
                self._handle_error(exc)

        def do_POST(self) -> None:  # noqa: N802
            try:
                parsed = urlparse(self.path)
                data = self._read_json()
                key = self._idempotency_key()
                route = parsed.path
                if route == "/resources":
                    resource = Resource(**data)
                    service.register_resource(resource)
                    self._send(201, {"accepted": True, "resource_code": resource.resource_code})
                elif route == "/declarations":
                    self._send(202, service.submit_declaration(Declaration(**data), request_id=key))
                elif route == "/constraints":
                    self._send(202, service.publish_constraint(ConstraintVersion(**data), request_id=key))
                elif route == "/maintenance":
                    self._send(202, service.publish_maintenance(MaintenanceWindow(**data), request_id=key))
                elif route == "/forecasts":
                    self._send(202, service.submit_forecast(Forecast(**data), request_id=key))
                elif route == "/meters":
                    self._send(202, service.submit_meter_reading(MeterReading(**data), request_id=key))
                elif route == "/clearings":
                    self._require(data, "stage", "period_codes")
                    self._send(201, service.run_clearing(data["stage"], tuple(data["period_codes"]), request_id=key))
                elif route == "/commitments/confirm":
                    self._require(data, "resource_code", "period_code")
                    self._send(200, service.confirm_commitment(data["resource_code"], data["period_code"], request_id=key))
                elif route == "/commitments/withdraw":
                    self._require(data, "resource_code", "period_code")
                    self._send(200, service.withdraw_plan(data["resource_code"], data["period_code"], request_id=key))
                elif route == "/settlements":
                    self._require(data, "period_code")
                    self._send(201, service.settle_period(data["period_code"], request_id=key))
                elif route == "/recover":
                    self._send(200, service.recover_pending())
                else:
                    self._send(404, {"error": {"code": "NOT_FOUND", "message": route}})
            except Exception as exc:  # noqa: BLE001
                self._handle_error(exc)

        @staticmethod
        def _require(values: dict[str, Any], *names: str) -> None:
            missing = [n for n in names if n not in values]
            if missing:
                raise DomainError("MISSING_FIELDS", f"缺少必填字段: {', '.join(missing)}", {"missing": missing})

    return _Handler


def serve(host: str = "127.0.0.1", port: int = 8080, db_path: str = "grid_commitment.db") -> tuple[ThreadingHTTPServer, GridCommitmentService]:
    service = _build_service(db_path)
    # 进程启动即恢复尚未确认的承诺投递
    service.recover_pending()
    handler = create_app(service)
    server = ThreadingHTTPServer((host, port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, service
