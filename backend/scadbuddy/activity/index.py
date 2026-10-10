"""The activity record's writes and reads (spec 2026-10-09 §3, §5, §6)."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from psycopg import Connection, sql
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool
from starlette.requests import Request

from scadbuddy.activity.models import FINISHED, ActivityRow
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

    async def get(self, activity_id: str) -> ActivityRow | None:
        return await asyncio.to_thread(self._get, activity_id)

    def _get(self, activity_id: str) -> ActivityRow | None:
        with self.pool.connection() as conn:
            found = conn.execute(
                sql.SQL("SELECT {} FROM activity WHERE id = %s AND source <> ''").format(
                    sql.SQL(", ").join(map(sql.Identifier, _COLUMNS))
                ),
                (activity_id,),
            ).fetchone()
        return None if found is None else ActivityRow.model_validate(found)


async def attribute_request(index: ActivityIndex, activity_id: str, request: Request) -> None:
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
