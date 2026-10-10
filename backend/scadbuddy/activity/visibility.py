"""Who may see what (spec 2026-10-09 §7). The browser sees everything; the agent's
headless browser sees its session owner's activity only. Headers label; none of them
authenticates, and the backend trusts no tier header."""

from __future__ import annotations

import asyncio

from psycopg import Connection, errors
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool

from scadbuddy.api.agent_actor import _session_id

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
        return "browser" if row["owner_kind"] == "browser" else row["owner_id"]

    return await asyncio.to_thread(read)
