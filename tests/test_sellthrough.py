import unittest
from datetime import datetime, timezone

from beverage_ops_foundation.clock import FixedClock
from beverage_ops_foundation.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from beverage_ops_foundation.service import DomainService
from beverage_ops_foundation.storage import Database
from channel_sellthrough import projection as proj
from channel_sellthrough.service import SellThroughService


class SellThroughTestBase(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        clock = FixedClock(datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc))
        self.foundation = DomainService(self.database, clock)
        self.service = SellThroughService(self.database, clock)
        self.foundation.register_organization(request_id="org", actor_id="bootstrap",
                                              organization_id="o1", name="酒业集团")
        self.foundation.register_organization(request_id="org2", actor_id="bootstrap",
                                              organization_id="o2", name="其他集团")
        self.foundation.register_actor(request_id="admin", actor_id="bootstrap",
                                       new_actor_id="a1", display_name="管理员",
                                       role="admin", organization_id="o1")
        self.foundation.register_actor(request_id="operator", actor_id="a1",
                                       new_actor_id="op1", display_name="运营",
                                       role="operator", organization_id="o1")
        self.foundation.register_actor(request_id="reviewer", actor_id="a1",
                                       new_actor_id="rv1", display_name="复核",
                                       role="reviewer", organization_id="o1")
        self.foundation.register_actor(request_id="auditor", actor_id="a1",
                                       new_actor_id="au1", display_name="审计",
                                       role="auditor", organization_id="o1")
        self.foundation.register_actor(request_id="outsider", actor_id="a1",
                                       new_actor_id="op2", display_name="外部运营",
                                       role="operator", organization_id="o2")
        self.service.register_channel(request_id="ch1", actor_id="op1", channel_id="C1",
                                      organization_id="o1", name="华东经销商", channel_type="distributor")
        self.service.register_channel(request_id="ch2", actor_id="op1", channel_id="C2",
                                      organization_id="o1", name="华南经销商", channel_type="distributor")
        self.service.register_location(request_id="loc-east", actor_id="op1", channel_id="C1",
                                       location_id="east", name="东部仓")
        self.service.register_product(request_id="p1", actor_id="op1", product_id="P1",
                                      name="经典白酒", category="baijiu")
        self.service.register_batch(request_id="b1", actor_id="op1", batch_id="B1",
                                    product_id="P1", batch_no="20260701", produced_on="2026-07-01")
        self.service.register_batch(request_id="b2", actor_id="op1", batch_id="B2",
                                    product_id="P1", batch_no="20260801", produced_on="2026-08-01")

    def tearDown(self):
        self.database.close()

    def event(self, **kwargs):
        kwargs.setdefault("actor_id", "op1")
        kwargs.setdefault("product_id", "P1")
        kwargs.setdefault("batch_id", "B1")
        kwargs.setdefault("channel_id", "C1")
        return self.service.record_event(**kwargs)

    def receive_stock(self, request_id, quantity, batch="B1", channel="C1",
                      location="main", date="2026-08-05", ref=None, final=True):
        ref = ref or f"SH-{request_id}"
        self.event(request_id=f"out-{request_id}", kind="outbound", quantity=quantity,
                   business_date=date, ref_no=ref, batch_id=batch, channel_id=channel)
        return self.event(request_id=f"rcv-{request_id}", kind="receipt", quantity=quantity,
                          business_date=date, ref_no=ref, to_location_id=location,
                          batch_id=batch, channel_id=channel, final=final)

    def bucket_of(self, channel, product, batch):
        view = self.service.inventory_view(channel_id=channel, product_id=product, batch_id=batch)
        self.assertEqual(1, len(view["items"]))
        return view["items"][0]


