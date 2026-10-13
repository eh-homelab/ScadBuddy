"""A flow turn's call outside the run's grant (#2482, spec 2026-10-11 §4.1-§4.6).

`on_outside="deny"` refuses it at once, recorded as denied by the flow. Otherwise it
parks in the run's queue (`flow_run_id`): at hop `parent`, the session that started the
run, when the verdict names one, else at once with the person. Who the parent is comes
from the verdict (the backend's record of the run), and its hop's length plus the
person's is the entry's one timer. The parent may only deny; no approval grant decides
a run's call."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest
import test_session_workflow as base
from session_support import Conn, Connect, events, settled
from temporalio import activity
from temporalio.client import WorkflowUpdateFailedError
from temporalio.worker import Worker
from test_session_flow_grant import RUN, send_flow

from scadbuddy_durable.gate.names import RESPOND_UPDATE
from scadbuddy_durable.session.models import PROJECTS_QUEUE
from scadbuddy_durable.session.workflow import ENTRY_MAX_S

pytestmark = [pytest.mark.requires_postgres, pytest.mark.requires_temporal]

harness = base.harness
PARENT = "5e551011-0000-4000-8000-000000000001"
PRINCIPAL = "token:0f0e0d0c-0b0a-4908-8706-050403020100"


class Verdict:
    """`flow_grant_check` answering outside, as the test sets it."""

    def __init__(self) -> None:
        self.answer: dict[str, Any] = {"verdict": "outside", "reason": "print limit reached"}

    @activity.defn(name="flow_grant_check")
    async def check(self, req: dict[str, Any]) -> dict[str, Any]:
        return self.answer


@pytest.fixture
async def outside(harness: base.Harness) -> AsyncIterator[Verdict]:
    verdict = Verdict()
    async with Worker(harness.client, task_queue=PROJECTS_QUEUE, activities=[verdict.check]):
        yield verdict


async def _row(agent_db: Conn, sid: str) -> dict[str, Any]:
    cur = await agent_db.execute(
        "SELECT request_id, flow_run_id, hop, parent_session::text, parent_principal,"
        " outside_reason, hops, extract(epoch FROM hop_until - created_at)::int,"
        " extract(epoch FROM expires_at - created_at)::int FROM ai_pending_input"
        " WHERE session_id = %s",
        (sid,),
    )
    [row] = await cur.fetchall()
    keys = ("id", "run", "hop", "parent", "principal", "reason", "hops", "hop_s", "expires_s")
    return dict(zip(keys, row, strict=True))


async def test_on_outside_deny_refuses_at_once_as_the_flow(
    harness: base.Harness, agent_db: Conn, connect: Connect, outside: Verdict
) -> None:
    outside.answer = {**outside.answer, "on_outside": "deny"}
    sid, handle = await harness.session(agent_db)
    await send_flow(handle, "print it")
    assert await settled(connect, sid) == "idle"
    assert harness.stand_in.ran == []
    cur = await agent_db.execute(
        "SELECT outcome, responder, reason FROM ai_input_responses WHERE session_id = %s", (sid,)
    )
    assert await cur.fetchall() == [
        ("denied", {"kind": "flow", "id": RUN, "label": "Flow"}, "print limit reached")
    ]
    cur = await agent_db.execute(
        "SELECT count(*) FROM ai_pending_input WHERE session_id = %s", (sid,)
    )
    assert await cur.fetchone() == (0,)
    assert "approval.required" not in base._types(await events(agent_db, sid))
    await base._replays(harness, handle)


async def test_an_outside_call_waits_first_at_the_starter_session(
    harness: base.Harness, agent_db: Conn, outside: Verdict
) -> None:
    outside.answer = {
        **outside.answer,
        "timeout_s": 60,
        "parent_hop": True,
        "hop_s": 30,
        "parent_session": PARENT,
        "parent_principal": PRINCIPAL,
    }
    sid, handle = await harness.session(agent_db)
    await send_flow(handle, "print it")
    [entry] = await base._parked(handle)
    row = await _row(agent_db, sid)
    assert row == {
        **row,
        "id": entry["id"],
        "run": RUN,
        "hop": "parent",
        "parent": PARENT,
        "principal": PRINCIPAL,
        "reason": "print limit reached",
        "hop_s": 30,
        # One timer covers both hops: the parent's 30 s, then the run's 60 s.
        "expires_s": 90,
    }
    assert [(h["hop"], h["action"], h["actor"]) for h in row["hops"]] == [
        ("parent", "parked", "flow")
    ]


async def test_a_long_parent_hop_leaves_the_person_their_timeout(
    harness: base.Harness, agent_db: Conn, outside: Verdict
) -> None:
    """#2862: a parent hop near the entry's cap was cut to ENTRY_MAX_S - 1, and the total
    to ENTRY_MAX_S, so the person had about a second. The parent's hop is cut instead."""
    outside.answer = {
        **outside.answer,
        "timeout_s": 60,
        "parent_hop": True,
        "hop_s": ENTRY_MAX_S,
        "parent_session": PARENT,
        "parent_principal": PRINCIPAL,
    }
    sid, handle = await harness.session(agent_db)
    await send_flow(handle, "print it")
    await base._parked(handle)
    row = await _row(agent_db, sid)
    assert (row["hop_s"], row["expires_s"]) == (ENTRY_MAX_S - 60, ENTRY_MAX_S)
    assert row["expires_s"] - row["hop_s"] == 60  # the person's own timeout


