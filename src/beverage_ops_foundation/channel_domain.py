"""定义渠道动销台账允许的事件类型与重建口径。

事件是台账中唯一的事实来源，任何库存数字都由事件重建，不允许覆盖。
每个事件描述同一批货在两个库存池之间的移动或状态变化，并显式记录
货权转移方向，从而同时重建数量账与货权账。
"""

from __future__ import annotations

#: 企业向经销商开单出库（货在途，货权仍属企业直到签收）。
SHIP_OUT = "ship_out"
#: 经销商签收企业出库（在途 -> 经销商可销售，货权企业 -> 经销商）。
RECEIPT = "receipt"
#: 仓间/跨仓调拨（在途 -> 目标仓，货权不变，归属渠道不变）。
TRANSFER = "transfer"
#: 终端售出（经销商可销售 -> 已售消费者，sell-out 唯一来源）。
TERMINAL_SALE = "terminal_sale"
#: 客户/终端向经销商退货（可销售/已售 -> 待退在途）。
RETURN = "return"
#: 退回货物由经销商签收入库（待退在途 -> 经销商可销售）。
RETURN_RECEIPT = "return_receipt"
#: 盘点差异，gain 为盘盈、loss 为盘亏（只调整数量，货权不变）。
STOCKTAKE = "stocktake"
#: 把批次某渠道的库存标记为争议冻结（数量从可销售移入争议）。
DISPUTE_OPEN = "dispute_open"
#: 争议解除（争议数量按处置结果转回可销售或核销）。
DISPUTE_RESOLVE = "dispute_resolve"

EVENT_TYPES = frozenset({
    SHIP_OUT, RECEIPT, TRANSFER, TERMINAL_SALE, RETURN,
    RETURN_RECEIPT, STOCKTAKE, DISPUTE_OPEN, DISPUTE_RESOLVE,
})

#: 库存状态池：可销售、在途、待退、争议。
SELLABLE = "sellable"
IN_TRANSIT = "in_transit"
PENDING_RETURN = "pending_return"
DISPUTED = "disputed"
SOLD = "sold"  # 已到消费者，仅用于 sell-out 累计，不再参与库存

INVENTORY_STATES = (SELLABLE, IN_TRANSIT, PENDING_RETURN, DISPUTED)

#: 货权方：企业（未实现销售）或经销商（已 sell-in）。
OWNER_ENTERPRISE = "enterprise"
OWNER_DISTRIBUTOR = "distributor"
OWNERS = frozenset({OWNER_ENTERPRISE, OWNER_DISTRIBUTOR})

#: 凭证来源渠道（企业 ERP、经销商签收、仓配、终端动销、客户退货、实地盘点）。
SOURCES = frozenset({
    "enterprise_erp",       # 企业出库单
    "distributor_receipt",  # 经销商签收回执
    "warehouse_transfer",   # 仓间调拨单
    "terminal_pos",         # 终端售出
    "return_order",         # 退货单
    "stocktake_sheet",      # 盘点单
    "dispute_case",         # 争议工单
})


def is_event_type(value: str) -> bool:
    return value in EVENT_TYPES
