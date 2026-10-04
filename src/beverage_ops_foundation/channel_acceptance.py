"""运行渠道动销对账的离线端到端验收。

在临时 SQLite 数据库中走通：企业出库 -> 在途 -> 经销商签收(sell-in) ->
终端售出(sell-out) -> 盘点差异；离线补传的重放与序列分叉隔离；关账后迟到
凭证追加到后续期间；争议只冻结相关批次/渠道且不影响其他渠道关账；
重建快照与审计链校验。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .channel_service import ChannelReconciliationService
from .clock import FixedClock
from .errors import ConflictError, SequenceForkError
from .service import DomainService
from .storage import Database


def run() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "channel_acceptance.sqlite3")
        base = DomainService(database, FixedClock(datetime(2026, 9, 1, tzinfo=timezone.utc)))
        base.register_organization(request_id="org", actor_id="bootstrap",
                                   organization_id="org-001", name="示范白酒企业")
        base.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="admin-001",
                            display_name="系统管理员", role="admin", organization_id="org-001")
        base.register_actor(request_id="operator", actor_id="admin-001", new_actor_id="op-001",
                            display_name="渠道业务员", role="operator", organization_id="org-001")
        base.register_actor(request_id="reviewer", actor_id="admin-001", new_actor_id="rv-001",
                            display_name="关账复核员", role="reviewer", organization_id="org-001")

        service = ChannelReconciliationService(
            database, FixedClock(datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)))
        service.register_product(actor_id="op-001", product_id="SKU-001", name="53度酱香型白酒")
        service.register_channel(actor_id="op-001", channel_id="D-NORTH", name="华北经销商")
        service.register_channel(actor_id="op-001", channel_id="D-EAST", name="华东经销商")

        def ev(channel, **kw):
            kw.setdefault("actor_id", "op-001")
            kw["channel_id"] = channel
            kw.setdefault("request_id", "req-" + kw["source_ref"] + "-" + channel)
            return service.record_event(**kw)

        # 华北：出库 100（在途/企业货权）-> 签收 100（sell-in）-> 售出 60（sell-out）
        ev("D-NORTH", event_type="ship_out", product_id="SKU-001", batch_no="B202609",
           quantity=100, source_type="enterprise_erp", source_ref="ERP-1",
           business_date="2026-09-02", unit_cost="800", stream_key="erp", stream_seq=1)
        ev("D-NORTH", event_type="receipt", product_id="SKU-001", batch_no="B202609",
           quantity=100, source_type="distributor_receipt", source_ref="RCP-1",
           business_date="2026-09-05", unit_cost="800", stream_key="rcp", stream_seq=1)
        ev("D-NORTH", event_type="terminal_sale", product_id="SKU-001", batch_no="B202609",
           quantity=60, source_type="terminal_pos", source_ref="POS-1",
           business_date="2026-09-12", payload={"unit_price": "1200"},
           stream_key="pos", stream_seq=1)
        # 盘点：账面 40，实盘 38，盘亏 2
        ev("D-NORTH", event_type="stocktake", product_id="SKU-001", batch_no="B202609",
           quantity=0, source_type="stocktake_sheet", source_ref="ST-1",
           business_date="2026-09-27", stocktake_count=38)

        september = service.reconciliation_report(period_id="2026-09", channel_id="D-NORTH")

        # 完全重放：同 request_id 与同来源序号，应返回重放回执而不新增事件
        replay = ev("D-NORTH", event_type="ship_out", product_id="SKU-001", batch_no="B202609",
                    quantity=100, source_type="enterprise_erp", source_ref="ERP-1",
                    business_date="2026-09-02", unit_cost="800", stream_key="erp", stream_seq=1)

        # 序列分叉：同流同序号不同数量，必须隔离且不入账
        fork_raised = False
        try:
            ev("D-NORTH", event_type="ship_out", product_id="SKU-001", batch_no="B202609",
               quantity=999, source_type="enterprise_erp", source_ref="ERP-FORK",
               business_date="2026-09-03", stream_key="erp", stream_seq=1,
               request_id="req-fork")
        except SequenceForkError:
            fork_raised = True

        # 关账华北 9 月
        service.close_period(request_id="close-north-9", actor_id="rv-001",
                             period_id="2026-09", channel_id="D-NORTH")
        # 关账后到达的 9 月售出凭证：追加为 10 月调整，9 月数字不变
        ev("D-NORTH", event_type="terminal_sale", product_id="SKU-001", batch_no="B202609",
           quantity=4, source_type="terminal_pos", source_ref="POS-LATE",
           business_date="2026-09-29", payload={"unit_price": "1200"},
           stream_key="pos", stream_seq=2, request_id="req-late")
        late = [e for e in service.list_events(channel_id="D-NORTH")
                if e.source_ref == "POS-LATE"][0]
        september_after = service.reconciliation_report(period_id="2026-09", channel_id="D-NORTH")
        october = service.reconciliation_report(period_id="2026-10", channel_id="D-NORTH")

        # 华东：出库 10 在途，对该批次开启争议（冻结在途/企业货权）
        ev("D-EAST", event_type="ship_out", product_id="SKU-001", batch_no="B202609",
           quantity=10, source_type="enterprise_erp", source_ref="ERP-2",
           business_date="2026-09-08")
        dispute = service.open_dispute(
            request_id="dispute-1", actor_id="rv-001", product_id="SKU-001",
            batch_no="B202609", channel_id="D-EAST", quantity=10, source_ref="CASE-1",
            reason="运输破损争议", from_state="in_transit", owner="enterprise")
        east_blocked = False
        try:
            service.close_period(request_id="close-east-9", actor_id="rv-001",
                                 period_id="2026-09", channel_id="D-EAST")
        except ConflictError:
            east_blocked = True
        # 华北不受华东争议影响，10 月仍可关账
        north_october_closed = True
        try:
            service.close_period(request_id="close-north-10", actor_id="rv-001",
                                 period_id="2026-10", channel_id="D-NORTH")
        except ConflictError:
            north_october_closed = False

        audit_valid, audit_events = base.verify_audit()
        result = {
            "status": "ok",
            "sell_in_quantity": september.sell_in_quantity,
            "sell_out_quantity": september.sell_out_quantity,
            "sellable_quantity": september.sellable_quantity,
            "inventory_days": september.inventory_days,
            "discrepancy_kinds": [d.kind for d in september.discrepancies],
            "replay_replayed": replay.replayed,
            "sequence_fork_detected": fork_raised,
            "quarantined_not_posted": not any(
                e.source_ref == "ERP-FORK" for e in service.list_events(channel_id="D-NORTH")),
            "late_adjustment_period": late.period_id,
            "late_is_adjustment": late.is_adjustment,
            "sealed_period_unchanged": (
                september_after.sell_out_quantity == september.sell_out_quantity
                and september_after.snapshot_valid is True
            ),
            "adjustment_in_next_period": october.sell_out_quantity == 4,
            "dispute_froze_east_close": east_blocked,
            "north_unaffected_and_closed": north_october_closed,
            "dispute_id": dispute["dispute_id"],
            "audit_valid": audit_valid,
            "audit_events": audit_events,
        }
        database.close()
        return result


def main() -> int:
    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    expected_true = [
        "replay_replayed", "sequence_fork_detected", "quarantined_not_posted",
        "late_is_adjustment", "sealed_period_unchanged", "adjustment_in_next_period",
        "dispute_froze_east_close", "north_unaffected_and_closed", "audit_valid",
    ]
    ok = result["status"] == "ok" and all(result[key] is True for key in expected_true)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
