"""从库存事件日志重建数量与货权。

事件日志是唯一事实来源：当前库存、期间快照与对账指标都由同一套折叠规则
按事件落库顺序（seq）重放得到，因此任何查询结果都可以用相同规则复算。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from beverage_ops_foundation.errors import ConflictError, ValidationError


# 库存桶
SELLABLE = "sellable"
IN_TRANSIT = "in_transit"
PENDING_RETURN = "pending_return"
DISPUTED = "disputed"
BUCKETS = (SELLABLE, IN_TRANSIT, PENDING_RETURN, DISPUTED)

# 货权归属
OWNER_ENTERPRISE = "enterprise"
OWNER_CHANNEL = "channel"

# 事件类别
KIND_OUTBOUND = "outbound"
KIND_RECEIPT = "receipt"
KIND_TRANSFER = "transfer"
KIND_SALE = "sale"
KIND_RETURN = "return"
KIND_STOCKTAKE = "stocktake"
KIND_DISPUTE_FREEZE = "dispute_freeze"
KIND_DISPUTE_RELEASE = "dispute_release"
MOVEMENT_KINDS = (
    KIND_OUTBOUND,
    KIND_RECEIPT,
    KIND_TRANSFER,
    KIND_SALE,
    KIND_RETURN,
    KIND_STOCKTAKE,
)
SYSTEM_KINDS = (KIND_DISPUTE_FREEZE, KIND_DISPUTE_RELEASE)

PHASE_SHIPPED = "shipped"
PHASE_RECEIVED = "received"

# 差异来源类别
VAR_STOCKTAKE_SURPLUS = "stocktake_surplus"
VAR_STOCKTAKE_SHORTAGE = "stocktake_shortage"
VAR_IN_TRANSIT_SHORTAGE = "in_transit_shortage"
VAR_TRANSFER_SHORTAGE = "transfer_shortage"
VAR_RETURN_SHORTAGE = "return_shortage"


@dataclass(frozen=True)
class Delta:
    """一次事件对某个库存桶造成的有向变化。"""

    product_id: str
    batch_id: str
    bucket: str
    owner: str
    quantity: int


@dataclass(frozen=True)
class VarianceDraft:
    """由事件推导出的待落库差异记录。"""

    variance_type: str
    product_id: str
    batch_id: str
    quantity: int
    detail: dict[str, Any]


@dataclass
class Shipment:
    """一张尚未结清的发运单据（企业出库或仓间调拨）。"""

    doc_kind: str
    ref_no: str
    channel_id: str
    product_id: str
    batch_id: str
    remaining: int
    to_location_id: str | None
    owner: str


@dataclass
class ReturnDoc:
    """一张尚未被企业接收的退货单据。"""

    ref_no: str
    channel_id: str
    product_id: str
    batch_id: str
    remaining: int


@dataclass
class State:
    """按 (渠道, 产品, 批次, 库位) 与单据维度保存的库存状态。"""

    sellable: dict[tuple[str, str, str, str], int] = field(default_factory=dict)
    disputed: dict[tuple[str, str, str, str], int] = field(default_factory=dict)
    shipments: dict[tuple[str, str, str], Shipment] = field(default_factory=dict)
    returns: dict[tuple[str, str], ReturnDoc] = field(default_factory=dict)

    def sellable_qty(self, channel_id: str, product_id: str, batch_id: str,
                     location_id: str) -> int:
        return self.sellable.get((channel_id, product_id, batch_id, location_id), 0)

    def combo_sellable(self, channel_id: str, product_id: str, batch_id: str) -> int:
        return sum(qty for (ch, p, b, _), qty in self.sellable.items()
                   if (ch, p, b) == (channel_id, product_id, batch_id))

    def combo_disputed(self, channel_id: str, product_id: str, batch_id: str) -> int:
        return sum(qty for (ch, p, b, _), qty in self.disputed.items()
                   if (ch, p, b) == (channel_id, product_id, batch_id))


def _bump(bucket: dict[tuple[str, str, str, str], int],
          key: tuple[str, str, str, str], qty: int) -> None:
    value = bucket.get(key, 0) + qty
    if value:
        bucket[key] = value
    else:
        bucket.pop(key, None)


def _require_sellable(state: State, channel_id: str, product_id: str, batch_id: str,
                      location_id: str, quantity: int) -> None:
    available = state.sellable_qty(channel_id, product_id, batch_id, location_id)
    if available < quantity:
        raise ValidationError(
            f"可销售库存不足：{product_id}/{batch_id}@{location_id} 现有 {available}，需要 {quantity}"
        )


def apply_event(state: State, event: Mapping[str, Any]) -> tuple[list[Delta], list[VarianceDraft]]:
    """把一条事件折叠进状态，返回库存桶变化与差异草稿。

    该函数同时服务于写入期校验与离线重建：同一规则、同一顺序、同一结果。
    """

    kind = event["kind"]
    phase = event["phase"]
    channel_id = event["channel_id"]
    product_id = event["product_id"]
    batch_id = event["batch_id"]
    quantity = int(event["quantity"])
    ref_no = event["ref_no"]
    deltas: list[Delta] = []
    variances: list[VarianceDraft] = []

    def delta(bucket: str, owner: str, qty: int) -> None:
        if qty:
            deltas.append(Delta(product_id, batch_id, bucket, owner, qty))

    if kind == KIND_OUTBOUND:
        key = (channel_id, KIND_OUTBOUND, ref_no)
        if key in state.shipments:
            raise ConflictError(f"发货单号 {ref_no} 已存在")
        state.shipments[key] = Shipment(KIND_OUTBOUND, ref_no, channel_id, product_id,
                                        batch_id, quantity, None, OWNER_ENTERPRISE)
        delta(IN_TRANSIT, OWNER_ENTERPRISE, quantity)
    elif kind == KIND_RECEIPT:
        ship = state.shipments.get((channel_id, KIND_OUTBOUND, ref_no))
        if ship is None:
            raise ValidationError(f"发货单 {ref_no} 不存在或已结清")
        if quantity > ship.remaining:
            raise ValidationError(
                f"签收数量 {quantity} 超过发货单 {ref_no} 剩余 {ship.remaining}"
            )
        location_id = event["to_location_id"]
        ship.remaining -= quantity
        _bump(state.sellable, (channel_id, product_id, batch_id, location_id), quantity)
        delta(IN_TRANSIT, OWNER_ENTERPRISE, -quantity)
        delta(SELLABLE, OWNER_CHANNEL, quantity)
        if ship.remaining == 0:
            del state.shipments[(channel_id, KIND_OUTBOUND, ref_no)]
        elif event["final"]:
            variances.append(VarianceDraft(
                VAR_IN_TRANSIT_SHORTAGE, product_id, batch_id, -ship.remaining,
                {"ref_no": ref_no, "written_off": ship.remaining},
            ))
            delta(IN_TRANSIT, OWNER_ENTERPRISE, -ship.remaining)
            del state.shipments[(channel_id, KIND_OUTBOUND, ref_no)]
    elif kind == KIND_TRANSFER and phase == PHASE_SHIPPED:
        key = (channel_id, KIND_TRANSFER, ref_no)
        if key in state.shipments:
            raise ConflictError(f"调拨单号 {ref_no} 已存在")
        from_location = event["from_location_id"]
        _require_sellable(state, channel_id, product_id, batch_id, from_location, quantity)
        _bump(state.sellable, (channel_id, product_id, batch_id, from_location), -quantity)
        state.shipments[key] = Shipment(KIND_TRANSFER, ref_no, channel_id, product_id,
                                        batch_id, quantity, event["to_location_id"],
                                        OWNER_CHANNEL)
        delta(SELLABLE, OWNER_CHANNEL, -quantity)
        delta(IN_TRANSIT, OWNER_CHANNEL, quantity)
    elif kind == KIND_TRANSFER and phase == PHASE_RECEIVED:
        ship = state.shipments.get((channel_id, KIND_TRANSFER, ref_no))
        if ship is None:
            raise ValidationError(f"调拨单 {ref_no} 不存在或已结清")
        if quantity > ship.remaining:
            raise ValidationError(
                f"调拨接收数量 {quantity} 超过调拨单 {ref_no} 剩余 {ship.remaining}"
            )
        ship.remaining -= quantity
        _bump(state.sellable, (channel_id, product_id, batch_id, ship.to_location_id), quantity)
        delta(IN_TRANSIT, OWNER_CHANNEL, -quantity)
        delta(SELLABLE, OWNER_CHANNEL, quantity)
        if ship.remaining == 0:
            del state.shipments[(channel_id, KIND_TRANSFER, ref_no)]
        elif event["final"]:
            variances.append(VarianceDraft(
                VAR_TRANSFER_SHORTAGE, product_id, batch_id, -ship.remaining,
                {"ref_no": ref_no, "written_off": ship.remaining},
            ))
            delta(IN_TRANSIT, OWNER_CHANNEL, -ship.remaining)
            del state.shipments[(channel_id, KIND_TRANSFER, ref_no)]
    elif kind == KIND_SALE:
        location_id = event["from_location_id"]
        _require_sellable(state, channel_id, product_id, batch_id, location_id, quantity)
        _bump(state.sellable, (channel_id, product_id, batch_id, location_id), -quantity)
        delta(SELLABLE, OWNER_CHANNEL, -quantity)
    elif kind == KIND_RETURN and phase == PHASE_SHIPPED:
        key = (channel_id, ref_no)
        if key in state.returns:
            raise ConflictError(f"退货单号 {ref_no} 已存在")
        location_id = event["from_location_id"]
        _require_sellable(state, channel_id, product_id, batch_id, location_id, quantity)
        _bump(state.sellable, (channel_id, product_id, batch_id, location_id), -quantity)
        state.returns[key] = ReturnDoc(ref_no, channel_id, product_id, batch_id, quantity)
        delta(SELLABLE, OWNER_CHANNEL, -quantity)
        delta(PENDING_RETURN, OWNER_CHANNEL, quantity)
    elif kind == KIND_RETURN and phase == PHASE_RECEIVED:
        doc = state.returns.get((channel_id, ref_no))
        if doc is None:
            raise ValidationError(f"退货单 {ref_no} 不存在或已结清")
        if quantity > doc.remaining:
            raise ValidationError(
                f"退货接收数量 {quantity} 超过退货单 {ref_no} 剩余 {doc.remaining}"
            )
        doc.remaining -= quantity
        delta(PENDING_RETURN, OWNER_CHANNEL, -quantity)
        if doc.remaining == 0:
            del state.returns[(channel_id, ref_no)]
        elif event["final"]:
            variances.append(VarianceDraft(
                VAR_RETURN_SHORTAGE, product_id, batch_id, -doc.remaining,
                {"ref_no": ref_no, "written_off": doc.remaining},
            ))
            delta(PENDING_RETURN, OWNER_CHANNEL, -doc.remaining)
            del state.returns[(channel_id, ref_no)]
    elif kind == KIND_STOCKTAKE:
        location_id = event["from_location_id"]
        key = (channel_id, product_id, batch_id, location_id)
        system_qty = state.sellable.get(key, 0)
        diff = quantity - system_qty
        if diff:
            _bump(state.sellable, key, diff)
            delta(SELLABLE, OWNER_CHANNEL, diff)
            variances.append(VarianceDraft(
                VAR_STOCKTAKE_SURPLUS if diff > 0 else VAR_STOCKTAKE_SHORTAGE,
                product_id, batch_id, diff,
                {"location_id": location_id, "system_qty": system_qty,
                 "counted_qty": quantity},
            ))
    elif kind == KIND_DISPUTE_FREEZE:
        for key in [k for k in state.sellable if k[:3] == (channel_id, product_id, batch_id)]:
            qty = state.sellable.pop(key)
            _bump(state.disputed, key, qty)
            delta(SELLABLE, OWNER_CHANNEL, -qty)
            delta(DISPUTED, OWNER_CHANNEL, qty)
    elif kind == KIND_DISPUTE_RELEASE:
        for key in [k for k in state.disputed if k[:3] == (channel_id, product_id, batch_id)]:
            qty = state.disputed.pop(key)
            _bump(state.sellable, key, qty)
            delta(DISPUTED, OWNER_CHANNEL, -qty)
            delta(SELLABLE, OWNER_CHANNEL, qty)
    else:  # pragma: no cover - 写入前已校验
        raise ValidationError(f"未知事件类别 {kind}")
    return deltas, variances


def fold(events: Iterable[Mapping[str, Any]]) -> State:
    """按落库顺序重放事件，得到某一时刻的库存状态。"""

    state = State()
    for event in events:
        apply_event(state, event)
    return state


@dataclass
class PeriodRebuild:
    """一个渠道在一个会计期间内的完整重建结果。"""

    opening: State
    closing: State
    deltas: list[Delta]
    variances: list[VarianceDraft]
    events: list[Mapping[str, Any]]


def rebuild_period(events: Iterable[Mapping[str, Any]], period: str) -> PeriodRebuild:
    """把单一渠道的事件按过账期间切分，重建期初、期间变动与期末状态。"""

    ordered = sorted(events, key=lambda item: item["seq"])
    opening = State()
    for event in ordered:
        if event["period"] < period:
            apply_event(opening, event)
    closing = copy.deepcopy(opening)
    deltas: list[Delta] = []
    variances: list[VarianceDraft] = []
    period_events: list[Mapping[str, Any]] = []
    for event in ordered:
        if event["period"] == period:
            d, v = apply_event(closing, event)
            deltas.extend(d)
            variances.extend(v)
            period_events.append(event)
    return PeriodRebuild(opening, closing, deltas, variances, period_events)


def bucket_totals(state: State, channel_id: str) -> dict[tuple[str, str, str, str], int]:
    """把状态汇总为 (产品, 批次, 库存桶, 货权) -> 数量。"""

    totals: dict[tuple[str, str, str, str], int] = {}

    def add(product_id: str, batch_id: str, bucket: str, owner: str, qty: int) -> None:
        if qty:
            key = (product_id, batch_id, bucket, owner)
            totals[key] = totals.get(key, 0) + qty

    for (ch, product_id, batch_id, _), qty in state.sellable.items():
        if ch == channel_id:
            add(product_id, batch_id, SELLABLE, OWNER_CHANNEL, qty)
    for (ch, product_id, batch_id, _), qty in state.disputed.items():
        if ch == channel_id:
            add(product_id, batch_id, DISPUTED, OWNER_CHANNEL, qty)
    for ship in state.shipments.values():
        if ship.channel_id == channel_id:
            add(ship.product_id, ship.batch_id, IN_TRANSIT, ship.owner, ship.remaining)
    for doc in state.returns.values():
        if doc.channel_id == channel_id:
            add(doc.product_id, doc.batch_id, PENDING_RETURN, OWNER_CHANNEL, doc.remaining)
    return totals


def snapshot_rows(channel_id: str, rebuild: PeriodRebuild) -> list[dict[str, Any]]:
    """把期间重建结果整理成可落库、可复算的快照明细行。"""

    opening = bucket_totals(rebuild.opening, channel_id)
    closing = bucket_totals(rebuild.closing, channel_id)
    movement: dict[tuple[str, str, str, str], list[int]] = {}
    for delta in rebuild.deltas:
        key = (delta.product_id, delta.batch_id, delta.bucket, delta.owner)
        in_out = movement.setdefault(key, [0, 0])
        if delta.quantity > 0:
            in_out[0] += delta.quantity
        else:
            in_out[1] += -delta.quantity
    rows = []
    for key in sorted(opening.keys() | closing.keys() | movement.keys()):
        product_id, batch_id, bucket, owner = key
        in_qty, out_qty = movement.get(key, [0, 0])
        open_qty = opening.get(key, 0)
        close_qty = closing.get(key, 0)
        if open_qty + in_qty - out_qty != close_qty:
            raise ConflictError(f"期间重建不平衡：{key} 期初 {open_qty} 入 {in_qty} 出 {out_qty} 期末 {close_qty}")
        rows.append({
            "product_id": product_id,
            "batch_id": batch_id,
            "bucket": bucket,
            "owner": owner,
            "opening_qty": open_qty,
            "in_qty": in_qty,
            "out_qty": out_qty,
            "closing_qty": close_qty,
        })
    return rows
