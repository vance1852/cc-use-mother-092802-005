"""把库存事件流重建成数量账、货权账与差异清单。

本模块是纯函数：输入为按确定顺序排列的事件字典，输出库存池与期间指标，
不读写数据库。任何数字都能由同一批事件再次复算得到，这是“可复算”的基础。
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from . import channel_domain as cd


def _money(value: Any) -> Decimal:
    if value is None:
        return Decimal(0)
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal(0)


def _key(event: dict[str, Any]) -> tuple[str, str, str]:
    return event["product_id"], event["batch_no"], event["channel_id"]


def ordered(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """事件的确定性入账顺序：期间、业务日期、单调插入序号。"""

    return sorted(
        events,
        key=lambda e: (e["period_id"], e["business_date"], e.get("entry_seq", 0)),
    )


class Rebuild:
    """持有累计库存池与差异收集器。"""

    def __init__(self) -> None:
        # pools[(product,batch,channel)][state][owner] = qty
        self.pools: dict[tuple[str, str, str], dict[str, dict[str, int]]] = defaultdict(
            lambda: defaultdict(lambda: defaultdict(int))
        )
        self.discrepancies: list[dict[str, Any]] = []

    def _discrepancy(self, event: dict[str, Any], *, kind: str, quantity: int,
                     detail: dict[str, Any]) -> None:
        self.discrepancies.append({
            "kind": kind,
            "product_id": event["product_id"],
            "batch_no": event["batch_no"],
            "channel_id": event["channel_id"],
            "quantity": quantity,
            "source_type": event["source_type"],
            "source_ref": event["source_ref"],
            "period_id": event["period_id"],
            "detail": detail,
        })

    def _move(self, event: dict[str, Any], *, state: str, owner: str, delta: int,
              kind: str) -> None:
        bucket = self.pools[_key(event)][state]
        bucket[owner] += delta
        if bucket[owner] < 0:
            # 账面被透支（例如无签收却售出、无出库却签收）：记录差异来源并钳到 0。
            shortfall = -bucket[owner]
            self._discrepancy(event, kind=kind, quantity=shortfall,
                              detail={"state": state, "owner": owner, "event_type": event["event_type"]})
            bucket[owner] = 0

    def apply(self, event: dict[str, Any]) -> None:
        et = event["event_type"]
        qty = int(event["quantity"])
        if et == cd.SHIP_OUT:
            self._move(event, state=cd.IN_TRANSIT, owner=cd.OWNER_ENTERPRISE,
                       delta=qty, kind="unmatched_intransit")
        elif et == cd.RECEIPT:
            self._move(event, state=cd.IN_TRANSIT, owner=cd.OWNER_ENTERPRISE,
                       delta=-qty, kind="unmatched_intransit")
            self._move(event, state=cd.SELLABLE, owner=cd.OWNER_DISTRIBUTOR,
                       delta=qty, kind="negative_pool")
        elif et == cd.TRANSFER:
            phase = str(event.get("payload", {}).get("phase", "dispatch"))
            if phase == "arrive":
                self._move(event, state=cd.IN_TRANSIT, owner=cd.OWNER_DISTRIBUTOR,
                           delta=-qty, kind="in_transit_gap")
                self._move(event, state=cd.SELLABLE, owner=cd.OWNER_DISTRIBUTOR,
                           delta=qty, kind="negative_pool")
            else:
                self._move(event, state=cd.SELLABLE, owner=cd.OWNER_DISTRIBUTOR,
                           delta=-qty, kind="negative_pool")
                self._move(event, state=cd.IN_TRANSIT, owner=cd.OWNER_DISTRIBUTOR,
                           delta=qty, kind="in_transit_gap")
        elif et == cd.TERMINAL_SALE:
            self._move(event, state=cd.SELLABLE, owner=cd.OWNER_DISTRIBUTOR,
                       delta=-qty, kind="sellout_without_stock")
        elif et == cd.RETURN:
            self._move(event, state=cd.PENDING_RETURN, owner=cd.OWNER_DISTRIBUTOR,
                       delta=qty, kind="negative_pool")
        elif et == cd.RETURN_RECEIPT:
            self._move(event, state=cd.PENDING_RETURN, owner=cd.OWNER_DISTRIBUTOR,
                       delta=-qty, kind="pending_return_gap")
            self._move(event, state=cd.SELLABLE, owner=cd.OWNER_DISTRIBUTOR,
                       delta=qty, kind="negative_pool")
        elif et == cd.STOCKTAKE:
            variance = int(event["variance_qty"])
            if variance != 0:
                # 盘点只调整在仓可销售现货，差异（盘盈/盘亏）都带来源可追溯。
                self._move(event, state=cd.SELLABLE, owner=cd.OWNER_DISTRIBUTOR,
                           delta=variance, kind="stocktake_variance")
                self._discrepancy(event, kind="stocktake_variance", quantity=abs(variance),
                                  detail={"variance": variance})
        elif et == cd.DISPUTE_OPEN:
            payload = event.get("payload", {})
            from_state = str(payload.get("from_state", cd.SELLABLE))
            owner = str(payload.get("owner", cd.OWNER_DISTRIBUTOR))
            self._move(event, state=from_state, owner=owner, delta=-qty, kind="negative_pool")
            self._move(event, state=cd.DISPUTED, owner=owner, delta=qty, kind="negative_pool")
        elif et == cd.DISPUTE_RESOLVE:
            payload = event.get("payload", {})
            resolution = str(payload.get("resolution", "release"))
            from_state = str(payload.get("from_state", cd.SELLABLE))
            owner = str(payload.get("owner", cd.OWNER_DISTRIBUTOR))
            self._move(event, state=cd.DISPUTED, owner=owner, delta=-qty, kind="dispute_gap")
            if resolution == "writeoff":
                self._discrepancy(event, kind="writeoff", quantity=qty,
                                  detail={"resolution": resolution})
            else:
                self._move(event, state=from_state, owner=owner, delta=qty, kind="negative_pool")

    def state_totals(self) -> dict[tuple[str, str, str], dict[str, int]]:
        """汇总到 (产品,批次,渠道) x 库存状态 的数量（不分货权）。"""

        return {
            key: {state: sum(owners.values()) for state, owners in states.items()}
            for key, states in self.pools.items()
        }


def rebuild(all_events: Iterable[dict[str, Any]], upto_period: str | None = None) -> Rebuild:
    """按顺序应用截至指定期间（含）的全部事件，返回累计重建结果。"""

    engine = Rebuild()
    for event in ordered(all_events):
        if upto_period is not None and event["period_id"] > upto_period:
            continue
        engine.apply(event)
    return engine


def period_metrics(period_events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """仅汇总某一期间内入账事件的动销量与金额（含追加调整）。"""

    sell_in_qty = sell_out_qty = return_qty = 0
    sell_in_value = Decimal(0)
    sell_out_value = Decimal(0)
    for event in ordered(period_events):
        et = event["event_type"]
        qty = int(event["quantity"])
        payload = event.get("payload", {})
        if et == cd.RECEIPT:
            sell_in_qty += qty
            sell_in_value += _money(event.get("unit_cost") or payload.get("unit_cost")) * qty
        elif et == cd.TERMINAL_SALE:
            sell_out_qty += qty
            price = payload.get("unit_price") or event.get("unit_cost") or payload.get("unit_cost")
            sell_out_value += _money(price) * qty
        elif et == cd.RETURN:
            return_qty += qty
    return {
        "sell_in_quantity": sell_in_qty,
        "sell_out_quantity": sell_out_qty,
        "return_quantity": return_qty,
        "net_sell_out_quantity": sell_out_qty - return_qty,
        "sell_in_value": str(sell_in_value),
        "sell_out_value": str(sell_out_value),
    }
