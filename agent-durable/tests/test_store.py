from __future__ import annotations

import dataclasses
import json
import uuid
from collections.abc import AsyncIterator
from typing import cast

import pytest
import pytest_asyncio
from claude_agent_sdk import SessionKey, SessionStoreEntry
from claude_agent_sdk.testing.session_store_conformance import run_session_store_conformance
from psycopg_pool import AsyncConnectionPool
from temporalio.testing import ActivityEnvironment

from scadbuddy_durable.store import PostgresSessionStore

pytestmark = pytest.mark.requires_postgres


def _entry(uuid: str, message: str) -> SessionStoreEntry:
    # The SDK's entries are pass-through blobs with arbitrary extra keys.
    return cast(SessionStoreEntry, {"type": "user", "uuid": uuid, "message": message})


@pytest_asyncio.fixture
async def pool(pg_conninfo: str) -> AsyncIterator[AsyncConnectionPool]:
    async with AsyncConnectionPool(pg_conninfo, open=False) as p:
        yield p


async def test_conformance(pool: AsyncConnectionPool) -> None:
    async def make_store() -> PostgresSessionStore:
        async with pool.connection() as conn:
            await conn.execute("TRUNCATE ai_session_entries")
        return PostgresSessionStore(pool)

    await run_session_store_conformance(make_store)


async def test_row_encoding_matches_the_typescript_store(pool: AsyncConnectionPool) -> None:
    store = PostgresSessionStore(pool)
    key: SessionKey = {"project_key": "p", "session_id": "s1"}
    entry = _entry("u1", 'héllo \u0000 {"a": 1}')
    await store.append(key, [entry])
    await store.append(key, [_entry("u1", "dup")])
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT project_key, subpath, uuid, entry FROM ai_session_entries")
        rows = await cur.fetchall()
    assert rows == [("p", "", "u1", json.dumps(entry, separators=(",", ":"), ensure_ascii=False))]
    assert await store.load(key) == [entry]


async def test_load_and_delete_are_keyed_by_project(pool: AsyncConnectionPool) -> None:
    store = PostgresSessionStore(pool)
    a: SessionKey = {"project_key": "a", "session_id": "s"}
    b: SessionKey = {"project_key": "b", "session_id": "s"}
    await store.append(a, [{"type": "user", "uuid": "1"}])
    assert await store.load(b) is None
    await store.append(b, [{"type": "user", "uuid": "2"}])
    await store.delete(a)
    assert await store.load(a) is None
    assert await store.load(b) == [{"type": "user", "uuid": "2"}]


async def test_delete_of_the_main_key_removes_subpaths(pool: AsyncConnectionPool) -> None:
    store = PostgresSessionStore(pool)
    main: SessionKey = {"project_key": "p", "session_id": "s"}
    sub: SessionKey = {"project_key": "p", "session_id": "s", "subpath": "subagents/x"}
    await store.append(main, [{"type": "user"}])
    await store.append(sub, [{"type": "user"}])
    assert await store.list_subkeys({"project_key": "p", "session_id": "s"}) == ["subagents/x"]
    await store.delete(main)
    assert await store.load(sub) is None


async def test_append_in_a_session_activity_records_the_owning_session(pool: AsyncConnectionPool) -> None:
    """forget-subject finds a failed segment's lines by it (its Claude id never reached
    ai_durable_segments)."""
    sid = str(uuid.uuid4())
    store = PostgresSessionStore(pool)
    env = ActivityEnvironment()
    env.info = dataclasses.replace(env.info, workflow_id=f"session-{sid}")
    await env.run(store.append, {"project_key": "p", "session_id": "claude-1"}, [_entry("u1", "hi")])
    # Outside a session's activity (another workflow, or none): no owner.
    env.info = dataclasses.replace(env.info, workflow_id="render-1")
    await env.run(store.append, {"project_key": "p", "session_id": "claude-2"}, [_entry("u2", "hi")])
    await store.append({"project_key": "p", "session_id": "claude-3"}, [_entry("u3", "hi")])
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT session_id, owner_session_id::text FROM ai_session_entries ORDER BY id"
        )
        rows = await cur.fetchall()
    assert rows == [("claude-1", sid), ("claude-2", None), ("claude-3", None)]
