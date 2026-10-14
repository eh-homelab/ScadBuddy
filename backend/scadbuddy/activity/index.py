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
from functools import partial
from typing import Any

from psycopg import Connection, errors, sql
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool

from scadbuddy.activity.models import FINISHED, ActivityRow, ActivityStatus
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


#: Stored-setting defaults (``activity_ttl_seconds``, ``activity_repair_window_seconds``).
ACTIVITY_TTL_DEFAULT = 30 * 86400.0
ACTIVITY_REPAIR_WINDOW_DEFAULT = 86400.0
#: How far inside the TTL the repair window stays (`main.py` `_sweep_activity_logged`).
ACTIVITY_PRUNE_MARGIN = 3600.0
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
    if not sep or not activity_id or "\x00" in activity_id:
        raise ValueError("not a cursor")
    at = datetime.fromisoformat(created)
    if at.tzinfo is None:
        raise ValueError("not a cursor")
    return at, activity_id


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


#: Placeholders are never read.
_VISIBLE = sql.SQL("source <> ''")


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
        restore: bool = False,
    ) -> bool:
        """Write ``row`` in the caller's transaction, under a savepoint: a failure is
        logged and counted, and the caller's own write still commits (Review Focus 4).
        A callable is built inside the guard, so a read or a mapping that raises cannot
        abort the source's write either. ``restore`` is the repair's write: it takes the
        row's ``created_at``/``finished_at`` (the source's, never now()) and touches only
        a placeholder, so a row another writer landed meanwhile is left alone. True when
        a row was written."""
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
                times = ", created_at, finished_at" if restore else ", finished_at"
                finish_value = (
                    "%(row_finished_at)s" if restore else "CASE WHEN %(finished)s THEN now() END"
                )
                created_value = ", %(row_created_at)s" if restore else ""
                update_times = (
                    ", created_at = EXCLUDED.created_at, finished_at = EXCLUDED.finished_at"
                    if restore
                    else ", finished_at = CASE WHEN %(finished)s"
                    " THEN coalesce(activity.finished_at, now()) END"
                )
                # A late or retried write never reopens a finished row.
                guard = (
                    " WHERE activity.source = ''"
                    if restore
                    else " WHERE activity.finished_at IS NULL OR EXCLUDED.finished_at IS NOT NULL"
                )
                found = conn.execute(
                    sql.SQL(
                        "INSERT INTO activity (id, {cols}"
                        + times
                        + ") VALUES (%(id)s, {vals}"
                        + created_value
                        + ", "
                        + finish_value
                        + ") ON CONFLICT (id) DO UPDATE SET {sets}, updated_at = now()"
                        + update_times
                        + guard
                        + " RETURNING principal"
                    ).format(
                        cols=sql.SQL(", ").join(map(sql.Identifier, _WRITTEN)),
                        vals=sql.SQL(", ").join(sql.Placeholder(c) for c in _WRITTEN),
                        sets=sql.SQL(", ").join(
                            sql.SQL("{c} = EXCLUDED.{c}").format(c=sql.Identifier(c))
                            for c in _WRITTEN
                        ),
                    ),
                    {
                        **values,
                        "id": row.id,
                        "finished": finished,
                        "row_created_at": row.created_at,
                        "row_finished_at": row.finished_at,
                    },
                ).fetchone()
                if found is None:
                    # A restore found a row that is no longer a placeholder, or a write
                    # would have reopened a finished one.
                    return False
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
            return True
        except Exception:
            logger.exception("an activity write failed", extra={"activity_id": activity_id})
            if self.metrics is not None:
                self.metrics.activity_write_failures.inc()
            return False

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

    def _ingest_agent_operation(self, operation_id: str, *, restore: bool = False) -> bool:
        with self.pool.connection() as conn, conn.transaction():
            try:
                with conn.transaction():
                    op = conn.execute(
                        "SELECT id, kind, subject, status, request, result, error, principal,"
                        " session, via_tool, workflow_id, workflow_run_id, created_at, finished_at"
                        " FROM ai_operations WHERE id = %s",
                        (operation_id,),
                    ).fetchone()
            except (errors.UndefinedTable, errors.UndefinedColumn):
                return False
            if op is None:
                return False
            return self.upsert_in(
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
                    created_at=op["created_at"],
                    finished_at=op["finished_at"],
                    detail={
                        "request": op["request"],
                        "result": op["result"],
                        "error": op["error"],
                    },
                ),
                attribution=Attribution(
                    op["principal"] or "browser", op["session"], op["via_tool"]
                ),
                restore=restore,
            )

    async def prune(self, ttl: float, placeholder_ttl: float) -> int:
        """Delete finished rows older than ``ttl`` seconds and placeholders older than
        ``placeholder_ttl`` (long enough for ``repair`` to fill one, and no longer)."""
        return await asyncio.to_thread(self._prune, ttl, placeholder_ttl)

    def _prune(self, ttl: float, placeholder_ttl: float) -> int:
        with self.pool.connection() as conn:
            return conn.execute(
                "DELETE FROM activity WHERE finished_at < now() - make_interval(secs => %(ttl)s)"
                " OR (source = '' AND created_at < now() - make_interval(secs => %(placeholder)s))",
                {"ttl": ttl, "placeholder": placeholder_ttl},
            ).rowcount

    async def repair(self, window: float) -> int:
        """Restore, from the sources, the rows changed within ``window`` seconds whose
        activity write was lost: no row, or only a placeholder (whose attribution stays)."""
        return await asyncio.to_thread(self._repair, window)

    def _repair(self, window: float) -> int:
        from scadbuddy.bambuddy.activity import row_of as print_row_of
        from scadbuddy.bambuddy.runs import PrintRun
        from scadbuddy.operations.activity import row_of as operation_row_of
        from scadbuddy.operations.store import Operation
        from scadbuddy.render.activity import row_of as render_row_of
        from scadbuddy.render.job_models import Job
        from scadbuddy.render.projection import PROJECTION_COLUMNS

        def timed(row: ActivityRow, found: Mapping[str, Any]) -> ActivityRow:
            row.created_at = found["created_at"]
            row.finished_at = found["finished_at"]
            return row

        sources: list[tuple[str, str, str, Callable[[Any], ActivityRow]]] = [
            (
                "render:",
                "render_jobs",
                ", ".join(f"r.{c}" for c in PROJECTION_COLUMNS),
                lambda f: timed(
                    render_row_of(Job.model_validate({c: f[c] for c in PROJECTION_COLUMNS})), f
                ),
            ),
            (
                "operation:",
                "operations",
                "r.id, r.kind, r.subject, r.status, r.result, r.error, r.created_at,"
                " r.finished_at, r.workflow_id, r.workflow_run_id",
                lambda f: timed(
                    operation_row_of(
                        Operation.model_validate(f), f["workflow_id"], f["workflow_run_id"]
                    ),
                    f,
                ),
            ),
            (
                "print_run:",
                "print_runs",
                "r.id, r.output_id, r.status, r.created_at, r.finished_at, r.result, r.error,"
                " (r.status = 'failed' AND r.enqueue_attempted) AS may_have_queued,"
                " r.workflow_id, r.workflow_run_id, r.slug",
                lambda f: timed(
                    print_row_of(
                        PrintRun.model_validate(f),
                        f["slug"] or "",
                        f["workflow_id"],
                        f["workflow_run_id"],
                    ),
                    f,
                ),
            ),
        ]
        restored = 0
        for prefix, table, columns, build in sources:
            try:
                restored += self._repair_source(window, prefix, table, columns, build)
            except Exception:
                logger.exception("an activity repair failed", extra={"table": table})
                self._count_failure()
        try:
            with self.pool.connection() as conn:
                agent_ids = [
                    r["id"]
                    for r in conn.execute(
                        self._missing("agent_operation:", "ai_operations", "r.id"),
                        {"window": window},
                    ).fetchall()
                ]
        except errors.UndefinedTable:
            agent_ids = []
        except Exception:
            logger.exception("an activity repair failed", extra={"table": "ai_operations"})
            self._count_failure()
            agent_ids = []
        for agent_id in agent_ids:
            try:
                restored += self._ingest_agent_operation(agent_id, restore=True)
            except Exception:
                logger.exception("an activity repair failed", extra={"activity_id": agent_id})
                self._count_failure()
        return restored

    def _count_failure(self) -> None:
        if self.metrics is not None:
            self.metrics.activity_write_failures.inc()

    @staticmethod
    def _missing(prefix: str, table: str, columns: str) -> sql.Composed:
        return sql.SQL(
            "SELECT {columns} FROM {table} r LEFT JOIN activity a ON a.id = {prefix} || r.id"
            " WHERE (a.id IS NULL OR a.source = '')"
            " AND (r.created_at >= now() - make_interval(secs => %(window)s)"
            " OR r.finished_at >= now() - make_interval(secs => %(window)s))"
        ).format(columns=sql.SQL(columns), table=sql.Identifier(table), prefix=sql.Literal(prefix))

    def _repair_source(
        self,
        window: float,
        prefix: str,
        table: str,
        columns: str,
        build: Callable[[Any], ActivityRow],
    ) -> int:
        restored = 0
        with self.pool.connection() as conn:
            found = conn.execute(
                self._missing(prefix, table, columns), {"window": window}
            ).fetchall()
            for row in found:
                restored += self.upsert_in(conn, partial(build, row), restore=True)
        return restored

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
        cursor: tuple[datetime, str] | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> tuple[list[ActivityRow], str | None]:
        """Newest first, and the cursor of the next page (None on the last)."""
        return await asyncio.to_thread(self._list, filters, cursor, limit)

    def _list(
        self,
        filters: ActivityFilter,
        cursor: tuple[datetime, str] | None,
        limit: int,
    ) -> tuple[builtins.list[ActivityRow], str | None]:
        if not 1 <= limit <= MAX_LIMIT:
            raise ValueError(f"limit must be 1 to {MAX_LIMIT}")
        where: builtins.list[sql.Composable] = [_VISIBLE]
        params: builtins.list[Any] = []

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

    async def children(self, activity_id: str) -> builtins.list[ActivityRow]:
        return await asyncio.to_thread(self._children, activity_id)

    def _children(self, activity_id: str) -> builtins.list[ActivityRow]:
        where: builtins.list[sql.Composable] = [_VISIBLE, sql.SQL("parent_id = %s")]
        with self.pool.connection() as conn:
            found = conn.execute(
                sql.SQL("{} WHERE {} ORDER BY created_at, id").format(_SELECT, _where(where)),
                [activity_id],
            ).fetchall()
        return [ActivityRow.model_validate(r) for r in found]

    async def counts(self, *, since: datetime | None) -> Counts:
        """The principals, tools and subjects present since ``since`` (default the last
        ``FACETS_WINDOW``), most frequent first."""
        return await asyncio.to_thread(self._counts, since)

    def _counts(self, since: datetime | None) -> Counts:
        start = since if since is not None else datetime.now(UTC) - FACETS_WINDOW
        where = _where([_VISIBLE, sql.SQL("created_at >= %s")])
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
                    [start],
                ).fetchall()
                counted[column] = [Count(r["value"], r["count"]) for r in found]
        return Counts(counted["principal"], counted["via_tool"], counted["subject"])


async def attribute_request(index: ActivityIndex, activity_id: str) -> None:
    """Who started ``activity_id``: the agent author, or the browser user (spec §3).
    Never raises: the start it follows is already accepted, so a failure here is logged
    and counted, not a 500."""
    try:
        await _attribute_request(index, activity_id)
    except Exception:
        logger.exception("an activity attribution failed", extra={"activity_id": activity_id})
        if index.metrics is not None:
            index.metrics.activity_write_failures.inc()


async def _attribute_request(index: ActivityIndex, activity_id: str) -> None:
    author = current_author()
    if author is not None and author.principal is not None:
        await index.attribute(
            activity_id, principal=author.principal, session=author.session, via_tool=author.tool
        )
    else:
        await index.attribute(activity_id, principal="browser", session=None, via_tool=None)
