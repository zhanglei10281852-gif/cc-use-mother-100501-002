"""新能源接入承诺清算的基础领域契约。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from hashlib import sha256
import json
from typing import Iterable


@dataclass(frozen=True, slots=True)
class CapacityCommitment:
    """保存最小且可校验的业务对象。"""

    commitment_code: str
    resource_code: str
    period_code: str
    state: str

    def __post_init__(self) -> None:
        for key, value in asdict(self).items():
            if isinstance(value, str) and not value.strip():
                raise ValueError(f"{key} 不能为空")
            if isinstance(value, int) and value < 1:
                raise ValueError(f"{key} 必须大于零")

    def evolve(self, **changes: object) -> "CapacityCommitment":
        """返回新版本，避免就地改写历史对象。"""
        return replace(self, **changes)

    def fingerprint(self) -> str:
        """生成稳定摘要，供幂等和审计使用。"""
        payload = json.dumps(asdict(self), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return sha256(payload.encode("utf-8")).hexdigest()


def unique_by_identity(items: Iterable[CapacityCommitment]) -> list[CapacityCommitment]:
    """按业务标识去重，并拒绝同标识不同内容。"""
    found: dict[str, CapacityCommitment] = {}
    for item in items:
        key = str(getattr(item, "commitment_code"))
        previous = found.get(key)
        if previous is not None and previous.fingerprint() != item.fingerprint():
            raise ValueError(f"业务标识 {key} 对应的内容发生冲突")
        found[key] = item
    return [found[key] for key in sorted(found)]
