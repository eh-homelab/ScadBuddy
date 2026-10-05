"""The Claude Agent SDK `SessionStore` on Postgres, for durable sessions (#1056).

Python counterpart of agent/src/sessions/store.ts (#300), on the same table,
`ai_session_entries` (agent/src/db/migrations/20260928T0107Z_sessions.sql), and the
same encoding, so each store reads what the other wrote:

  - `entry` is JSON TEXT (`json.dumps(entry, separators=(",", ":"), ensure_ascii=False)`),
    not jsonb, which would reject a "\\u0000" in a tool's output.
  - `id bigserial` orders a key's rows; one INSERT per batch keeps the batch in order.
  - `uuid` holds `entry["uuid"]` when it is a string; a partial unique index on
    (session_id, subpath, uuid) and ON CONFLICT DO NOTHING make it an idempotency key.
    Entries without a uuid are appended without dedup.
  - the main transcript is subpath ''.
  - `list_sessions` returns `mtime` as integer epoch milliseconds.

Derived from the SDK's reference adapter (claude-agent-sdk-python v0.2.160,
examples/session_stores/postgres_session_store.py).

ONE DEPARTURE FROM THE TYPESCRIPT STORE: it ignores `project_key` in load, delete and
list_subkeys, because every classic session has its own cwd. Every durable worker shares
`/srv/agent`, so this store keys by (project_key, session_id, subpath) as the SDK's
contract and its Postgres example do; only this store reads durable transcripts.
"""

from __future__ import annotations

import json

from claude_agent_sdk import (
    SessionKey,
    SessionListSubkeysKey,
    SessionStore,
    SessionStoreEntry,
    SessionStoreListEntry,
)
from psycopg_pool import AsyncConnectionPool


class PostgresSessionStore(SessionStore):
    # Optional and not implemented: the SDK falls back to list_sessions() + load(). Naming the
    # inherited default keeps the class concrete for mypy and is not an override at runtime.
    list_session_summaries = SessionStore.list_session_summaries

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def append(self, key: SessionKey, entries: list[SessionStoreEntry]) -> None:
        if not entries:
            return
        subpath = key.get("subpath") or ""
        rows = [
            (
                key["project_key"],
                key["session_id"],
                subpath,
                entry["uuid"] if isinstance(entry.get("uuid"), str) else None,
                json.dumps(entry, separators=(",", ":"), ensure_ascii=False),
            )
            for entry in entries
        ]
        async with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            # One transaction, rows inserted in array order: ids follow the batch.
            await cur.executemany(
                "INSERT INTO ai_session_entries (project_key, session_id, subpath, uuid, entry)"
                " VALUES (%s, %s, %s, %s, %s)"
                " ON CONFLICT (session_id, subpath, uuid) WHERE uuid IS NOT NULL DO NOTHING",
                rows,
            )

    async def load(self, key: SessionKey) -> list[SessionStoreEntry] | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT entry FROM ai_session_entries"
                " WHERE project_key = %s AND session_id = %s AND subpath = %s ORDER BY id",
                (key["project_key"], key["session_id"], key.get("subpath") or ""),
            )
            rows = await cur.fetchall()
        if not rows:
            return None
        return [json.loads(row[0]) for row in rows]

    async def list_sessions(self, project_key: str) -> list[SessionStoreListEntry]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT session_id, floor(extract(epoch FROM max(created_at)) * 1000)::bigint"
                " FROM ai_session_entries WHERE project_key = %s AND subpath = '' GROUP BY session_id",
                (project_key,),
            )
            rows = await cur.fetchall()
        return [{"session_id": r[0], "mtime": int(r[1])} for r in rows]

    async def delete(self, key: SessionKey) -> None:
        subpath = key.get("subpath")
        async with self._pool.connection() as conn:
            if subpath:
                await conn.execute(
                    "DELETE FROM ai_session_entries"
                    " WHERE project_key = %s AND session_id = %s AND subpath = %s",
                    (key["project_key"], key["session_id"], subpath),
                )
            else:
                # The main transcript takes its subpaths with it.
                await conn.execute(
                    "DELETE FROM ai_session_entries WHERE project_key = %s AND session_id = %s",
                    (key["project_key"], key["session_id"]),
                )

    async def list_subkeys(self, key: SessionListSubkeysKey) -> list[str]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT DISTINCT subpath FROM ai_session_entries"
                " WHERE project_key = %s AND session_id = %s AND subpath <> '' ORDER BY subpath",
                (key["project_key"], key["session_id"]),
            )
            rows = await cur.fetchall()
        return [r[0] for r in rows]
