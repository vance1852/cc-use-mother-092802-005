"""渠道动销台账在模块边界使用的数据对象。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Product:
    """可销售的酒类产品（SKU）。"""

    product_id: str
    name: str


@dataclass(frozen=True)
class Channel:
    """经销商渠道，动销与货权按渠道归属。"""

    channel_id: str
    name: str


@dataclass(frozen=True)
class AccountingPeriod:
    """会计期间（按月），关账后不可再被写入。"""

    period_id: str
    status: str  # open / closed
    closed_at: str | None = None
    closed_by: str | None = None
    snapshot_hash: str | None = None


@dataclass(frozen=True)
class InventoryEvent:
    """带来源凭证的库存事件，是台账中唯一的事实。"""

    event_id: str
    event_type: str
    product_id: str
    batch_no: str
    channel_id: str
    quantity: int
    source_type: str
    source_ref: str
    business_date: str
    period_id: str          # 实际入账期间（迟到凭证可能不同于业务发生期间）
    origin_period: str      # 业务发生期间
    is_adjustment: bool
    payload: dict[str, Any]
    recorded_by: str
    recorded_at: str
    stream_key: str | None = None
    stream_seq: int | None = None
    unit_cost: str | None = None


@dataclass(frozen=True)
class EventReceipt:
    """事件写入的幂等回执。"""

    request_id: str
    event_id: str
    replayed: bool
    forked: bool = False


@dataclass(frozen=True)
class BalanceRow:
    """按产品/批次/渠道/状态/货权重建出的库存余额。"""

    product_id: str
    batch_no: str
    channel_id: str
    state: str
    owner: str
    quantity: int
    goods_value: str | None


@dataclass(frozen=True)
class Discrepancy:
    """一条可追溯到来源凭证的差异。"""

    kind: str       # stocktake_variance / negative_pool / unmatched_intransit / pending_return / writeoff
    product_id: str
    batch_no: str
    channel_id: str
    quantity: int
    source_type: str
    source_ref: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReconciliationReport:
    """单个会计期间可复算的动销与库存结果。"""

    period_id: str
    sell_in_quantity: int
    sell_in_value: str
    sell_out_quantity: int
    sell_out_value: str
    return_quantity: int
    net_sell_out_quantity: int
    sellable_quantity: int
    in_transit_quantity: int
    pending_return_quantity: int
    disputed_quantity: int
    elapsed_days: int
    avg_daily_sell_out: str
    inventory_days: str
    discrepancies: list[Discrepancy]
    snapshot_valid: bool | None = None
