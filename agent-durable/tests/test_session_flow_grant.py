"""A run grant in a child session's flow turn (#2481, spec 2026-10-11 §3.3-§3.4, plan
Task A5). A call in a turn the run itself sent is checked against the run's grant
first: inside, no entry is opened, the approval is recorded with responder `run_grant`
and the call runs. Outside, or in any other turn, it parks as before. The backend's
`flow_grant_check` is a stand-in on the `projects` queue that records the use as
`match_grant` does."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import test_session_workflow as base
from session_support import ME, Conn, Connect, events, send, settled, until
from temporalio import activity
from temporalio.client import WorkflowHandle
from temporalio.worker import Worker

from scadbuddy_durable.gate.names import PENDING_INPUT_QUERY
from scadbuddy_durable.session.models import (
    PROJECTS_QUEUE,
    SEND_MESSAGE_UPDATE,
    FlowTurn,
    Message,
    SendAnswer,
)

pytestmark = [pytest.mark.requires_postgres, pytest.mark.requires_temporal]

harness = base.harness
Harness = base.Harness

GRANT_TABLES = (
    Path(__file__).resolve().parents[2]
    / "agent"
    / "test"
    / "fixtures"
    / "backend-flow-migrations"
    / "20261012T2029Z_flow_grants.sql"
)
RUN = "7e2a1c3b-4d5e-4f60-8a9b-0c1d2e3f4a5b"
HASH = "a" * 64  # the stand-in describe_call's


class GrantCheck:
    """`flow_grant_check` as the backend answers it."""

    def __init__(self, connect: Connect) -> None:
        self._connect = connect
        self.verdict = "inside"
        self.checked: list[dict[str, Any]] = []
        self.use_hash = HASH

    @activity.defn(name="flow_grant_check")
    async def check(self, req: dict[str, Any]) -> dict[str, Any]:
        self.checked.append(req)
        if self.verdict != "inside":
            return {"verdict": "outside", "reason": "no rule admits it", "timeout_s": 0}
        async with self._connect() as conn:
            await conn.execute(
                "INSERT INTO flow_grant_uses (request_id, grant_id, run_id, fn, tool, input_hash,"
                " rule_index, session_id) VALUES (%s, 'g1', %s, 'tool', %s, %s, 0, %s)",
                (req["request_id"], RUN, req["tool"], self.use_hash, req["session_id"]),
            )
        return {"verdict": "inside", "grant_id": "g1", "rule_index": 0, "input_hash": HASH}


@pytest.fixture
async def granted(
    harness: base.Harness, agent_db: Conn, connect: Connect
) -> AsyncIterator[GrantCheck]:
    await agent_db.execute(GRANT_TABLES.read_text())
    await agent_db.execute(
        "INSERT INTO flow_grants (id, run_id, status, spec, requested_by, approved_by,"
        " approved_at, expires_at) VALUES ('g1', %s, 'approved', '{}', '{}', 'browser', now(),"
        " now() + interval '1 hour')",
        (RUN,),
    )
    check = GrantCheck(connect)
    async with Worker(harness.client, task_queue=PROJECTS_QUEUE, activities=[check.check]):
        yield check


async def send_flow(handle: WorkflowHandle[Any, Any], text: str, call_id: str = "c1") -> None:
    await handle.execute_update(
        SEND_MESSAGE_UPDATE,
        Message(
            turn_id=str(uuid.uuid4()),
            text=text,
            author=ME,
            flow=FlowTurn(run_id=RUN, call_id=call_id),
        ),
        result_type=SendAnswer,
    )


async def test_a_flow_turns_call_inside_the_grant_runs_with_no_entry(
    harness: base.Harness, agent_db: Conn, connect: Connect, granted: GrantCheck
) -> None:
    sid, handle = await harness.session(agent_db)
    await send_flow(handle, "print it")
    assert await settled(connect, sid) == "idle"
    assert [r[0] for r in harness.stand_in.ran] == ["print_output"]
    [checked] = granted.checked
    assert (checked["session_id"], checked["flow_call_id"], checked["run_id"]) == (
        sid,
        "c1",
        RUN,
    )
    assert checked["request_id"].startswith(f"durable:{sid}:")
    assert (checked["fn"], checked["tool"], checked["args"]) == (
        "tool",
        "print_output",
        {"output_id": "o1"},
    )
    cur = await agent_db.execute(
        "SELECT outcome, responder FROM ai_input_responses WHERE request_id = %s",
        (checked["request_id"],),
    )
    assert await cur.fetchall() == [
        ("approved", {"kind": "run_grant", "id": "g1", "label": "Run grant"})
    ]
    cur = await agent_db.execute(
        "SELECT action, principal_kind, principal_id, input_hash FROM ai_audit"
        " WHERE request_id = %s AND kind = 'approval'",
        (checked["request_id"],),
    )
    assert await cur.fetchall() == [("approved", "run_grant", "g1", HASH)]
    log = base._types(await events(agent_db, sid))
    assert "approval.required" not in log and "input.resolved" not in log
    await base._replays(harness, handle)


async def test_a_flow_turns_plugin_tool_call_inside_the_grant_runs_with_no_entry(
    harness: base.Harness, agent_db: Conn, connect: Connect, granted: GrantCheck
) -> None:
    """#2649: a turn tool (a plugin's, or http_request) is described with an input hash
    like ScadBuddy's own, so a run grant that names it covers it in a flow turn, and its
    approval is bound to that hash: one of its own, not the grant reply's."""
    described = "d" * 64
    harness.stand_in.turn_offered = [base.NOTES_POST]
    harness.stand_in.input_hashes["notes__post"] = described
    granted.use_hash = described
    sid, handle = await harness.session(agent_db)
    await send_flow(handle, "post it")
    assert await settled(connect, sid) == "idle"
    assert harness.stand_in.ran == [("turn:notes__post", {"method": "POST", "body": "x"})]
    [checked] = granted.checked
    assert (checked["tool"], checked["args"]) == ("notes__post", {"method": "POST", "body": "x"})
    cur = await agent_db.execute(
        "SELECT outcome, responder FROM ai_input_responses WHERE request_id = %s",
        (checked["request_id"],),
    )
    assert await cur.fetchall() == [
        ("approved", {"kind": "run_grant", "id": "g1", "label": "Run grant"})
    ]
    cur = await agent_db.execute(
        "SELECT action, principal_kind, principal_id, input_hash FROM ai_audit"
        " WHERE request_id = %s AND kind = 'approval'",
        (checked["request_id"],),
    )
    assert await cur.fetchall() == [("approved", "run_grant", "g1", described)]
    log = base._types(await events(agent_db, sid))
    assert "approval.required" not in log and "input.resolved" not in log
    await base._replays(harness, handle)


