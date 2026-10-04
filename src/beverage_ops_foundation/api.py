"""提供不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import DomainError, ValidationError
from .channel_models import ReconciliationReport
from .channel_service import ChannelReconciliationService
from .service import DomainService
from .storage import Database


def _channel(service: DomainService) -> ChannelReconciliationService:
    return ChannelReconciliationService(service.database, service.clock)


def _report_to_dict(report: ReconciliationReport) -> dict[str, Any]:
    data = report.__dict__.copy()
    data["discrepancies"] = [item.__dict__ for item in report.discrepancies]
    return data


def route(service: DomainService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    actor_id = headers.get("X-Actor-Id", "")
    query = parse_qs(parsed.query)

    def q(name: str, default: str | None = None) -> str | None:
        return query.get(name, [default])[0]

    try:
        if method == "GET" and parsed.path == "/health":
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}
        if method == "POST" and parsed.path == "/organizations":
            receipt = service.register_organization(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/actors":
            receipt = service.register_actor(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/sites":
            receipt = service.register_site(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/domain-records":
            receipt = service.record_domain_data(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and parsed.path == "/domain-records":
            site_id = q("site_id", "")
            if not site_id:
                raise ValidationError("site_id 不能为空")
            return 200, {"items": [item.__dict__ for item in service.list_domain_data(site_id, q("category"))]}
        if method == "GET" and parsed.path == "/audit-events":
            after = int(q("after_sequence", "0"))
            return 200, {"items": service.audit_events(after)}

        # -- 渠道动销对账 -------------------------------------------------
        channels = _channel(service)
        if method == "POST" and parsed.path == "/channel/products":
            channels.register_product(actor_id=actor_id, **body)
            return 201, {"status": "registered"}
        if method == "POST" and parsed.path == "/channel/channels":
            channels.register_channel(actor_id=actor_id, **body)
            return 201, {"status": "registered"}
        if method == "POST" and parsed.path == "/channel/events":
            receipt = channels.record_event(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/channel/disputes/open":
            result = channels.open_dispute(actor_id=actor_id, **body)
            return 201, result
        if method == "POST" and parsed.path == "/channel/disputes/resolve":
            result = channels.resolve_dispute(actor_id=actor_id, **body)
            return 200, result
        if method == "POST" and parsed.path == "/channel/periods/close":
            result = channels.close_period(actor_id=actor_id, **body)
            return 200, result
        if method == "GET" and parsed.path == "/channel/events":
            channel_id = q("channel_id", "")
            if not channel_id:
                raise ValidationError("channel_id 不能为空")
            items = channels.list_events(channel_id=channel_id, period_id=q("period_id"),
                                         product_id=q("product_id"), batch_no=q("batch_no"))
            return 200, {"items": [item.__dict__ for item in items]}
        if method == "GET" and parsed.path == "/channel/inventory":
            channel_id = q("channel_id", "")
            if not channel_id:
                raise ValidationError("channel_id 不能为空")
            rows = channels.inventory(channel_id=channel_id, period_id=q("period_id"),
                                      product_id=q("product_id"), batch_no=q("batch_no"))
            return 200, {"items": [row.__dict__ for row in rows]}
        if method == "GET" and parsed.path == "/channel/reconciliation":
            channel_id = q("channel_id", "")
            period_id = q("period_id", "")
            if not channel_id or not period_id:
                raise ValidationError("channel_id 与 period_id 不能为空")
            report = channels.reconciliation_report(
                period_id=period_id, channel_id=channel_id,
                product_id=q("product_id"), batch_no=q("batch_no"))
            return 200, _report_to_dict(report)
        if method == "GET" and parsed.path == "/channel/anomalies":
            return 200, {"items": channels.list_anomalies(q("stream_key"))}
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: DomainService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")})
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动本地 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动技能赛训协作基础服务")
    parser.add_argument("--database", default="service.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.service = DomainService(database)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