class InventoryFlowTest(SellThroughTestBase):
    def test_outbound_then_receipt_moves_in_transit_to_sellable(self):
        self.event(request_id="o1", kind="outbound", quantity=100,
                   business_date="2026-08-05", ref_no="SH-1")
        view = self.bucket_of("C1", "P1", "B1")
        self.assertEqual(100, view["in_transit"])
        self.assertEqual(0, view["sellable"])
        self.assertEqual(100, view["owner_breakdown"]["enterprise"])
        self.event(request_id="r1", kind="receipt", quantity=60,
                   business_date="2026-08-06", ref_no="SH-1", to_location_id="main")
        view = self.bucket_of("C1", "P1", "B1")
        self.assertEqual(60, view["sellable"])
        self.assertEqual(40, view["in_transit"])
        self.assertEqual(60, view["owner_breakdown"]["channel"])

    def test_final_receipt_writes_off_shortage_as_variance(self):
        self.event(request_id="o1", kind="outbound", quantity=100,
                   business_date="2026-08-05", ref_no="SH-1")
        self.event(request_id="r1", kind="receipt", quantity=90, final=True,
                   business_date="2026-08-06", ref_no="SH-1")
        view = self.bucket_of("C1", "P1", "B1")
        self.assertEqual(90, view["sellable"])
        self.assertEqual(0, view["in_transit"])
        variances = self.service.list_variances(channel_id="C1")["items"]
        self.assertEqual(1, len(variances))
        self.assertEqual("in_transit_shortage", variances[0]["variance_type"])
        self.assertEqual(-10, variances[0]["quantity"])

    def test_receipt_cannot_exceed_remaining_shipment(self):
        self.event(request_id="o1", kind="outbound", quantity=50,
                   business_date="2026-08-05", ref_no="SH-1")
        with self.assertRaises(ValidationError):
            self.event(request_id="r1", kind="receipt", quantity=51,
                       business_date="2026-08-06", ref_no="SH-1")

    def test_receipt_unknown_shipment_rejected(self):
        with self.assertRaises(ValidationError):
            self.event(request_id="r1", kind="receipt", quantity=1,
                       business_date="2026-08-06", ref_no="SH-X")

    def test_duplicate_shipment_ref_rejected(self):
        self.event(request_id="o1", kind="outbound", quantity=50,
                   business_date="2026-08-05", ref_no="SH-1")
        with self.assertRaises(ConflictError):
            self.event(request_id="o2", kind="outbound", quantity=50,
                       business_date="2026-08-06", ref_no="SH-1")

    def test_transfer_moves_stock_between_locations(self):
        self.receive_stock("s1", 100)
        self.event(request_id="t1", kind="transfer", phase="shipped", quantity=30,
                   business_date="2026-08-10", from_location_id="main",
                   to_location_id="east", ref_no="TR-1")
        view = self.bucket_of("C1", "P1", "B1")
        self.assertEqual(70, view["sellable"])
        self.assertEqual(30, view["in_transit"])
        self.assertEqual(100, view["owner_breakdown"]["channel"])
        self.event(request_id="t2", kind="transfer", phase="received", quantity=30,
                   business_date="2026-08-11", ref_no="TR-1")
        view = self.bucket_of("C1", "P1", "B1")
        self.assertEqual(100, view["sellable"])
        self.assertEqual(0, view["in_transit"])
        locations = {loc["location_id"]: loc for loc in view["locations"]}
        self.assertEqual(70, locations["main"]["sellable"])
        self.assertEqual(30, locations["east"]["sellable"])

    def test_transfer_final_writes_off_shortage(self):
        self.receive_stock("s1", 100)
        self.event(request_id="t1", kind="transfer", phase="shipped", quantity=30,
                   business_date="2026-08-10", from_location_id="main",
                   to_location_id="east", ref_no="TR-1")
        self.event(request_id="t2", kind="transfer", phase="received", quantity=25,
                   final=True, business_date="2026-08-11", ref_no="TR-1")
        variances = self.service.list_variances(channel_id="C1")["items"]
        self.assertEqual(["transfer_shortage"], [v["variance_type"] for v in variances])
        self.assertEqual(-5, variances[0]["quantity"])

    def test_return_goes_through_pending_bucket(self):
        self.receive_stock("s1", 100)
        self.event(request_id="rt1", kind="return", phase="shipped", quantity=15,
                   business_date="2026-08-12", from_location_id="main", ref_no="RT-1")
        view = self.bucket_of("C1", "P1", "B1")
        self.assertEqual(85, view["sellable"])
        self.assertEqual(15, view["pending_return"])
        self.event(request_id="rt2", kind="return", phase="received", quantity=15,
                   business_date="2026-08-13", ref_no="RT-1")
        view = self.bucket_of("C1", "P1", "B1")
        self.assertEqual(85, view["sellable"])
        self.assertEqual(0, view["pending_return"])
        self.assertEqual(85, view["total"])

    def test_sale_requires_sufficient_sellable(self):
        self.receive_stock("s1", 10)
        with self.assertRaises(ValidationError):
            self.event(request_id="sale", kind="sale", quantity=11,
                       business_date="2026-08-09", from_location_id="main")

    def test_stocktake_records_surplus_and_shortage(self):
        self.receive_stock("s1", 100)
        self.event(request_id="st1", kind="stocktake", quantity=105, actor_id="rv1",
                   business_date="2026-08-20", from_location_id="main")
        self.assertEqual(105, self.bucket_of("C1", "P1", "B1")["sellable"])
        self.event(request_id="st2", kind="stocktake", quantity=95, actor_id="rv1",
                   business_date="2026-08-21", from_location_id="main")
        self.assertEqual(95, self.bucket_of("C1", "P1", "B1")["sellable"])
        variances = self.service.list_variances(channel_id="C1")["items"]
        self.assertEqual([("stocktake_surplus", 5), ("stocktake_shortage", -10)],
                         [(v["variance_type"], v["quantity"]) for v in variances])

    def test_future_business_date_rejected(self):
        with self.assertRaises(ValidationError):
            self.event(request_id="future", kind="outbound", quantity=1,
                       business_date="2026-12-01", ref_no="SH-F")

    def test_request_id_replays_and_conflicts(self):
        first = self.event(request_id="o1", kind="outbound", quantity=50,
                           business_date="2026-08-05", ref_no="SH-1")
        second = self.event(request_id="o1", kind="outbound", quantity=50,
                            business_date="2026-08-05", ref_no="SH-1")
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertEqual(first.event_id, second.event_id)
        with self.assertRaises(ConflictError):
            self.event(request_id="o1", kind="outbound", quantity=51,
                       business_date="2026-08-05", ref_no="SH-1")

    def test_permissions(self):
        with self.assertRaises(PermissionDenied):
            self.event(request_id="au-sale", actor_id="au1", kind="sale", quantity=1,
                       business_date="2026-08-09")
        with self.assertRaises(PermissionDenied):
            self.event(request_id="rv-sale", actor_id="rv1", kind="sale", quantity=1,
                       business_date="2026-08-09")
        with self.assertRaises(PermissionDenied):
            self.event(request_id="cross", actor_id="op2", kind="outbound", quantity=1,
                       business_date="2026-08-05", ref_no="SH-X")
        self.receive_stock("s1", 10)
        outcome = self.event(request_id="rv-st", actor_id="rv1", kind="stocktake",
                             quantity=10, business_date="2026-08-09", from_location_id="main")
        self.assertFalse(outcome.replayed)