async def test_a_grant_used_for_another_input_does_not_admit_a_plugin_tools_call(
    harness: base.Harness, agent_db: Conn, granted: GrantCheck
) -> None:
    """#2649: the recorded use must be for this call's own input hash; a verdict for
    another input parks the turn tool's call, as it does ScadBuddy's own."""
    harness.stand_in.turn_offered = [base.NOTES_POST]
    harness.stand_in.input_hashes["notes__post"] = "d" * 64
    granted.use_hash = "e" * 64
    sid, handle = await harness.session(agent_db)
    await send_flow(handle, "post it")
    [entry] = await base._parked(handle)
    assert entry["kind"] == "approval"
    assert harness.stand_in.ran == []
    cur = await agent_db.execute(
        "SELECT count(*) FROM ai_input_responses WHERE session_id = %s", (sid,)
    )
    assert await cur.fetchone() == (0,)


async def test_a_turn_the_person_sends_is_no_flow_turn(
    harness: base.Harness, agent_db: Conn, granted: GrantCheck
) -> None:
    _, handle = await harness.session(agent_db)
    await send(handle, "print it")
    [entry] = await base._parked(handle)
    assert entry["kind"] == "approval"
    assert granted.checked == []


async def test_a_flow_turns_call_outside_the_grant_parks(
    harness: base.Harness, agent_db: Conn, granted: GrantCheck
) -> None:
    granted.verdict = "outside"
    _, handle = await harness.session(agent_db)
    await send_flow(handle, "print it")
    [entry] = await base._parked(handle)
    assert entry["kind"] == "approval"
    assert len(granted.checked) == 1
    assert harness.stand_in.ran == []


async def test_a_verdict_the_grants_record_does_not_hold_parks(
    harness: base.Harness, agent_db: Conn, granted: GrantCheck
) -> None:
    """The recorded use must be for this call's hash and an in-force grant: a verdict
    alone never approves, as for the flow's own calls."""
    granted.use_hash = "b" * 64
    sid, handle = await harness.session(agent_db)
    await send_flow(handle, "print it")
    [entry] = await base._parked(handle)
    assert entry["kind"] == "approval"
    assert harness.stand_in.ran == []
    cur = await agent_db.execute(
        "SELECT count(*) FROM ai_input_responses WHERE session_id = %s", (sid,)
    )
    assert await cur.fetchone() == (0,)


async def test_a_revoked_grant_parks_the_call(
    harness: base.Harness, agent_db: Conn, granted: GrantCheck
) -> None:
    await agent_db.execute("UPDATE flow_grants SET revoked_at = now(), revoked_by = 'browser'")
    _, handle = await harness.session(agent_db)
    await send_flow(handle, "print it")
    [entry] = await base._parked(handle)
    assert entry["kind"] == "approval"
    assert harness.stand_in.ran == []


async def test_a_flow_turns_call_parks_when_no_projects_worker_answers(
    harness: base.Harness, agent_db: Conn
) -> None:
    """Nothing serves the `projects` queue (its worker down, or a deployment without
    flows): the call parks once the check's wait runs out, never waits for ever."""
    _, handle = await harness.session(agent_db)
    await send_flow(handle, "print it")
    entries: list[dict[str, Any]] = []

    async def parked() -> bool:
        nonlocal entries
        entries = await handle.query(PENDING_INPUT_QUERY)
        return bool(entries)

    await until(parked, timeout=150)
    assert [e["kind"] for e in entries] == ["approval"]
    assert harness.stand_in.ran == []
