"""渠道动销对账服务：事件写入、离线补传、争议冻结、期间关账与可复算查询。"""

from __future__ import annotations

import json
import re
import uuid
from datetime import date, timedelta
from typing import Any, Callable, Iterable, Mapping

from beverage_ops_foundation.audit import append_event, canonical_json, digest
from beverage_ops_foundation.clock import Clock, SystemClock
from beverage_ops_foundation.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from beverage_ops_foundation.storage import Database

from . import projection as proj
from .models import BackfillItemResult, CloseOutcome, DisputeOutcome, EventOutcome
from .storage import ensure_schema


DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
DEFAULT_LOCATION = "main"
DOI_WINDOW_DAYS = 30

WRITE_ROLES = ("admin", "operator")
STOCKTAKE_ROLES = ("admin", "operator", "reviewer")
CONTROL_ROLES = ("admin", "reviewer")


def period_of(business_date: str) -> str:
    """由业务日期推导自然会计期间（YYYY-MM）。"""

    return business_date[:7]


def next_period(period: str) -> str:
    year, month = int(period[:4]), int(period[5:7])
    if month == 12:
        return f"{year + 1:04d}-01"
    return f"{year:04d}-{month + 1:02d}"


def period_end_date(period: str) -> str:
    """返回期间（YYYY-MM）最后一天的日期。"""

    year, month = int(period[:4]), int(period[5:7])
    if month == 12:
        first_of_next = date(year + 1, 1, 1)
    else:
        first_of_next = date(year, month + 1, 1)
    return (first_of_next - timedelta(days=1)).isoformat()


