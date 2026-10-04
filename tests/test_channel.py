import unittest
from datetime import datetime, timezone

from beverage_ops_foundation.channel_domain import (
    DISPUTED, IN_TRANSIT, PENDING_RETURN, SELLABLE, SOURCES,
)
from beverage_ops_foundation.channel_service import ChannelReconciliationService
from beverage_ops_foundation.clock import FixedClock
from beverage_ops_foundation.errors import (
    ConflictError, PermissionDenied, SequenceForkError, ValidationError,
)
from beverage_ops_foundation.service import DomainService
from beverage_ops_foundation.storage import Database


class ChannelCase(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.base = DomainService(self.database, FixedClock(datetime(2026, 9, 1, tzinfo=timezone.utc)))
        self.base.register_organization(request_id="org", actor_id="bootstrap",
                                        organization_id="o1", name="白酒厂")
        self.base.register_actor(request_id="adm", actor_id="bootstrap", new_actor_id="ad",
                                 display_name="管理员", role="admin", organization_id="o1")
        self.base.register_actor(request_id="op", actor_id="ad", new_actor_id="op",
                                 display_name="操作员", role="operator", organization_id="o1")
        self.base.register_actor(request_id="rv", actor_id="ad", new_actor_id="rv",
                                 display_name="复核员", role="reviewer", organization_id="o1")
        self.base.register_actor(request_id="au", actor_id="ad", new_actor_id="au",
                                 display_name="审计员", role="auditor", organization_id="o1")
        self.service = ChannelReconciliationService(
            self.database, FixedClock(datetime(2026, 9, 20, tzinfo=timezone.utc)))
        self.service.register_product(actor_id="op", product_id="P1", name="产品一")
        self.service.register_channel(actor_id="op", channel_id="C1", name="渠道一")
        self.service.register_channel(actor_id="op", channel_id="C2", name="渠道二")

    def tearDown(self):
        self.database.close()

    def event(self, **overrides):
        params = {
            "actor_id": "op", "event_type": "ship_out", "product_id": "P1",
            "batch_no": "B1", "channel_id": "C1", "quantity": 10,
            "source_type": "enterprise_erp", "source_ref": "S1",
            "business_date": "2026-09-02",
        }
        params.update(overrides)
        default_sources = {
            "ship_out": "enterprise_erp",
            "receipt": "distributor_receipt",
            "transfer": "warehouse_transfer",
            "terminal_sale": "terminal_pos",
            "return": "return_order",
            "return_receipt": "return_order",
            "stocktake": "stocktake_sheet",
        }
        if overrides.get("source_type") not in SOURCES:
            params["source_type"] = default_sources.get(params["event_type"], "enterprise_erp")
        params.setdefault("request_id", "req-" + params["source_ref"])
        return self.service.record_event(**params)

    # -- 重建与指标 -------------------------------------------------------

    def test_sell_in_sell_out_and_inventory_states(self):
        self.event(event_type="ship_out", source_ref="SO", quantity=100)
        self.event(event_type="receipt", source_type="RC", quantity=100,
                   business_date="2026-09-05", unit_cost="800")
        self.event(event_type="terminal_sale", source_ref="POS", quantity=60,
                   business_date="2026-09-10", payload={"unit_price": "1200"})

        report = self.service.reconciliation_report(period_id="2026-09", channel_id="C1")
        self.assertEqual(100, report.sell_in_quantity)
        self.assertEqual("80000", report.sell_in_value)
        self.assertEqual(60, report.sell_out_quantity)
        self.assertEqual("72000", report.sell_out_value)
        self.assertEqual(40, report.sellable_quantity)
        self.assertEqual(0, report.in_transit_quantity)
        # 60 瓶 / 30 天 = 日均 2；40 / 2 = 20 天
        self.assertEqual("20.00", report.inventory_days)

        balances = {(b.state, b.owner): b.quantity
                    for b in self.service.inventory(channel_id="C1", period_id="2026-09")}
        self.assertEqual(40, balances[(SELLABLE, "distributor")])
        self.assertNotIn((SELLABLE, "enterprise"), balances)

    def test_ship_out_is_in_transit_owned_by_enterprise(self):
        self.event(event_type="ship_out", source_ref="SO", quantity=100)
        balances = {(b.state, b.owner): b.quantity
                    for b in self.service.inventory(channel_id="C1", period_id="2026-09")}
        self.assertEqual(100, balances[(IN_TRANSIT, "enterprise")])
        report = self.service.reconciliation_report(period_id="2026-09", channel_id="C1")
        self.assertEqual(100, report.in_transit_quantity)
        self.assertEqual(0, report.sell_in_quantity)  # 未签收不确认 sell-in

    def test_transfer_moves_through_in_transit_without_owner_change(self):
        self.event(event_type="receipt", source_ref="RC", quantity=50,
                   business_date="2026-09-03")
        self.event(event_type="transfer", source_type="warehouse_transfer",
                   source_ref="TR1", quantity=20, business_date="2026-09-04",
                   payload={"phase": "dispatch"})
        mid = self.service.reconciliation_report(period_id="2026-09", channel_id="C1")
        # 调拨发出后：30 可销售 + 20 在途（货权仍为经销商）
        self.assertEqual(30, mid.sellable_quantity)
        self.assertEqual(20, mid.in_transit_quantity)
        self.event(event_type="transfer", source_type="warehouse_transfer",
                   source_ref="TR2", quantity=20, business_date="2026-09-06",
                   payload={"phase": "arrive"})
        end = self.service.reconciliation_report(period_id="2026-09", channel_id="C1")
        self.assertEqual(50, end.sellable_quantity)
        self.assertEqual(0, end.in_transit_quantity)

    def test_return_flow_creates_pending_return_then_sellable(self):
        self.event(event_type="receipt", source_ref="RC", quantity=50,
                   business_date="2026-09-03")
        self.event(event_type="terminal_sale", source_ref="POS0", quantity=8,
                   business_date="2026-09-07")
        self.event(event_type="return", source_type="return_order", source_ref="RET1",
                   quantity=8, business_date="2026-09-08")
        pending = self.service.reconciliation_report(period_id="2026-09", channel_id="C1")
        self.assertEqual(8, pending.pending_return_quantity)
        self.assertEqual(8, pending.return_quantity)
        self.event(event_type="return_receipt", source_type="return_order",
                   source_ref="RET1IN", quantity=8, business_date="2026-09-09")
        done = self.service.reconciliation_report(period_id="2026-09", channel_id="C1")
        self.assertEqual(0, done.pending_return_quantity)
        self.assertEqual(50, done.sellable_quantity)

    def test_stocktake_loss_records_discrepancy_with_source(self):
        self.event(event_type="receipt", source_ref="RC", quantity=40,
                   business_date="2026-09-03")
        self.event(event_type="stocktake", source_type="stocktake_sheet",
                   source_ref="ST1", quantity=0, business_date="2026-09-28",
                   stocktake_count=37)
        report = self.service.reconciliation_report(period_id="2026-09", channel_id="C1")
        self.assertEqual(37, report.sellable_quantity)
        kinds = [(d.kind, d.quantity, d.source_ref) for d in report.discrepancies]
        self.assertIn(("stocktake_variance", 3, "ST1"), kinds)

    def test_stocktake_gain_also_records_discrepancy(self):
        self.event(event_type="receipt", source_ref="RC", quantity=40,
                   business_date="2026-09-03")
        self.event(event_type="stocktake", source_type="stocktake_sheet",
                   source_ref="ST2", quantity=0, business_date="2026-09-28",
                   stocktake_count=43)
        report = self.service.reconciliation_report(period_id="2026-09", channel_id="C1")
        self.assertEqual(43, report.sellable_quantity)
        self.assertIn(("stocktake_variance", 3, "ST2"),
                      [(d.kind, d.quantity, d.source_ref) for d in report.discrepancies])

    def test_stocktake_ignores_in_transit_and_disputed_quantity(self):
        self.event(event_type="ship_out", source_ref="SO", quantity=100,
                   business_date="2026-09-01")  # 全部在途/企业
        # 实盘货架为 0：账面可销售也是 0，差异应为 0，而不是 -100
        self.event(event_type="stocktake", source_type="stocktake_sheet",
                   source_ref="ST3", quantity=0, business_date="2026-09-28",
                   stocktake_count=0)
        report = self.service.reconciliation_report(period_id="2026-09", channel_id="C1")
        self.assertEqual(0, report.sellable_quantity)
        self.assertEqual(100, report.in_transit_quantity)
        self.assertEqual([], report.discrepancies)

    def test_dispute_freezes_only_specific_batch_not_sibling_batch(self):
        self.event(event_type="receipt", source_ref="RC1", batch_no="B1", quantity=20,
                   business_date="2026-09-03")
        self.event(event_type="receipt", source_ref="RC2", batch_no="B2", quantity=15,
                   business_date="2026-09-03")
        self.service.open_dispute(request_id="d1", actor_id="rv", product_id="P1",
                                  batch_no="B1", channel_id="C1", quantity=20,
                                  source_ref="CASE1", reason="B1 疑点")
        # B1 被冻结不能售出
        with self.assertRaises(ConflictError):
            self.event(event_type="terminal_sale", batch_no="B1", source_ref="POS1",
                       quantity=1, business_date="2026-09-04", request_id="req-POS1")
        # 同渠道的 B2 仍可正常售出
        self.event(event_type="terminal_sale", batch_no="B2", source_ref="POS2",
                   quantity=5, business_date="2026-09-04")
        report = self.service.reconciliation_report(period_id="2026-09", channel_id="C1")
        self.assertEqual(10, report.sellable_quantity)   # B2 剩余
        self.assertEqual(20, report.disputed_quantity)   # 仅 B1

    # -- 离线补传：重放与分叉 --------------------------------------------

    def test_identical_retry_is_idempotent(self):
        first = self.event(source_ref="SO", request_id="r1", quantity=10, stream_key="k",
                           stream_seq=1)
        second = self.event(source_ref="SO", request_id="r1", quantity=10, stream_key="k",
                            stream_seq=1)
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertEqual(first.event_id, second.event_id)
        self.assertEqual(1, len(self.service.list_events(channel_id="C1")))

    def test_same_stream_seq_different_content_is_fork_and_quarantined(self):
        self.event(source_ref="SO", request_id="r1", quantity=10, stream_key="k",
                   stream_seq=1)
        with self.assertRaises(SequenceForkError):
            self.event(source_ref="SO-OTHER", request_id="r2", quantity=99,
                       stream_key="k", stream_seq=1, business_date="2026-09-03")
        anomalies = self.service.list_anomalies("k")
        self.assertEqual(1, len(anomalies))
        # 分叉内容绝不入库存事件表
        refs = {e.source_ref for e in self.service.list_events(channel_id="C1")}
        self.assertNotIn("SO-OTHER", refs)

    def test_request_id_reuse_with_other_payload_conflicts(self):
        self.event(source_ref="SO", request_id="r1", quantity=10)
        with self.assertRaises(ConflictError):
            self.event(source_ref="SO2", request_id="r1", quantity=20,
                       business_date="2026-09-03")

    # -- 关账与调整 -------------------------------------------------------

    def test_late_voucher_after_close_is_appended_to_next_period(self):
        self.event(event_type="terminal_sale", source_type="POS1", quantity=10,
                   business_date="2026-09-10", payload={"unit_price": "100"})
        self.service.close_period(request_id="close9", actor_id="rv",
                                  period_id="2026-09", channel_id="C1")
        self.event(event_type="terminal_sale", source_ref="POSLATE", quantity=3,
                   business_date="2026-09-25", payload={"unit_price": "100"},
                   request_id="req-POSLATE")
        late = [e for e in self.service.list_events(channel_id="C1")
                if e.source_ref == "POSLATE"][0]
        self.assertEqual("2026-09", late.origin_period)
        self.assertEqual("2026-10", late.period_id)
        self.assertTrue(late.is_adjustment)
        # 原期间数字与快照不变
        sep = self.service.reconciliation_report(period_id="2026-09", channel_id="C1")
        self.assertEqual(10, sep.sell_out_quantity)
        self.assertTrue(sep.snapshot_valid)
        # 调整进入后续期间
        octo = self.service.reconciliation_report(period_id="2026-10", channel_id="C1")
        self.assertEqual(3, octo.sell_out_quantity)

    def test_closed_period_cannot_be_closed_twice(self):
        self.service.close_period(request_id="c1", actor_id="rv",
                                  period_id="2026-09", channel_id="C1")
        with self.assertRaises(ConflictError):
            self.service.close_period(request_id="c2", actor_id="rv",
                                      period_id="2026-09", channel_id="C1")

    # -- 争议冻结粒度 -----------------------------------------------------

    def test_dispute_freezes_only_batch_channel(self):
        self.event(event_type="ship_out", source_ref="SOC2", quantity=10,
                   channel_id="C2", business_date="2026-09-02")
        dispute = self.service.open_dispute(
            request_id="d1", actor_id="rv", product_id="P1", batch_no="B1",
            channel_id="C2", quantity=10, source_ref="CASE1", reason="破损",
            from_state=IN_TRANSIT, owner="enterprise")
        # 冻结批次在 C2 不能售出
        with self.assertRaises(ConflictError):
            self.event(event_type="terminal_sale", source_ref="POSC2", quantity=1,
                       channel_id="C2", business_date="2026-09-03", request_id="req-POSC2")
        # 同产品同批次在 C1 不受影响，C1 可正常关账
        self.event(event_type="receipt", source_ref="RCC1", quantity=5,
                   channel_id="C1", business_date="2026-09-03")
        self.service.close_period(request_id="closeC1", actor_id="rv",
                                  period_id="2026-09", channel_id="C1")
        # C2 因未决争议不能关账
        with self.assertRaises(ConflictError):
            self.service.close_period(request_id="closeC2", actor_id="rv",
                                      period_id="2026-09", channel_id="C2")
        # 争议核销后 C2 可以关账，核销成为差异来源
        self.service.resolve_dispute(request_id="d1r", actor_id="rv",
                                     dispute_id=dispute["dispute_id"], resolution="writeoff")
        self.service.close_period(request_id="closeC2b", actor_id="rv",
                                  period_id="2026-09", channel_id="C2")
        report = self.service.reconciliation_report(period_id="2026-09", channel_id="C2")
        self.assertEqual(0, report.disputed_quantity)
        self.assertTrue(any(d.kind == "writeoff" and d.quantity == 10
                            for d in report.discrepancies))

    def test_disputed_quantity_separated_from_sellable(self):
        self.event(event_type="receipt", source_ref="RC", quantity=50,
                   business_date="2026-09-03")
        self.service.open_dispute(request_id="d1", actor_id="rv", product_id="P1",
                                  batch_no="B1", channel_id="C1", quantity=12,
                                  source_ref="CASE1", reason="疑点")
        report = self.service.reconciliation_report(period_id="2026-09", channel_id="C1")
        self.assertEqual(38, report.sellable_quantity)
        self.assertEqual(12, report.disputed_quantity)
        balances = {(b.state): b.quantity
                    for b in self.service.inventory(channel_id="C1", period_id="2026-09")}
        self.assertEqual(12, balances[DISPUTED])
        self.assertNotIn(PENDING_RETURN, balances)

    # -- 权限与校验 -------------------------------------------------------

    def test_auditor_cannot_record_events(self):
        with self.assertRaises(PermissionDenied):
            self.event(actor_id="au", request_id="denied")

    def test_operator_cannot_close_period(self):
        with self.assertRaises(PermissionDenied):
            self.service.close_period(request_id="denied", actor_id="op",
                                      period_id="2026-09", channel_id="C1")

    def test_unknown_event_type_rejected(self):
        with self.assertRaises(ValidationError):
            self.event(event_type="magic_move", request_id="bad")

    def test_negative_quantity_rejected(self):
        with self.assertRaises(ValidationError):
            self.event(quantity=-5, request_id="neg")


if __name__ == "__main__":
    unittest.main()
