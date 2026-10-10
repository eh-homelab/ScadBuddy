"""The activity record's writes and reads (spec 2026-10-09 §3, §5, §6)."""

from __future__ import annotations

import asyncio
import base64
import binascii
import builtins
import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg import Connection, errors, sql
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool
from starlette.requests import Request

from scadbuddy.activity.models import FINISHED, ActivityRow, ActivityStatus
from scadbuddy.activity.visibility import UNKNOWN_SESSION, session_owner
from scadbuddy.api.agent_actor import AGENT_ACTOR_HEADER
from scadbuddy.core.authorship import current_author
from scadbuddy.core.events import ActivityEvent
from scadbuddy.core.metrics import Metrics
from scadbuddy.render.pg_store import TransactionalEvents

logger = logging.getLogger(__name__)

_WRITTEN = (
    "kind",
    "subject",
    "status",
    "title",
    "parent_id",
    "facets",
    "detail",
    "source",
    "source_id",
    "workflow_id",
    "run_id",
    "traceparent",
)
_COLUMNS = (
    "id",
    "kind",
    "principal",
    "session",
    "via_tool",
    "subject",
    "status",
    "title",
    "parent_id",
    "facets",
    "detail",
    "source",
    "source_id",
    "workflow_id",
    "run_id",
    "traceparent",
    "created_at",
    "updated_at",
    "finished_at",
)


#: ``ai_operations.status`` (agent ``src/operations/``, its CHECK); anything else raises
#: inside ``upsert_in``'s guard, so it is logged and counted, never lost in a listener task.
_AGENT_STATUS: dict[str, ActivityStatus] = {
    "running": "running",
    "succeeded": "succeeded",
    "failed": "failed",
}


DEFAULT_LIMIT = 50
MAX_LIMIT = 200
#: The window ``facets`` counts over when its caller names none.
FACETS_WINDOW = timedelta(days=7)


@dataclass(frozen=True)
class ActivityFilter:
    """What ``list`` keeps; every field set must match, a tuple matches any member."""

    kinds: tuple[str, ...] = ()
    statuses: tuple[str, ...] = ()
    principal: str | None = None
    session: str | None = None
    via_tool: str | None = None
    subject: str | None = None
    parent: str | None = None
    #: A substring of the title, case-insensitive.
    q: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    #: Facet name to value, each matched as ``facets @> {name: value}``.
    facets: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Count:
    value: str
    count: int


@dataclass(frozen=True)
class Counts:
    principals: list[Count]
    tools: list[Count]
    subjects: list[Count]


