"""渠道动销对账服务：事件入账、离线补传、关账与可复算查询。

在基础服务的权限、幂等、事务与审计边界之上，维护一套只增不改的库存事件
台账。核心规则：

- 所有库存事实（出库、签收、调拨、售出、退货、盘点、争议）都是带来源凭证
  的事件，余额只能由事件重建；
- 离线补传按来源流 (stream_key, stream_seq) 去重：同序号同内容视为重放，
  同序号异内容判定为序列分叉，隔离登记且绝不入账；
- 会计期间关账后，迟到凭证不覆盖原期间，而是标记为调整追加到后续开放期间；
- 争议只冻结相关产品批次+渠道，其余渠道与批次照常关账。
"""

from __future__ import annotations

import calendar
import json
import re
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from . import channel_domain as cd
from .audit import append_event, canonical_json, digest
from .channel_ledger import period_metrics, rebuild
from .channel_models import (
    AccountingPeriod, BalanceRow, Discrepancy, EventReceipt, InventoryEvent,
    ReconciliationReport,
)
from .clock import Clock, SystemClock
from .errors import (
    ConflictError, NotFoundError, PermissionDenied, SequenceForkError,
    ValidationError,
)
from .storage import Database

PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def period_of(business_date: str) -> str:
    """业务日期所属会计期间（按月）。"""

    return business_date[:7]


def period_days(period_id: str) -> int:
    """该会计期间包含的自然日数。"""

    year, month = (int(part) for part in period_id.split("-"))
    return calendar.monthrange(year, month)[1]


