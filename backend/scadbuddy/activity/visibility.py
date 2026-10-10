"""Who may see what (spec 2026-10-09 §7). The browser sees everything; the agent's
headless browser sees its session owner's activity only. Headers label; none of them
authenticates, and the backend trusts no tier header."""

from __future__ import annotations

import asyncio

from psycopg import Connection, errors
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool
from starlette.requests import Request

from scadbuddy.api.agent_actor import AGENT_ACTOR_HEADER, _session_id
from scadbuddy.core.authorship import principal_header

UNKNOWN_SESSION = "session:unknown"


async def session_owner(pool: ConnectionPool[Connection[DictRow]], session_id: str) -> str | None:
    canonical = _session_id(session_id)
    if canonical is None:
        return None

    def read() -> str | None:
        with pool.connection() as conn:
            try:
                with conn.transaction():
                    row = conn.execute(
                        "SELECT owner_kind, owner_id FROM ai_sessions WHERE id = %s", (canonical,)
                    ).fetchone()
            except errors.UndefinedTable:
                return None
        if row is None:
            return None
        return "browser" if row["owner_kind"] == "browser" else principal_header(row["owner_id"])

    return await asyncio.to_thread(read)


async def pinned_principal(
    request: Request, pool: ConnectionPool[Connection[DictRow]]
) -> str | None:
    """The one principal a request may read, or None when it may read all: a request
    with the agent-actor marker reads its session owner's activity, unless that owner is
    the browser user. A marker that names no session is pinned to
    :data:`UNKNOWN_SESSION`, which reads nothing (``ActivityIndex``): the rows attributed
    to it were started by other unresolved sessions."""
    marker = request.headers.get(AGENT_ACTOR_HEADER)
    if marker is None:
        return None
    owner = await session_owner(pool, marker)
    if owner == "browser":
        return None
    return owner or UNKNOWN_SESSION


def readable(pinned: str | None, principal: str | None) -> bool:
    """Whether a request pinned to ``pinned`` may read a row of ``principal``."""
    return pinned is None or (pinned != UNKNOWN_SESSION and principal == pinned)