class PeriodCloseTest(SellThroughTestBase):
    def test_close_snapshots_and_verifies(self):
        self.receive_stock("s1", 100, date="2026-08-05")
        self.event(request_id="sale1", kind="sale", quantity=40,
                   business_date="2026-08-15", from_location_id="main")
        outcome = self.service.close_period(request_id="close-aug", actor_id="rv1",
                                            channel_id="C1", period="2026-08")
        self.assertFalse(outcome.replayed)
        snapshot = self.service.period_snapshot(channel_id="C1", period="2026-08")
        self.assertTrue(snapshot["verified"])
        sellable = [r for r in snapshot["rows"] if r["bucket"] == "sellable"]
        self.assertEqual(1, len(sellable))
        self.assertEqual(0, sellable[0]["opening_qty"])
        self.assertEqual(100, sellable[0]["in_qty"])
        self.assertEqual(40, sellable[0]["out_qty"])
        self.assertEqual(60, sellable[0]["closing_qty"])

    def test_late_voucher_appends_to_next_open_period(self):
        self.receive_stock("s1", 100, date="2026-08-05")
        closed = self.service.close_period(request_id="close-aug", actor_id="rv1",
                                           channel_id="C1", period="2026-08")
        late = self.event(request_id="late-sale", kind="sale", quantity=5,
                          business_date="2026-08-30", from_location_id="main")
        self.assertTrue(late.is_adjustment)
        self.assertEqual("2026-08", late.original_period)
        self.assertEqual("2026-09", late.period)
        self.assertIn("2026-09", late.warnings[0])
        snapshot = self.service.period_snapshot(channel_id="C1", period="2026-08")
        self.assertEqual(closed.snapshot_hash, snapshot["snapshot_hash"])
        self.assertTrue(snapshot["verified"])
        metrics_aug = self.service.sell_through_metrics(channel_id="C1", period="2026-08")
        metrics_sep = self.service.sell_through_metrics(channel_id="C1", period="2026-09")
        self.assertEqual(0, metrics_aug["sell_out"])
        self.assertEqual(5, metrics_sep["sell_out"])
        self.assertEqual(1, metrics_sep["adjustment_count"])

    def test_close_requires_earlier_periods_closed(self):
        self.receive_stock("s1", 10, date="2026-08-05")
        self.receive_stock("s2", 10, date="2026-09-05")
        with self.assertRaises(ConflictError):
            self.service.close_period(request_id="close-sep", actor_id="rv1",
                                      channel_id="C1", period="2026-09")
        self.service.close_period(request_id="close-aug", actor_id="rv1",
                                  channel_id="C1", period="2026-08")
        outcome = self.service.close_period(request_id="close-sep2", actor_id="rv1",
                                            channel_id="C1", period="2026-09")
        self.assertFalse(outcome.replayed)

    def test_close_replays_and_rejects_second_close(self):
        self.receive_stock("s1", 10, date="2026-08-05")
        first = self.service.close_period(request_id="close-aug", actor_id="rv1",
                                          channel_id="C1", period="2026-08")
        replay = self.service.close_period(request_id="close-aug", actor_id="rv1",
                                           channel_id="C1", period="2026-08")
        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(first.snapshot_hash, replay.snapshot_hash)
        with self.assertRaises(ConflictError):
            self.service.close_period(request_id="close-aug-again", actor_id="rv1",
                                      channel_id="C1", period="2026-08")

    def test_snapshot_of_unknown_period_is_404(self):
        with self.assertRaises(NotFoundError):
            self.service.period_snapshot(channel_id="C1", period="2026-08")

    def test_late_receipt_against_open_shipment_keeps_closed_snapshot(self):
        self.event(request_id="o1", kind="outbound", quantity=100,
                   business_date="2026-08-05", ref_no="SH-1")
        closed = self.service.close_period(request_id="close-aug", actor_id="rv1",
                                           channel_id="C1", period="2026-08")
        late = self.event(request_id="r1", kind="receipt", quantity=95, final=True,
                          business_date="2026-08-30", ref_no="SH-1")
        self.assertTrue(late.is_adjustment)
        self.assertEqual("2026-09", late.period)
        view = self.bucket_of("C1", "P1", "B1")
        self.assertEqual(95, view["sellable"])
        self.assertEqual(0, view["in_transit"])
        variances = self.service.list_variances(channel_id="C1", period="2026-09")["items"]
        self.assertEqual("in_transit_shortage", variances[0]["variance_type"])
        self.assertEqual(-5, variances[0]["quantity"])
        # 8 月快照保持原记录：在途 100 仍在账上，复算哈希不变
        snapshot = self.service.period_snapshot(channel_id="C1", period="2026-08")
        self.assertEqual(closed.snapshot_hash, snapshot["snapshot_hash"])
        in_transit = [r for r in snapshot["rows"] if r["bucket"] == "in_transit"]
        self.assertEqual(100, in_transit[0]["closing_qty"])
        self.assertTrue(snapshot["verified"])


