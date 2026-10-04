import unittest
from datetime import datetime, timezone

from beverage_ops_foundation.api import route
from beverage_ops_foundation.clock import FixedClock
from beverage_ops_foundation.service import DomainService
from beverage_ops_foundation.storage import Database


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = DomainService(
            self.database, FixedClock(datetime(2026, 9, 20, tzinfo=timezone.utc)))
        self.service.register_organization(request_id="org", actor_id="bootstrap",
                                           organization_id="o1", name="经营主体")
        self.service.register_actor(request_id="adm", actor_id="bootstrap", new_actor_id="ad",
                                    display_name="管理员", role="admin", organization_id="o1")
        self.service.register_actor(request_id="op", actor_id="ad", new_actor_id="op",
                                    display_name="操作员", role="operator", organization_id="o1")
        self.service.register_actor(request_id="rv", actor_id="ad", new_actor_id="rv",
                                    display_name="复核员", role="reviewer", organization_id="o1")

    def tearDown(self):
        self.database.close()

    def _headers(self, actor="op"):
        return {"X-Actor-Id": actor}

    def test_health_is_available_without_actor(self):
        status, payload = route(self.service, "GET", "/health", None)
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])

    def test_unknown_route_returns_404(self):
        status, payload = route(self.service, "GET", "/missing", None)
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])

    def test_invalid_json_shape_returns_400(self):
        status, payload = route(self.service, "POST", "/organizations", {"request_id": "x"},
                                {"X-Actor-Id": "bootstrap"})
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", payload["error"])

    def test_channel_event_and_reconciliation_flow(self):
        route(self.service, "POST", "/channel/products",
              {"product_id": "P1", "name": "产品"}, self._headers("ad"))
        route(self.service, "POST", "/channel/channels",
              {"channel_id": "C1", "name": "渠道"}, self._headers("ad"))

        status, receipt = route(self.service, "POST", "/channel/events", {
            "request_id": "e1", "event_type": "ship_out", "product_id": "P1", "batch_no": "B1",
            "channel_id": "C1", "quantity": 20, "source_type": "enterprise_erp",
            "source_ref": "SO1", "business_date": "2026-09-02"}, self._headers())
        self.assertEqual(201, status)
        self.assertFalse(receipt["replayed"])

        status, receipt = route(self.service, "POST", "/channel/events", {
            "request_id": "e1", "event_type": "ship_out", "product_id": "P1", "batch_no": "B1",
            "channel_id": "C1", "quantity": 20, "source_type": "enterprise_erp",
            "source_ref": "SO1", "business_date": "2026-09-02"}, self._headers())
        self.assertEqual(200, status)
        self.assertTrue(receipt["replayed"])

        status, payload = route(
            self.service, "GET",
            "/channel/reconciliation?channel_id=C1&period_id=2026-09", None, self._headers())
        self.assertEqual(200, status)
        self.assertEqual(20, payload["in_transit_quantity"])
        self.assertEqual(0, payload["sell_in_quantity"])
        self.assertIn("inventory_days", payload)

    def test_sequence_fork_returns_409_and_is_quarantined(self):
        route(self.service, "POST", "/channel/products",
              {"product_id": "P1", "name": "产品"}, self._headers("ad"))
        route(self.service, "POST", "/channel/channels",
              {"channel_id": "C1", "name": "渠道"}, self._headers("ad"))
        route(self.service, "POST", "/channel/events", {
            "request_id": "f1", "event_type": "ship_out", "product_id": "P1", "batch_no": "B1",
            "channel_id": "C1", "quantity": 10, "source_type": "enterprise_erp",
            "source_ref": "SO1", "business_date": "2026-09-02",
            "stream_key": "k", "stream_seq": 1}, self._headers())
        status, payload = route(self.service, "POST", "/channel/events", {
            "request_id": "f2", "event_type": "ship_out", "product_id": "P1", "batch_no": "B1",
            "channel_id": "C1", "quantity": 99, "source_type": "enterprise_erp",
            "source_ref": "SO2", "business_date": "2026-09-03",
            "stream_key": "k", "stream_seq": 1}, self._headers())
        self.assertEqual(409, status)
        self.assertEqual("sequence_fork", payload["error"])
        status, anomalies = route(self.service, "GET", "/channel/anomalies?stream_key=k",
                                 None, self._headers())
        self.assertEqual(1, len(anomalies["items"]))

    def test_dispute_freeze_and_period_close_endpoints(self):
        route(self.service, "POST", "/channel/products",
              {"product_id": "P1", "name": "产品"}, self._headers("ad"))
        route(self.service, "POST", "/channel/channels",
              {"channel_id": "C1", "name": "渠道"}, self._headers("ad"))
        route(self.service, "POST", "/channel/events", {
            "request_id": "e1", "event_type": "receipt", "product_id": "P1", "batch_no": "B1",
            "channel_id": "C1", "quantity": 30, "source_type": "distributor_receipt",
            "source_ref": "RC1", "business_date": "2026-09-03"}, self._headers())
        status, dispute = route(self.service, "POST", "/channel/disputes/open", {
            "request_id": "d1", "product_id": "P1", "batch_no": "B1", "channel_id": "C1",
            "quantity": 5, "source_ref": "CASE1", "reason": "疑点"}, self._headers("rv"))
        self.assertEqual(201, status)
        status, payload = route(self.service, "GET",
                                "/channel/inventory?channel_id=C1&period_id=2026-09",
                                None, self._headers())
        self.assertEqual(200, status)
        states = {row["state"]: row["quantity"] for row in payload["items"]}
        self.assertEqual(5, states["disputed"])
        self.assertEqual(25, states["sellable"])
        # 未决争议阻断关账
        status, payload = route(self.service, "POST", "/channel/periods/close", {
            "request_id": "c1", "period_id": "2026-09", "channel_id": "C1"}, self._headers("rv"))
        self.assertEqual(409, status)
        route(self.service, "POST", "/channel/disputes/resolve", {
            "request_id": "dr1", "dispute_id": dispute["dispute_id"],
            "resolution": "release"}, self._headers("rv"))
        status, payload = route(self.service, "POST", "/channel/periods/close", {
            "request_id": "c2", "period_id": "2026-09", "channel_id": "C1"}, self._headers("rv"))
        self.assertEqual(200, status)
        self.assertEqual(64, len(payload["snapshot_hash"]))


if __name__ == "__main__":
    unittest.main()
