"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_records (
    record_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    category TEXT NOT NULL,
    external_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, category, external_key)
);
CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);
-- 渠道动销对账台账 ------------------------------------------------------
CREATE TABLE IF NOT EXISTS channel_products (
    product_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS channel_channels (
    channel_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inventory_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    product_id TEXT NOT NULL,
    batch_no TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK(quantity >= 0),
    variance_qty INTEGER,
    source_type TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    business_date TEXT NOT NULL,
    period_id TEXT NOT NULL,
    origin_period TEXT NOT NULL,
    is_adjustment INTEGER NOT NULL CHECK(is_adjustment IN (0, 1)),
    unit_cost TEXT,
    stream_key TEXT,
    stream_seq INTEGER,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    UNIQUE(source_type, source_ref)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_events_stream
    ON inventory_events(stream_key, stream_seq) WHERE stream_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_events_rebuild
    ON inventory_events(channel_id, period_id, product_id, batch_no);
CREATE TABLE IF NOT EXISTS stream_anomalies (
    anomaly_id TEXT PRIMARY KEY,
    stream_key TEXT NOT NULL,
    stream_seq INTEGER NOT NULL,
    request_id TEXT NOT NULL,
    existing_event_id TEXT NOT NULL,
    existing_payload_hash TEXT NOT NULL,
    incoming_payload_hash TEXT NOT NULL,
    detected_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS disputes (
    dispute_id TEXT PRIMARY KEY,
    product_id TEXT NOT NULL,
    batch_no TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    status TEXT NOT NULL CHECK(status IN ('open', 'resolved')),
    source_type TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    opened_event_id TEXT NOT NULL,
    resolved_event_id TEXT,
    resolution TEXT,
    opened_at TEXT NOT NULL,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_disputes_freeze
    ON disputes(channel_id, status, product_id, batch_no);
CREATE TABLE IF NOT EXISTS period_closings (
    period_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    closed_at TEXT NOT NULL,
    closed_by TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL,
    PRIMARY KEY(period_id, channel_id)
);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。"""

        self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()
