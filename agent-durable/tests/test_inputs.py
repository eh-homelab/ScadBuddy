"""The committed messages a run loads and takes (`ai_durable_inputs`, inputs.py)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from psycopg_pool import AsyncConnectionPool
from temporalio import activity

from scadbuddy_durable.inputs import Inputs, make_input_activities
from scadbuddy_durable.models import LOAD_INPUTS, START_INPUT, Loaded, LoadInputs, Message, StartInput

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
    assert await inputs.start(StartInput(sid, taken)) is True
    # A retry whose first answer was lost: still this run's.
    assert await inputs.start(StartInput(sid, taken)) is True
    assert await inputs.start(StartInput(sid, abandoned)) is False
    assert await inputs.start(StartInput(str(uuid.uuid4()), taken)) is False
    loaded = await inputs.load(LoadInputs(sid, asked=taken))
    assert loaded == Loaded([], "run")


async def test_the_activities_serve_the_workflow(pool: AsyncConnectionPool) -> None:
    sid = await make_session(pool)
    message_id = await commit(pool, sid, "hi")
    load, start = make_input_activities(Inputs(pool))
    assert activity._Definition.must_from_callable(load).name == LOAD_INPUTS
    assert activity._Definition.must_from_callable(start).name == START_INPUT
    assert (await load(LoadInputs(sid))).pending == [Message(message_id, "hi")]
    assert await start(StartInput(sid, message_id)) is True
