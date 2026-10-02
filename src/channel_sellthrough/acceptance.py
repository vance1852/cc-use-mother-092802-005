"""运行渠道动销对账服务的离线端到端验收。

场景覆盖：企业出库 → 经销商签收（短收差异）→ 仓间调拨 → 终端售出 →
退货 → 盘点差异 → 期间关账 → 迟到凭证追加调整 → 离线补传（重放/分叉/缺口）
→ 争议冻结与解除 → 其他渠道照常关账 → 审计链校验。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from beverage_ops_foundation.clock import FixedClock
from beverage_ops_foundation.service import DomainService

from .service import SellThroughService
from beverage_ops_foundation.storage import Database


def run() -> dict[str, object]:
    """执行一条完整对账链并返回关键结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "acceptance.sqlite3")
        clock = FixedClock(datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc))
        foundation = DomainService(database, clock)
        service = SellThroughService(database, clock)

        foundation.register_organization(request_id="acc-org", actor_id="bootstrap",
                                         organization_id="org-001", name="示范酒业集团")
        foundation.register_actor(request_id="acc-admin", actor_id="bootstrap",
                                  new_actor_id="admin-001", display_name="系统管理员",
                                  role="admin", organization_id="org-001")
        foundation.register_actor(request_id="acc-operator", actor_id="admin-001",
                                  new_actor_id="operator-001", display_name="渠道运营",
                                  role="operator", organization_id="org-001")
        foundation.register_actor(request_id="acc-reviewer", actor_id="admin-001",
                                  new_actor_id="reviewer-001", display_name="财务复核",
                                  role="reviewer", organization_id="org-001")

        service.register_channel(request_id="acc-ch1", actor_id="operator-001",
                                 channel_id="CH-001", organization_id="org-001",
                                 name="华东经销商", channel_type="distributor")
        service.register_channel(request_id="acc-ch2", actor_id="operator-001",
                                 channel_id="CH-002", organization_id="org-001",
                                 name="华南经销商", channel_type="distributor")
        service.register_location(request_id="acc-loc-east", actor_id="operator-001",
                                  channel_id="CH-001", location_id="east", name="东部仓")
        service.register_product(request_id="acc-prod", actor_id="operator-001",
                                 product_id="P-001", name="经典白酒 500ml", category="baijiu")
        service.register_batch(request_id="acc-batch1", actor_id="operator-001",
                               batch_id="B-001", product_id="P-001", batch_no="20260701",
                               produced_on="2026-07-01")
        service.register_batch(request_id="acc-batch2", actor_id="operator-001",
                               batch_id="B-002", product_id="P-001", batch_no="20260801",
                               produced_on="2026-08-01")

        # 8 月：出库 1000 → 签收 950（终收，短收 50 计入差异）→ 售出/调拨/退货/盘点
        service.record_event(request_id="acc-out1", actor_id="operator-001", kind="outbound",
                             product_id="P-001", batch_id="B-001", channel_id="CH-001",
                             quantity=1000, business_date="2026-08-05", ref_no="SH-1")
        service.record_event(request_id="acc-rcv1", actor_id="operator-001", kind="receipt",
                             product_id="P-001", batch_id="B-001", channel_id="CH-001",
                             quantity=950, business_date="2026-08-08", ref_no="SH-1",
                             to_location_id="main", final=True)
        service.record_event(request_id="acc-sale1", actor_id="operator-001", kind="sale",
                             product_id="P-001", batch_id="B-001", channel_id="CH-001",
                             quantity=400, business_date="2026-08-15", from_location_id="main")
        service.record_event(request_id="acc-tr1-out", actor_id="operator-001", kind="transfer",
                             phase="shipped", product_id="P-001", batch_id="B-001",
                             channel_id="CH-001", quantity=100, business_date="2026-08-16",
                             from_location_id="main", to_location_id="east", ref_no="TR-1")
        service.record_event(request_id="acc-tr1-in", actor_id="operator-001", kind="transfer",
                             phase="received", product_id="P-001", batch_id="B-001",
                             channel_id="CH-001", quantity=100, business_date="2026-08-17",
                             ref_no="TR-1")
        service.record_event(request_id="acc-rt1-out", actor_id="operator-001", kind="return",
                             phase="shipped", product_id="P-001", batch_id="B-001",
                             channel_id="CH-001", quantity=20, business_date="2026-08-20",
                             from_location_id="main", ref_no="RT-1")
        service.record_event(request_id="acc-rt1-in", actor_id="operator-001", kind="return",
                             phase="received", product_id="P-001", batch_id="B-001",
                             channel_id="CH-001", quantity=20, business_date="2026-08-25",
                             ref_no="RT-1")
        service.record_event(request_id="acc-stock1", actor_id="reviewer-001", kind="stocktake",
                             product_id="P-001", batch_id="B-001", channel_id="CH-001",
                             quantity=425, business_date="2026-08-31", from_location_id="main")

        close_aug = service.close_period(request_id="acc-close-aug", actor_id="reviewer-001",
                                         channel_id="CH-001", period="2026-08")

        # 关账后到达的 8 月凭证只能追加到 9 月，不能覆盖 8 月记录
        late = service.record_event(request_id="acc-late-sale", actor_id="operator-001",
                                    kind="sale", product_id="P-001", batch_id="B-001",
                                    channel_id="CH-001", quantity=10,
                                    business_date="2026-08-30", from_location_id="main")

        # 9 月：第二个批次正常流转，用于验证争议只冻结相关批次
        service.record_event(request_id="acc-out2", actor_id="operator-001", kind="outbound",
                             product_id="P-001", batch_id="B-002", channel_id="CH-001",
                             quantity=100, business_date="2026-09-10", ref_no="SH-2")
        service.record_event(request_id="acc-rcv2", actor_id="operator-001", kind="receipt",
                             product_id="P-001", batch_id="B-002", channel_id="CH-001",
                             quantity=100, business_date="2026-09-11", ref_no="SH-2")

        # 离线补传：重放、序列分叉、序号缺口
        first_upload = service.backfill_events(actor_id="operator-001", source_id="POS-1",
                                               items=[
                                                   {"source_seq": 1, "kind": "sale",
                                                    "product_id": "P-001", "batch_id": "B-001",
                                                    "channel_id": "CH-001", "quantity": 5,
                                                    "business_date": "2026-09-03",
                                                    "from_location_id": "east"},
                                                   {"source_seq": 2, "kind": "sale",
                                                    "product_id": "P-001", "batch_id": "B-001",
                                                    "channel_id": "CH-001", "quantity": 7,
                                                    "business_date": "2026-09-04",
                                                    "from_location_id": "east"},
                                               ])
        second_upload = service.backfill_events(actor_id="operator-001", source_id="POS-1",
                                                items=[
                                                    {"source_seq": 1, "kind": "sale",
                                                     "product_id": "P-001", "batch_id": "B-001",
                                                     "channel_id": "CH-001", "quantity": 5,
                                                     "business_date": "2026-09-03",
                                                     "from_location_id": "east"},
                                                    {"source_seq": 2, "kind": "sale",
                                                     "product_id": "P-001", "batch_id": "B-001",
                                                     "channel_id": "CH-001", "quantity": 9,
                                                     "business_date": "2026-09-04",
                                                     "from_location_id": "east"},
                                                    {"source_seq": 5, "kind": "sale",
                                                     "product_id": "P-001", "batch_id": "B-001",
                                                     "channel_id": "CH-001", "quantity": 3,
                                                     "business_date": "2026-09-06",
                                                     "from_location_id": "east"},
                                                ])

        # 争议只冻结 B-001：B-001 售出被拒，B-002 仍可售出
        dispute = service.open_dispute(request_id="acc-dispute", actor_id="reviewer-001",
                                       channel_id="CH-001", product_id="P-001",
                                       batch_id="B-001", reason="终端扫码与签收数量不符")
        frozen_sale_blocked = False
        try:
            service.record_event(request_id="acc-blocked-sale", actor_id="operator-001",
                                 kind="sale", product_id="P-001", batch_id="B-001",
                                 channel_id="CH-001", quantity=1, business_date="2026-10-01",
                                 from_location_id="main")
        except Exception:
            frozen_sale_blocked = True
        service.record_event(request_id="acc-sale-b2", actor_id="operator-001", kind="sale",
                             product_id="P-001", batch_id="B-002", channel_id="CH-001",
                             quantity=30, business_date="2026-09-20", from_location_id="main")

        # 争议未解除，其他渠道与争议渠道本身都能完成 9 月关账
        close_sep_ch2 = service.close_period(request_id="acc-close-sep-ch2",
                                             actor_id="reviewer-001",
                                             channel_id="CH-002", period="2026-09")
        close_sep_ch1 = service.close_period(request_id="acc-close-sep-ch1",
                                             actor_id="reviewer-001",
                                             channel_id="CH-001", period="2026-09")
        service.resolve_dispute(request_id="acc-resolve", actor_id="reviewer-001",
                                dispute_id=dispute.dispute_id, resolution="差异已确认为扫码延迟")

        metrics_aug = service.sell_through_metrics(channel_id="CH-001", period="2026-08")
        metrics_sep = service.sell_through_metrics(channel_id="CH-001", period="2026-09")
        snapshot_aug = service.period_snapshot(channel_id="CH-001", period="2026-08")
        inventory = service.inventory_view(channel_id="CH-001")
        variances = service.list_variances(channel_id="CH-001", period="2026-08")
        anomalies = service.list_source_anomalies(source_id="POS-1")
        audit_valid, audit_events = foundation.verify_audit()

        result = {
            "status": "ok",
            "audit_valid": audit_valid,
            "audit_events": audit_events,
            "aug_sell_in": metrics_aug["sell_in"],
            "aug_sell_out": metrics_aug["sell_out"],
            "aug_verified": metrics_aug["verified"],
            "aug_variance_sources": metrics_aug["variance_sources"],
            "aug_snapshot_rows": close_aug.snapshot_rows,
            "aug_snapshot_verified": snapshot_aug["verified"],
            "late_posted_period": late.period,
            "late_is_adjustment": late.is_adjustment,
            "sep_sell_out": metrics_sep["sell_out"],
            "sep_adjustments": metrics_sep["adjustment_count"],
            "backfill_first": first_upload["summary"],
            "backfill_second": second_upload["summary"],
            "anomaly_kinds": [a["kind"] for a in anomalies["items"]],
            "dispute_frozen": dispute.frozen_quantity,
            "frozen_sale_blocked": frozen_sale_blocked,
            "ch2_closed_during_dispute": close_sep_ch2.period == "2026-09",
            "ch1_closed_during_dispute": close_sep_ch1.period == "2026-09",
            "inventory_items": len(inventory["items"]),
            "aug_variance_count": len(variances["items"]),
        }
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    ok = (result["status"] == "ok" and result["audit_valid"]
          and result["aug_verified"] and result["aug_snapshot_verified"]
          and result["late_is_adjustment"] and result["frozen_sale_blocked"]
          and result["ch2_closed_during_dispute"])
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