class DisputeTest(SellThroughTestBase):
    def test_dispute_freezes_only_target_batch(self):
        self.receive_stock("s1", 100, batch="B1", date="2026-08-05")
        self.receive_stock("s2", 50, batch="B2", date="2026-08-06")
        dispute = self.service.open_dispute(request_id="d1", actor_id="rv1", channel_id="C1",
                                            product_id="P1", batch_id="B1", reason="扫码不符")
        self.assertEqual(100, dispute.frozen_quantity)
        view = self.bucket_of("C1", "P1", "B1")
        self.assertEqual(0, view["sellable"])
        self.assertEqual(100, view["disputed"])
        with self.assertRaises(ConflictError):
            self.event(request_id="blocked", kind="sale", quantity=1,
                       business_date="2026-08-10", from_location_id="main")
        moved = self.event(request_id="allowed", kind="sale", quantity=5, batch_id="B2",
                           business_date="2026-08-10", from_location_id="main")
        self.assertFalse(moved.replayed)
        resolved = self.service.resolve_dispute(request_id="d1-resolve", actor_id="rv1",
                                                dispute_id=dispute.dispute_id,
                                                resolution="已核实")
        self.assertEqual("resolved", resolved.status)
        self.assertEqual(100, self.bucket_of("C1", "P1", "B1")["sellable"])

    def test_dispute_does_not_block_period_close_anywhere(self):
        self.receive_stock("s1", 100, date="2026-08-05")
        self.receive_stock("s2", 20, channel="C2", date="2026-08-05")
        self.service.open_dispute(request_id="d1", actor_id="rv1", channel_id="C1",
                                  product_id="P1", batch_id="B1", reason="待核实")
        own = self.service.close_period(request_id="close-c1", actor_id="rv1",
                                        channel_id="C1", period="2026-08")
        other = self.service.close_period(request_id="close-c2", actor_id="rv1",
                                          channel_id="C2", period="2026-08")
        self.assertFalse(own.replayed)
        self.assertFalse(other.replayed)
        # 冻结事件过账到其发生期间（2026-10），该期间快照单独列示争议库存
        self.service.close_period(request_id="close-c1-sep", actor_id="rv1",
                                  channel_id="C1", period="2026-09")
        self.service.close_period(request_id="close-c1-oct", actor_id="rv1",
                                  channel_id="C1", period="2026-10")
        snapshot = self.service.period_snapshot(channel_id="C1", period="2026-10")
        self.assertTrue(snapshot["verified"])
        disputed = [r for r in snapshot["rows"] if r["bucket"] == "disputed"]
        self.assertEqual(100, disputed[0]["closing_qty"])

    def test_duplicate_open_dispute_rejected(self):
        self.receive_stock("s1", 10, date="2026-08-05")
        self.service.open_dispute(request_id="d1", actor_id="rv1", channel_id="C1",
                                  product_id="P1", batch_id="B1", reason="一")
        with self.assertRaises(ConflictError):
            self.service.open_dispute(request_id="d2", actor_id="rv1", channel_id="C1",
                                      product_id="P1", batch_id="B1", reason="二")

    def test_operator_cannot_open_dispute(self):
        with self.assertRaises(PermissionDenied):
            self.service.open_dispute(request_id="d1", actor_id="op1", channel_id="C1",
                                      product_id="P1", batch_id="B1", reason="越权")


