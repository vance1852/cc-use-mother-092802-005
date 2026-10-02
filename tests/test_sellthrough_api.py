import unittest
from datetime import datetime, timezone

from beverage_ops_foundation.clock import FixedClock
from beverage_ops_foundation.service import DomainService
from beverage_ops_foundation.storage import Database
from channel_sellthrough.api import route
from channel_sellthrough.service import SellThroughService


class SellThroughApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        clock = FixedClock(datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc))
        self.foundation = DomainService(self.database, clock)
        self.service = SellThroughService(self.database, clock)
        self.foundation.register_organization(request_id="org", actor_id="bootstrap",
                                              organization_id="o1", name="酒业集团")
        self.foundation.register_actor(request_id="admin", actor_id="bootstrap",
                                       new_actor_id="a1", display_name="管理员",
                                       role="admin", organization_id="o1")
        self.foundation.register_actor(request_id="op", actor_id="a1",
                                       new_actor_id="op1", display_name="运营",
                                       role="operator", organization_id="o1")
        self.foundation.register_actor(request_id="rv", actor_id="a1",
                                       new_actor_id="rv1", display_name="复核",
                                       role="reviewer", organization_id="o1")

    def tearDown(self):
        self.database.close()

    def call(self, method, path, body=None, actor="op1"):
        return route(self.service, self.foundation, method, path, body,
                     {"X-Actor-Id": actor})

    def seed_catalog(self):
        status, _ = self.call("POST", "/channels", {
            "request_id": "ch1", "channel_id": "C1", "organization_id": "o1",
            "name": "华东经销商", "channel_type": "distributor"})
        self.assertEqual(201, status)
        self.call("POST", "/products", {
            "request_id": "p1", "product_id": "P1", "name": "经典白酒", "category": "baijiu"})
        self.call("POST", "/batches", {
            "request_id": "b1", "batch_id": "B1", "product_id": "P1",
            "batch_no": "20260701", "produced_on": "2026-07-01"})
        self.call("POST", "/inventory-events", {
            "request_id": "o1", "kind": "outbound", "product_id": "P1", "batch_id": "B1",
            "channel_id": "C1", "quantity": 100, "business_date": "2026-08-05", "ref_no": "SH-1"})
        self.call("POST", "/inventory-events", {
            "request_id": "r1", "kind": "receipt", "product_id": "P1", "batch_id": "B1",
            "channel_id": "C1", "quantity": 100, "business_date": "2026-08-06", "ref_no": "SH-1"})

    def test_full_flow_over_http(self):
        self.seed_catalog()
        # GET 库存视图
        status, payload = self.call("GET", "/inventory?channel_id=C1", actor="")
        self.assertEqual(200, status)
        self.assertEqual(100, payload["items"][0]["sellable"])
        # 售出 40 → 指标可复算
        status, _ = self.call("POST", "/inventory-events", {
            "request_id": "s1", "kind": "sale", "product_id": "P1", "batch_id": "B1",
            "channel_id": "C1", "quantity": 40, "business_date": "2026-08-15",
            "from_location_id": "main"})
        self.assertEqual(201, status)
        status, payload = self.call("GET", "/metrics/sell-through?channel_id=C1&period=2026-08")
        self.assertEqual(200, status)
        self.assertEqual(100, payload["sell_in"])
        self.assertEqual(40, payload["sell_out"])
        # 关账 → 快照可复算校验
        status, payload = self.call("POST", "/periods/close", {
            "request_id": "close-aug", "channel_id": "C1", "period": "2026-08"}, actor="rv1")
        self.assertEqual(201, status)
        snapshot_hash = payload["snapshot_hash"]
        status, payload = self.call("GET", "/periods/snapshot?channel_id=C1&period=2026-08")
        self.assertEqual(200, status)
        self.assertTrue(payload["verified"])
        self.assertEqual(snapshot_hash, payload["snapshot_hash"])
        # 迟到凭证追加到后续期间
        status, payload = self.call("POST", "/inventory-events", {
            "request_id": "late", "kind": "sale", "product_id": "P1", "batch_id": "B1",
            "channel_id": "C1", "quantity": 2, "business_date": "2026-08-30"})
        self.assertEqual(201, status)
        self.assertTrue(payload["is_adjustment"])
        self.assertEqual("2026-09", payload["period"])

    def test_event_replay_returns_200(self):
        self.seed_catalog()
        body = {"request_id": "s1", "kind": "sale", "product_id": "P1", "batch_id": "B1",
                "channel_id": "C1", "quantity": 5, "business_date": "2026-08-15"}
        first_status, _ = self.call("POST", "/inventory-events", body)
        replay_status, replay = self.call("POST", "/inventory-events", body)
        self.assertEqual(201, first_status)
        self.assertEqual(200, replay_status)
        self.assertTrue(replay["replayed"])

    def test_backfill_over_http(self):
        self.seed_catalog()
        status, payload = self.call("POST", "/backfill", {
            "source_id": "POS-1", "items": [
                {"source_seq": 1, "kind": "sale", "product_id": "P1", "batch_id": "B1",
                 "channel_id": "C1", "quantity": 3, "business_date": "2026-08-20"},
                {"source_seq": 1, "kind": "sale", "product_id": "P1", "batch_id": "B1",
                 "channel_id": "C1", "quantity": 8, "business_date": "2026-08-20"},
            ]})
        self.assertEqual(200, status)
        self.assertEqual(["accepted", "fork"], [i["status"] for i in payload["items"]])
        status, payload = self.call("GET", "/source-anomalies?source_id=POS-1")
        self.assertEqual(200, status)
        self.assertEqual("fork", payload["items"][0]["kind"])

    def test_dispute_flow_over_http(self):
        self.seed_catalog()
        status, payload = self.call("POST", "/disputes", {
            "request_id": "d1", "channel_id": "C1", "product_id": "P1", "batch_id": "B1",
            "reason": "待核实"}, actor="rv1")
        self.assertEqual(201, status)
        dispute_id = payload["dispute_id"]
        status, payload = self.call("POST", "/inventory-events", {
            "request_id": "blocked", "kind": "sale", "product_id": "P1", "batch_id": "B1",
            "channel_id": "C1", "quantity": 1, "business_date": "2026-08-15"})
        self.assertEqual(409, status)
        status, payload = self.call("POST", "/disputes/resolve", {
            "request_id": "d1-r", "dispute_id": dispute_id, "resolution": "已核实"}, actor="rv1")
        self.assertEqual(200, status)
        status, payload = self.call("GET", "/disputes?channel_id=C1&status=resolved")
        self.assertEqual(1, len(payload["items"]))

    def test_missing_channel_param_is_400(self):
        status, payload = self.call("GET", "/inventory")
        self.assertEqual(400, status)
        self.assertEqual("validation_error", payload["error"])

    def test_invalid_event_kind_is_400(self):
        self.seed_catalog()
        status, payload = self.call("POST", "/inventory-events", {
            "request_id": "bad", "kind": "teleport", "product_id": "P1", "batch_id": "B1",
            "channel_id": "C1", "quantity": 1, "business_date": "2026-08-15"})
        self.assertEqual(400, status)
        self.assertEqual("validation_error", payload["error"])

    def test_unknown_channel_is_404(self):
        status, payload = self.call("GET", "/inventory?channel_id=NOPE")
        self.assertEqual(404, status)

    def test_foundation_routes_still_work(self):
        status, payload = self.call("GET", "/health", actor="")
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])
        status, payload = self.call("GET", "/missing", actor="")
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])

    def test_variances_endpoint(self):
        self.seed_catalog()
        self.call("POST", "/inventory-events", {
            "request_id": "st1", "kind": "stocktake", "product_id": "P1", "batch_id": "B1",
            "channel_id": "C1", "quantity": 97, "business_date": "2026-08-31"}, actor="rv1")
        status, payload = self.call("GET", "/variances?channel_id=C1&period=2026-08")
        self.assertEqual(200, status)
        self.assertEqual(1, len(payload["items"]))
        self.assertEqual("stocktake_shortage", payload["items"][0]["variance_type"])
        self.assertEqual(-3, payload["items"][0]["quantity"])


if __name__ == "__main__":
    unittest.main()
