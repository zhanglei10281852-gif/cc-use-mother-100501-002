"""领域枚举与值对象。

只依赖标准库，所有对象尽量不可变；需要留痕的内容均带有序号或显式时间，
以便冻结输入、回放和审计。
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from enum import Enum
from hashlib import sha256
import json
from typing import Any
from uuid import uuid4


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------


class ResourceType(str, Enum):
    WIND = "WIND"
    SOLAR = "SOLAR"
    STORAGE = "STORAGE"
    HYDRO = "HYDRO"
    OTHER = "OTHER"


class Stage(str, Enum):
    """清算阶段：权威度随序号递增，实时阶段覆盖日前/日内结论。"""

    DAY_AHEAD = "DAY_AHEAD"
    INTRADAY = "INTRADAY"
    REALTIME = "REALTIME"

    @property
    def authority(self) -> int:
        return _STAGE_ORDER[self]

    @staticmethod
    def from_value(value: str | Stage) -> "Stage":
        if isinstance(value, Stage):
            return value
        try:
            return Stage(value)
        except ValueError as exc:
            raise DomainError("UNKNOWN_STAGE", f"未知清算阶段: {value}") from exc


_STAGE_ORDER = {Stage.DAY_AHEAD: 1, Stage.INTRADAY: 2, Stage.REALTIME: 3}


class CommitmentState(str, Enum):
    PENDING = "PENDING"          # 已收件，尚未完成清算
    ISSUED = "ISSUED"            # 清算完成，待场站确认
    CONFIRMED = "CONFIRMED"      # 场站已确认
    WITHDRAWN = "WITHDRAWN"      # 计划已撤回
    SUPERSEDED = "SUPERSEDED"    # 被更高阶段版本替代，仅作历史
    SETTLED = "SETTLED"          # 已结算，冻结
    DISPUTED = "DISPUTED"        # 存在未决争议


class StorageMode(str, Enum):
    DISCHARGE = "DISCHARGE"
    CHARGE = "CHARGE"
    IDLE = "IDLE"


class RequestKind(str, Enum):
    RESOURCE = "RESOURCE"
    DECLARATION = "DECLARATION"
    CONSTRAINT = "CONSTRAINT"
    MAINTENANCE = "MAINTENANCE"
    FORECAST = "FORECAST"
    CLEARING_RUN = "CLEARING_RUN"
    CONFIRM = "CONFIRM"
    WITHDRAW = "WITHDRAW"
    METER_READING = "METER_READING"
    SETTLE = "SETTLE"


class RevisionReason(str, Enum):
    DECLARATION_FIRST = "DECLARATION_FIRST"
    DECLARATION_REVISED = "DECLARATION_REVISED"      # 预测/申报修订
    FORECAST_LATE = "FORECAST_LATE"                  # 迟到预测：只能进下一阶段
    CONSTRAINT_VERSIONED = "CONSTRAINT_VERSIONED"
    MAINTENANCE_VERSIONED = "MAINTENANCE_VERSIONED"
    STAGE_SUPERSEDED = "STAGE_SUPERSEDED"            # 更高阶段修订
    PLAN_WITHDRAWN = "PLAN_WITHDRAWN"
    WITHDRAWAL_REJECTED_SETTLED = "WITHDRAWAL_REJECTED_SETTLED"
    DUPLICATE_REQUEST = "DUPLICATE_REQUEST"
    METER_CORRECTED = "METER_CORRECTED"
    SETTLEMENT_LOCKED = "SETTLEMENT_LOCKED"
    RECOVERED_ON_STARTUP = "RECOVERED_ON_STARTUP"


class DisputeKind(str, Enum):
    SECURITY_INFEASIBLE = "SECURITY_INFEASIBLE"      # 最小出力也无法满足边界
    METER_DEVIATION = "METER_DEVIATION"              # 计量与计划偏差超容忍带
    METER_CORRECTION_AFTER_SETTLE = "METER_CORRECTION_AFTER_SETTLE"
    WITHDRAWAL_REJECTED = "WITHDRAWAL_REJECTED"


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class DomainError(Exception):
    """携带稳定错误码的领域异常，便于 API 返回与测试断言。"""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def q6(value: float) -> float:
    """统一数量舍入，保证冻结摘要与持久化结果稳定。"""
    return round(float(value) + 0.0, 6)


def q4(value: float) -> float:
    return round(float(value) + 0.0, 4)


def canonical(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=_default)


def fingerprint_of(payload: Any) -> str:
    return sha256(canonical(payload).encode("utf-8")).hexdigest()


def new_code(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


def _default(obj: Any) -> Any:
    if hasattr(obj, "__dataclass_fields__"):
        return asdict(obj)
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, timedelta):
        return obj.total_seconds()
    raise TypeError(f"不可序列化的对象: {type(obj)!r}")


def parse_ts(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise DomainError("BAD_TIMESTAMP", f"时间格式非法: {value}") from exc


def period_start(period_code: str) -> datetime:
    return parse_ts(period_code)


def period_code_at(start: datetime) -> str:
    return start.isoformat(timespec="minutes")


def shift_period(period_code: str, minutes: int) -> str:
    return period_code_at(period_start(period_code) + timedelta(minutes=minutes))


# ---------------------------------------------------------------------------
# 主数据 / 申报 / 网络版本
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Resource:
    resource_code: str
    resource_type: str
    meter_point_code: str
    feeder_code: str
    priority_rank: int
    compensation_rate: float                 # 被安全限发时每 MWh 补偿
    storage_charge_max_mw: float = 0.0
    storage_discharge_max_mw: float = 0.0
    registered_at: str = ""

    def __post_init__(self) -> None:
        if not self.resource_code.strip():
            raise DomainError("VALIDATION", "resource_code 不能为空")
        if self.resource_type not in {t.value for t in ResourceType}:
            raise DomainError("VALIDATION", f"未知资源类型: {self.resource_type}")
        for name in ("meter_point_code", "feeder_code"):
            if not getattr(self, name).strip():
                raise DomainError("VALIDATION", f"{name} 不能为空")
        if self.priority_rank < 1:
            raise DomainError("VALIDATION", "priority_rank 必须 >= 1（1 为最高优先级）")
        if self.compensation_rate < 0:
            raise DomainError("VALIDATION", "补偿费率不能为负")
        if self.storage_charge_max_mw < 0 or self.storage_discharge_max_mw < 0:
            raise DomainError("VALIDATION", "储能容量不能为负")

    @property
    def is_storage(self) -> bool:
        return self.resource_type == ResourceType.STORAGE.value


@dataclass(frozen=True, slots=True)
class Declaration:
    """某资源某时段的可用区间与爬坡申报（按 revision_seq 版本化）。"""

    resource_code: str
    period_code: str
    revision_seq: int
    available_min_mw: float
    available_max_mw: float
    ramp_up_mw_per_min: float
    ramp_down_mw_per_min: float
    issued_at: str
    storage_mode: str | None = None
    discharge_request_mw: float = 0.0
    charge_request_mw: float = 0.0

    def __post_init__(self) -> None:
        period_start(self.period_code)
        parse_ts(self.issued_at)
        if self.revision_seq < 1:
            raise DomainError("VALIDATION", "declaration revision_seq 必须 >= 1")
        if self.available_min_mw < 0 or self.available_max_mw < self.available_min_mw:
            raise DomainError("VALIDATION", "可用区间需满足 0 <= min <= max")
        if self.ramp_up_mw_per_min < 0 or self.ramp_down_mw_per_min < 0:
            raise DomainError("VALIDATION", "爬坡能力不能为负")
        if self.discharge_request_mw < 0 or self.charge_request_mw < 0:
            raise DomainError("VALIDATION", "储能请求功率不能为负")
        if self.storage_mode is not None and self.storage_mode not in {m.value for m in StorageMode}:
            raise DomainError("VALIDATION", f"未知储能模式: {self.storage_mode}")


@dataclass(frozen=True, slots=True)
class ConstraintVersion:
    """馈线安全约束版本，按 effective_from 生效。"""

    feeder_code: str
    version: int
    capacity_mw: float
    effective_from: str
    published_at: str

    def __post_init__(self) -> None:
        parse_ts(self.effective_from)
        parse_ts(self.published_at)
        if self.version < 1:
            raise DomainError("VALIDATION", "约束版本号必须 >= 1")
        if self.capacity_mw < 0:
            raise DomainError("VALIDATION", "容量不能为负")


@dataclass(frozen=True, slots=True)
class MaintenanceWindow:
    """检修窗口（可修订）；命中时段馈线容量取 residual_capacity_mw。"""

    window_code: str
    feeder_code: str
    revision_seq: int
    starts_at: str
    ends_at: str
    residual_capacity_mw: float
    published_at: str

    def __post_init__(self) -> None:
        start, end = parse_ts(self.starts_at), parse_ts(self.ends_at)
        if end <= start:
            raise DomainError("VALIDATION", "检修窗口结束时间必须晚于开始时间")
        if self.residual_capacity_mw < 0:
            raise DomainError("VALIDATION", "检修残余容量不能为负")
        if self.revision_seq < 1:
            raise DomainError("VALIDATION", "检修修订序号必须 >= 1")


@dataclass(frozen=True, slots=True)
class Forecast:
    """预测快照：迟到预测只产生修订，不回改已冻结阶段。"""

    resource_code: str
    period_code: str
    sequence: int
    max_mw: float
    issued_at: str

    def __post_init__(self) -> None:
        parse_ts(self.issued_at)
        if self.sequence < 1:
            raise DomainError("VALIDATION", "预测序号必须 >= 1")
        if self.max_mw < 0:
            raise DomainError("VALIDATION", "预测功率不能为负")


@dataclass(frozen=True, slots=True)
class MeterReading:
    resource_code: str
    period_code: str
    sequence: int
    energy_mwh: float
    recorded_at: str

    def __post_init__(self) -> None:
        if self.sequence < 1:
            raise DomainError("VALIDATION", "计量序号必须 >= 1")


# ---------------------------------------------------------------------------
# 清算输出
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Trace:
    """一条可解释结论：稳定 code + 中文说明 + 参数。"""

    code: str
    message: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AllocationLine:
    period_code: str
    resource_code: str
    feeder_code: str
    stage: str
    resource_type: str
    mode: str                              # GEN / DISCHARGE / CHARGE / IDLE
    requested_mw: float
    allocated_mw: float                    # 净值：放电为正、充电为负
    allocated_discharge_mw: float
    allocated_charge_mw: float
    curtailed_mw: float
    curtailed_energy_mwh: float
    cumulative_curtailment_mwh_after: float
    feeder_flow_mw: float
    feeder_capacity_mw: float
    declaration_revision_seq: int = 0
    traces: tuple[Trace, ...] = ()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AllocationLine":
        traces = tuple(Trace(**t) for t in data.get("traces", ()))
        kept = {k: v for k, v in data.items() if k != "traces"}
        return cls(traces=traces, **kept)


@dataclass(frozen=True, slots=True)
class InputFreeze:
    stage: str
    period_codes: tuple[str, ...]
    fingerprint: str
    payload: dict[str, Any]
    created_at: str


@dataclass(frozen=True, slots=True)
class ClearingResult:
    run_id: str
    stage: str
    period_codes: tuple[str, ...]
    freeze: InputFreeze
    lines: tuple[AllocationLine, ...]
    disputes: tuple[dict[str, Any], ...]
    created_at: str
    deduped: bool = False


# ---------------------------------------------------------------------------
# 修订 / 承诺版本 / 结算 / 争议
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RevisionRecord:
    code: str
    resource_code: str | None
    period_code: str | None
    kind: str
    reason: str
    request_id: str | None
    payload_hash: str
    detail: str
    created_at: str


@dataclass(frozen=True, slots=True)
class CommitmentVersion:
    code: str
    resource_code: str
    period_code: str
    stage: str
    seq: int
    state: str
    requested_mw: float
    allocated_mw: float
    curtail_mw: float
    run_id: str | None
    created_at: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Settlement:
    period_code: str
    resource_code: str
    stage: str
    desired_energy_mwh: float
    scheduled_energy_mwh: float
    metered_energy_mwh: float
    directed_curtailment_mwh: float
    compensable_curtailment_mwh: float
    compensation_rate: float
    compensation_amount: float
    meter_sequence: int
    final_energy_mwh: float
    settled_at: str
    basis: tuple[Trace, ...] = ()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Settlement":
        basis = tuple(Trace(**t) for t in data.get("basis", ()))
        kept = {k: v for k, v in data.items() if k != "basis"}
        return cls(basis=basis, **kept)


@dataclass(frozen=True, slots=True)
class SettlementAdjustment:
    """已结算之后的计量更正：原结算不动，仅追加可追溯调整与争议。"""

    code: str
    period_code: str
    resource_code: str
    original_meter_sequence: int
    corrected_meter_sequence: int
    original_energy_mwh: float
    corrected_energy_mwh: float
    delta_compensable_mwh: float
    delta_compensation: float
    created_at: str
    note: str


@dataclass(frozen=True, slots=True)
class Dispute:
    code: str
    resource_code: str
    period_code: str
    kind: str
    status: str                            # OPEN / RESOLVED
    detail: str
    created_at: str
    resolved_at: str | None = None


def commitment_code(resource_code: str, period_code: str) -> str:
    return f"{resource_code}@{period_code}"