class BackfillTest(SellThroughTestBase):
    def setUp(self):
        super().setUp()
        self.receive_stock("s1", 500, date="2026-08-05")

    def sale_item(self, seq, quantity, request_id=None):
        item = {"source_seq": seq, "kind": "sale", "product_id": "P1", "batch_id": "B1",
                "channel_id": "C1", "quantity": quantity, "business_date": "2026-08-20",
                "from_location_id": "main"}
        if request_id:
            item["request_id"] = request_id
        return item

    def test_replay_returns_original_event(self):
        first = self.service.backfill_events(actor_id="op1", source_id="POS-1",
                                             items=[self.sale_item(1, 5)])
        second = self.service.backfill_events(actor_id="op1", source_id="POS-1",
                                              items=[self.sale_item(1, 5)])
        self.assertEqual("accepted", first["items"][0]["status"])
        self.assertEqual("replayed", second["items"][0]["status"])
        self.assertEqual(first["items"][0]["event_id"], second["items"][0]["event_id"])
        events = self.service.list_events(channel_id="C1", kind="sale")["items"]
        self.assertEqual(1, len(events))

    def test_fork_is_rejected_and_logged(self):
        self.service.backfill_events(actor_id="op1", source_id="POS-1",
                                     items=[self.sale_item(1, 5)])
        result = self.service.backfill_events(actor_id="op1", source_id="POS-1",
                                              items=[self.sale_item(1, 9)])
        self.assertEqual("fork", result["items"][0]["status"])
        anomalies = self.service.list_source_anomalies(source_id="POS-1")["items"]
        self.assertEqual(["fork"], [a["kind"] for a in anomalies])
        events = self.service.list_events(channel_id="C1", kind="sale")["items"]
        self.assertEqual(1, len(events))
        self.assertEqual(5, events[0]["quantity"])

    def test_gap_is_accepted_with_warning_and_logged(self):
        self.service.backfill_events(actor_id="op1", source_id="POS-1",
                                     items=[self.sale_item(1, 5)])
        result = self.service.backfill_events(actor_id="op1", source_id="POS-1",
                                              items=[self.sale_item(4, 2)])
        item = result["items"][0]
        self.assertEqual("accepted", item["status"])
        self.assertIn("sequence_gap", item["warnings"][0])
        anomalies = self.service.list_source_anomalies(source_id="POS-1")["items"]
        self.assertEqual(["gap"], [a["kind"] for a in anomalies])

    def test_late_fill_is_logged(self):
        self.service.backfill_events(actor_id="op1", source_id="POS-1",
                                     items=[self.sale_item(1, 5), self.sale_item(4, 2)])
        result = self.service.backfill_events(actor_id="op1", source_id="POS-1",
                                              items=[self.sale_item(2, 1)])
        self.assertEqual("accepted", result["items"][0]["status"])
        kinds = [a["kind"] for a in self.service.list_source_anomalies(source_id="POS-1")["items"]]
        self.assertEqual(["gap", "late_fill"], kinds)

    def test_invalid_item_does_not_block_others(self):
        result = self.service.backfill_events(
            actor_id="op1", source_id="POS-1",
            items=[self.sale_item(1, 5),
                     {"source_seq": 2, "kind": "sale", "product_id": "P1", "batch_id": "B1",
                      "channel_id": "C1", "quantity": 99999, "business_date": "2026-08-20",
                      "from_location_id": "main"},
                     self.sale_item(3, 1)])
        self.assertEqual(["accepted", "error", "accepted"],
                         [item["status"] for item in result["items"]])
        self.assertEqual({"accepted": 2, "replayed": 0, "fork": 0, "error": 1},
                         result["summary"])


