"""定义渠道动销对账服务在模块边界使用的数据对象。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class EventOutcome:
    """描述一条库存事件写入后的稳定结果。"""

    request_id: str
    event_id: str
    kind: str
    channel_id: str
    period: str
    original_period: str
    is_adjustment: bool
    replayed: bool
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "event_id": self.event_id,
            "kind": self.kind,
            "channel_id": self.channel_id,
            "period": self.period,
            "original_period": self.original_period,
            "is_adjustment": self.is_adjustment,
            "replayed": self.replayed,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class BackfillItemResult:
    """描述离线补传批次中单个事件的处理结果。"""

    source_seq: int
    status: str
    event_id: str | None = None
    code: str | None = None
    message: str | None = None
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_seq": self.source_seq,
            "status": self.status,
            "event_id": self.event_id,
            "code": self.code,
            "message": self.message,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class CloseOutcome:
    """描述一次期间关账的稳定结果。"""

    request_id: str
    channel_id: str
    period: str
    snapshot_hash: str
    snapshot_rows: int
    replayed: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "channel_id": self.channel_id,
            "period": self.period,
            "snapshot_hash": self.snapshot_hash,
            "snapshot_rows": self.snapshot_rows,
            "replayed": self.replayed,
        }


@dataclass(frozen=True)
class DisputeOutcome:
    """描述争议登记或解除的稳定结果。"""

    request_id: str
    dispute_id: str
    status: str
    frozen_quantity: int
    replayed: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "dispute_id": self.dispute_id,
            "status": self.status,
            "frozen_quantity": self.frozen_quantity,
            "replayed": self.replayed,
        }
