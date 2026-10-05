"""应用服务：对外唯一的业务编排入口。

职责边界：
- 校验与版本号分配、幂等请求处理、修订留痕；
- 调用 :mod:`grid_commitment.freeze` 冻结输入、:mod:`grid_commitment.engine` 清算；
- 承诺版本状态流转、发件箱投递与重启恢复；
- 时段结算与结算后计量更正调整。
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from functools import wraps
import threading
from typing import Any, Callable, Protocol

from .domain import (
    CommitmentState,
    CommitmentVersion,
    ConstraintVersion,
    Declaration,
    Dispute,
    DisputeKind,
    DomainError,
    Forecast,
    MaintenanceWindow,
    MeterReading,
    RequestKind,
    Resource,
    RevisionReason,
    RevisionRecord,
    Stage,
    StorageMode,
    commitment_code,
    fingerprint_of,
    new_code,
    parse_ts,
    period_start,
)
from .engine import clear, _period_hours
from .freeze import InputCatalog, build_freeze
from .repository import Repository, dumps
from .serialization import result_to_json
from .settlement import build_meter_correction_adjustment, build_settlement


class DeliveryPort(Protocol):
    """承诺投递端口（调度/场站通道）。失败抛异常以便重试。"""

    def deliver(self, payload: dict[str, Any]) -> bool: ...


class LoggingDelivery:
    """默认投递实现：记录到内存列表，测试与冒烟使用。"""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def deliver(self, payload: dict[str, Any]) -> bool:
        self.sent.append(payload)
        return True


def _now() -> datetime:
    return datetime.now()


def _locked(method: Callable[..., Any]) -> Callable[..., Any]:
    """串行化写操作，保证 HTTP 多线程下 SQLite 连接与状态流转安全。"""

    @wraps(method)
    def wrapper(self: "GridCommitmentService", *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


class _RepoCatalog(InputCatalog):
    def __init__(self, repo: Repository, run_at: datetime) -> None:
        self.repo = repo
        self.run_at = run_at

    def list_resources(self) -> list[Resource]:
        return self.repo.list_resources()

    def latest_declaration(self, resource_code: str, period_code: str) -> Declaration | None:
        return self.repo.latest_declaration(resource_code, period_code)

    def latest_forecast(self, resource_code: str, period_code: str) -> Forecast | None:
        return self.repo.latest_forecast(resource_code, period_code)

    def active_constraint(self, feeder_code: str, at: datetime) -> ConstraintVersion | None:
        return self.repo.active_constraint(feeder_code, at)

    def active_maintenance(self, feeder_code: str, at: datetime) -> MaintenanceWindow | None:
        return self.repo.active_maintenance(feeder_code, at)


class GridCommitmentService:
    def __init__(
        self,
        repo: Repository,
        delivery: DeliveryPort | None = None,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self.repo = repo
        self.delivery = delivery or LoggingDelivery()
        self.clock = clock
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ 工具

    def _iso(self) -> str:
        return self.clock().isoformat(timespec="seconds")

    def _revision(self, *, kind: str, reason: str, detail: str,
                  resource_code: str | None = None, period_code: str | None = None,
                  request_id: str | None = None, payload: Any = None) -> RevisionRecord:
        rev = RevisionRecord(
            code=new_code("rev"),
            resource_code=resource_code,
            period_code=period_code,
            kind=kind,
            reason=reason,
            request_id=request_id,
            payload_hash=fingerprint_of(payload if payload is not None else detail),
            detail=detail,
            created_at=self._iso(),
        )
        self.repo.add_revision(rev)
        return rev

    def _begin_request(self, request_id: str | None, kind: str, payload: Any) -> dict[str, Any] | None:
        """幂等处理：返回既有响应表示重放；None 表示继续执行。"""
        if not request_id:
            return None
        fp = fingerprint_of(payload)
        row = self.repo.begin_idempotent(request_id, kind, fp)
        if row is None:
            return None
        import json
        if row["fingerprint"] != fp:
            raise DomainError(
                "IDEMPOTENCY_CONFLICT",
                f"请求 {request_id} 曾以不同内容提交",
                {"original_fingerprint": row["fingerprint"], "current_fingerprint": fp},
            )
        return json.loads(row["response_json"]) if row["response_json"] else {"replayed": True, "run_id": row["run_id"]}

    def _finish_request(self, request_id: str | None, kind: str, payload: Any,
                        response: dict[str, Any], run_id: str | None = None) -> None:
        if request_id:
            self.repo.store_idempotent(request_id, kind, fingerprint_of(payload), "DONE", response, run_id)

    # ------------------------------------------------------------------ 主数据

    @_locked
    def register_resource(self, resource: Resource) -> Resource:
        existing = self.repo.get_resource(resource.resource_code)
        self.repo.upsert_resource(resource)
        self.repo.commit()
        if existing is None:
            return resource
        return resource

    @_locked
    def submit_declaration(self, decl: Declaration, request_id: str | None = None) -> dict[str, Any]:
        payload = asdict(decl)
        replay = self._begin_request(request_id, RequestKind.DECLARATION.value, payload)
        if replay is not None:
            return replay
        if self.repo.get_resource(decl.resource_code) is None:
            raise DomainError("UNKNOWN_RESOURCE", f"资源未注册: {decl.resource_code}")
        try:
            seq = self.repo.next_declaration_seq(decl.resource_code, decl.period_code)
            if decl.revision_seq != seq:
                raise DomainError(
                    "REVISION_SEQ_MISMATCH",
                    f"申报修订序号应为 {seq}，收到 {decl.revision_seq}",
                    {"expected": seq, "received": decl.revision_seq},
                )
            if self.repo.is_period_settled(decl.period_code):
                self._revision(
                    kind=RequestKind.DECLARATION.value,
                    reason=RevisionReason.SETTLEMENT_LOCKED.value,
                    detail="时段已结算，新申报只留痕，不改变已结算结论",
                    resource_code=decl.resource_code, period_code=decl.period_code,
                    request_id=request_id, payload=payload,
                )
                response = {"accepted": False, "reason": "SETTLEMENT_LOCKED", "revision_seq": seq}
                self._finish_request(request_id, RequestKind.DECLARATION.value, payload, response)
                self.repo.commit()
                return response

            self.repo.add_declaration(decl)
            prior_runs = self.repo.list_runs(decl.period_code)
            reason = (
                RevisionReason.DECLARATION_FIRST.value if seq == 1
                else (RevisionReason.FORECAST_LATE.value if prior_runs else RevisionReason.DECLARATION_REVISED.value)
            )
            detail = (
                f"首次申报 seq={seq}" if seq == 1
                else (f"申报/预测在已有清算之后修订 seq={seq}，仅影响后续阶段"
                      if prior_runs else f"申报修订 seq={seq}，历史版本保留")
            )
            self._revision(
                kind=RequestKind.DECLARATION.value, reason=reason, detail=detail,
                resource_code=decl.resource_code, period_code=decl.period_code,
                request_id=request_id, payload=payload,
            )
            response = {"accepted": True, "revision_seq": seq, "reason": reason}
            self._finish_request(request_id, RequestKind.DECLARATION.value, payload, response)
            self.repo.commit()
            return response
        except Exception:
            self.repo.rollback()
            raise

    @_locked
    def publish_constraint(self, cons: ConstraintVersion, request_id: str | None = None) -> dict[str, Any]:
        payload = asdict(cons)
        replay = self._begin_request(request_id, RequestKind.CONSTRAINT.value, payload)
        if replay is not None:
            return replay
        try:
            self.repo.add_constraint(cons)
            self._revision(
                kind=RequestKind.CONSTRAINT.value,
                reason=RevisionReason.CONSTRAINT_VERSIONED.value,
                detail=f"馈线 {cons.feeder_code} 约束版本 v{cons.version} 生效，容量 {cons.capacity_mw} MW",
                period_code=None, request_id=request_id, payload=payload,
            )
            response = {"accepted": True, "version": cons.version}
            self._finish_request(request_id, RequestKind.CONSTRAINT.value, payload, response)
            self.repo.commit()
            return response
        except Exception:
            self.repo.rollback()
            raise

    @_locked
    def publish_maintenance(self, win: MaintenanceWindow, request_id: str | None = None) -> dict[str, Any]:
        payload = asdict(win)
        replay = self._begin_request(request_id, RequestKind.MAINTENANCE.value, payload)
        if replay is not None:
            return replay
        try:
            self.repo.add_maintenance(win)
            self._revision(
                kind=RequestKind.MAINTENANCE.value,
                reason=RevisionReason.MAINTENANCE_VERSIONED.value,
                detail=(f"检修窗口 {win.window_code} 修订 {win.revision_seq}：{win.starts_at}~{win.ends_at}，"
                        f"残余容量 {win.residual_capacity_mw} MW"),
                request_id=request_id, payload=payload,
            )
            response = {"accepted": True, "revision_seq": win.revision_seq}
            self._finish_request(request_id, RequestKind.MAINTENANCE.value, payload, response)
            self.repo.commit()
            return response
        except Exception:
            self.repo.rollback()
            raise

    @_locked
    def submit_forecast(self, fc: Forecast, request_id: str | None = None) -> dict[str, Any]:
        payload = asdict(fc)
        replay = self._begin_request(request_id, RequestKind.FORECAST.value, payload)
        if replay is not None:
            return replay
        try:
            seq = self.repo.next_forecast_seq(fc.resource_code, fc.period_code)
            if fc.sequence != seq:
                raise DomainError(
                    "FORECAST_SEQ_MISMATCH",
                    f"预测序号应为 {seq}，收到 {fc.sequence}",
                    {"expected": seq, "received": fc.sequence},
                )
            self.repo.add_forecast(fc)
            prior_runs = self.repo.list_runs(fc.period_code)
            if prior_runs:
                reason, detail = (
                    RevisionReason.FORECAST_LATE.value,
                    f"预测迟到 seq={seq}：阶段输入已冻结，只影响后续更高阶段清算",
                )
            else:
                reason, detail = (
                    RevisionReason.DECLARATION_FIRST.value if seq == 1 else RevisionReason.DECLARATION_REVISED.value,
                    f"预测 seq={seq} 已纳入下一次冻结",
                )
            self._revision(
                kind=RequestKind.FORECAST.value, reason=reason, detail=detail,
                resource_code=fc.resource_code, period_code=fc.period_code,
                request_id=request_id, payload=payload,
            )
            response = {"accepted": True, "sequence": seq, "reason": reason}
            self._finish_request(request_id, RequestKind.FORECAST.value, payload, response)
            self.repo.commit()
            return response
        except Exception:
            self.repo.rollback()
            raise

    # ------------------------------------------------------------------ 清算

    @_locked
    def run_clearing(
        self,
        stage: str | Stage,
        period_codes: tuple[str, ...] | list[str],
        request_id: str | None = None,
    ) -> dict[str, Any]:
        stage = Stage.from_value(stage)
        periods = tuple(sorted(period_codes, key=period_start))
        request_payload = {"stage": stage.value, "period_codes": list(periods), "request_id": request_id}
        replay = self._begin_request(request_id, RequestKind.CLEARING_RUN.value, request_payload)
        if replay is not None:
            return replay
        try:
            for period in periods:
                if self.repo.is_period_settled(period):
                    self._revision(
                        kind=RequestKind.CLEARING_RUN.value,
                        reason=RevisionReason.SETTLEMENT_LOCKED.value,
                        detail=f"时段 {period} 已结算，拒绝重开清算",
                        period_code=period, request_id=request_id, payload=request_payload,
                    )
                    raise DomainError("PERIOD_SETTLED", f"时段 {period} 已结算，不能重新清算")

            # 权威阶段单调：已有更高阶段结论时，低阶段请求拒绝
            for period in periods:
                chosen = self.repo.latest_run_for_periods((period,))
                if period in chosen and Stage(chosen[period]["stage"]).authority > stage.authority:
                    raise DomainError(
                        "STAGE_ORDER_VIOLATION",
                        f"时段 {period} 已存在更高阶段 {chosen[period]['stage']} 结论，不能回退到 {stage.value}",
                    )

            run_at = self.clock()
            freeze = build_freeze(_RepoCatalog(self.repo, run_at), stage, periods, run_at)
            existing = self.repo.find_run_by_signature(stage.value, periods, freeze.fingerprint)
            if existing is not None:
                result = self.repo.get_run(existing)
                response = result_to_json(result)
                response["deduped"] = True
                self._revision(
                    kind=RequestKind.CLEARING_RUN.value,
                    reason=RevisionReason.DUPLICATE_REQUEST.value,
                    detail=f"相同冻结输入的重复清算请求，直接复用 {existing}",
                    request_id=request_id, payload=freeze.fingerprint,
                )
                self._finish_request(request_id, RequestKind.CLEARING_RUN.value, request_payload,
                                     response, run_id=existing)
                self.repo.commit()
                return response

            history = self.repo.cumulative_curtailment(set(periods))
            result = clear(freeze, initial_cumulative=history, created_at=run_at)
            self.repo.save_run(result, dumps(freeze.payload), initial_cumulative=history)

            hours_map = _period_hours(periods)
            now_iso = run_at.isoformat(timespec="seconds")
            for line in result.lines:
                code = commitment_code(line.resource_code, line.period_code)
                cur = self.repo.current_commitment(code)
                if cur is not None and cur.state in (
                    CommitmentState.PENDING.value,
                    CommitmentState.ISSUED.value,
                    CommitmentState.CONFIRMED.value,
                    CommitmentState.DISPUTED.value,
                ):
                    self.repo.add_commitment_version(CommitmentVersion(
                        **{**asdict(cur), "seq": self.repo.next_commitment_seq(code),
                           "state": CommitmentState.SUPERSEDED.value,
                           "detail": f"被 {stage.value} 运行 {result.run_id} 替代"}
                    ))
                seq = self.repo.next_commitment_seq(code)
                state = CommitmentState.ISSUED.value
                version = CommitmentVersion(
                    code=code,
                    resource_code=line.resource_code,
                    period_code=line.period_code,
                    stage=stage.value,
                    seq=seq,
                    state=state,
                    requested_mw=line.requested_mw,
                    allocated_mw=line.allocated_mw,
                    curtail_mw=line.curtailed_mw,
                    run_id=result.run_id,
                    created_at=now_iso,
                    detail=line.traces[0].message if line.traces else "",
                )
                self.repo.add_commitment_version(version)
                self.repo.enqueue_outbox(code, result.run_id, now_iso)
                if cur is not None:
                    self._revision(
                        kind=RequestKind.CLEARING_RUN.value,
                        reason=RevisionReason.STAGE_SUPERSEDED.value,
                        detail=(f"{stage.value} 清算替代 seq={cur.seq}（{cur.stage}/{cur.state}）："
                                f"分配 {line.allocated_mw} MW，限发 {line.curtailed_mw} MW"),
                        resource_code=line.resource_code, period_code=line.period_code,
                        request_id=request_id, payload=result.run_id,
                    )

            for d in result.disputes:
                if not self.repo.open_dispute_exists(d["resource_code"], d["period_code"], d["kind"]):
                    self.repo.add_dispute(Dispute(
                        code=new_code("disp"),
                        resource_code=d["resource_code"],
                        period_code=d["period_code"],
                        kind=d["kind"],
                        status="OPEN",
                        detail=d["detail"],
                        created_at=now_iso,
                    ))

            response = result_to_json(result)
            self._finish_request(request_id, RequestKind.CLEARING_RUN.value, request_payload,
                                 response, run_id=result.run_id)
            self.repo.commit()
            self._dispatch_outbox()
            return response
        except Exception:
            self.repo.rollback()
            raise

    # ------------------------------------------------------------------ 确认/撤回

    @_locked
    def confirm_commitment(self, resource_code: str, period_code: str,
                           request_id: str | None = None) -> dict[str, Any]:
        payload = {"resource": resource_code, "period": period_code}
        replay = self._begin_request(request_id, RequestKind.CONFIRM.value, payload)
        if replay is not None:
            return replay
        try:
            code = commitment_code(resource_code, period_code)
            cur = self.repo.current_commitment(code)
            if cur is None:
                raise DomainError("NO_COMMITMENT", f"不存在承诺: {code}")
            if cur.state == CommitmentState.SETTLED.value:
                raise DomainError("PERIOD_SETTLED", f"时段 {period_code} 已结算")
            if cur.state != CommitmentState.ISSUED.value:
                raise DomainError("NOT_CONFIRMABLE", f"当前状态 {cur.state} 不可确认", {"state": cur.state})
            new_version = CommitmentVersion(**{**asdict(cur), "seq": cur.seq + 1,
                                               "state": CommitmentState.CONFIRMED.value})
            self.repo.add_commitment_version(new_version)
            self._finish_request(request_id, RequestKind.CONFIRM.value, payload,
                                 {"confirmed": True, "code": code, "seq": new_version.seq})
            self.repo.commit()
            return {"confirmed": True, "code": code, "seq": new_version.seq}
        except Exception:
            self.repo.rollback()
            raise

    @_locked
    def withdraw_plan(self, resource_code: str, period_code: str,
                      request_id: str | None = None) -> dict[str, Any]:
        payload = {"resource": resource_code, "period": period_code}
        replay = self._begin_request(request_id, RequestKind.WITHDRAW.value, payload)
        if replay is not None:
            return replay
        try:
            code = commitment_code(resource_code, period_code)
            cur = self.repo.current_commitment(code)
            if self.repo.is_period_settled(period_code) or (cur and cur.state == CommitmentState.SETTLED.value):
                self._revision(
                    kind=RequestKind.WITHDRAW.value,
                    reason=RevisionReason.WITHDRAWAL_REJECTED_SETTLED.value,
                    detail=f"时段 {period_code} 已结算，撤回被拒绝，原结论不变",
                    resource_code=resource_code, period_code=period_code,
                    request_id=request_id, payload=payload,
                )
                self.repo.add_dispute(Dispute(
                    code=new_code("disp"),
                    resource_code=resource_code, period_code=period_code,
                    kind=DisputeKind.WITHDRAWAL_REJECTED.value, status="OPEN",
                    detail="撤回请求针对已结算时段，已拒绝并留痕", created_at=self._iso(),
                ))
                self._finish_request(request_id, RequestKind.WITHDRAW.value, payload,
                                     {"accepted": False, "reason": "SETTLEMENT_LOCKED"})
                self.repo.commit()
                return {"accepted": False, "reason": "SETTLEMENT_LOCKED"}
            if cur is None:
                raise DomainError("NO_COMMITMENT", f"不存在承诺: {code}")
            if self.clock() >= period_start(period_code):
                self._revision(
                    kind=RequestKind.WITHDRAW.value,
                    reason=RevisionReason.WITHDRAWAL_REJECTED_SETTLED.value,
                    detail=f"时段 {period_code} 已开始，撤回被拒绝并留痕",
                    resource_code=resource_code, period_code=period_code,
                    request_id=request_id, payload=payload,
                )
                self.repo.add_dispute(Dispute(
                    code=new_code("disp"),
                    resource_code=resource_code, period_code=period_code,
                    kind=DisputeKind.WITHDRAWAL_REJECTED.value, status="OPEN",
                    detail="撤回请求针对已开始时段，已拒绝并留痕", created_at=self._iso(),
                ))
                self._finish_request(request_id, RequestKind.WITHDRAW.value, payload,
                                     {"accepted": False, "reason": "PERIOD_STARTED"})
                self.repo.commit()
                return {"accepted": False, "reason": "PERIOD_STARTED"}
            new_version = CommitmentVersion(**{**asdict(cur), "seq": cur.seq + 1,
                                               "state": CommitmentState.WITHDRAWN.value})
            self.repo.add_commitment_version(new_version)
            self._revision(
                kind=RequestKind.WITHDRAW.value,
                reason=RevisionReason.PLAN_WITHDRAWN.value,
                detail=f"计划撤回：{code} seq={new_version.seq} 标记 WITHDRAWN",
                resource_code=resource_code, period_code=period_code,
                request_id=request_id, payload=payload,
            )
            self._finish_request(request_id, RequestKind.WITHDRAW.value, payload,
                                 {"accepted": True, "code": code, "seq": new_version.seq})
            self.repo.commit()
            return {"accepted": True, "code": code, "seq": new_version.seq}
        except Exception:
            self.repo.rollback()
            raise

    # ------------------------------------------------------------------ 计量与结算

    @_locked
    def submit_meter_reading(self, meter: MeterReading, request_id: str | None = None) -> dict[str, Any]:
        payload = asdict(meter)
        replay = self._begin_request(request_id, RequestKind.METER_READING.value, payload)
        if replay is not None:
            return replay
        try:
            seq = self.repo.next_meter_seq(meter.resource_code, meter.period_code)
            if meter.sequence != seq:
                raise DomainError(
                    "METER_SEQ_MISMATCH",
                    f"计量序号应为 {seq}，收到 {meter.sequence}",
                    {"expected": seq, "received": meter.sequence},
                )
            self.repo.add_meter(meter)
            settled = self.repo.get_settlement(meter.period_code, meter.resource_code)
            if seq > 1 or settled is not None:
                self._revision(
                    kind=RequestKind.METER_READING.value,
                    reason=RevisionReason.METER_CORRECTED.value,
                    detail=(f"计量更正 seq={seq}：{meter.energy_mwh} MWh" if seq > 1
                            else f"结算后补抄计量 seq={seq}：{meter.energy_mwh} MWh，不改变已结算结论"),
                    resource_code=meter.resource_code, period_code=meter.period_code,
                    request_id=request_id, payload=payload,
                )
            adjustment = None
            if settled is not None:
                adj, dispute = build_meter_correction_adjustment(settled, meter, self.clock())
                self.repo.add_adjustment(adj)
                if not self.repo.open_dispute_exists(meter.resource_code, meter.period_code, dispute.kind):
                    self.repo.add_dispute(dispute)
                adjustment = asdict(adj)
            response = {"accepted": True, "sequence": seq, "settled": settled is not None,
                        "adjustment": adjustment}
            self._finish_request(request_id, RequestKind.METER_READING.value, payload, response)
            self.repo.commit()
            return response
        except Exception:
            self.repo.rollback()
            raise

    @_locked
    def settle_period(self, period_code: str, request_id: str | None = None) -> dict[str, Any]:
        payload = {"period": period_code}
        replay = self._begin_request(request_id, RequestKind.SETTLE.value, payload)
        if replay is not None:
            return replay
        try:
            if self.repo.is_period_settled(period_code):
                existing = [asdict(s) for s in self.repo.list_settlements(period_code)]
                response = {"period_code": period_code, "settlements": existing, "deduped": True}
                self._finish_request(request_id, RequestKind.SETTLE.value, payload, response)
                self.repo.commit()
                return response
            chosen = self.repo.latest_run_for_periods((period_code,))
            if period_code not in chosen:
                raise DomainError("NO_PLAN", f"时段 {period_code} 没有可结算的权威计划")
            result = chosen[period_code]
            hours = _period_hours(tuple(result["period_codes"]))[period_code]
            settled_items: list[dict[str, Any]] = []
            now = self.clock()
            for line in result["lines"]:
                if line["period_code"] != period_code:
                    continue
                code = commitment_code(line["resource_code"], period_code)
                cur = self.repo.current_commitment(code)
                if cur is not None and cur.state == CommitmentState.WITHDRAWN.value:
                    continue
                resource = self.repo.get_resource(line["resource_code"])
                if resource is None:
                    continue
                meter = self.repo.latest_meter(line["resource_code"], period_code)
                settlement, disputes = build_settlement(line, hours, meter, resource.compensation_rate, now)
                self.repo.save_settlement(settlement)
                for dispute in disputes:
                    if not self.repo.open_dispute_exists(dispute.resource_code, period_code, dispute.kind):
                        self.repo.add_dispute(dispute)
                if cur is not None:
                    self.repo.add_commitment_version(CommitmentVersion(
                        **{**asdict(cur), "seq": cur.seq + 1, "state": CommitmentState.SETTLED.value}
                    ))
                settled_items.append(asdict(settlement))
            self._revision(
                kind=RequestKind.SETTLE.value,
                reason=RevisionReason.SETTLEMENT_LOCKED.value,
                detail=f"时段 {period_code} 完成结算并冻结，共 {len(settled_items)} 个资源",
                period_code=period_code, request_id=request_id, payload=payload,
            )
            response = {"period_code": period_code, "settlements": settled_items}
            self._finish_request(request_id, RequestKind.SETTLE.value, payload, response)
            self.repo.commit()
            return response
        except Exception:
            self.repo.rollback()
            raise

    # ------------------------------------------------------------------ 发件箱/恢复

    def _dispatch_outbox(self) -> dict[str, int]:
        delivered = failed = 0
        for row in self.repo.pending_outbox():
            cur = self.repo.current_commitment(row["commitment_code"])
            payload = {
                "commitment_code": row["commitment_code"],
                "run_id": row["run_id"],
                "version": asdict(cur) if cur else None,
            }
            try:
                ok = self.delivery.deliver(payload)
            except Exception:
                ok = False
            now = self._iso()
            if ok:
                self.repo.mark_outbox(row["id"], "DELIVERED", now)
                delivered += 1
            else:
                self.repo.mark_outbox(row["id"], "PENDING", now)
                failed += 1
        self.repo.commit()
        return {"delivered": delivered, "failed": failed}

    @_locked
    def recover_pending(self) -> dict[str, Any]:
        """进程重启后调用：继续投递发件箱中尚未确认的承诺并留痕。"""
        pending = self.repo.pending_outbox()
        stats = self._dispatch_outbox()
        if pending:
            self._revision(
                kind=RequestKind.CLEARING_RUN.value,
                reason=RevisionReason.RECOVERED_ON_STARTUP.value,
                detail=f"启动恢复：重新投递 {len(pending)} 条未确认承诺，成功 {stats['delivered']}、失败 {stats['failed']}",
                payload=[r["commitment_code"] for r in pending],
            )
            self.repo.commit()
        return {"pending_found": len(pending), **stats}

    # ------------------------------------------------------------------ 查询

    def replay_run(self, run_id: str) -> dict[str, Any]:
        """用持久化的冻结输入重新执行纯函数清算，核对分配结论是否逐行一致。"""
        from .domain import InputFreeze
        stored = self.repo.get_run(run_id)
        if stored is None:
            raise DomainError("RUN_NOT_FOUND", f"清算运行不存在: {run_id}")
        payload = self.repo.get_run_freeze_payload(run_id)
        if not payload:
            raise DomainError("FREEZE_MISSING", f"运行 {run_id} 未保存冻结输入（旧数据）")
        freeze = InputFreeze(
            stage=payload["stage"],
            period_codes=tuple(payload["period_codes"]),
            fingerprint=stored.freeze.fingerprint,
            payload=payload,
            created_at=stored.freeze.created_at,
        )
        replayed = clear(freeze, run_id=stored.run_id,
                         initial_cumulative=self.repo.get_run_initial_cumulative(run_id),
                         created_at=parse_ts(stored.created_at))
        same = [
            (a.resource_code, a.period_code, a.allocated_mw, a.curtailed_mw)
            for a in replayed.lines
        ] == [
            (b.resource_code, b.period_code, b.allocated_mw, b.curtailed_mw)
            for b in stored.lines
        ]
        return {"run_id": run_id, "matches": same,
                "stored_fingerprint": stored.freeze.fingerprint,
                "recomputed_fingerprint": freeze.fingerprint}

    def explain(self, resource_code: str, period_code: str) -> dict[str, Any]:
        """场站查询：某时段为何获配/被限发/进入争议，以及最终电量与补偿依据。"""
        code = commitment_code(resource_code, period_code)
        versions = self.repo.list_commitment_versions(code)
        chosen = self.repo.latest_run_for_periods((period_code,))
        line = None
        if period_code in chosen:
            for candidate in chosen[period_code]["lines"]:
                if candidate["resource_code"] == resource_code and candidate["period_code"] == period_code:
                    line = candidate
                    break
        settlement = self.repo.get_settlement(period_code, resource_code)
        adjustments = [asdict(a) for a in self.repo.list_adjustments(period_code, resource_code)]
        disputes = [asdict(d) for d in self.repo.list_disputes(resource_code, period_code)]
        revisions = [asdict(r) for r in self.repo.list_revisions(resource_code, period_code)]
        why = "NO_PLAN"
        if line is not None:
            if line["curtailed_mw"] > 1e-6:
                why = "CURTAILED"
            elif any(t["code"] == "SECURITY_INFEASIBLE" for t in line["traces"]):
                why = "DISPUTED"
            elif line["allocated_mw"] != 0.0:
                why = "ALLOCATED"
            else:
                why = "ZERO_ALLOCATION"
        return {
            "commitment_code": code,
            "resource_code": resource_code,
            "period_code": period_code,
            "why": why,
            "current_state": versions[-1].state if versions else None,
            "allocation": line,
            "traces": line["traces"] if line else [],
            "settlement": asdict(settlement) if settlement else None,
            "settlement_basis": [
                {"code": t.code, "message": t.message, "params": t.params}
                for t in settlement.basis
            ] if settlement else [],
            "adjustments": adjustments,
            "disputes": disputes,
            "revisions": revisions,
            "version_history": [asdict(v) for v in versions],
            "freeze_fingerprint": chosen[period_code]["freeze"]["fingerprint"] if period_code in chosen else None,
        }

    def list_open_disputes(self) -> list[dict[str, Any]]:
        return [asdict(d) for d in self.repo.list_disputes(status="OPEN")]
