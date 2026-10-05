from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from psycopg_pool import AsyncConnectionPool
from temporalio.api.common.v1 import Payload
from temporalio.claude_agent_sdk import AgentState, ToolOutcome
from temporalio.converter import DataConverter

from scadbuddy_durable.models import InFlight, SnapshotInput
from scadbuddy_durable.segments import Segments, SessionLimits, Snapshots, make_save_snapshot

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
            " creator_id, status, max_turns, budget_usd) VALUES (%s, 'chat', 'u', 'u', 'u', 'u', 'u',"
            " 'running', 7, 2.5)",
            (sid,),
        )
    return sid


async def test_record_sums_cost_and_counts_segments(pool: AsyncConnectionPool) -> None:
    sid = await make_session(pool)
    segments = Segments(pool)
    await segments.record(sid, 0, 1, "c0", 0.01)
    await segments.record(sid, 0, 2, "c0", 0.01)
    await segments.record(sid, 1, 1, "c1", 0.02)
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT cost_usd, turns FROM ai_sessions WHERE id = %s", (sid,))
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == pytest.approx(0.04)
    assert row[1] == 2


async def test_record_twice_keeps_one_row(pool: AsyncConnectionPool) -> None:
    sid = await make_session(pool)
    segments = Segments(pool)
    await segments.record(sid, 0, 1, "c0", 0.01)
    await segments.record(sid, 0, 1, "c0", 0.01)
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT count(*) FROM ai_durable_segments WHERE session_id = %s", (sid,))
        row = await cur.fetchone()
        cur = await conn.execute("SELECT cost_usd FROM ai_sessions WHERE id = %s", (sid,))
        cost = await cur.fetchone()
    assert row == (1,)
    assert cost == (pytest.approx(0.01),)


async def test_limits(pool: AsyncConnectionPool) -> None:
    sid = await make_session(pool)
    segments = Segments(pool)
    await segments.record(sid, 0, 1, "c0", 0.5)
    assert await segments.limits(sid) == SessionLimits(budget_usd=2.5, cost_usd=0.5, max_turns=7)


async def test_snapshot_version_never_moves_backwards(pool: AsyncConnectionPool) -> None:
    sid = await make_session(pool)
    snaps = Snapshots(pool)
    assert await snaps.latest(sid) is None
    for version in (3, 5, 4):
        state = AgentState(session_id=f"v{version}", segments=version)
        await snaps.save(SnapshotInput(sid, state, [InFlight("t1", "tool", "started")], version))
    row = await snaps.latest(sid)
    assert row is not None
    assert row.version == 5
    assert row.in_flight == [InFlight("t1", "tool", "started")]
    assert row.saved_at is not None
    converter = DataConverter.default.payload_converter
    payload = Payload(metadata={"encoding": b"json/plain"}, data=row.state_json.encode())
    [back] = converter.from_payloads([payload], [AgentState])
    assert back.session_id == "v5"


async def test_agent_state_with_pending_outcomes_round_trips(pool: AsyncConnectionPool) -> None:
    sid = await make_session(pool)
    state = AgentState(
        session_id="claude-1",
        checkpoint="chk",
        segment_index=2,
        pending={
            "a": ToolOutcome(content={"x": [1, "é"]}, is_error=False),
            "b": ToolOutcome(content="boom", is_error=True, blocks=[{"type": "text", "text": "hi"}]),
        },
        recent_call_ids=["a", "b"],
        segments=2,
        tool_calls=4,
    )
    snaps = Snapshots(pool)
    await snaps.save(SnapshotInput(sid, state, [], 1))
    row = await snaps.latest(sid)
    assert row is not None
    payload = Payload(metadata={"encoding": b"json/plain"}, data=row.state_json.encode())
    [back] = DataConverter.default.payload_converter.from_payloads([payload], [AgentState])
    assert back == state


async def test_save_snapshot_activity(pool: AsyncConnectionPool) -> None:
    sid = await make_session(pool)
    snaps = Snapshots(pool)
    activity_fn = make_save_snapshot(snaps)
    await activity_fn(SnapshotInput(sid, AgentState(segments=1), [], 1))
    row = await snaps.latest(sid)
    assert row is not None and row.version == 1
