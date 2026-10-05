"""SQLite 持久化适配器。

只依赖标准库，单文件数据库、WAL 模式；所有写入在短事务内完成，
崩溃后通过发件箱（outbox）继续投递尚未确认的承诺。
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
import json
import sqlite3
from typing import Any, Iterable

from .domain import (
    CommitmentVersion,
    ConstraintVersion,
    Declaration,
    Dispute,
    Enum,
    Forecast,
    MaintenanceWindow,
    MeterReading,
    Resource,
    RevisionRecord,
    Settlement,
    SettlementAdjustment,
    Stage,
    commitment_code,
    parse_ts,
    period_start,
)


def _json_default(obj: Any) -> Any:
    if isinstance(obj, Enum):
        return obj.value
    if hasattr(obj, "__dataclass_fields__"):
        return asdict(obj)
    raise TypeError(type(obj).__name__)


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, default=_json_default)


SCHEMA = """
CREATE TABLE IF NOT EXISTS resources (
    resource_code TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS declarations (
    resource_code TEXT NOT NULL,
    period_code TEXT NOT NULL,
    revision_seq INTEGER NOT NULL,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (resource_code, period_code, revision_seq)
);
CREATE TABLE IF NOT EXISTS constraints_v (
    feeder_code TEXT NOT NULL,
    version INTEGER NOT NULL,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (feeder_code, version)
);
CREATE TABLE IF NOT EXISTS maintenance_v (
    window_code TEXT NOT NULL,
    revision_seq INTEGER NOT NULL,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (window_code, revision_seq)
);
CREATE TABLE IF NOT EXISTS forecasts (
    resource_code TEXT NOT NULL,
    period_code TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (resource_code, period_code, sequence)
);
CREATE TABLE IF NOT EXISTS meters (
    resource_code TEXT NOT NULL,
    period_code TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (resource_code, period_code, sequence)
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    stage TEXT NOT NULL,
    period_codes TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    signature TEXT NOT NULL,
    created_at TEXT NOT NULL,
    result_json TEXT NOT NULL,
    freeze_json TEXT NOT NULL,
    initial_cumulative_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS idempotency (
    request_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL,
    response_json TEXT,
    run_id TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS run_signatures (
    signature TEXT PRIMARY KEY,
    run_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS commitment_versions (
    code TEXT NOT NULL,
    seq INTEGER NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY (code, seq)
);
CREATE TABLE IF NOT EXISTS revisions (
    code TEXT PRIMARY KEY,
    resource_code TEXT,
    period_code TEXT,
    kind TEXT NOT NULL,
    reason TEXT NOT NULL,
    request_id TEXT,
    payload_hash TEXT NOT NULL,
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settlements (
    period_code TEXT NOT NULL,
    resource_code TEXT NOT NULL,
    data_json TEXT NOT NULL,
    settled_at TEXT NOT NULL,
    PRIMARY KEY (period_code, resource_code)
);
CREATE TABLE IF NOT EXISTS adjustments (
    code TEXT PRIMARY KEY,
    period_code TEXT NOT NULL,
    resource_code TEXT NOT NULL,
    data_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS disputes (
    code TEXT PRIMARY KEY,
    resource_code TEXT NOT NULL,
    period_code TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    data_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    commitment_code TEXT NOT NULL,
    run_id TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class Repository:
    def __init__(self, path: str = ":memory:") -> None:
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.row_lock = __import__("threading").RLock()
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        with self.row_lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ 主数据

    def upsert_resource(self, resource: Resource) -> None:
        self.conn.execute(
            "INSERT INTO resources(resource_code,data,created_at) VALUES(?,?,?) "
            "ON CONFLICT(resource_code) DO UPDATE SET data=excluded.data",
            (resource.resource_code, dumps(asdict(resource)), resource.registered_at or datetime.now().isoformat(timespec="seconds")),
        )
        self.conn.commit()

    def list_resources(self) -> list[Resource]:
        rows = self.conn.execute("SELECT data FROM resources ORDER BY resource_code").fetchall()
        return [Resource(**json.loads(r["data"])) for r in rows]

    def get_resource(self, resource_code: str) -> Resource | None:
        row = self.conn.execute("SELECT data FROM resources WHERE resource_code=?", (resource_code,)).fetchone()
        return None if row is None else Resource(**json.loads(row["data"]))

    def add_declaration(self, decl: Declaration) -> None:
        self.conn.execute(
            "INSERT INTO declarations(resource_code,period_code,revision_seq,data,created_at) VALUES(?,?,?,?,?)",
            (decl.resource_code, decl.period_code, decl.revision_seq, dumps(asdict(decl)), decl.issued_at),
        )

    def latest_declaration(self, resource_code: str, period_code: str) -> Declaration | None:
        row = self.conn.execute(
            "SELECT data FROM declarations WHERE resource_code=? AND period_code=? "
            "ORDER BY revision_seq DESC LIMIT 1",
            (resource_code, period_code),
        ).fetchone()
        return None if row is None else Declaration(**json.loads(row["data"]))

    def next_declaration_seq(self, resource_code: str, period_code: str) -> int:
        row = self.conn.execute(
            "SELECT MAX(revision_seq) AS m FROM declarations WHERE resource_code=? AND period_code=?",
            (resource_code, period_code),
        ).fetchone()
        return int(row["m"] or 0) + 1

    def add_constraint(self, cons: ConstraintVersion) -> None:
        self.conn.execute(
            "INSERT INTO constraints_v(feeder_code,version,data,created_at) VALUES(?,?,?,?)",
            (cons.feeder_code, cons.version, dumps(asdict(cons)), cons.published_at),
        )

    def active_constraint(self, feeder_code: str, at: datetime) -> ConstraintVersion | None:
        rows = self.conn.execute(
            "SELECT data FROM constraints_v WHERE feeder_code=? ORDER BY version DESC", (feeder_code,)
        ).fetchall()
        chosen = None
        for row in rows:
            obj = ConstraintVersion(**json.loads(row["data"]))
            if parse_ts(obj.effective_from) <= at:
                chosen = obj
                break
        return chosen

    def add_maintenance(self, win: MaintenanceWindow) -> None:
        self.conn.execute(
            "INSERT INTO maintenance_v(window_code,revision_seq,data,created_at) VALUES(?,?,?,?)",
            (win.window_code, win.revision_seq, dumps(asdict(win)), win.published_at),
        )

    def active_maintenance(self, feeder_code: str, at: datetime) -> MaintenanceWindow | None:
        rows = self.conn.execute("SELECT data FROM maintenance_v").fetchall()
        best: MaintenanceWindow | None = None
        for row in rows:
            win = MaintenanceWindow(**json.loads(row["data"]))
            if win.feeder_code != feeder_code:
                continue
            start, end = parse_ts(win.starts_at), parse_ts(win.ends_at)
            if start <= at < end and (best is None or win.revision_seq > best.revision_seq):
                best = win
        return best

    def add_forecast(self, fc: Forecast) -> None:
        self.conn.execute(
            "INSERT INTO forecasts(resource_code,period_code,sequence,data,created_at) VALUES(?,?,?,?,?)",
            (fc.resource_code, fc.period_code, fc.sequence, dumps(asdict(fc)), fc.issued_at),
        )

    def latest_forecast(self, resource_code: str, period_code: str) -> Forecast | None:
        row = self.conn.execute(
            "SELECT data FROM forecasts WHERE resource_code=? AND period_code=? ORDER BY sequence DESC LIMIT 1",
            (resource_code, period_code),
        ).fetchone()
        return None if row is None else Forecast(**json.loads(row["data"]))

    def next_forecast_seq(self, resource_code: str, period_code: str) -> int:
        row = self.conn.execute(
            "SELECT MAX(sequence) AS m FROM forecasts WHERE resource_code=? AND period_code=?",
            (resource_code, period_code),
        ).fetchone()
        return int(row["m"] or 0) + 1

    def add_meter(self, meter: MeterReading) -> None:
        self.conn.execute(
            "INSERT INTO meters(resource_code,period_code,sequence,data,created_at) VALUES(?,?,?,?,?)",
            (meter.resource_code, meter.period_code, meter.sequence, dumps(asdict(meter)), meter.recorded_at),
        )

    def latest_meter(self, resource_code: str, period_code: str) -> MeterReading | None:
        row = self.conn.execute(
            "SELECT data FROM meters WHERE resource_code=? AND period_code=? ORDER BY sequence DESC LIMIT 1",
            (resource_code, period_code),
        ).fetchone()
        return None if row is None else MeterReading(**json.loads(row["data"]))

    def next_meter_seq(self, resource_code: str, period_code: str) -> int:
        row = self.conn.execute(
            "SELECT MAX(sequence) AS m FROM meters WHERE resource_code=? AND period_code=?",
            (resource_code, period_code),
        ).fetchone()
        return int(row["m"] or 0) + 1

    # ------------------------------------------------------------------ 修订

    def add_revision(self, rev: RevisionRecord) -> None:
        self.conn.execute(
            "INSERT INTO revisions(code,resource_code,period_code,kind,reason,request_id,"
            "payload_hash,detail,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (rev.code, rev.resource_code, rev.period_code, rev.kind, rev.reason, rev.request_id,
             rev.payload_hash, rev.detail, rev.created_at),
        )

    def list_revisions(self, resource_code: str | None = None, period_code: str | None = None) -> list[RevisionRecord]:
        sql = "SELECT * FROM revisions WHERE 1=1"
        args: list[Any] = []
        if resource_code:
            sql += " AND resource_code=?"
            args.append(resource_code)
        if period_code:
            sql += " AND period_code=?"
            args.append(period_code)
        sql += " ORDER BY rowid"
        rows = self.conn.execute(sql, args).fetchall()
        return [RevisionRecord(**{k: r[k] for k in r.keys()}) for r in rows]

    # ------------------------------------------------------------------ 幂等

    def begin_idempotent(self, request_id: str | None, kind: str, fingerprint: str) -> sqlite3.Row | str | None:
        """返回 None=可继续；Row=既有请求（重复或冲突标记由调用方判断）。"""
        if not request_id:
            return None
        row = self.conn.execute("SELECT * FROM idempotency WHERE request_id=?", (request_id,)).fetchone()
        return row

    def store_idempotent(self, request_id: str, kind: str, fingerprint: str,
                         status: str, response: dict[str, Any], run_id: str | None) -> None:
        self.conn.execute(
            "INSERT INTO idempotency(request_id,kind,fingerprint,status,response_json,run_id,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (request_id, kind, fingerprint, status, dumps(response), run_id,
             datetime.now().isoformat(timespec="seconds")),
        )

    # ------------------------------------------------------------------ 运行

    def save_run(self, result: Any, freeze_json: str,
                 initial_cumulative: dict[str, float] | None = None) -> None:
        from .serialization import result_to_json, result_from_json  # 避免循环导入
        self.conn.execute(
            "INSERT INTO runs(run_id,stage,period_codes,fingerprint,signature,created_at,"
            "result_json,freeze_json,initial_cumulative_json) VALUES(?,?,?,?,?,?,?,?,?)",
            (result.run_id, result.stage, dumps(list(result.period_codes)), result.freeze.fingerprint,
             self.run_signature(result.stage, result.period_codes, result.freeze.fingerprint),
             result.created_at, dumps(result_to_json(result)), freeze_json,
             dumps(initial_cumulative or {})),
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO run_signatures(signature,run_id) VALUES(?,?)",
            (self.run_signature(result.stage, result.period_codes, result.freeze.fingerprint), result.run_id),
        )

    def get_run_initial_cumulative(self, run_id: str) -> dict[str, float]:
        row = self.conn.execute(
            "SELECT initial_cumulative_json FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        return {} if row is None else {k: float(v) for k, v in json.loads(row["initial_cumulative_json"]).items()}

    @staticmethod
    def run_signature(stage: str, period_codes: Iterable[str], fingerprint: str) -> str:
        return f"{stage}|{','.join(sorted(period_codes))}|{fingerprint}"

    def find_run_by_signature(self, stage: str, period_codes: Iterable[str], fingerprint: str) -> str | None:
        sig = self.run_signature(stage, period_codes, fingerprint)
        row = self.conn.execute("SELECT run_id FROM run_signatures WHERE signature=?", (sig,)).fetchone()
        return None if row is None else row["run_id"]

    def get_run(self, run_id: str) -> Any | None:
        from .serialization import result_from_json
        row = self.conn.execute("SELECT result_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return None if row is None else result_from_json(json.loads(row["result_json"]))

    def get_run_freeze_payload(self, run_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT freeze_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return None if row is None else json.loads(row["freeze_json"])

    def list_runs(self, period_code: str | None = None) -> list[str]:
        if period_code:
            rows = self.conn.execute(
                "SELECT run_id, period_codes FROM runs ORDER BY created_at, rowid"
            ).fetchall()
            return [r["run_id"] for r in rows if period_code in json.loads(r["period_codes"])]
        return [r["run_id"] for r in self.conn.execute("SELECT run_id FROM runs ORDER BY rowid").fetchall()]

    def latest_run_for_periods(self, period_codes: tuple[str, ...]) -> dict[str, Any]:
        """每个时段返回权威度最高的一次运行（含结果 JSON）。"""
        rows = self.conn.execute("SELECT result_json, period_codes FROM runs").fetchall()
        chosen: dict[str, dict[str, Any]] = {}
        for row in rows:
            result = json.loads(row["result_json"])
            if not set(result["period_codes"]) & set(period_codes):
                continue
            authority = Stage(result["stage"]).authority
            for period in result["period_codes"]:
                if period in period_codes and (period not in chosen or authority > Stage(chosen[period]["stage"]).authority):
                    chosen[period] = result
        return chosen

    # ------------------------------------------------------------------ 承诺版本

    def current_commitment(self, code: str) -> CommitmentVersion | None:
        row = self.conn.execute(
            "SELECT data FROM commitment_versions WHERE code=? ORDER BY seq DESC LIMIT 1", (code,)
        ).fetchone()
        return None if row is None else CommitmentVersion(**json.loads(row["data"]))

    def list_commitment_versions(self, code: str) -> list[CommitmentVersion]:
        rows = self.conn.execute(
            "SELECT data FROM commitment_versions WHERE code=? ORDER BY seq", (code,)
        ).fetchall()
        return [CommitmentVersion(**json.loads(r["data"])) for r in rows]

    def next_commitment_seq(self, code: str) -> int:
        row = self.conn.execute(
            "SELECT MAX(seq) AS m FROM commitment_versions WHERE code=?", (code,)
        ).fetchone()
        return int(row["m"] or 0) + 1

    def add_commitment_version(self, version: CommitmentVersion) -> None:
        self.conn.execute(
            "INSERT INTO commitment_versions(code,seq,data) VALUES(?,?,?)",
            (version.code, version.seq, dumps(asdict(version))),
        )

    def codes_with_state(self, states: Iterable[str]) -> list[str]:
        states = list(states)
        if not states:
            return []
        rows = self.conn.execute(
            "SELECT code FROM commitment_versions GROUP BY code"
        ).fetchall()
        result = []
        for row in rows:
            cur = self.current_commitment(row["code"])
            if cur is not None and cur.state in states:
                result.append(cur.code)
        return result

    # ------------------------------------------------------------------ 争议

    def add_dispute(self, dispute: Dispute) -> None:
        self.conn.execute(
            "INSERT INTO disputes(code,resource_code,period_code,kind,status,data_json,created_at,resolved_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (dispute.code, dispute.resource_code, dispute.period_code, dispute.kind, dispute.status,
             dumps(asdict(dispute)), dispute.created_at, dispute.resolved_at),
        )

    def list_disputes(self, resource_code: str | None = None, period_code: str | None = None,
                      status: str | None = None) -> list[Dispute]:
        sql = "SELECT data_json FROM disputes WHERE 1=1"
        args: list[Any] = []
        if resource_code:
            sql += " AND resource_code=?"
            args.append(resource_code)
        if period_code:
            sql += " AND period_code=?"
            args.append(period_code)
        if status:
            sql += " AND status=?"
            args.append(status)
        sql += " ORDER BY rowid"
        return [Dispute(**json.loads(r["data_json"])) for r in self.conn.execute(sql, args).fetchall()]

    def open_dispute_exists(self, resource_code: str, period_code: str, kind: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM disputes WHERE resource_code=? AND period_code=? AND kind=? AND status='OPEN' LIMIT 1",
            (resource_code, period_code, kind),
        ).fetchone() is not None

    # ------------------------------------------------------------------ 结算

    def save_settlement(self, settlement: Settlement) -> None:
        self.conn.execute(
            "INSERT INTO settlements(period_code,resource_code,data_json,settled_at) VALUES(?,?,?,?)",
            (settlement.period_code, settlement.resource_code, dumps(asdict(settlement)), settlement.settled_at),
        )

    def get_settlement(self, period_code: str, resource_code: str) -> Settlement | None:
        row = self.conn.execute(
            "SELECT data_json FROM settlements WHERE period_code=? AND resource_code=?",
            (period_code, resource_code),
        ).fetchone()
        return None if row is None else Settlement.from_dict(json.loads(row["data_json"]))

    def list_settlements(self, period_code: str) -> list[Settlement]:
        rows = self.conn.execute(
            "SELECT data_json FROM settlements WHERE period_code=? ORDER BY resource_code", (period_code,)
        ).fetchall()
        return [Settlement.from_dict(json.loads(r["data_json"])) for r in rows]

    def is_period_settled(self, period_code: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM settlements WHERE period_code=? LIMIT 1", (period_code,)
        ).fetchone() is not None

    def add_adjustment(self, adj: SettlementAdjustment) -> None:
        self.conn.execute(
            "INSERT INTO adjustments(code,period_code,resource_code,data_json,created_at) VALUES(?,?,?,?,?)",
            (adj.code, adj.period_code,
             adj.resource_code, dumps(asdict(adj)), adj.created_at),
        )

    def list_adjustments(self, period_code: str | None = None, resource_code: str | None = None) -> list[SettlementAdjustment]:
        sql = "SELECT data_json FROM adjustments WHERE 1=1"
        args: list[Any] = []
        if period_code:
            sql += " AND period_code=?"
            args.append(period_code)
        if resource_code:
            sql += " AND resource_code=?"
            args.append(resource_code)
        rows = self.conn.execute(sql, args).fetchall()
        return [SettlementAdjustment(**json.loads(r["data_json"])) for r in rows]

    # ------------------------------------------------------------------ 发件箱

    def enqueue_outbox(self, code: str, run_id: str, now: str) -> None:
        self.conn.execute(
            "INSERT INTO outbox(commitment_code,run_id,status,attempts,created_at,updated_at) "
            "VALUES(?,?, 'PENDING', 0, ?, ?)",
            (code, run_id, now, now),
        )

    def pending_outbox(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM outbox WHERE status='PENDING' ORDER BY id"
        ).fetchall()

    def mark_outbox(self, outbox_id: int, status: str, now: str) -> None:
        self.conn.execute(
            "UPDATE outbox SET status=?, attempts=attempts+1, updated_at=? WHERE id=?",
            (status, now, outbox_id),
        )

    # ------------------------------------------------------------------ 累计公平

    def cumulative_curtailment(self, target_periods: set[str]) -> dict[str, float]:
        """取每个时段权威度最高运行的限发电量，累计目标时段之外的历史台账。

        多时段运行只有部分时段被更高阶段替代时，仅统计仍具权威性的时段明细。
        """
        chosen = self.latest_run_for_periods(tuple(sorted(set(self._all_periods()))))
        period_run = {period: result["run_id"] for period, result in chosen.items()}
        totals: dict[str, float] = {}
        counted_runs: set[str] = set()
        for period, result in chosen.items():
            if period in target_periods or result["run_id"] in counted_runs:
                continue
            counted_runs.add(result["run_id"])
            for line in result["lines"]:
                line_period = line["period_code"]
                if line_period in target_periods or period_run.get(line_period) != result["run_id"]:
                    continue
                totals[line["resource_code"]] = totals.get(line["resource_code"], 0.0) + float(
                    line["curtailed_energy_mwh"]
                )
        return totals

    def _all_periods(self) -> list[str]:
        periods: set[str] = set()
        for row in self.conn.execute("SELECT period_codes FROM runs").fetchall():
            periods.update(json.loads(row["period_codes"]))
        return sorted(periods)

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()