def encode_cursor(row: ActivityRow) -> str:
    assert row.created_at is not None  # a read row has its timestamps
    raw = f"{row.created_at.isoformat()}|{row.id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    """The position ``encode_cursor`` wrote; ValueError for anything else."""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
    except (binascii.Error, UnicodeError) as error:
        raise ValueError("not a cursor") from error
    created, sep, activity_id = raw.partition("|")
    if not sep or not activity_id:
        raise ValueError("not a cursor")
    at = datetime.fromisoformat(created)
    if at.tzinfo is None:
        raise ValueError("not a cursor")
    return at, activity_id


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _visible(pinned: str | None) -> list[sql.Composable]:
    """Placeholders are never read; a pinned request reads its principal's rows only,
    and one pinned to an unknown session reads none."""
    where: list[sql.Composable] = [sql.SQL("source <> ''")]
    if pinned == UNKNOWN_SESSION:
        where.append(sql.SQL("false"))
    elif pinned is not None:
        where.append(sql.SQL("principal = {}").format(sql.Literal(pinned)))
    return where


def _where(conditions: list[sql.Composable]) -> sql.Composable:
    return sql.SQL(" AND ").join(conditions)


_SELECT = sql.SQL("SELECT {} FROM activity").format(
    sql.SQL(", ").join(map(sql.Identifier, _COLUMNS))
)


@dataclass(frozen=True)
class Attribution:
    principal: str
    session: str | None
    via_tool: str | None


class ActivityIndex:
    def __init__(
        self,
        pool: ConnectionPool[Connection[DictRow]],
        *,
        events: TransactionalEvents | None,
        metrics: Metrics | None = None,
    ) -> None:
        self.pool = pool
        self.events = events
        self.metrics = metrics

    def upsert_in(
        self,
        conn: Connection[Any],
        row: ActivityRow | Callable[[], ActivityRow],
        *,
        attribution: Attribution | None = None,
    ) -> None:
        """Write ``row`` in the caller's transaction, under a savepoint: a failure is
        logged and counted, and the caller's own write still commits (Review Focus 4).
        A callable is built inside the guard, so a read or a mapping that raises cannot
        abort the source's write either."""
        activity_id = row.id if isinstance(row, ActivityRow) else None
        try:
            with conn.transaction():
                if not isinstance(row, ActivityRow):
                    row = row()
                activity_id = row.id
                values = row.model_dump(include=set(_WRITTEN))
                values["facets"] = json.dumps(values["facets"])
                values["detail"] = json.dumps(values["detail"])
                finished = row.status in FINISHED
                found = conn.execute(
                    sql.SQL(
                        "INSERT INTO activity (id, {cols}, finished_at) VALUES (%(id)s, {vals}, "
                        "CASE WHEN %(finished)s THEN now() END) "
                        "ON CONFLICT (id) DO UPDATE SET {sets}, updated_at = now(), "
                        "finished_at = CASE WHEN %(finished)s "
                        "THEN coalesce(activity.finished_at, now()) END "
                        "RETURNING principal"
                    ).format(
                        cols=sql.SQL(", ").join(map(sql.Identifier, _WRITTEN)),
                        vals=sql.SQL(", ").join(sql.Placeholder(c) for c in _WRITTEN),
                        sets=sql.SQL(", ").join(
                            sql.SQL("{c} = EXCLUDED.{c}").format(c=sql.Identifier(c))
                            for c in _WRITTEN
                        ),
                    ),
                    {**values, "id": row.id, "finished": finished},
                ).fetchone()
                if attribution is not None:
                    found = conn.execute(
                        "UPDATE activity SET principal = coalesce(principal, %s),"
                        " session = coalesce(session, %s), via_tool = coalesce(via_tool, %s)"
                        " WHERE id = %s RETURNING principal",
                        (attribution.principal, attribution.session, attribution.via_tool, row.id),
                    ).fetchone()
                if self.events is not None:
                    principal = None if found is None else found["principal"]
                    self.events.publish_in(
                        conn, ActivityEvent(activity_id=row.id, principal=principal)
                    )
        except Exception:
            logger.exception("an activity write failed", extra={"activity_id": activity_id})
            if self.metrics is not None:
                self.metrics.activity_write_failures.inc()

    async def attribute(
        self, activity_id: str, *, principal: str, session: str | None, via_tool: str | None
    ) -> None:
        await asyncio.to_thread(self._attribute, activity_id, principal, session, via_tool)

    def _attribute(
        self, activity_id: str, principal: str, session: str | None, via_tool: str | None
    ) -> None:
        with self.pool.connection() as conn, conn.transaction():
            conn.execute(
                "INSERT INTO activity"
                " (id, kind, principal, session, via_tool, status, title, source, source_id)"
                " VALUES (%s, '', %s, %s, %s, 'queued', '', '', '')"
                " ON CONFLICT (id) DO UPDATE SET"
                " principal = coalesce(activity.principal, EXCLUDED.principal),"
                " session = coalesce(activity.session, EXCLUDED.session),"
                " via_tool = coalesce(activity.via_tool, EXCLUDED.via_tool)",
                (activity_id, principal, session, via_tool),
            )
            if self.events is not None:
                self.events.publish_in(
                    conn, ActivityEvent(activity_id=activity_id, principal=principal)
                )

    async def ingest_agent_operation(self, operation_id: str) -> None:
        """Mirror the agent's ``ai_operations`` row (spec §5). A no-op when the agent's
        tables, or the attribution columns its migration adds, are not there."""
        await asyncio.to_thread(self._ingest_agent_operation, operation_id)

    def _ingest_agent_operation(self, operation_id: str) -> None:
        with self.pool.connection() as conn, conn.transaction():
            try:
                with conn.transaction():
                    op = conn.execute(
                        "SELECT id, kind, subject, status, request, result, error, principal,"
                        " session, via_tool, workflow_id, workflow_run_id"
                        " FROM ai_operations WHERE id = %s",
                        (operation_id,),
                    ).fetchone()
            except (errors.UndefinedTable, errors.UndefinedColumn):
                return
            if op is None:
                return
            self.upsert_in(
                conn,
                lambda: ActivityRow(
                    id=f"agent_operation:{op['id']}",
                    kind=f"agent.{op['kind']}",
                    subject=op["subject"] or "",
                    status=_AGENT_STATUS[op["status"]],
                    title=(
                        f"{op['kind'].replace('_', ' ').capitalize()} {op['subject'] or ''}"
                    ).strip(),
                    source="ai_operations",
                    source_id=op["id"],
                    workflow_id=op["workflow_id"],
                    run_id=op["workflow_run_id"],
                    detail={
                        "request": op["request"],
                        "result": op["result"],
                        "error": op["error"],
                    },
                ),
                attribution=Attribution(
                    op["principal"] or "browser", op["session"], op["via_tool"]
                ),
            )

    async def get(self, activity_id: str) -> ActivityRow | None:
        return await asyncio.to_thread(self._get, activity_id)

    def _get(self, activity_id: str) -> ActivityRow | None:
        with self.pool.connection() as conn:
            found = conn.execute(
                sql.SQL("{} WHERE id = %s AND source <> ''").format(_SELECT), (activity_id,)
            ).fetchone()
        return None if found is None else ActivityRow.model_validate(found)

    async def list(
        self,
        filters: ActivityFilter,
        *,
        pinned: str | None,
        cursor: tuple[datetime, str] | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> tuple[list[ActivityRow], str | None]:
        """Newest first, and the cursor of the next page (None on the last). ``pinned``
        is ``visibility.pinned_principal``'s answer: a ``principal`` filter that differs
        from it matches nothing."""
        return await asyncio.to_thread(self._list, filters, pinned, cursor, limit)

    def _list(
        self,
        filters: ActivityFilter,
        pinned: str | None,
        cursor: tuple[datetime, str] | None,
        limit: int,
    ) -> tuple[builtins.list[ActivityRow], str | None]:
        if not 1 <= limit <= MAX_LIMIT:
            raise ValueError(f"limit must be 1 to {MAX_LIMIT}")
        where = _visible(pinned)
        params: list[Any] = []

        def add(condition: str, *values: Any) -> None:
            where.append(sql.SQL(condition))  # fixed text from this module only
            params.extend(values)

        if filters.kinds:
            add("kind = ANY(%s)", list(filters.kinds))
        if filters.statuses:
            add("status = ANY(%s)", list(filters.statuses))
        for column, value in (
            ("principal", filters.principal),
            ("session", filters.session),
            ("via_tool", filters.via_tool),
            ("subject", filters.subject),
            ("parent_id", filters.parent),
        ):
            if value is not None:
                where.append(sql.SQL("{} = %s").format(sql.Identifier(column)))
                params.append(value)
        if filters.q is not None:
            add("title ILIKE '%%' || %s || '%%'", _escape_like(filters.q))
        if filters.since is not None:
            add("created_at >= %s", filters.since)
        if filters.until is not None:
            add("created_at < %s", filters.until)
        for name, value in filters.facets.items():
            add("facets @> %s::jsonb", json.dumps({name: value}))
        if cursor is not None:
            add("(created_at, id) < (%s, %s)", *cursor)
        params.append(limit + 1)
        with self.pool.connection() as conn:
            found = conn.execute(
                sql.SQL("{} WHERE {} ORDER BY created_at DESC, id DESC LIMIT %s").format(
                    _SELECT, _where(where)
                ),
                params,
            ).fetchall()
        rows = [ActivityRow.model_validate(r) for r in found[:limit]]
        return rows, encode_cursor(rows[-1]) if len(found) > limit else None

    async def children(self, activity_id: str, *, pinned: str | None) -> builtins.list[ActivityRow]:
        return await asyncio.to_thread(self._children, activity_id, pinned)

    def _children(self, activity_id: str, pinned: str | None) -> builtins.list[ActivityRow]:
        where = [*_visible(pinned), sql.SQL("parent_id = %s")]
        with self.pool.connection() as conn:
            found = conn.execute(
                sql.SQL("{} WHERE {} ORDER BY created_at, id").format(_SELECT, _where(where)),
                (activity_id,),
            ).fetchall()
        return [ActivityRow.model_validate(r) for r in found]

    async def counts(self, *, since: datetime | None, pinned: str | None) -> Counts:
        """The principals, tools and subjects present since ``since`` (default the last
        ``FACETS_WINDOW``), most frequent first."""
        return await asyncio.to_thread(self._counts, since, pinned)

    def _counts(self, since: datetime | None, pinned: str | None) -> Counts:
        start = since if since is not None else datetime.now(UTC) - FACETS_WINDOW
        where = _where([*_visible(pinned), sql.SQL("created_at >= %s")])
        counted: dict[str, list[Count]] = {}
        with self.pool.connection() as conn:
            for column, blank in (("principal", None), ("via_tool", None), ("subject", "")):
                present = sql.SQL("{} IS NOT NULL").format(sql.Identifier(column))
                if blank is not None:
                    present = sql.SQL("{} <> ''").format(sql.Identifier(column))
                found = conn.execute(
                    sql.SQL(
                        "SELECT {c} AS value, count(*) AS count FROM activity"
                        " WHERE {where} AND {present} GROUP BY {c} ORDER BY count DESC, {c}"
                    ).format(c=sql.Identifier(column), where=where, present=present),
                    (start,),
                ).fetchall()
                counted[column] = [Count(r["value"], r["count"]) for r in found]
        return Counts(counted["principal"], counted["via_tool"], counted["subject"])


async def attribute_request(index: ActivityIndex, activity_id: str, request: Request) -> None:
    """Who started ``activity_id``: the agent author, the headless-browser session's owner,
    or the browser user (spec §3). Never raises: the start it follows is already accepted,
    so a failure here is logged and counted, not a 500."""
    try:
        await _attribute_request(index, activity_id, request)
    except Exception:
        logger.exception("an activity attribution failed", extra={"activity_id": activity_id})
        if index.metrics is not None:
            index.metrics.activity_write_failures.inc()


async def _attribute_request(index: ActivityIndex, activity_id: str, request: Request) -> None:
    """Who started ``activity_id``: the agent author, the headless-browser session's owner,
    or the browser user (spec §3)."""
    author = current_author()
    session_marker = request.headers.get(AGENT_ACTOR_HEADER)
    if author is not None and author.principal is not None:
        await index.attribute(
            activity_id, principal=author.principal, session=author.session, via_tool=author.tool
        )
    elif session_marker is not None:
        principal = await session_owner(index.pool, session_marker)
        tool = None if author is None else author.tool
        await index.attribute(
            activity_id,
            principal=principal or UNKNOWN_SESSION,
            session=session_marker,
            via_tool=tool,
        )
    else:
        await index.attribute(activity_id, principal="browser", session=None, via_tool=None)
