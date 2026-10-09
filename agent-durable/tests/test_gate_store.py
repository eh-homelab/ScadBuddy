"""open_input and resolve_input against the agent's own tables (spec 2026-10-01 §6.6)."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from temporalio.testing import ActivityEnvironment

from scadbuddy_durable.gate.activities import GateActivities
from scadbuddy_durable.gate.ids import durable_request_id
from scadbuddy_durable.gate.store import SYSTEM, OpenInput, ResolveInput, open_input, resolve_input

pytestmark = pytest.mark.requires_postgres

ME = {"kind": "browser", "id": "browser", "label": "You"}

Conn = psycopg.AsyncConnection[Any]


async def _session(conn: Conn) -> str:
    sid = str(uuid.uuid4())
    await conn.execute(
        """
        INSERT INTO ai_sessions
          (id, origin, owner_kind, owner_id, owner_label, creator_kind, creator_id,
           status, max_turns, budget_usd, mode)
        VALUES (%s, 'chat', 'browser', 'browser', 'You', 'browser', 'browser',
                'running', 10, 1, 'durable')
        """,
        (sid,),
    )
    return sid


def _open(
    sid: str, run: str = "run-1", kind: str = "approval", tool_use: str = "toolu_1"
) -> OpenInput:
    expires = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
    if kind == "approval":
        return OpenInput(
            request_id=durable_request_id(sid, run, tool_use),
            session_id=sid,
            workflow_id=f"session-{sid}",
            workflow_run_id=run,
            kind="approval",
            tool="print_output",
            tool_use_id=tool_use,
            summary='{"output":"box"}',
            input_hash="a" * 64,
            prompt="",
            requested_by=ME,
            responders=["browser", "grant"],
            expires_at=expires,
        )
    return OpenInput(
        request_id=durable_request_id(sid, run, tool_use),
        session_id=sid,
        workflow_id=f"session-{sid}",
        workflow_run_id=run,
        kind="answer",
        tool="ask_user",
        tool_use_id=tool_use,
        summary="",
        input_hash=None,
        prompt="Which colour?",
        requested_by=ME,
        responders=["browser"],
        expires_at=expires,
        questions=[
            {"question": "Which colour?", "header": "Colour", "multiSelect": False, "options": []}
        ],
    )


async def _events(conn: Conn, sid: str) -> list[dict[str, Any]]:
    cur = await conn.execute(
        "SELECT event FROM ai_session_events WHERE session_id = %s ORDER BY seq", (sid,)
    )
    return [json.loads(r[0]) for r in await cur.fetchall()]


async def _count(conn: Conn, sql: str, *params: Any) -> int:
    cur = await conn.execute(sql, params)
    row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def test_open_writes_the_row_and_its_events_once(agent_db: Conn) -> None:
    sid = await _session(agent_db)
    args = _open(sid)
    assert await open_input(agent_db, args) is True
    assert await open_input(agent_db, args) is False  # a retried activity adds nothing
    assert await _count(agent_db, "SELECT count(*) FROM ai_pending_input") == 1
    types = [e["type"] for e in await _events(agent_db, sid)]
    assert types == ["approval.required", "input.requested", "session.status"]
    events = await _events(agent_db, sid)
    assert events[0]["id"] == args.request_id
    entry = events[1]["entry"]
    assert entry["id"] == args.request_id and entry["input_hash"] == "a" * 64
    assert "input" not in entry
    cur = await agent_db.execute("SELECT status FROM ai_sessions WHERE id = %s", (sid,))
    assert await cur.fetchone() == ("waiting_approval",)


async def test_resolve_records_the_outcome_events_and_audit(agent_db: Conn) -> None:
    sid = await _session(agent_db)
    args = _open(sid)
    await open_input(agent_db, args)
    assert await resolve_input(agent_db, ResolveInput(args.request_id, "approved", ME)) is True
    assert await _count(agent_db, "SELECT count(*) FROM ai_pending_input") == 0
    cur = await agent_db.execute("SELECT outcome, responder FROM ai_input_responses")
    assert await cur.fetchone() == ("approved", ME)
    types = [e["type"] for e in await _events(agent_db, sid)]
    assert types[-3:] == ["approval.resolved", "input.resolved", "session.status"]
    cur = await agent_db.execute(
        "SELECT kind, action, principal_kind, request_id, approval_id, outcome FROM ai_audit"
    )
    assert await cur.fetchall() == [
        ("approval", "approved", "browser", args.request_id, None, "ok")
    ]
    cur = await agent_db.execute("SELECT status FROM ai_sessions WHERE id = %s", (sid,))
    assert await cur.fetchone() == ("running",)


async def test_an_open_retried_after_its_resolution_opens_nothing(agent_db: Conn) -> None:
    sid = await _session(agent_db)
    args = _open(sid)
    await open_input(agent_db, args)
    await resolve_input(agent_db, ResolveInput(args.request_id, "denied", ME))
    assert await open_input(agent_db, args) is False
    assert await _count(agent_db, "SELECT count(*) FROM ai_pending_input") == 0


async def test_an_expiry_is_recorded_as_expired_by_the_system(agent_db: Conn) -> None:
    sid = await _session(agent_db)
    args = _open(sid)
    await open_input(agent_db, args)
    await resolve_input(
        agent_db, ResolveInput(args.request_id, "expired", SYSTEM, reason="timeout")
    )
    cur = await agent_db.execute("SELECT surface, principal_kind, action, outcome FROM ai_audit")
    assert await cur.fetchall() == [("system", "system", "expired", "refused")]
    resolved = [e for e in await _events(agent_db, sid) if e["type"] == "approval.resolved"]
    assert resolved[0]["decision"] == "expired" and "by" not in resolved[0]


async def test_an_answer_keeps_its_response(agent_db: Conn) -> None:
    sid = await _session(agent_db)
    args = _open(sid, kind="answer")
    await open_input(agent_db, args)
    types = [e["type"] for e in await _events(agent_db, sid)]
    assert types[0] == "question.asked"
    response = {"answers": ["Red"]}
    await resolve_input(agent_db, ResolveInput(args.request_id, "answered", ME, response))
    cur = await agent_db.execute("SELECT response FROM ai_input_responses")
    assert await cur.fetchone() == (response,)
    assert await _count(agent_db, "SELECT count(*) FROM ai_audit") == 0


async def test_two_resolutions_race_and_one_wins(
    agent_db: Conn, connect: Callable[[], AbstractAsyncContextManager[Conn]]
) -> None:
    sid = await _session(agent_db)
    args = _open(sid)
    await open_input(agent_db, args)

    async def resolve(outcome: str) -> bool:
        async with connect() as conn:
            return await resolve_input(conn, ResolveInput(args.request_id, outcome, ME))  # type: ignore[arg-type]

    results = await asyncio.gather(resolve("approved"), resolve("denied"))
    assert sorted(results) == [False, True]
    resolved = [e for e in await _events(agent_db, sid) if e["type"] == "input.resolved"]
    assert len(resolved) == 1
    assert await _count(agent_db, "SELECT count(*) FROM ai_input_responses") == 1


async def test_after_a_reset_the_old_runs_entry_is_cancelled(agent_db: Conn) -> None:
    sid = await _session(agent_db)
    before = _open(sid, run="run-1")
    after = _open(sid, run="run-2")
    await open_input(agent_db, before)
    await open_input(agent_db, after)
    cur = await agent_db.execute("SELECT request_id FROM ai_pending_input")
    assert await cur.fetchall() == [(after.request_id,)]
    cur = await agent_db.execute("SELECT request_id, outcome, reason FROM ai_input_responses")
    assert await cur.fetchall() == [(before.request_id, "cancelled", "reset")]
    # A respond carrying the pre-Reset id finds nothing.
    assert await resolve_input(agent_db, ResolveInput(before.request_id, "approved", ME)) is False


async def test_the_activities_run_over_their_connection(
    agent_db: Conn, connect: Callable[[], AbstractAsyncContextManager[Conn]]
) -> None:
    sid = await _session(agent_db)
    args = _open(sid)
    gate = GateActivities(connect)
    env = ActivityEnvironment()
    assert await env.run(gate.open_input, args) is True
    assert await env.run(gate.resolve_input, ResolveInput(args.request_id, "denied", ME)) is True