class SellThroughService:
    """在基础服务边界之上协调渠道库存事件、期间与对账查询。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()
        ensure_schema(database.connection)

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------
    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _today(self) -> str:
        return self.clock.now().date().isoformat()

    def _identifier(self, value: str, field: str) -> str:
        value = str(value).strip()
        if not IDENTIFIER.fullmatch(value):
            raise ValidationError(f"{field} 格式无效")
        return value

    def _text(self, value: str, field: str, limit: int = 200) -> str:
        value = str(value).strip()
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    def _business_date(self, value: str) -> str:
        value = str(value).strip()
        if not DATE_RE.fullmatch(value):
            raise ValidationError("business_date 必须是 YYYY-MM-DD")
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValidationError("business_date 不是有效日期") from exc
        if value > self._today():
            raise ValidationError("business_date 不能晚于当前日期")
        return value

    def _period(self, value: str) -> str:
        value = str(value).strip()
        if not PERIOD_RE.fullmatch(value):
            raise ValidationError("period 必须是 YYYY-MM")
        return value

    def _quantity(self, value: Any, *, allow_zero: bool = False) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValidationError("quantity 必须是整数")
        if value < 0 or (value == 0 and not allow_zero):
            raise ValidationError("quantity 必须为正整数" if not allow_zero else "quantity 不能为负")
        return value

    def _actor(self, connection, actor_id: str) -> Mapping[str, Any]:
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        if not row["active"]:
            raise PermissionDenied("操作者已停用")
        return row

    def _require(self, actor: Mapping[str, Any], *roles: str) -> None:
        if actor["role"] not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _channel(self, connection, channel_id: str) -> Mapping[str, Any]:
        row = connection.execute("SELECT * FROM st_channels WHERE channel_id=?", (channel_id,)).fetchone()
        if row is None:
            raise NotFoundError("渠道不存在")
        return row

    def _check_org(self, actor: Mapping[str, Any], organization_id: str) -> None:
        if actor["role"] != "admin" and actor["organization_id"] != organization_id:
            raise PermissionDenied("不能操作其他组织的渠道")

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any],
                    create: Callable[[], tuple[str, str, dict[str, Any]]]) -> tuple[dict[str, Any], bool]:
        """与基础服务同语义的请求级幂等：同 request_id 同内容重放，异内容冲突。"""

        request_id = self._identifier(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return json.loads(row["response_json"]), True
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,response_json,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._now()),
        )
        return response, False

    # ------------------------------------------------------------------
    # 主数据登记
    # ------------------------------------------------------------------
    def register_product(self, *, request_id: str, actor_id: str, product_id: str,
                         name: str, category: str) -> dict[str, Any]:
        payload = {"product_id": product_id, "name": name, "category": category}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *WRITE_ROLES)
            product_id = self._identifier(product_id, "product_id")
            name = self._text(name, "name")
            category = self._text(category, "category", 80)

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO st_products(product_id,name,category,created_by,created_at) VALUES(?,?,?,?,?)",
                        (product_id, name, category, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("产品编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="sellthrough.product_registered",
                             resource_type="st_product", resource_id=product_id,
                             detail={"name": name, "category": category}, occurred_at=self._now())
                return "st_product", product_id, {"product_id": product_id}

            response, replayed = self._idempotent(
                connection, request_id=request_id, action="st.register_product",
                payload=payload, create=create)
            return {**response, "replayed": replayed}

    def register_batch(self, *, request_id: str, actor_id: str, batch_id: str,
                       product_id: str, batch_no: str, produced_on: str) -> dict[str, Any]:
        payload = {"batch_id": batch_id, "product_id": product_id,
                   "batch_no": batch_no, "produced_on": produced_on}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *WRITE_ROLES)
            batch_id = self._identifier(batch_id, "batch_id")
            batch_no = self._text(batch_no, "batch_no", 80)
            if not DATE_RE.fullmatch(str(produced_on).strip()):
                raise ValidationError("produced_on 必须是 YYYY-MM-DD")
            if connection.execute("SELECT 1 FROM st_products WHERE product_id=?",
                                  (product_id,)).fetchone() is None:
                raise NotFoundError("产品不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO st_batches(batch_id,product_id,batch_no,produced_on,created_at) "
                        "VALUES(?,?,?,?,?)",
                        (batch_id, product_id, batch_no, produced_on, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("批次编号或批号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="sellthrough.batch_registered",
                             resource_type="st_batch", resource_id=batch_id,
                             detail={"product_id": product_id, "batch_no": batch_no,
                                     "produced_on": produced_on}, occurred_at=self._now())
                return "st_batch", batch_id, {"batch_id": batch_id}

            response, replayed = self._idempotent(
                connection, request_id=request_id, action="st.register_batch",
                payload=payload, create=create)
            return {**response, "replayed": replayed}

    def register_channel(self, *, request_id: str, actor_id: str, channel_id: str,
                         organization_id: str, name: str, channel_type: str) -> dict[str, Any]:
        payload = {"channel_id": channel_id, "organization_id": organization_id,
                   "name": name, "channel_type": channel_type}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *WRITE_ROLES)
            self._check_org(actor, organization_id)
            if connection.execute("SELECT 1 FROM organizations WHERE organization_id=?",
                                  (organization_id,)).fetchone() is None:
                raise NotFoundError("组织不存在")
            channel_id = self._identifier(channel_id, "channel_id")
            name = self._text(name, "name")
            channel_type = self._text(channel_type, "channel_type", 40)

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO st_channels(channel_id,organization_id,name,channel_type,created_at) "
                        "VALUES(?,?,?,?,?)",
                        (channel_id, organization_id, name, channel_type, self._now()),
                    )
                    connection.execute(
                        "INSERT INTO st_locations(channel_id,location_id,name,created_at) VALUES(?,?,?,?)",
                        (channel_id, DEFAULT_LOCATION, "主仓", self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("渠道编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="sellthrough.channel_registered",
                             resource_type="st_channel", resource_id=channel_id,
                             detail={"organization_id": organization_id, "name": name,
                                     "channel_type": channel_type}, occurred_at=self._now())
                return "st_channel", channel_id, {"channel_id": channel_id,
                                                  "default_location_id": DEFAULT_LOCATION}

            response, replayed = self._idempotent(
                connection, request_id=request_id, action="st.register_channel",
                payload=payload, create=create)
            return {**response, "replayed": replayed}

    def register_location(self, *, request_id: str, actor_id: str, channel_id: str,
                          location_id: str, name: str) -> dict[str, Any]:
        payload = {"channel_id": channel_id, "location_id": location_id, "name": name}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *WRITE_ROLES)
            channel = self._channel(connection, channel_id)
            self._check_org(actor, channel["organization_id"])
            location_id = self._identifier(location_id, "location_id")
            name = self._text(name, "name")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO st_locations(channel_id,location_id,name,created_at) VALUES(?,?,?,?)",
                        (channel_id, location_id, name, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("库位编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="sellthrough.location_registered",
                             resource_type="st_location", resource_id=f"{channel_id}/{location_id}",
                             detail={"channel_id": channel_id, "location_id": location_id,
                                     "name": name}, occurred_at=self._now())
                return "st_location", f"{channel_id}/{location_id}", {"location_id": location_id}

            response, replayed = self._idempotent(
                connection, request_id=request_id, action="st.register_location",
                payload=payload, create=create)
            return {**response, "replayed": replayed}

    # ------------------------------------------------------------------
    # 库存事件写入
    # ------------------------------------------------------------------
    def _validate_event_shape(self, connection, *, kind: str, phase: str | None,
                              product_id: str, batch_id: str, channel_id: str,
                              from_location_id: str | None, to_location_id: str | None,
                              ref_no: str | None, final: bool) -> tuple[str | None, str | None]:
        if kind not in proj.MOVEMENT_KINDS:
            raise ValidationError("kind 必须是出库/签收/调拨/售出/退货/盘点之一")
        if kind in (proj.KIND_TRANSFER, proj.KIND_RETURN):
            if phase not in (proj.PHASE_SHIPPED, proj.PHASE_RECEIVED):
                raise ValidationError("调拨与退货事件必须携带 phase=shipped 或 received")
        elif phase is not None:
            raise ValidationError("只有调拨与退货事件允许携带 phase")
        batch = connection.execute("SELECT * FROM st_batches WHERE batch_id=?", (batch_id,)).fetchone()
        if batch is None:
            raise NotFoundError("批次不存在")
        if batch["product_id"] != product_id:
            raise ValidationError("批次不属于指定产品")

        def location(location_id: str | None) -> str:
            location_id = location_id or DEFAULT_LOCATION
            if connection.execute("SELECT 1 FROM st_locations WHERE channel_id=? AND location_id=?",
                                  (channel_id, location_id)).fetchone() is None:
                raise NotFoundError(f"库位 {location_id} 不存在")
            return location_id

        if kind == proj.KIND_OUTBOUND:
            if not ref_no:
                raise ValidationError("企业出库必须携带发货单号 ref_no")
            from_location_id, to_location_id = None, None
        elif kind == proj.KIND_RECEIPT:
            if not ref_no:
                raise ValidationError("经销商签收必须携带发货单号 ref_no")
            from_location_id, to_location_id = None, location(to_location_id)
        elif kind == proj.KIND_TRANSFER:
            if not ref_no:
                raise ValidationError("仓间调拨必须携带调拨单号 ref_no")
            if phase == proj.PHASE_SHIPPED:
                from_location_id, to_location_id = location(from_location_id), location(to_location_id)
                if from_location_id == to_location_id:
                    raise ValidationError("调拨调出与调入库位不能相同")
            else:
                from_location_id, to_location_id = None, None
        elif kind == proj.KIND_SALE:
            from_location_id, to_location_id = location(from_location_id), None
        elif kind == proj.KIND_RETURN:
            if not ref_no:
                raise ValidationError("退货必须携带退货单号 ref_no")
            if phase == proj.PHASE_SHIPPED:
                from_location_id, to_location_id = location(from_location_id), None
            else:
                from_location_id, to_location_id = None, None
        elif kind == proj.KIND_STOCKTAKE:
            from_location_id, to_location_id = location(from_location_id), None
            if final:
                raise ValidationError("盘点事件不支持 final 标记")
        return from_location_id, to_location_id

    def _dispute_open(self, connection, channel_id: str, product_id: str, batch_id: str) -> bool:
        return connection.execute(
            "SELECT 1 FROM st_disputes WHERE channel_id=? AND product_id=? AND batch_id=? AND status='open'",
            (channel_id, product_id, batch_id),
        ).fetchone() is not None

    def _closed_periods(self, connection, channel_id: str) -> set[str]:
        rows = connection.execute(
            "SELECT period FROM st_period_closes WHERE channel_id=?", (channel_id,)
        ).fetchall()
        return {row["period"] for row in rows}

    def _resolve_posting(self, connection, channel_id: str, business_date: str) -> tuple[str, str, bool]:
        """关账后的凭证只能追加到其后的首个开放期间，绝不回写已关期间。

        过账期间还必须不早于该渠道已有事件的最大过账期间，保证期间随落库
        顺序单调不减——这样按期间切分重建与按落库顺序折叠才永远一致。
        """

        natural = period_of(business_date)
        closed = self._closed_periods(connection, channel_id)
        posting = natural
        while posting in closed:
            posting = next_period(posting)
        row = connection.execute(
            "SELECT MAX(period) AS max_period FROM st_events WHERE channel_id=?",
            (channel_id,),
        ).fetchone()
        max_existing = row["max_period"] if row else None
        if max_existing and posting < max_existing:
            posting = max_existing
        return posting, natural, posting != natural

    def _channel_events(self, connection, channel_id: str) -> list[Mapping[str, Any]]:
        return connection.execute(
            "SELECT * FROM st_events WHERE channel_id=? ORDER BY seq", (channel_id,)
        ).fetchall()

    def _record_anomaly(self, connection, *, source_id: str, kind: str,
                        source_seq: int, detail: dict[str, Any]) -> None:
        connection.execute(
            "INSERT INTO st_source_anomalies(anomaly_id,source_id,kind,source_seq,detail_json,detected_at) "
            "VALUES(?,?,?,?,?,?)",
            (uuid.uuid4().hex, source_id, kind, source_seq,
             canonical_json(detail), self._now()),
        )

    def record_event(self, *, request_id: str, actor_id: str, kind: str,
                     product_id: str, batch_id: str, channel_id: str,
                     quantity: int, business_date: str,
                     phase: str | None = None,
                     from_location_id: str | None = None,
                     to_location_id: str | None = None,
                     ref_no: str | None = None,
                     final: bool = False,
                     source_id: str | None = None,
                     source_seq: int | None = None) -> EventOutcome:
        business_payload = {
            "kind": kind, "phase": phase, "product_id": product_id, "batch_id": batch_id,
            "channel_id": channel_id, "from_location_id": from_location_id,
            "to_location_id": to_location_id, "ref_no": ref_no, "quantity": quantity,
            "final": bool(final), "business_date": business_date,
        }
        request_payload = {**business_payload, "actor_id": actor_id,
                           "source_id": source_id, "source_seq": source_seq}
        try:
            return self._record_event_inner(
                request_id=request_id, actor_id=actor_id,
                business_payload=business_payload, request_payload=request_payload,
                source_id=source_id, source_seq=source_seq)
        except _SequenceFork as fork:
            # 序列分叉必须留痕：异常与审计在独立事务中落库，不被业务回滚带走。
            with self.database.transaction(immediate=True) as connection:
                self._record_anomaly(
                    connection, source_id=fork.source_id, kind="fork",
                    source_seq=fork.source_seq,
                    detail={"stored_hash": fork.stored_hash, "incoming_hash": fork.incoming_hash})
                append_event(connection, actor_id=actor_id, action="sellthrough.sequence_fork",
                             resource_type="st_source", resource_id=fork.source_id,
                             detail={"source_seq": fork.source_seq}, occurred_at=self._now())
            raise SequenceForkError(
                fork.source_id, fork.source_seq,
                f"来源 {fork.source_id} 序号 {fork.source_seq} 出现序列分叉："
                "同一序号携带了不同内容") from fork

    def _record_event_inner(self, *, request_id: str, actor_id: str,
                            business_payload: dict[str, Any],
                            request_payload: dict[str, Any],
                            source_id: str | None,
                            source_seq: int | None) -> EventOutcome:
        kind = business_payload["kind"]
        channel_id = business_payload["channel_id"]
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            if kind == proj.KIND_STOCKTAKE:
                self._require(actor, *STOCKTAKE_ROLES)
            else:
                self._require(actor, *WRITE_ROLES)
            channel = self._channel(connection, channel_id)
            self._check_org(actor, channel["organization_id"])

            quantity = self._quantity(business_payload["quantity"],
                                      allow_zero=(kind == proj.KIND_STOCKTAKE))
            business_date = self._business_date(business_payload["business_date"])
            from_location_id, to_location_id = self._validate_event_shape(
                connection, kind=kind, phase=business_payload["phase"],
                product_id=business_payload["product_id"], batch_id=business_payload["batch_id"],
                channel_id=channel_id,
                from_location_id=business_payload["from_location_id"],
                to_location_id=business_payload["to_location_id"],
                ref_no=business_payload["ref_no"], final=bool(business_payload["final"]))
            business_payload["from_location_id"] = from_location_id
            business_payload["to_location_id"] = to_location_id
            if self._dispute_open(connection, channel_id, business_payload["product_id"],
                                  business_payload["batch_id"]):
                raise ConflictError("该批次存在未解除的争议，库存已被冻结")
            if (source_id is None) != (source_seq is None):
                raise ValidationError("source_id 与 source_seq 必须同时提供")
            if source_id is not None:
                source_id = self._identifier(source_id, "source_id")
                if not isinstance(source_seq, int) or isinstance(source_seq, bool) or source_seq < 1:
                    raise ValidationError("source_seq 必须是不小于 1 的整数")
            payload_hash = digest(business_payload)
            if source_id is not None:
                # 来源序号是离线事件的强身份：同序号同内容按重放处理，
                # 同序号异内容是序列分叉，优先于请求级幂等判定。
                existing = connection.execute(
                    "SELECT * FROM st_events WHERE source_id=? AND source_seq=?",
                    (source_id, source_seq),
                ).fetchone()
                if existing is not None:
                    if existing["payload_hash"] != payload_hash:
                        raise _SequenceFork(source_id, source_seq,
                                            existing["payload_hash"], payload_hash)
                    return EventOutcome(
                        request_id=request_id, event_id=existing["event_id"],
                        kind=existing["kind"], channel_id=existing["channel_id"],
                        period=existing["period"], original_period=existing["original_period"],
                        is_adjustment=bool(existing["is_adjustment"]), replayed=True)

            def create() -> tuple[str, str, dict[str, Any]]:
                warnings: list[str] = []
                if source_id is not None:
                    cursor = connection.execute(
                        "SELECT last_seq FROM st_source_cursors WHERE source_id=?", (source_id,)
                    ).fetchone()
                    last_seq = cursor["last_seq"] if cursor else 0
                    if source_seq > last_seq + 1:
                        self._record_anomaly(
                            connection, source_id=source_id, kind="gap", source_seq=source_seq,
                            detail={"expected": last_seq + 1, "received": source_seq})
                        warnings.append(f"sequence_gap: 期望序号 {last_seq + 1}，收到 {source_seq}")
                    elif source_seq < last_seq:
                        self._record_anomaly(
                            connection, source_id=source_id, kind="late_fill",
                            source_seq=source_seq,
                            detail={"last_seq": last_seq, "received": source_seq})
                        warnings.append(f"late_fill: 序号 {source_seq} 晚于 {last_seq} 到达")
                period, original_period, is_adjustment = self._resolve_posting(
                    connection, channel_id, business_date)
                if is_adjustment:
                    warnings.append(
                        f"posted_to_later_period: 原期间 {original_period} 已关账，追加到 {period}")
                state = proj.fold(self._channel_events(connection, channel_id))
                event_row = {
                    "kind": kind, "phase": business_payload["phase"],
                    "product_id": business_payload["product_id"],
                    "batch_id": business_payload["batch_id"], "channel_id": channel_id,
                    "from_location_id": from_location_id, "to_location_id": to_location_id,
                    "ref_no": business_payload["ref_no"], "quantity": quantity,
                    "final": 1 if business_payload["final"] else 0,
                }
                deltas, variances = proj.apply_event(state, event_row)
                event_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO st_events(event_id,request_id,kind,phase,product_id,batch_id,channel_id,"
                    "from_location_id,to_location_id,ref_no,quantity,final,business_date,period,"
                    "original_period,is_adjustment,source_id,source_seq,payload_hash,actor_id,recorded_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (event_id, request_id, kind, business_payload["phase"],
                     business_payload["product_id"], business_payload["batch_id"], channel_id,
                     from_location_id, to_location_id, business_payload["ref_no"], quantity,
                     1 if business_payload["final"] else 0, business_date, period,
                     original_period, 1 if is_adjustment else 0, source_id, source_seq,
                     payload_hash, actor_id, self._now()),
                )
                for draft in variances:
                    connection.execute(
                        "INSERT INTO st_variances(variance_id,event_id,channel_id,product_id,batch_id,"
                        "variance_type,quantity,period,detail_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (uuid.uuid4().hex, event_id, channel_id, draft.product_id, draft.batch_id,
                         draft.variance_type, draft.quantity, period,
                         canonical_json(draft.detail), self._now()),
                    )
                if source_id is not None:
                    connection.execute(
                        "INSERT INTO st_source_cursors(source_id,last_seq,updated_at) VALUES(?,?,?) "
                        "ON CONFLICT(source_id) DO UPDATE SET last_seq=MAX(last_seq, excluded.last_seq), "
                        "updated_at=excluded.updated_at",
                        (source_id, source_seq, self._now()),
                    )
                append_event(connection, actor_id=actor_id, action="sellthrough.event_recorded",
                             resource_type="st_event", resource_id=event_id,
                             detail={"kind": kind, "channel_id": channel_id,
                                     "product_id": business_payload["product_id"],
                                     "batch_id": business_payload["batch_id"],
                                     "quantity": quantity, "period": period,
                                     "is_adjustment": is_adjustment,
                                     "payload_hash": payload_hash},
                             occurred_at=self._now())
                stored = {"event_id": event_id, "kind": kind, "channel_id": channel_id,
                          "period": period, "original_period": original_period,
                          "is_adjustment": is_adjustment}
                return "st_event", event_id, _outcome_dict(request_id, stored, False, tuple(warnings))

            response, replayed = self._idempotent(
                connection, request_id=request_id, action="st.record_event",
                payload=request_payload, create=create)
            return EventOutcome(
                request_id=request_id, event_id=response["event_id"], kind=response["kind"],
                channel_id=response["channel_id"], period=response["period"],
                original_period=response["original_period"],
                is_adjustment=bool(response["is_adjustment"]),
                replayed=replayed or response.get("replayed", False),
                warnings=tuple(response.get("warnings", ())),
            )

    # ------------------------------------------------------------------
    # 离线补传
    # ------------------------------------------------------------------
    def backfill_events(self, *, actor_id: str, source_id: str,
                        items: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
        """逐条处理离线来源补传的事件，识别重放、序列分叉与序号缺口。"""

        source_id = self._identifier(source_id, "source_id")
        results: list[BackfillItemResult] = []
        for item in items:
            seq = item.get("source_seq")
            request_id = item.get("request_id") or f"{source_id}:{seq}"
            try:
                outcome = self.record_event(
                    request_id=request_id, actor_id=actor_id,
                    kind=item["kind"], product_id=item["product_id"],
                    batch_id=item["batch_id"], channel_id=item["channel_id"],
                    quantity=item["quantity"], business_date=item["business_date"],
                    phase=item.get("phase"),
                    from_location_id=item.get("from_location_id"),
                    to_location_id=item.get("to_location_id"),
                    ref_no=item.get("ref_no"), final=bool(item.get("final", False)),
                    source_id=source_id, source_seq=seq)
            except ConflictError as exc:
                status = "fork" if exc.code == SequenceForkError.code else "error"
                results.append(BackfillItemResult(seq, status, code=exc.code, message=str(exc)))
                continue
            except (ValidationError, NotFoundError, PermissionDenied) as exc:
                results.append(BackfillItemResult(seq, "error", code=exc.code, message=str(exc)))
                continue
            status = "replayed" if outcome.replayed else "accepted"
            results.append(BackfillItemResult(seq, status, event_id=outcome.event_id,
                                              warnings=outcome.warnings))
        summary = {
            "accepted": sum(1 for r in results if r.status == "accepted"),
            "replayed": sum(1 for r in results if r.status == "replayed"),
            "fork": sum(1 for r in results if r.status == "fork"),
            "error": sum(1 for r in results if r.status == "error"),
        }
        return {"source_id": source_id, "summary": summary,
                "items": [r.as_dict() for r in results]}

    # ------------------------------------------------------------------
    # 争议冻结
    # ------------------------------------------------------------------
    def open_dispute(self, *, request_id: str, actor_id: str, channel_id: str,
                     product_id: str, batch_id: str, reason: str) -> DisputeOutcome:
        payload = {"channel_id": channel_id, "product_id": product_id,
                   "batch_id": batch_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *CONTROL_ROLES)
            channel = self._channel(connection, channel_id)
            self._check_org(actor, channel["organization_id"])
            reason = self._text(reason, "reason", 400)

            def create() -> tuple[str, str, dict[str, Any]]:
                if self._dispute_open(connection, channel_id, product_id, batch_id):
                    raise ConflictError("该批次已存在未解除的争议")
                state = proj.fold(self._channel_events(connection, channel_id))
                frozen = state.combo_sellable(channel_id, product_id, batch_id)
                dispute_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO st_disputes(dispute_id,channel_id,product_id,batch_id,reason,status,"
                    "opened_by,opened_at) VALUES(?,?,?,?,?,'open',?,?)",
                    (dispute_id, channel_id, product_id, batch_id, reason, actor_id, self._now()),
                )
                event_id = self._append_system_event(
                    connection, actor_id=actor_id, kind=proj.KIND_DISPUTE_FREEZE,
                    channel_id=channel_id, product_id=product_id, batch_id=batch_id,
                    quantity=frozen, ref_no=dispute_id)
                append_event(connection, actor_id=actor_id, action="sellthrough.dispute_opened",
                             resource_type="st_dispute", resource_id=dispute_id,
                             detail={"channel_id": channel_id, "product_id": product_id,
                                     "batch_id": batch_id, "frozen_quantity": frozen,
                                     "freeze_event_id": event_id, "reason": reason},
                             occurred_at=self._now())
                return "st_dispute", dispute_id, {
                    "dispute_id": dispute_id, "status": "open", "frozen_quantity": frozen}

            response, replayed = self._idempotent(
                connection, request_id=request_id, action="st.open_dispute",
                payload=payload, create=create)
            return DisputeOutcome(request_id, response["dispute_id"], response["status"],
                                  response["frozen_quantity"], replayed)

    def resolve_dispute(self, *, request_id: str, actor_id: str, dispute_id: str,
                        resolution: str) -> DisputeOutcome:
        payload = {"dispute_id": dispute_id, "resolution": resolution}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *CONTROL_ROLES)
            row = connection.execute("SELECT * FROM st_disputes WHERE dispute_id=?",
                                     (dispute_id,)).fetchone()
            if row is None:
                raise NotFoundError("争议不存在")
            channel = self._channel(connection, row["channel_id"])
            self._check_org(actor, channel["organization_id"])
            resolution = self._text(resolution, "resolution", 400)

            def create() -> tuple[str, str, dict[str, Any]]:
                if row["status"] != "open":
                    raise ConflictError("争议已解除")
                state = proj.fold(self._channel_events(connection, row["channel_id"]))
                released = state.combo_disputed(row["channel_id"], row["product_id"], row["batch_id"])
                connection.execute(
                    "UPDATE st_disputes SET status='resolved', resolved_by=?, resolved_at=?, "
                    "resolution=? WHERE dispute_id=?",
                    (actor_id, self._now(), resolution, dispute_id),
                )
                event_id = self._append_system_event(
                    connection, actor_id=actor_id, kind=proj.KIND_DISPUTE_RELEASE,
                    channel_id=row["channel_id"], product_id=row["product_id"],
                    batch_id=row["batch_id"], quantity=released, ref_no=dispute_id)
                append_event(connection, actor_id=actor_id, action="sellthrough.dispute_resolved",
                             resource_type="st_dispute", resource_id=dispute_id,
                             detail={"released_quantity": released, "release_event_id": event_id,
                                     "resolution": resolution}, occurred_at=self._now())
                return "st_dispute", dispute_id, {
                    "dispute_id": dispute_id, "status": "resolved", "frozen_quantity": released}

            response, replayed = self._idempotent(
                connection, request_id=request_id, action="st.resolve_dispute",
                payload=payload, create=create)
            return DisputeOutcome(request_id, response["dispute_id"], response["status"],
                                  response["frozen_quantity"], replayed)

    def _append_system_event(self, connection, *, actor_id: str, kind: str,
                             channel_id: str, product_id: str, batch_id: str,
                             quantity: int, ref_no: str) -> str:
        """把争议冻结/释放写成库存事件，保证从事件日志即可完整复算。"""

        today = self._today()
        period, original_period, _ = self._resolve_posting(connection, channel_id, today)
        event_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO st_events(event_id,request_id,kind,phase,product_id,batch_id,channel_id,"
            "from_location_id,to_location_id,ref_no,quantity,final,business_date,period,"
            "original_period,is_adjustment,source_id,source_seq,payload_hash,actor_id,recorded_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (event_id, f"system:{event_id[:24]}", kind, None, product_id, batch_id, channel_id,
             None, None, ref_no, quantity, 0, today, period, original_period,
             1 if period != original_period else 0, None, None,
             digest({"kind": kind, "ref_no": ref_no, "quantity": quantity}), actor_id, self._now()),
        )
        return event_id

    # ------------------------------------------------------------------
    # 期间关账
    # ------------------------------------------------------------------
    def close_period(self, *, request_id: str, actor_id: str, channel_id: str,
                     period: str) -> CloseOutcome:
        payload = {"channel_id": channel_id, "period": period}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *CONTROL_ROLES)
            channel = self._channel(connection, channel_id)
            self._check_org(actor, channel["organization_id"])
            period = self._period(period)

            def create() -> tuple[str, str, dict[str, Any]]:
                closed = self._closed_periods(connection, channel_id)
                if period in closed:
                    raise ConflictError(f"期间 {period} 已关账")
                events = self._channel_events(connection, channel_id)
                earlier_open = sorted({e["period"] for e in events
                                       if e["period"] < period and e["period"] not in closed})
                if earlier_open:
                    raise ConflictError(f"更早的期间 {earlier_open[0]} 尚未关账，不能关账 {period}")
                rebuild = proj.rebuild_period(events, period)
                rows = proj.snapshot_rows(channel_id, rebuild)
                snapshot_hash = digest({"channel_id": channel_id, "period": period, "rows": rows})
                connection.execute(
                    "INSERT INTO st_period_closes(channel_id,period,closed_by,closed_at,snapshot_hash) "
                    "VALUES(?,?,?,?,?)",
                    (channel_id, period, actor_id, self._now(), snapshot_hash),
                )
                for row in rows:
                    connection.execute(
                        "INSERT INTO st_period_snapshots(channel_id,period,product_id,batch_id,bucket,"
                        "owner,opening_qty,in_qty,out_qty,closing_qty) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (channel_id, period, row["product_id"], row["batch_id"], row["bucket"],
                         row["owner"], row["opening_qty"], row["in_qty"], row["out_qty"],
                         row["closing_qty"]),
                    )
                append_event(connection, actor_id=actor_id, action="sellthrough.period_closed",
                             resource_type="st_period", resource_id=f"{channel_id}/{period}",
                             detail={"channel_id": channel_id, "period": period,
                                     "snapshot_hash": snapshot_hash, "rows": len(rows)},
                             occurred_at=self._now())
                return "st_period", f"{channel_id}/{period}", {
                    "channel_id": channel_id, "period": period,
                    "snapshot_hash": snapshot_hash, "snapshot_rows": len(rows)}

            response, replayed = self._idempotent(
                connection, request_id=request_id, action="st.close_period",
                payload=payload, create=create)
            return CloseOutcome(request_id, response["channel_id"], response["period"],
                                response["snapshot_hash"], response["snapshot_rows"], replayed)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def inventory_view(self, *, channel_id: str, product_id: str | None = None,
                       batch_id: str | None = None) -> dict[str, Any]:
        """按产品×批次给出可销售、在途、待退、争议四个库存桶与货权。"""

        with self.database.transaction() as connection:
            self._channel(connection, channel_id)
            state = proj.fold(self._channel_events(connection, channel_id))
        combos: dict[tuple[str, str], dict[str, Any]] = {}

        def combo(product: str, batch: str) -> dict[str, Any]:
            return combos.setdefault((product, batch), {
                "product_id": product, "batch_id": batch,
                proj.SELLABLE: 0, proj.IN_TRANSIT: 0, proj.PENDING_RETURN: 0, proj.DISPUTED: 0,
                "owner_breakdown": {proj.OWNER_ENTERPRISE: 0, proj.OWNER_CHANNEL: 0},
                "locations": {},
            })

        for (ch, product, batch, location), qty in sorted(state.sellable.items()):
            item = combo(product, batch)
            item[proj.SELLABLE] += qty
            item["owner_breakdown"][proj.OWNER_CHANNEL] += qty
            item["locations"].setdefault(location, {proj.SELLABLE: 0, proj.DISPUTED: 0})[proj.SELLABLE] += qty
        for (ch, product, batch, location), qty in sorted(state.disputed.items()):
            item = combo(product, batch)
            item[proj.DISPUTED] += qty
            item["owner_breakdown"][proj.OWNER_CHANNEL] += qty
            item["locations"].setdefault(location, {proj.SELLABLE: 0, proj.DISPUTED: 0})[proj.DISPUTED] += qty
        for ship in state.shipments.values():
            item = combo(ship.product_id, ship.batch_id)
            item[proj.IN_TRANSIT] += ship.remaining
            item["owner_breakdown"][ship.owner] += ship.remaining
        for doc in state.returns.values():
            item = combo(doc.product_id, doc.batch_id)
            item[proj.PENDING_RETURN] += doc.remaining
            item["owner_breakdown"][proj.OWNER_CHANNEL] += doc.remaining
        items = []
        for (product, batch), item in sorted(combos.items()):
            if product_id and product != product_id:
                continue
            if batch_id and batch != batch_id:
                continue
            item["total"] = (item[proj.SELLABLE] + item[proj.IN_TRANSIT]
                             + item[proj.PENDING_RETURN] + item[proj.DISPUTED])
            item["locations"] = [{"location_id": loc, **buckets}
                                 for loc, buckets in sorted(item["locations"].items())]
            items.append(item)
        return {"channel_id": channel_id, "generated_at": self._now(), "items": items}

    def sell_through_metrics(self, *, channel_id: str, period: str | None = None,
                             window_days: int = DOI_WINDOW_DAYS) -> dict[str, Any]:
        """给出 sell-in、sell-out、库存天数与差异来源的可复算结果。"""

        if window_days < 1:
            raise ValidationError("window_days 必须为正整数")
        period = self._period(period) if period else period_of(self._today())
        with self.database.transaction() as connection:
            self._channel(connection, channel_id)
            events = self._channel_events(connection, channel_id)
            closed = period in self._closed_periods(connection, channel_id)
            period_events = [e for e in events if e["period"] == period]
            sell_in = sum(e["quantity"] for e in period_events if e["kind"] == proj.KIND_RECEIPT)
            sell_out = sum(e["quantity"] for e in period_events if e["kind"] == proj.KIND_SALE)
            returns_received = sum(
                e["quantity"] for e in period_events
                if e["kind"] == proj.KIND_RETURN and e["phase"] == proj.PHASE_RECEIVED)
            movement_totals: dict[str, int] = {}
            for event in period_events:
                key = event["kind"] if not event["phase"] else f"{event['kind']}:{event['phase']}"
                movement_totals[key] = movement_totals.get(key, 0) + event["quantity"]
            variance_rows = connection.execute(
                "SELECT variance_type, SUM(quantity) AS qty, COUNT(*) AS count FROM st_variances "
                "WHERE channel_id=? AND period=? GROUP BY variance_type",
                (channel_id, period),
            ).fetchall()
            if closed:
                closing_sellable = connection.execute(
                    "SELECT COALESCE(SUM(closing_qty),0) AS qty FROM st_period_snapshots "
                    "WHERE channel_id=? AND period=? AND bucket=?",
                    (channel_id, period, proj.SELLABLE),
                ).fetchone()["qty"]
                verified = self._verify_snapshot(connection, channel_id, period, events)
            else:
                closing_sellable = sum(
                    qty for (ch, _, _, _), qty in
                    proj.fold(events).sellable.items() if ch == channel_id)
                verified = None
        window_end = min(period_end_date(period), self._today())
        window_start = (date.fromisoformat(window_end) - timedelta(days=window_days - 1)).isoformat()
        window_sell_out = sum(
            e["quantity"] for e in events
            if e["kind"] == proj.KIND_SALE and window_start <= e["business_date"] <= window_end)
        avg_daily = window_sell_out / window_days
        days_of_inventory = round(closing_sellable / avg_daily, 2) if avg_daily > 0 else None
        return {
            "channel_id": channel_id,
            "period": period,
            "closed": closed,
            "verified": verified,
            "sell_in": sell_in,
            "sell_out": sell_out,
            "returns_received": returns_received,
            "net_sell_in": sell_in - returns_received,
            "movement_totals": movement_totals,
            "event_count": len(period_events),
            "adjustment_count": sum(1 for e in period_events if e["is_adjustment"]),
            "closing_sellable": closing_sellable,
            "days_of_inventory": {
                "window_days": window_days,
                "window_start": window_start,
                "window_end": window_end,
                "sell_out_in_window": window_sell_out,
                "avg_daily_sell_out": round(avg_daily, 4),
                "closing_sellable": closing_sellable,
                "days": days_of_inventory,
            },
            "variance_sources": [
                {"variance_type": row["variance_type"], "quantity": row["qty"], "count": row["count"]}
                for row in variance_rows
            ],
        }

    def _verify_snapshot(self, connection, channel_id: str, period: str,
                         events: list[Mapping[str, Any]]) -> bool:
        close = connection.execute(
            "SELECT snapshot_hash FROM st_period_closes WHERE channel_id=? AND period=?",
            (channel_id, period),
        ).fetchone()
        if close is None:
            return False
        rebuild = proj.rebuild_period(events, period)
        rows = proj.snapshot_rows(channel_id, rebuild)
        return digest({"channel_id": channel_id, "period": period, "rows": rows}) == close["snapshot_hash"]

    def period_snapshot(self, *, channel_id: str, period: str) -> dict[str, Any]:
        """返回关账快照并用当前事件日志复算校验，保证结果可复算。"""

        period = self._period(period)
        with self.database.transaction() as connection:
            self._channel(connection, channel_id)
            close = connection.execute(
                "SELECT * FROM st_period_closes WHERE channel_id=? AND period=?",
                (channel_id, period),
            ).fetchone()
            if close is None:
                raise NotFoundError("该期间尚未关账")
            rows = connection.execute(
                "SELECT product_id,batch_id,bucket,owner,opening_qty,in_qty,out_qty,closing_qty "
                "FROM st_period_snapshots WHERE channel_id=? AND period=? "
                "ORDER BY product_id,batch_id,bucket,owner",
                (channel_id, period),
            ).fetchall()
            events = self._channel_events(connection, channel_id)
            verified = self._verify_snapshot(connection, channel_id, period, events)
        return {
            "channel_id": channel_id,
            "period": period,
            "closed_by": close["closed_by"],
            "closed_at": close["closed_at"],
            "snapshot_hash": close["snapshot_hash"],
            "verified": verified,
            "rows": [dict(row) for row in rows],
        }

    def list_periods(self, *, channel_id: str) -> dict[str, Any]:
        with self.database.transaction() as connection:
            self._channel(connection, channel_id)
            rows = connection.execute(
                "SELECT * FROM st_period_closes WHERE channel_id=? ORDER BY period",
                (channel_id,),
            ).fetchall()
        return {"channel_id": channel_id, "items": [dict(row) for row in rows]}

    def list_variances(self, *, channel_id: str, period: str | None = None) -> dict[str, Any]:
        with self.database.transaction() as connection:
            self._channel(connection, channel_id)
            query = ("SELECT * FROM st_variances WHERE channel_id=?")
            params: list[Any] = [channel_id]
            if period:
                query += " AND period=?"
                params.append(self._period(period))
            query += " ORDER BY created_at, rowid"
            rows = connection.execute(query, params).fetchall()
        items = []
        for row in rows:
            items.append({
                "variance_id": row["variance_id"], "event_id": row["event_id"],
                "product_id": row["product_id"], "batch_id": row["batch_id"],
                "variance_type": row["variance_type"], "quantity": row["quantity"],
                "period": row["period"], "detail": json.loads(row["detail_json"]),
                "created_at": row["created_at"],
            })
        return {"channel_id": channel_id, "items": items}

    def list_events(self, *, channel_id: str, period: str | None = None,
                    kind: str | None = None, product_id: str | None = None,
                    batch_id: str | None = None) -> dict[str, Any]:
        with self.database.transaction() as connection:
            self._channel(connection, channel_id)
            query = "SELECT * FROM st_events WHERE channel_id=?"
            params: list[Any] = [channel_id]
            if period:
                query += " AND period=?"
                params.append(self._period(period))
            if kind:
                query += " AND kind=?"
                params.append(kind)
            if product_id:
                query += " AND product_id=?"
                params.append(product_id)
            if batch_id:
                query += " AND batch_id=?"
                params.append(batch_id)
            query += " ORDER BY seq"
            rows = connection.execute(query, params).fetchall()
        return {"channel_id": channel_id, "items": [dict(row) for row in rows]}

    def list_disputes(self, *, channel_id: str | None = None,
                      status: str | None = None) -> dict[str, Any]:
        with self.database.transaction() as connection:
            query = "SELECT * FROM st_disputes WHERE 1=1"
            params: list[Any] = []
            if channel_id:
                self._channel(connection, channel_id)
                query += " AND channel_id=?"
                params.append(channel_id)
            if status:
                if status not in ("open", "resolved"):
                    raise ValidationError("status 必须是 open 或 resolved")
                query += " AND status=?"
                params.append(status)
            query += " ORDER BY opened_at, dispute_id"
            rows = connection.execute(query, params).fetchall()
        return {"items": [dict(row) for row in rows]}

    def list_source_anomalies(self, *, source_id: str | None = None) -> dict[str, Any]:
        with self.database.transaction() as connection:
            query = "SELECT * FROM st_source_anomalies"
            params: list[Any] = []
            if source_id:
                query += " WHERE source_id=?"
                params.append(source_id)
            query += " ORDER BY detected_at, rowid"
            rows = connection.execute(query, params).fetchall()
        items = []
        for row in rows:
            items.append({
                "anomaly_id": row["anomaly_id"], "source_id": row["source_id"],
                "kind": row["kind"], "source_seq": row["source_seq"],
                "detail": json.loads(row["detail_json"]), "detected_at": row["detected_at"],
            })
        return {"items": items}


def _outcome_dict(request_id: str, stored: Mapping[str, Any],
                  replayed: bool, warnings: tuple[str, ...]) -> dict[str, Any]:
    return {
        "event_id": stored["event_id"], "kind": stored["kind"],
        "channel_id": stored["channel_id"], "period": stored["period"],
        "original_period": stored["original_period"],
        "is_adjustment": bool(stored["is_adjustment"]),
        "replayed": replayed, "warnings": list(warnings),
    }


class SequenceForkError(ConflictError):
    """同一来源序号携带了不同内容，已拒绝并留痕。"""

    code = "sequence_fork"

    def __init__(self, source_id: str, source_seq: int, message: str) -> None:
        super().__init__(message)
        self.source_id = source_id
        self.source_seq = source_seq


class _SequenceFork(Exception):
    """同一来源序号携带了不同内容。"""

    def __init__(self, source_id: str, source_seq: int,
                 stored_hash: str, incoming_hash: str) -> None:
        super().__init__(f"sequence fork at {source_id}#{source_seq}")
        self.source_id = source_id
        self.source_seq = source_seq
        self.stored_hash = stored_hash
        self.incoming_hash = incoming_hash
