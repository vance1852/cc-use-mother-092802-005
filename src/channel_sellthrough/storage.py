"""为渠道动销对账服务在共享 SQLite 数据库中补充业务表。"""

from __future__ import annotations


SCHEMA = """
CREATE TABLE IF NOT EXISTS st_products (
    product_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS st_batches (
    batch_id TEXT PRIMARY KEY,
    product_id TEXT NOT NULL REFERENCES st_products(product_id),
    batch_no TEXT NOT NULL,
    produced_on TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(product_id, batch_no)
);
CREATE TABLE IF NOT EXISTS st_channels (
    channel_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    channel_type TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS st_locations (
    channel_id TEXT NOT NULL REFERENCES st_channels(channel_id),
    location_id TEXT NOT NULL,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(channel_id, location_id)
);
CREATE TABLE IF NOT EXISTS st_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    request_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    phase TEXT,
    product_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    from_location_id TEXT,
    to_location_id TEXT,
    ref_no TEXT,
    quantity INTEGER NOT NULL,
    final INTEGER NOT NULL CHECK(final IN (0, 1)),
    business_date TEXT NOT NULL,
    period TEXT NOT NULL,
    original_period TEXT NOT NULL,
    is_adjustment INTEGER NOT NULL CHECK(is_adjustment IN (0, 1)),
    source_id TEXT,
    source_seq INTEGER,
    payload_hash TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS st_events_source
    ON st_events(source_id, source_seq) WHERE source_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS st_events_document
    ON st_events(channel_id, kind, ref_no)
    WHERE (kind = 'outbound' AND ref_no IS NOT NULL)
       OR (kind IN ('transfer', 'return') AND phase = 'shipped' AND ref_no IS NOT NULL);
CREATE INDEX IF NOT EXISTS st_events_channel_period ON st_events(channel_id, period);
CREATE INDEX IF NOT EXISTS st_events_channel_product ON st_events(channel_id, product_id, batch_id);
CREATE TABLE IF NOT EXISTS st_variances (
    variance_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES st_events(event_id),
    channel_id TEXT NOT NULL,
    product_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    variance_type TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    period TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS st_variances_channel_period ON st_variances(channel_id, period);
CREATE TABLE IF NOT EXISTS st_disputes (
    dispute_id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL,
    product_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open', 'resolved')),
    opened_by TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    resolved_by TEXT,
    resolved_at TEXT,
    resolution TEXT
);
CREATE INDEX IF NOT EXISTS st_disputes_open
    ON st_disputes(channel_id, product_id, batch_id) WHERE status = 'open';
CREATE TABLE IF NOT EXISTS st_period_closes (
    channel_id TEXT NOT NULL,
    period TEXT NOT NULL,
    closed_by TEXT NOT NULL,
    closed_at TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL,
    PRIMARY KEY(channel_id, period)
);
CREATE TABLE IF NOT EXISTS st_period_snapshots (
    channel_id TEXT NOT NULL,
    period TEXT NOT NULL,
    product_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    bucket TEXT NOT NULL,
    owner TEXT NOT NULL,
    opening_qty INTEGER NOT NULL,
    in_qty INTEGER NOT NULL,
    out_qty INTEGER NOT NULL,
    closing_qty INTEGER NOT NULL,
    PRIMARY KEY(channel_id, period, product_id, batch_id, bucket, owner)
);
CREATE TABLE IF NOT EXISTS st_source_cursors (
    source_id TEXT PRIMARY KEY,
    last_seq INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS st_source_anomalies (
    anomaly_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    source_seq INTEGER NOT NULL,
    detail_json TEXT NOT NULL,
    detected_at TEXT NOT NULL
);
"""


def ensure_schema(connection) -> None:
    """在既有数据库连接上幂等地创建对账服务的数据表。"""

    connection.executescript(SCHEMA)
