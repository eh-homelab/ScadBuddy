"""The committed messages a run loads and takes (`ai_durable_inputs`, inputs.py)."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from psycopg_pool import AsyncConnectionPool
from temporalio import activity

from scadbuddy_durable.inputs import Inputs, make_input_activities
from scadbuddy_durable.models import (
    LOAD_INPUTS,
    RELEASE_INPUT,
    START_INPUT,
    STOPPED_BEFORE_IT_RAN,
    Loaded,
    LoadInputs,
    Message,
    ReleaseInput,
    StartInput,
)

pytestmark = pytest.mark.requires_postgres


@pytest_asyncio.fixture
async def pool(pg_conninfo: str) -> AsyncIterator[AsyncConnectionPool]:
    async with AsyncConnectionPool(pg_conninfo, open=False) as p:
        yield p


async def make_session(pool: AsyncConnectionPool) -> str:
    sid = str(uuid.uuid4())
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO ai_sessions (id, origin, owner_kind, owner_id, owner_label, creator_kind,"
            " creator_id, status, max_turns, budget_usd, mode) VALUES (%s, 'chat', 'u', 'u', 'u', 'u',"
            " 'u', 'running', 7, 2.5, 'durable')",
            (sid,),
        )
    return sid


async def commit(pool: AsyncConnectionPool, sid: str, text: str, context: str | None = None) -> str:
    message_id = str(uuid.uuid4())
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO ai_durable_inputs (id, session_id, text, context) VALUES (%s, %s, %s, %s)",
            (message_id, sid, text, context),
        )
    return message_id


async def test_load_returns_the_pending_messages_in_order_and_the_asked_status(
    pool: AsyncConnectionPool,
) -> None:
    sid, other = await make_session(pool), await make_session(pool)
    first = await commit(pool, sid, "first", "page: /")
    second = await commit(pool, sid, "second")
    await commit(pool, other, "not this session's")
    inputs = Inputs(pool)
    async with pool.connection() as conn:
        await conn.execute("UPDATE ai_durable_inputs SET note = 'lost: x' WHERE id = %s", (second,))
    assert await inputs.load(LoadInputs(sid)) == Loaded(
        [Message(first, "first", "page: /"), Message(second, "second", None, "lost: x")], None
    )
    assert (await inputs.load(LoadInputs(sid, asked=first))).status == "pending"
    assert (await inputs.load(LoadInputs(sid, asked=str(uuid.uuid4())))).status is None
    # Another session's message is not this session's to ask about.
    assert (await inputs.load(LoadInputs(other, asked=first))).status is None


async def test_start_takes_a_pending_message_once_and_never_an_abandoned_one(
    pool: AsyncConnectionPool,
) -> None:
    sid = await make_session(pool)
    taken, abandoned = await commit(pool, sid, "run me"), await commit(pool, sid, "stopped")
    async with pool.connection() as conn:
        await conn.execute("UPDATE ai_durable_inputs SET status = 'abandoned' WHERE id = %s", (abandoned,))
    inputs = Inputs(pool)
    assert await inputs.start(StartInput(sid, taken, "take-1")) is True
    # A retry of the same take whose first answer was lost: still this take's.
    assert await inputs.start(StartInput(sid, taken, "take-1")) is True
    # Any other take of it (a nudge loaded it back meanwhile, say): refused (round 2, item 1).
    assert await inputs.start(StartInput(sid, taken, "take-2")) is False
    assert await inputs.start(StartInput(sid, abandoned, "take-3")) is False
    assert await inputs.start(StartInput(str(uuid.uuid4()), taken, "take-1")) is False
    loaded = await inputs.load(LoadInputs(sid, asked=taken))
    assert loaded == Loaded([], "run")


async def errors(pool: AsyncConnectionPool, sid: str) -> list[dict[str, object]]:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT event FROM ai_session_events WHERE session_id = %s ORDER BY seq", (sid,)
        )
        rows = [json.loads(e) if isinstance(e, str) else e for (e,) in await cur.fetchall()]
        return [e for e in rows if e["type"] == "error"]


async def test_release_abandons_a_take_a_stop_cut_short_and_logs_it_once(pool: AsyncConnectionPool) -> None:
    # Round 2, item 3: taken and never started, the message is abandoned with an
    # `interrupted` error; released again, or by another take, nothing more is logged.
    sid = await make_session(pool)
    taken, other = await commit(pool, sid, "taken"), await commit(pool, sid, "taken elsewhere")
    inputs = Inputs(pool)
    assert await inputs.start(StartInput(sid, taken, "take-1")) is True
    assert await inputs.start(StartInput(sid, other, "take-2")) is True
    assert await inputs.release(ReleaseInput(sid, taken, "take-1")) is True
    assert await inputs.release(ReleaseInput(sid, taken, "take-1")) is False
    assert await inputs.release(ReleaseInput(sid, other, "take-9")) is False
    assert (await inputs.load(LoadInputs(sid, asked=taken))).status == "abandoned"
    assert (await inputs.load(LoadInputs(sid, asked=other))).status == "run"
    assert await inputs.start(StartInput(sid, taken, "take-1")) is False
    assert await errors(pool, sid) == [
        {"v": 1, "type": "error", "sessionId": sid, "code": "interrupted", "message": STOPPED_BEFORE_IT_RAN}
    ]


async def test_the_activities_serve_the_workflow(pool: AsyncConnectionPool) -> None:
    sid = await make_session(pool)
    message_id = await commit(pool, sid, "hi")
    load, start, release = make_input_activities(Inputs(pool))
    assert activity._Definition.must_from_callable(load).name == LOAD_INPUTS
    assert activity._Definition.must_from_callable(start).name == START_INPUT
    assert activity._Definition.must_from_callable(release).name == RELEASE_INPUT
    assert (await load(LoadInputs(sid))).pending == [Message(message_id, "hi")]
    assert await start(StartInput(sid, message_id, "t")) is True
    assert await release(ReleaseInput(sid, message_id, "t")) is True
