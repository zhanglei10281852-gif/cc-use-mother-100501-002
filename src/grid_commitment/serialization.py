"""清算结果与领域对象的 JSON 序列化（用于持久化与 HTTP 响应）。"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from .domain import AllocationLine, ClearingResult, InputFreeze, Trace


def trace_to_json(trace: Trace) -> dict[str, Any]:
    return {"code": trace.code, "message": trace.message, "params": trace.params}


def line_to_json(line: AllocationLine) -> dict[str, Any]:
    data = asdict(line)
    data["traces"] = [trace_to_json(t) for t in line.traces]
    return data


def result_to_json(result: ClearingResult) -> dict[str, Any]:
    return {
        "run_id": result.run_id,
        "stage": result.stage,
        "period_codes": list(result.period_codes),
        "freeze": {
            "stage": result.freeze.stage,
            "period_codes": list(result.freeze.period_codes),
            "fingerprint": result.freeze.fingerprint,
            "created_at": result.freeze.created_at,
        },
        "lines": [line_to_json(line) for line in result.lines],
        "disputes": list(result.disputes),
        "created_at": result.created_at,
        "deduped": result.deduped,
    }


def result_from_json(data: dict[str, Any]) -> ClearingResult:
    freeze_data = data["freeze"]
    freeze = InputFreeze(
        stage=freeze_data["stage"],
        period_codes=tuple(freeze_data["period_codes"]),
        fingerprint=freeze_data["fingerprint"],
        payload={},
        created_at=freeze_data["created_at"],
    )
    lines = tuple(AllocationLine.from_dict(line) for line in data["lines"])
    return ClearingResult(
        run_id=data["run_id"],
        stage=data["stage"],
        period_codes=tuple(data["period_codes"]),
        freeze=freeze,
        lines=lines,
        disputes=tuple(data["disputes"]),
        created_at=data["created_at"],
        deduped=data.get("deduped", False),
    )
