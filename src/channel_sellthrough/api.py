"""为渠道动销对账服务提供不依赖第三方框架的 HTTP/JSON 边界。

路由优先匹配对账服务端点，未命中的请求回退到基础服务路由，
因此一个进程即可同时提供基础能力与对账能力。
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from beverage_ops_foundation import api as foundation_api
from beverage_ops_foundation.errors import DomainError, ValidationError
from beverage_ops_foundation.service import DomainService
from beverage_ops_foundation.storage import Database

from .service import SellThroughService


def _query(parsed, name: str, default: str | None = None) -> str | None:
    values = parse_qs(parsed.query).get(name)
    return values[0] if values else default


def _require_query(parsed, name: str) -> str:
    value = _query(parsed, name)
    if not value:
        raise ValidationError(f"{name} 不能为空")
    return value


def route(service: SellThroughService, foundation: DomainService,
          method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到对账服务或基础服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    actor_id = headers.get("X-Actor-Id", "")
    try:
        if method == "POST" and parsed.path == "/products":
            return _replied(service.register_product(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/batches":
            return _replied(service.register_batch(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/channels":
            return _replied(service.register_channel(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/locations":
            return _replied(service.register_location(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/inventory-events":
            outcome = service.record_event(actor_id=actor_id, **body)
            return (200 if outcome.replayed else 201), outcome.as_dict()
        if method == "POST" and parsed.path == "/backfill":
            return 200, service.backfill_events(actor_id=actor_id, **body)
        if method == "POST" and parsed.path == "/disputes":
            outcome = service.open_dispute(actor_id=actor_id, **body)
            return (200 if outcome.replayed else 201), outcome.as_dict()
        if method == "POST" and parsed.path == "/disputes/resolve":
            outcome = service.resolve_dispute(actor_id=actor_id, **body)
            return 200, outcome.as_dict()
        if method == "POST" and parsed.path == "/periods/close":
            outcome = service.close_period(actor_id=actor_id, **body)
            return (200 if outcome.replayed else 201), outcome.as_dict()
        if method == "GET" and parsed.path == "/inventory":
            return 200, service.inventory_view(
                channel_id=_require_query(parsed, "channel_id"),
                product_id=_query(parsed, "product_id"),
                batch_id=_query(parsed, "batch_id"))
        if method == "GET" and parsed.path == "/metrics/sell-through":
            window = _query(parsed, "window_days")
            return 200, service.sell_through_metrics(
                channel_id=_require_query(parsed, "channel_id"),
                period=_query(parsed, "period"),
                window_days=int(window) if window else 30)
        if method == "GET" and parsed.path == "/periods":
            return 200, service.list_periods(channel_id=_require_query(parsed, "channel_id"))
        if method == "GET" and parsed.path == "/periods/snapshot":
            return 200, service.period_snapshot(
                channel_id=_require_query(parsed, "channel_id"),
                period=_require_query(parsed, "period"))
        if method == "GET" and parsed.path == "/variances":
            return 200, service.list_variances(
                channel_id=_require_query(parsed, "channel_id"),
                period=_query(parsed, "period"))
        if method == "GET" and parsed.path == "/inventory-events":
            return 200, service.list_events(
                channel_id=_require_query(parsed, "channel_id"),
                period=_query(parsed, "period"), kind=_query(parsed, "kind"),
                product_id=_query(parsed, "product_id"), batch_id=_query(parsed, "batch_id"))
        if method == "GET" and parsed.path == "/disputes":
            return 200, service.list_disputes(
                channel_id=_query(parsed, "channel_id"), status=_query(parsed, "status"))
        if method == "GET" and parsed.path == "/source-anomalies":
            return 200, service.list_source_anomalies(source_id=_query(parsed, "source_id"))
        return foundation_api.route(foundation, method, path, body, headers)
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError, KeyError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


def _replied(response: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    return (200 if response.get("replayed") else 201), response


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: SellThroughService
    foundation: DomainService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.foundation, self.command, self.path, body,
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
    """启动渠道动销对账 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动渠道动销对账服务")
    parser.add_argument("--database", default="sellthrough.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.foundation = DomainService(database)
    Handler.service = SellThroughService(database)
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