class MetricsTest(SellThroughTestBase):
    def test_metrics_are_recomputable(self):
        self.receive_stock("s1", 120, date="2026-09-01")
        self.event(request_id="sale1", kind="sale", quantity=60,
                   business_date="2026-09-15", from_location_id="main")
        metrics = self.service.sell_through_metrics(channel_id="C1", period="2026-09")
        self.assertEqual(120, metrics["sell_in"])
        self.assertEqual(60, metrics["sell_out"])
        self.assertEqual(60, metrics["closing_sellable"])
        doi = metrics["days_of_inventory"]
        self.assertEqual(30, doi["window_days"])
        self.assertEqual("2026-09-01", doi["window_start"])
        self.assertEqual("2026-09-30", doi["window_end"])
        self.assertEqual(60, doi["sell_out_in_window"])
        self.assertEqual(2.0, doi["avg_daily_sell_out"])
        self.assertEqual(30.0, doi["days"])
        self.assertFalse(metrics["closed"])
        self.assertIsNone(metrics["verified"])

    def test_days_of_inventory_is_none_without_sales(self):
        self.receive_stock("s1", 50, date="2026-09-01")
        metrics = self.service.sell_through_metrics(channel_id="C1", period="2026-09")
        self.assertIsNone(metrics["days_of_inventory"]["days"])

    def test_metrics_after_close_are_verified(self):
        self.receive_stock("s1", 100, date="2026-08-05")
        self.event(request_id="sale1", kind="sale", quantity=25,
                   business_date="2026-08-15", from_location_id="main")
        self.service.close_period(request_id="close-aug", actor_id="rv1",
                                  channel_id="C1", period="2026-08")
        metrics = self.service.sell_through_metrics(channel_id="C1", period="2026-08")
        self.assertTrue(metrics["closed"])
        self.assertTrue(metrics["verified"])
        self.assertEqual(100, metrics["sell_in"])
        self.assertEqual(25, metrics["sell_out"])
        self.assertEqual(75, metrics["closing_sellable"])

    def test_net_sell_in_accounts_for_received_returns(self):
        self.receive_stock("s1", 100, date="2026-08-05")
        self.event(request_id="rt1", kind="return", phase="shipped", quantity=10,
                   business_date="2026-08-10", from_location_id="main", ref_no="RT-1")
        self.event(request_id="rt2", kind="return", phase="received", quantity=10,
                   business_date="2026-08-12", ref_no="RT-1")
        metrics = self.service.sell_through_metrics(channel_id="C1", period="2026-08")
        self.assertEqual(100, metrics["sell_in"])
        self.assertEqual(10, metrics["returns_received"])
        self.assertEqual(90, metrics["net_sell_in"])