async def test_with_no_parent_it_is_the_persons_at_once(
    harness: base.Harness, agent_db: Conn, outside: Verdict
) -> None:
    outside.answer = {**outside.answer, "timeout_s": 60}
    sid, handle = await harness.session(agent_db)
    await send_flow(handle, "print it")
    await base._parked(handle)
    row = await _row(agent_db, sid)
    assert (row["run"], row["hop"], row["parent"], row["hop_s"], row["expires_s"]) == (
        RUN,
        "person",
        None,
        None,
        60,
    )


def _as(entry: dict[str, Any], decision: str, role: str, who: dict[str, str]) -> Any:
    return {
        "request_id": entry["id"],
        "response": {"kind": "approval", "decision": decision},
        "responder": who,
        "role": role,
    }


SESSION = {"kind": "session", "id": PARENT, "label": "Starting session"}
GRANT = {"kind": "bearer", "id": "tok-c", "label": "Another agent"}


async def test_the_parent_may_deny_but_never_approve_and_no_grant_decides(
    harness: base.Harness, agent_db: Conn, connect: Connect, outside: Verdict
) -> None:
    outside.answer = {
        **outside.answer,
        "timeout_s": 60,
        "parent_hop": True,
        "hop_s": 30,
        "parent_session": PARENT,
        "parent_principal": PRINCIPAL,
    }
    sid, handle = await harness.session(agent_db)
    await send_flow(handle, "print it")
    [entry] = await base._parked(handle)
    for decision, role, who in (("approve", "parent", SESSION), ("approve", "grant", GRANT)):
        with pytest.raises(WorkflowUpdateFailedError) as refused:
            await handle.execute_update(RESPOND_UPDATE, _as(entry, decision, role, who))
        assert base._failure_type(refused.value) == "GateRefused:forbidden"
    result = await handle.execute_update(RESPOND_UPDATE, _as(entry, "deny", "parent", SESSION))
    assert result == {"kind": "approval", "outcome": "denied"}
    assert await settled(connect, sid) == "idle"
    assert harness.stand_in.ran == []
    cur = await agent_db.execute(
        "SELECT outcome, responder, hops FROM ai_input_responses WHERE session_id = %s", (sid,)
    )
    [(outcome, responder, hops)] = await cur.fetchall()
    assert (outcome, responder) == ("denied", SESSION)
    # The way up, with the decision as its last step, kept with the outcome.
    assert [(h["hop"], h["action"], h["actor"]) for h in hops] == [
        ("parent", "parked", "flow"),
        ("parent", "deny", f"session:{PARENT}"),
    ]
    await base._replays(harness, handle)


async def test_the_timer_covers_both_hops(
    harness: base.Harness, agent_db: Conn, connect: Connect, outside: Verdict
) -> None:
    """The workflow does not know hops: at hop `parent` past `hop_until` nothing is
    denied (the agent's sweep moves it up); only the one timer, both hops long, ends it."""
    outside.answer = {
        **outside.answer,
        "timeout_s": 2,
        "parent_hop": True,
        "hop_s": 2,
        "parent_session": PARENT,
        "parent_principal": PRINCIPAL,
    }
    sid, handle = await harness.session(agent_db)
    await send_flow(handle, "print it")
    await base._parked(handle)
    cur = await agent_db.execute(
        "SELECT created_at FROM ai_pending_input WHERE session_id = %s", (sid,)
    )
    [(parked_at,)] = await cur.fetchall()
    assert await settled(connect, sid, timeout=30) == "idle"
    cur = await agent_db.execute(
        "SELECT outcome, created_at FROM ai_input_responses WHERE session_id = %s", (sid,)
    )
    [(outcome, ended_at)] = await cur.fetchall()
    assert outcome == "expired"
    assert ended_at - parked_at >= timedelta(seconds=4)