class ChannelReconciliationService:
    """协调渠道台账的入账、冻结、关账与重建查询。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    # -- 基础工具 ---------------------------------------------------------

    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _actor(self, connection, actor_id: str) -> Any:
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        if not row["active"]:
            raise PermissionDenied("操作者已停用")
        return row

    def _require_roles(self, actor: Any, *roles: str) -> None:
        if actor["role"] not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _qty(self, value: Any, field: str = "quantity") -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValidationError(f"{field} 必须是非负整数")
        if value < 0:
            raise ValidationError(f"{field} 不能为负数")
        return value

    def _date(self, value: Any) -> str:
        value = str(value).strip()
        if not DATE_RE.fullmatch(value):
            raise ValidationError("business_date 必须是 YYYY-MM-DD")
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValidationError("business_date 不是合法日期") from exc
        return value

    def _event_from_row(self, row: Any) -> dict[str, Any]:
        return {
            "event_id": row["event_id"], "event_type": row["event_type"],
            "product_id": row["product_id"], "batch_no": row["batch_no"],
            "channel_id": row["channel_id"], "quantity": row["quantity"],
            "variance_qty": row["variance_qty"],
            "source_type": row["source_type"], "source_ref": row["source_ref"],
            "business_date": row["business_date"], "period_id": row["period_id"],
            "origin_period": row["origin_period"], "is_adjustment": bool(row["is_adjustment"]),
            "unit_cost": row["unit_cost"], "stream_key": row["stream_key"],
            "stream_seq": row["stream_seq"], "payload": json.loads(row["payload_json"]),
            "payload_hash": row["payload_hash"], "recorded_by": row["recorded_by"],
            "recorded_at": row["recorded_at"], "entry_seq": row["entry_seq"],
        }

    # -- 主数据 -----------------------------------------------------------

    def register_product(self, *, actor_id: str, product_id: str, name: str) -> None:
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_roles(actor, "admin", "operator")
            name = str(name).strip()
            if not name:
                raise ValidationError("name 不能为空")
            try:
                conn.execute("INSERT INTO channel_products(product_id,name,created_at) VALUES(?,?,?)",
                             (product_id, name, self._now()))
            except Exception as exc:
                raise ConflictError("产品编号已经存在") from exc
            append_event(conn, actor_id=actor_id, action="channel.product_registered",
                         resource_type="product", resource_id=product_id,
                         detail={"name": name}, occurred_at=self._now())

    def register_channel(self, *, actor_id: str, channel_id: str, name: str) -> None:
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_roles(actor, "admin", "operator")
            name = str(name).strip()
            if not name:
                raise ValidationError("name 不能为空")
            try:
                conn.execute("INSERT INTO channel_channels(channel_id,name,created_at) VALUES(?,?,?)",
                             (channel_id, name, self._now()))
            except Exception as exc:
                raise ConflictError("渠道编号已经存在") from exc
            append_event(conn, actor_id=actor_id, action="channel.channel_registered",
                         resource_type="channel", resource_id=channel_id,
                         detail={"name": name}, occurred_at=self._now())

    # -- 关账状态 ---------------------------------------------------------

    def _is_closed(self, conn, period_id: str, channel_id: str) -> bool:
        return conn.execute(
            "SELECT 1 FROM period_closings WHERE period_id=? AND channel_id=?",
            (period_id, channel_id),
        ).fetchone() is not None

    def _open_dispute(self, conn, *, product_id: str, batch_no: str, channel_id: str) -> Any:
        return conn.execute(
            "SELECT * FROM disputes WHERE product_id=? AND batch_no=? AND channel_id=? AND status='open'",
            (product_id, batch_no, channel_id),
        ).fetchone()

    def close_period(self, *, request_id: str, actor_id: str, period_id: str,
                     channel_id: str) -> dict[str, Any]:
        """冻结渠道在某期间的台账并固化重建快照。"""

        if not PERIOD_RE.fullmatch(period_id):
            raise ValidationError("period_id 必须是 YYYY-MM")
        payload = {"actor_id": actor_id, "period_id": period_id, "channel_id": channel_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_roles(actor, "admin", "reviewer")
            if conn.execute("SELECT 1 FROM channel_channels WHERE channel_id=?", (channel_id,)).fetchone() is None:
                raise NotFoundError("渠道不存在")
            if self._is_closed(conn, period_id, channel_id):
                raise ConflictError("该渠道期间已关账")
            # 争议只冻结相关批次：本渠道存在未决争议则阻止本渠道关账，其他渠道不受影响。
            open_row = conn.execute(
                "SELECT product_id,batch_no FROM disputes WHERE channel_id=? AND status='open' LIMIT 1",
                (channel_id,),
            ).fetchone()
            if open_row is not None:
                raise ConflictError(
                    f"渠道存在未决争议，批次 {open_row['product_id']}/{open_row['batch_no']} 冻结中，不能关账"
                )
            snapshot_hash = self._snapshot_hash(conn, period_id, channel_id)
            if conn.execute("SELECT 1 FROM request_receipts WHERE request_id=?",
                            (request_id,)).fetchone():
                raise ConflictError("request_id 已被使用")
            conn.execute(
                "INSERT INTO period_closings(period_id,channel_id,closed_at,closed_by,snapshot_hash) "
                "VALUES(?,?,?,?,?)",
                (period_id, channel_id, self._now(), actor_id, snapshot_hash),
            )
            conn.execute(
                "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,"
                "response_json,created_at) VALUES(?,?,?,?,?,?,?)",
                (request_id, "close_period", digest(payload), "period_closing",
                 f"{period_id}:{channel_id}", canonical_json({"snapshot_hash": snapshot_hash}), self._now()),
            )
            append_event(conn, actor_id=actor_id, action="channel.period_closed",
                         resource_type="period_closing", resource_id=f"{period_id}:{channel_id}",
                         detail={"period_id": period_id, "channel_id": channel_id,
                                 "snapshot_hash": snapshot_hash}, occurred_at=self._now())
            return {"period_id": period_id, "channel_id": channel_id, "snapshot_hash": snapshot_hash}

    def get_period(self, period_id: str, channel_id: str) -> AccountingPeriod:
        row = self.database.connection.execute(
            "SELECT * FROM period_closings WHERE period_id=? AND channel_id=?",
            (period_id, channel_id),
        ).fetchone()
        if row is None:
            return AccountingPeriod(period_id, "open")
        return AccountingPeriod(period_id, "closed", row["closed_at"], row["closed_by"],
                                row["snapshot_hash"])

    # -- 事件入账 ---------------------------------------------------------

    def record_event(self, *, request_id: str, actor_id: str, event_type: str,
                     product_id: str, batch_no: str, channel_id: str, quantity: int,
                     source_type: str, source_ref: str, business_date: str,
                     payload: dict[str, Any] | None = None, unit_cost: str | None = None,
                     stream_key: str | None = None, stream_seq: int | None = None,
                     stocktake_count: int | None = None) -> EventReceipt:
        """登记一条带来源凭证的库存事件，处理重放、分叉与关账调整。"""

        payload = payload or {}
        if not isinstance(payload, dict):
            raise ValidationError("payload 必须是对象")

        def _txn(conn) -> EventReceipt | SequenceForkError:
            actor = self._actor(conn, actor_id)
            self._require_roles(actor, "admin", "operator", "reviewer")

            qty = self._qty(quantity)
            normalized_date = self._date(business_date)

            self._validate_event(conn, event_type=event_type, product_id=product_id,
                                 batch_no=batch_no, channel_id=channel_id, quantity=quantity,
                                 source_type=source_type, source_ref=source_ref,
                                 business_date=normalized_date, payload=payload,
                                 unit_cost=unit_cost, stream_key=stream_key,
                                 stream_seq=stream_seq, stocktake_count=stocktake_count)

            origin_period = period_of(normalized_date)

            canonical = self._canonical_event(
                event_type=event_type, product_id=product_id, batch_no=batch_no,
                channel_id=channel_id, quantity=qty, source_type=source_type,
                source_ref=source_ref, business_date=normalized_date, payload=payload,
                unit_cost=unit_cost, stocktake_count=stocktake_count)
            incoming_hash = canonical["payload_hash"]

            # 幂等：同一 request_id 重放原回执；内容不同则冲突。
            receipt = conn.execute("SELECT * FROM request_receipts WHERE request_id=?",
                                   (request_id,)).fetchone()
            if receipt is not None:
                if receipt["action"] != "record_event" or receipt["payload_hash"] != incoming_hash:
                    raise ConflictError("request_id 已被不同内容使用")
                return EventReceipt(request_id, receipt["resource_id"], True, False)

            # 序列分叉检测：同来源流同序号已有不同内容。
            if stream_key is not None:
                fork = conn.execute(
                    "SELECT event_id,payload_hash FROM inventory_events "
                    "WHERE stream_key=? AND stream_seq=?", (stream_key, stream_seq),
                ).fetchone()
                if fork is not None and fork["payload_hash"] != incoming_hash:
                    self._register_anomaly(conn, request_id=request_id, stream_key=stream_key,
                                           stream_seq=stream_seq, existing_event_id=fork["event_id"],
                                           existing_hash=fork["payload_hash"], incoming_hash=incoming_hash)
                    append_event(conn, actor_id=actor_id, action="channel.sequence_forked",
                                 resource_type="stream_anomaly", resource_id=stream_key,
                                 detail={"stream_key": stream_key, "stream_seq": stream_seq,
                                         "source_ref": source_ref}, occurred_at=self._now())
                    return SequenceForkError(
                        f"来源流 {stream_key} 序号 {stream_seq} 出现序列分叉，已隔离登记且未入账"
                    )
                if fork is not None:
                    # 完全重放：返回原事件，不再入账。
                    return self._replay_receipt(conn, request_id, fork["event_id"])

            # 来源凭证唯一：同一凭证再次到达且内容一致即重放。
            existing = conn.execute(
                "SELECT event_id,payload_hash FROM inventory_events WHERE source_type=? AND source_ref=?",
                (source_type, source_ref),
            ).fetchone()
            if existing is not None:
                if existing["payload_hash"] != incoming_hash:
                    # 同一凭证编号携带不同业务内容，按分叉处理。
                    self._register_anomaly(
                        conn, request_id=request_id,
                        stream_key=stream_key or f"source:{source_type}",
                        stream_seq=stream_seq if stream_seq is not None else -1,
                        existing_event_id=existing["event_id"],
                        existing_hash=existing["payload_hash"],
                        incoming_hash=incoming_hash)
                    append_event(conn, actor_id=actor_id, action="channel.sequence_forked",
                                 resource_type="stream_anomaly",
                                 resource_id=stream_key or f"source:{source_type}",
                                 detail={"stream_key": stream_key or f"source:{source_type}",
                                         "stream_seq": stream_seq if stream_seq is not None else -1,
                                         "source_ref": source_ref}, occurred_at=self._now())
                    return SequenceForkError("同一来源凭证出现不同内容，判定为重放分叉，已隔离且未入账")
                return self._replay_receipt(conn, request_id, existing["event_id"])

            # 关账规则：业务期间已关账时，追加为后续开放期间的调整，不覆盖原记录。
            posting_period = origin_period
            is_adjustment = False
            if self._is_closed(conn, origin_period, channel_id):
                posting_period = self._next_open_period(conn, origin_period, channel_id)
                is_adjustment = True

            # 争议冻结：相关批次+渠道在争议中，禁止移动其在途/可销售库存；
            # 客户退货只增加待退池，不触碰冻结池，仍然允许。
            if event_type != cd.RETURN and self._open_dispute(
                    conn, product_id=product_id, batch_no=batch_no,
                    channel_id=channel_id) is not None:
                raise ConflictError("该批次在争议冻结中，不能移动或盘点其库存")

            event_id = uuid.uuid4().hex
            variance_qty = None
            if event_type == cd.STOCKTAKE:
                variance_qty = self._stocktake_variance(
                    conn, product_id=product_id, batch_no=batch_no, channel_id=channel_id,
                    stocktake_count=stocktake_count, quantity=qty, payload=payload)

            conn.execute(
                "INSERT INTO inventory_events(event_id,event_type,product_id,batch_no,channel_id,"
                "quantity,variance_qty,source_type,source_ref,business_date,period_id,origin_period,"
                "is_adjustment,unit_cost,stream_key,stream_seq,payload_json,payload_hash,recorded_by,"
                "recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (event_id, event_type, product_id, batch_no, channel_id, qty, variance_qty,
                 source_type, source_ref, business_date, posting_period, origin_period,
                 1 if is_adjustment else 0, unit_cost, stream_key, stream_seq,
                 canonical["payload_json"], canonical["payload_hash"], actor_id, self._now()),
            )
            self._store_receipt(conn, request_id=request_id, action="record_event",
                                payload_hash=canonical["payload_hash"], event_id=event_id,
                                response={"posting_period": posting_period,
                                          "is_adjustment": is_adjustment})
            append_event(conn, actor_id=actor_id, action="channel.event_recorded",
                         resource_type="inventory_event", resource_id=event_id,
                         detail={"event_type": event_type, "product_id": product_id,
                                 "batch_no": batch_no, "channel_id": channel_id,
                                 "quantity": qty, "source_type": source_type,
                                 "source_ref": source_ref, "origin_period": origin_period,
                                 "posting_period": posting_period,
                                 "is_adjustment": is_adjustment},
                         occurred_at=self._now())
            return EventReceipt(request_id, event_id, False, False)
        result: EventReceipt | SequenceForkError
        with self.database.transaction(immediate=True) as conn:
            result = _txn(conn)
        if isinstance(result, SequenceForkError):
            raise result
        return result

    # -- 入账校验与辅助 ---------------------------------------------------

    def _validate_event(self, conn, *, event_type, product_id, batch_no, channel_id, quantity,
                        source_type, source_ref, business_date, payload, unit_cost,
                        stream_key, stream_seq, stocktake_count) -> None:
        if not cd.is_event_type(event_type):
            raise ValidationError("event_type 不在允许范围内")
        if not str(batch_no).strip():
            raise ValidationError("batch_no 不能为空")
        if conn.execute("SELECT 1 FROM channel_products WHERE product_id=?", (product_id,)).fetchone() is None:
            raise NotFoundError("产品不存在")
        if conn.execute("SELECT 1 FROM channel_channels WHERE channel_id=?", (channel_id,)).fetchone() is None:
            raise NotFoundError("渠道不存在")
        if source_type not in cd.SOURCES:
            raise ValidationError("source_type 不在允许范围内")
        if not str(source_ref).strip():
            raise ValidationError("source_ref 不能为空")
        if (stream_key is None) != (stream_seq is None):
            raise ValidationError("stream_key 与 stream_seq 必须同时提供")
        if stream_seq is not None and (isinstance(stream_seq, bool) or not isinstance(stream_seq, int)
                                       or stream_seq < 0):
            raise ValidationError("stream_seq 必须是非负整数")
        if unit_cost is not None:
            try:
                Decimal(str(unit_cost))
            except Exception as exc:
                raise ValidationError("unit_cost 必须是十进制金额") from exc
        if event_type == cd.STOCKTAKE and stocktake_count is None and "counted_qty" not in payload:
            raise ValidationError("盘点事件必须提供 stocktake_count 或 payload.counted_qty")

    def _canonical_event(self, *, event_type, product_id, batch_no, channel_id, quantity,
                         source_type, source_ref, business_date, payload, unit_cost,
                         stocktake_count) -> dict[str, str]:
        body = {
            "event_type": event_type, "product_id": product_id, "batch_no": batch_no,
            "channel_id": channel_id, "quantity": quantity, "source_type": source_type,
            "source_ref": source_ref, "business_date": business_date,
            "payload": payload, "unit_cost": unit_cost,
        }
        if event_type == cd.STOCKTAKE:
            counted = stocktake_count if stocktake_count is not None else payload.get("counted_qty")
            body["stocktake_count"] = counted
        # payload_json 列只存业务 payload；payload_hash 覆盖完整规范化事件，用于分叉识别。
        return {"payload_json": canonical_json(payload), "payload_hash": digest(body)}

    def _stocktake_variance(self, conn, *, product_id, batch_no, channel_id,
                            stocktake_count, quantity, payload) -> int:
        counted = stocktake_count if stocktake_count is not None else payload.get("counted_qty")
        counted = self._qty(counted, "stocktake_count")
        engine = rebuild(self._channel_events(conn, channel_id))
        totals = engine.state_totals().get((product_id, batch_no, channel_id), {})
        # 盘点只核对在仓可销售现货；在途、待退、争议货不在货架，不纳入账实比对。
        book = totals.get(cd.SELLABLE, 0)
        return counted - book

    def _next_open_period(self, conn, origin_period: str, channel_id: str) -> str:
        year, month = (int(part) for part in origin_period.split("-"))
        for _ in range(120):
            month += 1
            if month > 12:
                month, year = 1, year + 1
            candidate = f"{year:04d}-{month:02d}"
            if not self._is_closed(conn, candidate, channel_id):
                return candidate
        raise ConflictError("没有可接收调整的开放期间")

    def _register_anomaly(self, conn, *, request_id, stream_key, stream_seq, existing_event_id,
                          existing_hash, incoming_hash) -> None:
        conn.execute(
            "INSERT INTO stream_anomalies(anomaly_id,stream_key,stream_seq,request_id,"
            "existing_event_id,existing_payload_hash,incoming_payload_hash,detected_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, stream_key, stream_seq, request_id, existing_event_id,
             existing_hash, incoming_hash, self._now()),
        )

    def _replay_receipt(self, conn, request_id: str, event_id: str) -> EventReceipt:
        row = conn.execute("SELECT payload_hash FROM inventory_events WHERE event_id=?",
                           (event_id,)).fetchone()
        self._store_receipt(conn, request_id=request_id, action="record_event",
                            payload_hash=row["payload_hash"] if row else "", event_id=event_id,
                            response={"replayed": True})
        return EventReceipt(request_id, event_id, True, False)

    def _store_receipt(self, conn, *, request_id, action, payload_hash, event_id, response) -> None:
        conn.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,"
            "response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, "inventory_event", event_id,
             canonical_json(response), self._now()),
        )

    # -- 争议 -------------------------------------------------------------

    def open_dispute(self, *, request_id: str, actor_id: str, product_id: str,
                     batch_no: str, channel_id: str, quantity: int, source_ref: str,
                     reason: str, from_state: str = cd.SELLABLE,
                     owner: str = cd.OWNER_DISTRIBUTOR) -> dict[str, Any]:
        qty = self._qty(quantity)
        if qty <= 0:
            raise ValidationError("争议数量必须大于 0")
        if from_state not in (cd.SELLABLE, cd.IN_TRANSIT, cd.PENDING_RETURN):
            raise ValidationError("争议来源库存状态非法")
        if owner not in cd.OWNERS:
            raise ValidationError("货权方非法")
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_roles(actor, "admin", "reviewer")
            if conn.execute("SELECT 1 FROM channel_products WHERE product_id=?",
                            (product_id,)).fetchone() is None:
                raise NotFoundError("产品不存在")
            if self._open_dispute(conn, product_id=product_id, batch_no=batch_no,
                                  channel_id=channel_id) is not None:
                raise ConflictError("该批次渠道已有未决争议")
            # 只在对应来源池确有足额货权时才允许冻结。
            engine = rebuild(self._channel_events(conn, channel_id))
            available = engine.pools.get((product_id, batch_no, channel_id), {}).get(
                from_state, {}).get(owner, 0)
            if available < qty:
                raise ConflictError(
                    f"可冻结库存不足：{from_state}/{owner} 现有 {available}，申请 {qty}")
            today = self.clock.now().date().isoformat()
            origin = period_of(today)
            posting_period = origin
            is_adjustment = False
            if self._is_closed(conn, origin, channel_id):
                posting_period = self._next_open_period(conn, origin, channel_id)
                is_adjustment = True
            event_id = uuid.uuid4().hex
            dispute_id = uuid.uuid4().hex
            payload = {"reason": reason, "from_state": from_state, "owner": owner}
            body_hash = digest(payload)
            conn.execute(
                "INSERT INTO inventory_events(event_id,event_type,product_id,batch_no,channel_id,"
                "quantity,variance_qty,source_type,source_ref,business_date,period_id,origin_period,"
                "is_adjustment,unit_cost,stream_key,stream_seq,payload_json,payload_hash,recorded_by,"
                "recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (event_id, cd.DISPUTE_OPEN, product_id, batch_no, channel_id, qty, None,
                 "dispute_case", source_ref, today, posting_period, origin,
                 1 if is_adjustment else 0, None, None, None,
                 canonical_json(payload), body_hash, actor_id, self._now()),
            )
            conn.execute(
                "INSERT INTO disputes(dispute_id,product_id,batch_no,channel_id,quantity,status,"
                "source_type,source_ref,opened_event_id,opened_at) VALUES(?,?,?,?,?,'open',?,?,?,?)",
                (dispute_id, product_id, batch_no, channel_id, qty, "dispute_case",
                 source_ref, event_id, self._now()),
            )
            self._store_receipt(conn, request_id=request_id, action="open_dispute",
                                payload_hash=body_hash, event_id=dispute_id,
                                response={"dispute_id": dispute_id})
            append_event(conn, actor_id=actor_id, action="channel.dispute_opened",
                         resource_type="dispute", resource_id=dispute_id,
                         detail={"product_id": product_id, "batch_no": batch_no,
                                 "channel_id": channel_id, "quantity": qty},
                         occurred_at=self._now())
            return {"dispute_id": dispute_id, "event_id": event_id,
                    "posting_period": posting_period, "is_adjustment": is_adjustment}

    def resolve_dispute(self, *, request_id: str, actor_id: str, dispute_id: str,
                        resolution: str = "release") -> dict[str, Any]:
        """解除争议：release 转回可销售，writeoff 核销并记差异。"""

        if resolution not in ("release", "writeoff"):
            raise ValidationError("resolution 必须是 release 或 writeoff")
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_roles(actor, "admin", "reviewer")
            row = conn.execute("SELECT * FROM disputes WHERE dispute_id=?", (dispute_id,)).fetchone()
            if row is None:
                raise NotFoundError("争议不存在")
            if row["status"] == "resolved":
                raise ConflictError("争议已解除")
            open_row = conn.execute("SELECT payload_json FROM inventory_events WHERE event_id=?",
                                    (row["opened_event_id"],)).fetchone()
            open_payload = json.loads(open_row["payload_json"]) if open_row else {}
            from_state = open_payload.get("from_state", cd.SELLABLE)
            owner = open_payload.get("owner", cd.OWNER_DISTRIBUTOR)
            today = self.clock.now().date().isoformat()
            origin = period_of(today)
            posting_period = origin
            is_adjustment = False
            if self._is_closed(conn, origin, row["channel_id"]):
                posting_period = self._next_open_period(conn, origin, row["channel_id"])
                is_adjustment = True
            event_id = uuid.uuid4().hex
            payload = {"resolution": resolution, "dispute_id": dispute_id,
                       "from_state": from_state, "owner": owner}
            conn.execute(
                "INSERT INTO inventory_events(event_id,event_type,product_id,batch_no,channel_id,"
                "quantity,variance_qty,source_type,source_ref,business_date,period_id,origin_period,"
                "is_adjustment,unit_cost,stream_key,stream_seq,payload_json,payload_hash,recorded_by,"
                "recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (event_id, cd.DISPUTE_RESOLVE, row["product_id"], row["batch_no"], row["channel_id"],
                 row["quantity"], None, "dispute_case", f"{row['source_ref']}:resolve", today,
                 posting_period, origin, 1 if is_adjustment else 0, None, None, None,
                 canonical_json(payload), digest(payload), actor_id, self._now()),
            )
            conn.execute(
                "UPDATE disputes SET status='resolved',resolved_event_id=?,resolution=?,resolved_at=? "
                "WHERE dispute_id=?", (event_id, resolution, self._now(), dispute_id),
            )
            self._store_receipt(conn, request_id=request_id, action="resolve_dispute",
                                payload_hash=digest(payload), event_id=event_id,
                                response={"resolution": resolution})
            append_event(conn, actor_id=actor_id, action="channel.dispute_resolved",
                         resource_type="dispute", resource_id=dispute_id,
                         detail={"resolution": resolution, "quantity": row["quantity"]},
                         occurred_at=self._now())
            return {"dispute_id": dispute_id, "event_id": event_id, "resolution": resolution}

    # -- 读取与重建 -------------------------------------------------------

    def _channel_events(self, conn, channel_id: str) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT rowid AS entry_seq, * FROM inventory_events WHERE channel_id=? "
            "ORDER BY period_id,business_date,rowid", (channel_id,),
        ).fetchall()
        return [self._event_from_row(row) for row in rows]

    def list_events(self, *, channel_id: str, period_id: str | None = None,
                    product_id: str | None = None, batch_no: str | None = None) -> list[InventoryEvent]:
        query = "SELECT * FROM inventory_events WHERE 1=1"
        params: list[Any] = []
        if channel_id is not None:
            query += " AND channel_id=?"
            params.append(channel_id)
        if period_id:
            query += " AND period_id=?"
            params.append(period_id)
        if product_id:
            query += " AND product_id=?"
            params.append(product_id)
        if batch_no:
            query += " AND batch_no=?"
            params.append(batch_no)
        query += " ORDER BY period_id,business_date,rowid"
        events = []
        for row in self.database.connection.execute(query, params):
            events.append(InventoryEvent(
                row["event_id"], row["event_type"], row["product_id"], row["batch_no"],
                row["channel_id"], row["quantity"], row["source_type"], row["source_ref"],
                row["business_date"], row["period_id"], row["origin_period"],
                bool(row["is_adjustment"]), json.loads(row["payload_json"]), row["recorded_by"],
                row["recorded_at"], row["stream_key"], row["stream_seq"], row["unit_cost"]))
        return events

    def inventory(self, *, channel_id: str, period_id: str | None = None,
                  product_id: str | None = None, batch_no: str | None = None) -> list[BalanceRow]:
        """重建截至某期间的库存，按状态（可销售/在途/待退/争议）与货权分列。"""

        with self.database.transaction() as conn:
            events = self._channel_events(conn, channel_id)
        if product_id:
            events = [e for e in events if e["product_id"] == product_id]
        if batch_no:
            events = [e for e in events if e["batch_no"] == batch_no]
        engine = rebuild(events, upto_period=period_id)
        rows: list[BalanceRow] = []
        for (pid, batch, ch), states in engine.pools.items():
            for state, owners in states.items():
                for owner, qty in owners.items():
                    if qty:
                        rows.append(BalanceRow(pid, batch, ch, state, owner, qty, None))
        rows.sort(key=lambda r: (r.product_id, r.batch_no, r.state, r.owner))
        return rows

    def _snapshot_hash(self, conn, period_id: str, channel_id: str) -> str:
        events = [e for e in self._channel_events(conn, channel_id) if e["period_id"] <= period_id]
        engine = rebuild(events)
        material = {
            "period_id": period_id, "channel_id": channel_id,
            "balances": {":".join(k): v for k, v in engine.state_totals().items()},
            "event_ids": [e["event_id"] for e in events],
        }
        return digest(material)

    def reconciliation_report(self, *, period_id: str, channel_id: str,
                              product_id: str | None = None,
                              batch_no: str | None = None) -> ReconciliationReport:
        """重建某渠道期间的 sell-in/sell-out、库存天数与差异来源。"""

        if not PERIOD_RE.fullmatch(period_id):
            raise ValidationError("period_id 必须是 YYYY-MM")
        with self.database.transaction() as conn:
            if conn.execute("SELECT 1 FROM channel_channels WHERE channel_id=?",
                            (channel_id,)).fetchone() is None:
                raise NotFoundError("渠道不存在")
            events = self._channel_events(conn, channel_id)
            closing = conn.execute(
                "SELECT * FROM period_closings WHERE period_id=? AND channel_id=?",
                (period_id, channel_id),
            ).fetchone()

        if product_id:
            events = [e for e in events if e["product_id"] == product_id]
        if batch_no:
            events = [e for e in events if e["batch_no"] == batch_no]

        upto = rebuild(events, upto_period=period_id)
        totals = upto.state_totals()
        sellable = sum(v.get(cd.SELLABLE, 0) for v in totals.values())
        in_transit = sum(v.get(cd.IN_TRANSIT, 0) for v in totals.values())
        pending = sum(v.get(cd.PENDING_RETURN, 0) for v in totals.values())
        disputed = sum(v.get(cd.DISPUTED, 0) for v in totals.values())

        period_events = [e for e in events if e["period_id"] == period_id]
        metrics = period_metrics(period_events)

        elapsed = period_days(period_id)
        net_out = metrics["net_sell_out_quantity"]
        avg_daily = Decimal(net_out) / Decimal(elapsed) if net_out > 0 else Decimal(0)
        inventory_days = (Decimal(sellable) / avg_daily) if avg_daily > 0 else None

        discrepancies = [
            Discrepancy(d["kind"], d["product_id"], d["batch_no"], d["channel_id"],
                        d["quantity"], d["source_type"], d["source_ref"], d.get("detail", {}))
            for d in upto.discrepancies
            if d.get("period_id") == period_id
            and (product_id is None or d["product_id"] == product_id)
            and (batch_no is None or d["batch_no"] == batch_no)
        ]

        snapshot_valid = None
        if closing is not None and product_id is None and batch_no is None:
            # 用全量事件重新固化快照并与关账时的值比对，任何事后改动都会暴露。
            with self.database.transaction() as conn:
                snapshot_valid = self._snapshot_hash(conn, period_id, channel_id) == closing["snapshot_hash"]

        return ReconciliationReport(
            period_id=period_id,
            sell_in_quantity=metrics["sell_in_quantity"],
            sell_in_value=metrics["sell_in_value"],
            sell_out_quantity=metrics["sell_out_quantity"],
            sell_out_value=metrics["sell_out_value"],
            return_quantity=metrics["return_quantity"],
            net_sell_out_quantity=net_out,
            sellable_quantity=sellable,
            in_transit_quantity=in_transit,
            pending_return_quantity=pending,
            disputed_quantity=disputed,
            elapsed_days=elapsed,
            avg_daily_sell_out=str(avg_daily.quantize(Decimal("0.0001"))),
            inventory_days=None if inventory_days is None else str(inventory_days.quantize(Decimal("0.01"))),
            discrepancies=discrepancies,
            snapshot_valid=snapshot_valid,
        )

    def list_anomalies(self, stream_key: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM stream_anomalies"
        params: list[Any] = []
        if stream_key:
            query += " WHERE stream_key=?"
            params.append(stream_key)
        query += " ORDER BY detected_at"
        return [dict(row) for row in self.database.connection.execute(query, params)]