class ProjectionTest(unittest.TestCase):
    def test_snapshot_rows_balance(self):
        events = [
            {"seq": 1, "kind": "outbound", "phase": None, "channel_id": "C1",
             "product_id": "P1", "batch_id": "B1", "from_location_id": None,
             "to_location_id": None, "ref_no": "SH-1", "quantity": 10, "final": 0,
             "period": "2026-08"},
            {"seq": 2, "kind": "receipt", "phase": None, "channel_id": "C1",
             "product_id": "P1", "batch_id": "B1", "from_location_id": None,
             "to_location_id": "main", "ref_no": "SH-1", "quantity": 10, "final": 0,
             "period": "2026-08"},
            {"seq": 3, "kind": "sale", "phase": None, "channel_id": "C1",
             "product_id": "P1", "batch_id": "B1", "from_location_id": "main",
             "to_location_id": None, "ref_no": None, "quantity": 4, "final": 0,
             "period": "2026-08"},
        ]
        rebuild = proj.rebuild_period(events, "2026-08")
        rows = proj.snapshot_rows("C1", rebuild)
        by_bucket = {(r["bucket"], r["owner"]): r for r in rows}
        self.assertEqual(6, by_bucket[("sellable", "channel")]["closing_qty"])
        self.assertEqual(0, by_bucket[("in_transit", "enterprise")]["closing_qty"])
        for row in rows:
            self.assertEqual(row["opening_qty"] + row["in_qty"] - row["out_qty"],
                             row["closing_qty"])


if __name__ == "__main__":
    unittest.main()
