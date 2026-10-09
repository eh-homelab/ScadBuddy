"""DurableSession on a dev server: turns, the gate, interrupt, continue-as-new (§6, §8).

The model is the plugin's ScriptedClaude; agent-tools is a stand-in (session_support).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest
from session_support import (
    MANIFEST,
    Conn,
    Connect,
    StandInTools,
    events,
    insert_session,
    send,
    settled,
    start_session,
    tools_worker,
    until,
)
from temporalio import activity
from temporalio.claude_agent_sdk.testing import Final, HistoryItem, ScriptedClaude, ToolCall
from temporalio.client import Client, WorkflowHandle, WorkflowUpdateFailedError
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from scadbuddy_durable.codec import PgPayloadKeys, data_converter
from scadbuddy_durable.gate.names import (
    CANCEL_INPUT_UPDATE,
    INTERRUPT_SIGNAL,
    PENDING_INPUT_QUERY,
    RESPOND_UPDATE,
)
from scadbuddy_durable.secrets import kek_from_base64
from scadbuddy_durable.session import tools
from scadbuddy_durable.session.activities import SessionActivities
from scadbuddy_durable.session.models import FinishTurn
from scadbuddy_durable.worker import build_worker

pytestmark = [pytest.mark.requires_postgres, pytest.mark.requires_temporal]

KEK = kek_from_base64("WlpaWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlo=")
QUESTION = {
    "questions": [
        {
            "question": "Which colour?",
            "header": "Colour",
            "multiSelect": False,
            "options": [
                {"label": "Red", "description": "warm"},
                {"label": "Blue", "description": "cool"},
            ],
        }
    ]
}
WAIT = {"reason": "question", "message": "Still there?", "timeout_s": 10}


def policy(prompt: str, history: list[HistoryItem]) -> ToolCall | Final:
    """The scripted model: the prompt names the tool to call, then it reports the result."""
    if history:
        last = history[-1]
        return Final(f"{last.name}: {'error' if last.is_error else 'ok'}: {last.content}")
    for name, args in (
        ("print it", ("print_output", {"output_id": "o1"})),
        ("ask me", ("ask_user", QUESTION)),
        ("ask badly", ("ask_user", {"questions": "red?"})),
        ("wait for me", ("wait_for_user", WAIT)),
        ("render it", ("render_preview", {"model": "m"})),
    ):
        if prompt.startswith(name):
            return ToolCall(*args)
    return Final(f"you said: {prompt}")


@dataclass
class Harness:
    client: Client
    connect: Connect
    stand_in: StandInTools
    queue: str
    agent: Worker
    handles: list[WorkflowHandle[Any, Any]]
    session_activities: SessionActivities | None = None

    async def session(self, conn: Conn) -> tuple[str, WorkflowHandle[Any, Any]]:
        sid = await insert_session(conn)
        handle = await start_session(self.client, sid, self.queue)
        self.handles.append(handle)
        return sid, handle

    def restart_agent(self) -> Worker:
        self.agent = build_worker(
            self.client,
            self.connect,
            ScriptedClaude(policy),
            task_queue=self.queue,
            session=self.session_activities,
        )
        return self.agent


@pytest.fixture
async def harness(temporal_env: WorkflowEnvironment, connect: Connect) -> AsyncIterator[Harness]:
    tools.use_manifest(MANIFEST)
    keys = PgPayloadKeys(connect, KEK)
    client = await Client.connect(
        temporal_env.client.service_client.config.target_host,
        namespace=temporal_env.client.namespace,
        data_converter=data_converter(keys),
    )
    stand_in = StandInTools(connect)
    queue = f"agent-{uuid.uuid4().hex[:8]}"
    h = Harness(client, connect, stand_in, queue, None, [])  # type: ignore[arg-type]
    agent = h.restart_agent()
    async with tools_worker(client, stand_in):
        task = asyncio.create_task(agent.run())
        try:
            yield h
        finally:
            for handle in h.handles:
                with contextlib.suppress(Exception):
                    await handle.terminate("test over")
            # Only the worker still running: shutting down one that never ran (or ran
            # and stopped) waits forever.
            if agent.is_running:
                await agent.shutdown()
            task.cancel()


async def _setting(conn: Conn, key: str, value: int) -> None:
    await conn.execute(
        "INSERT INTO ai_settings (key, value) VALUES (%s, %s)"
        " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (key, json.dumps(value)),
    )


async def _parked(handle: WorkflowHandle[Any, Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []

    async def some() -> bool:
        nonlocal entries
        entries = await handle.query(PENDING_INPUT_QUERY)
        return bool(entries)

    await until(some)
    return entries


def _respond(entry: dict[str, Any], response: dict[str, Any], role: str = "browser") -> Any:
    return {
        "request_id": entry["id"],
        "response": response,
        "responder": {"kind": "browser", "id": "browser", "label": "You"},
        "role": role,
    }


def _types(log: list[dict[str, Any]]) -> list[str]:
    return [e["type"] for e in log]


async def test_a_turn_without_tools_writes_the_panels_events(
    harness: Harness, agent_db: Conn, connect: Connect
) -> None:
    sid, handle = await harness.session(agent_db)
    answer = await send(handle, "hello there")
    assert answer.accepted
    assert await settled(connect, sid) == "idle"
    log = await events(agent_db, sid)
    texts = [e["delta"] for e in log if e["type"] == "assistant.text.delta"]
    assert texts == ["you said: hello there"]
    assert _types(log)[-2:] == ["session.result", "session.status"]
    assert log[-1]["status"] == "idle"
    assert log[-2]["turns"] == 0  # the scripted runner does not count turns: the real one does
    # A second turn continues the session.
    await agent_db.execute("UPDATE ai_sessions SET status = 'running' WHERE id = %s", (sid,))
    await send(handle, "again")
    assert await settled(connect, sid) == "idle"
    texts = [e["delta"] for e in await events(agent_db, sid) if e["type"] == "assistant.text.delta"]
    assert texts == ["you said: hello there", "you said: again"]


async def test_history_holds_no_plaintext(
    harness: Harness, agent_db: Conn, connect: Connect
) -> None:
    sid, handle = await harness.session(agent_db)
    await send(handle, "a secret plan for a red bracket")
    await settled(connect, sid)
    history = await handle.fetch_history()
    raw = b"".join(e.SerializeToString() for e in history.events)
    assert b"red bracket" not in raw
    assert b"binary/scadbuddy-subject" in raw


async def test_an_approved_call_runs_once_and_is_reported(
    harness: Harness, agent_db: Conn, connect: Connect
) -> None:
    sid, handle = await harness.session(agent_db)
    await send(handle, "print it")
    [entry] = await _parked(handle)
    assert entry["kind"] == "approval"
    assert entry["summary"] == "print_output call"
    assert entry["input_hash"] == "a" * 64
    assert entry["responders"] == ["browser", "grant"]
    assert await _status_now(agent_db, sid) == "waiting_approval"
    # The validator refuses before anything is written.
    with pytest.raises(WorkflowUpdateFailedError) as refused:
        await handle.execute_update(
            RESPOND_UPDATE,
            _respond({"id": "durable:x"}, {"kind": "approval", "decision": "approve"}),
        )
    assert _failure_type(refused.value) == "GateRefused:stale"
    with pytest.raises(WorkflowUpdateFailedError) as refused:
        await handle.execute_update(
            RESPOND_UPDATE, _respond(entry, {"kind": "approval", "decision": "approve"}, "owner")
        )
    assert _failure_type(refused.value) == "GateRefused:forbidden"
    result = await handle.execute_update(
        RESPOND_UPDATE, _respond(entry, {"kind": "approval", "decision": "approve"})
    )
    assert result == {"kind": "approval", "outcome": "approved"}
    assert await settled(connect, sid) == "idle"
    assert [r[0] for r in harness.stand_in.ran] == ["print_output"]
    log = await events(agent_db, sid)
    assert _types(log).count("approval.required") == 1
    resolved = [e for e in log if e["type"] == "input.resolved"]
    assert [e["outcome"] for e in resolved] == ["approved"]
    [call] = [e for e in log if e["type"] == "tool.call"]
    assert call["name"] == "print_output"
    assert call["risk"] == "outward"
    [res] = [e for e in log if e["type"] == "tool.result"]
    assert res["ok"] is True
    assert await handle.query(PENDING_INPUT_QUERY) == []


async def test_a_denied_call_never_runs(harness: Harness, agent_db: Conn, connect: Connect) -> None:
    sid, handle = await harness.session(agent_db)
    await send(handle, "print it")
    [entry] = await _parked(handle)
    result = await handle.execute_update(
        RESPOND_UPDATE, _respond(entry, {"kind": "approval", "decision": "deny"})
    )
    assert result == {"kind": "approval", "outcome": "denied"}
    assert await settled(connect, sid) == "idle"
    assert harness.stand_in.ran == []
    [res] = [e for e in await events(agent_db, sid) if e["type"] == "tool.result"]
    assert res == {**res, "ok": False, "summary": "rejected"}


async def test_an_approval_expires_and_its_call_never_runs(
    harness: Harness, agent_db: Conn, connect: Connect
) -> None:
    await _setting(agent_db, "approval_expiry_seconds", 10)
    sid, handle = await harness.session(agent_db)
    await send(handle, "print it")
    await _parked(handle)
    assert await settled(connect, sid, timeout=40) == "idle"
    assert harness.stand_in.ran == []
    resolved = [e for e in await events(agent_db, sid) if e["type"] == "input.resolved"]
    assert [e["outcome"] for e in resolved] == ["expired"]


async def test_a_question_is_answered(harness: Harness, agent_db: Conn, connect: Connect) -> None:
    sid, handle = await harness.session(agent_db)
    await send(handle, "ask me")
    [entry] = await _parked(handle)
    assert entry["kind"] == "answer"
    assert entry["prompt"] == "Which colour?"
    assert entry["requested_by"] is None
    assert await _status_now(agent_db, sid) == "waiting_input"
    result = await handle.execute_update(
        RESPOND_UPDATE, _respond(entry, {"kind": "answer", "answers": {"Which colour?": "Red"}})
    )
    assert result == {"kind": "answer", "outcome": "answered"}
    assert await settled(connect, sid) == "idle"
    assert [r[0] for r in harness.stand_in.ran] == ["ask_user"]
    log = await events(agent_db, sid)
    [asked] = [e for e in log if e["type"] == "question.asked"]
    assert asked["questions"][0]["question"] == "Which colour?"
    text = "".join(e["delta"] for e in log if e["type"] == "assistant.text.delta")
    assert '"answers": ["Red"]' in text


async def test_a_question_nobody_answers_is_cancelled(
    harness: Harness, agent_db: Conn, connect: Connect
) -> None:
    await _setting(agent_db, "question_expiry_seconds", 10)
    sid, handle = await harness.session(agent_db)
    await send(handle, "ask me")
    await _parked(handle)
    assert await settled(connect, sid, timeout=40) == "idle"
    resolved = [e for e in await events(agent_db, sid) if e["type"] == "input.resolved"]
    assert [(e["outcome"], e.get("reason")) for e in resolved] == [
        ("cancelled", "nobody answered in time")
    ]


async def test_an_attention_request_times_out_and_proceeds(
    harness: Harness, agent_db: Conn, connect: Connect
) -> None:
    sid, handle = await harness.session(agent_db)
    await send(handle, "wait for me")
    [entry] = await _parked(handle)
    assert entry["attention"] == {"reason": "question", "on_timeout": "proceed"}
    assert await settled(connect, sid, timeout=40) == "idle"
    assert harness.stand_in.ran == [("wait_for_user", WAIT)]
    resolved = [e for e in await events(agent_db, sid) if e["type"] == "input.resolved"]
    assert [e["outcome"] for e in resolved] == ["timed_out"]


async def test_a_malformed_question_is_refused_without_parking(
    harness: Harness, agent_db: Conn, connect: Connect
) -> None:
    sid, handle = await harness.session(agent_db)
    await send(handle, "ask badly")
    assert await settled(connect, sid) == "idle"
    log = await events(agent_db, sid)
    assert "question.asked" not in _types(log)
    assert harness.stand_in.ran == []


async def test_cancel_input_resolves_the_entry_and_the_turn_goes_on(
    harness: Harness, agent_db: Conn, connect: Connect
) -> None:
    sid, handle = await harness.session(agent_db)
    await send(handle, "print it")
    await _parked(handle)
    assert await handle.execute_update(CANCEL_INPUT_UPDATE, {"reason": "handed off"}) == "cancelled"
    assert await settled(connect, sid) == "idle"
    assert await handle.execute_update(CANCEL_INPUT_UPDATE, {"reason": "again"}) == "none"
    resolved = [e for e in await events(agent_db, sid) if e["type"] == "input.resolved"]
    assert [(e["outcome"], e.get("reason")) for e in resolved] == [("cancelled", "handed off")]
    assert harness.stand_in.ran == []


async def test_a_busy_session_refuses_a_second_message(
    harness: Harness, agent_db: Conn, connect: Connect
) -> None:
    sid, handle = await harness.session(agent_db)
    await send(handle, "print it")
    await _parked(handle)
    with pytest.raises(WorkflowUpdateFailedError) as refused:
        await send(handle, "and another")
    assert _failure_type(refused.value) == "busy"
    await handle.execute_update(CANCEL_INPUT_UPDATE, {"reason": "done"})
    await settled(connect, sid)


async def test_interrupt_ends_the_turn_even_while_the_worker_is_down(
    harness: Harness, agent_db: Conn, connect: Connect
) -> None:
    sid, handle = await harness.session(agent_db)
    await send(handle, "print it")
    await _parked(handle)
    await harness.agent.shutdown()
    await handle.signal(INTERRUPT_SIGNAL, {"reason": "stop"})
    async with harness.restart_agent():
        assert await settled(connect, sid) == "idle"
    log = await events(agent_db, sid)
    errors = [e for e in log if e["type"] == "error"]
    assert [e["code"] for e in errors] == ["interrupted"]
    resolved = [e for e in log if e["type"] == "input.resolved"]
    assert [(e["outcome"], e.get("reason")) for e in resolved] == [
        ("cancelled", "the turn was interrupted")
    ]
    assert harness.stand_in.ran == []


async def test_the_handler_set_is_the_gates_and_the_plugins(
    harness: Harness, agent_db: Conn, connect: Connect
) -> None:
    sid, handle = await harness.session(agent_db)
    await send(handle, "hi")
    await settled(connect, sid)
    meta = await handle.query("__temporal_workflow_metadata")
    definition = meta.definition
    updates = {u.name for u in definition.update_definitions}
    signals = {s.name for s in definition.signal_definitions}
    queries = {q.name for q in definition.query_definitions}
    assert {"send_message", "respond", "cancel_input"} <= updates
    assert "interrupt" in signals
    assert {"pending_input", "segment_context"} <= queries
    ours = {
        "send_message",
        "respond",
        "cancel_input",
        "interrupt",
        "pending_input",
        "segment_context",
    }
    others = (updates | signals | queries) - ours
    # Workflow Streams' and the plugin's own.
    assert all(n.startswith("__") for n in others), others


async def _status_now(conn: Conn, sid: str) -> str:
    cur = await conn.execute("SELECT status FROM ai_sessions WHERE id = %s", (sid,))
    row = await cur.fetchone()
    assert row is not None
    return str(row[0])


def _failure_type(err: WorkflowUpdateFailedError) -> str | None:
    cause = err.cause
    return cause.type if isinstance(cause, ApplicationError) else None


class HeldFinish(SessionActivities):
    """finish_turn held until the test lets it go, to open the window a turn ends in."""

    def __init__(self, connect: Connect) -> None:
        super().__init__(connect)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    @activity.defn(name="finish_turn")
    async def finish_turn(self, args: FinishTurn) -> bool:
        self.entered.set()
        await self.release.wait()
        return await super().finish_turn(args)


async def test_a_send_while_a_turn_is_finishing_is_refused_busy(
    harness: Harness, agent_db: Conn, connect: Connect
) -> None:
    """Review of #1958: the plugin's run() has returned, but finish_turn has not: a send
    accepted then would be dropped by continue-as-new or marked idle by finish_turn."""
    held = HeldFinish(connect)
    await harness.agent.shutdown()
    harness.session_activities = held
    async with harness.restart_agent():
        sid, handle = await harness.session(agent_db)
        await send(handle, "hello there")
        await asyncio.wait_for(held.entered.wait(), 30)
        with pytest.raises(WorkflowUpdateFailedError) as refused:
            await send(handle, "too soon")
        assert _failure_type(refused.value) == "busy"
        held.release.set()
        assert await settled(connect, sid) == "idle"
        await agent_db.execute("UPDATE ai_sessions SET status = 'running' WHERE id = %s", (sid,))
        assert (await send(handle, "now")).accepted
        assert await settled(connect, sid) == "idle"
    log = await events(agent_db, sid)
    texts = [e["delta"] for e in log if e["type"] == "assistant.text.delta"]
    assert texts == ["you said: hello there", "you said: now"]


async def test_two_responds_at_once_refuse_the_second_and_the_session_goes_on(
    harness: Harness, agent_db: Conn, connect: Connect
) -> None:
    """Review of #1958: both pass the validator; the second finds the entry resolving and
    must fail as an Update, not fail the workflow task."""
    sid, handle = await harness.session(agent_db)
    await send(handle, "print it")
    [entry] = await _parked(handle)
    approve = _respond(entry, {"kind": "approval", "decision": "approve"})
    # Sent while the worker is down, both reach it in one activation: both validators
    # pass before either handler runs.
    await harness.agent.shutdown()
    both = asyncio.gather(
        handle.execute_update(RESPOND_UPDATE, approve),
        handle.execute_update(RESPOND_UPDATE, approve),
        return_exceptions=True,
    )
    await asyncio.sleep(2)
    async with harness.restart_agent():
        results = await asyncio.wait_for(both, 60)
        assert await settled(connect, sid) == "idle"
    ok = [r for r in results if not isinstance(r, BaseException)]
    failed = [r for r in results if isinstance(r, BaseException)]
    assert ok == [{"kind": "approval", "outcome": "approved"}]
    assert len(failed) == 1
    assert isinstance(failed[0], WorkflowUpdateFailedError)
    assert _failure_type(failed[0]) in ("GateRefused:resolving", "GateRefused:stale")
    assert [r[0] for r in harness.stand_in.ran] == ["print_output"]


async def test_a_call_that_cannot_be_described_is_refused_not_left_waiting(
    harness: Harness, agent_db: Conn, connect: Connect
) -> None:
    """Review of #1958: describe_call failing must not leave the turn waiting forever."""
    harness.stand_in.describe_refuses = True
    sid, handle = await harness.session(agent_db)
    await send(handle, "print it")
    assert await settled(connect, sid) == "idle"
    assert harness.stand_in.ran == []
    assert await handle.query(PENDING_INPUT_QUERY) == []
    texts = [e["delta"] for e in await events(agent_db, sid) if e["type"] == "assistant.text.delta"]
    assert texts and texts[-1].startswith("print_output: error")
