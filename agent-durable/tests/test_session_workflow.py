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
from pathlib import Path
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
    send_next,
    settled,
    start_session,
    tools_worker,
    until,
)
from temporalio import activity
from temporalio.claude_agent_sdk import ClaudeAgentPlugin
from temporalio.claude_agent_sdk.testing import Final, HistoryItem, ScriptedClaude, ToolCall
from temporalio.client import (
    Client,
    WorkflowExecutionStatus,
    WorkflowHandle,
    WorkflowUpdateFailedError,
)
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

from scadbuddy_durable.codec import PgPayloadKeys, data_converter
from scadbuddy_durable.gate.activities import GateActivities
from scadbuddy_durable.gate.names import (
    CANCEL_INPUT_UPDATE,
    END_SIGNAL,
    INTERRUPT_SIGNAL,
    PENDING_INPUT_QUERY,
    RESPOND_UPDATE,
)
from scadbuddy_durable.gate.store import OpenInput, ResolveInput
from scadbuddy_durable.payload_store import OFFLOAD_BYTES, external_storage
from scadbuddy_durable.secrets import kek_from_base64
from scadbuddy_durable.session import tools
from scadbuddy_durable.session.activities import SessionActivities
from scadbuddy_durable.session.models import (
    SEGMENT_CONTEXT_QUERY,
    TURN_QUERY,
    FinishTurn,
    GateSettings,
    SegmentContext,
)
from scadbuddy_durable.session.workflow import DurableSession
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
        ("get it", ("notes__post", {"method": "GET", "body": "y"})),
        ("post it", ("notes__post", {"method": "POST", "body": "x"})),
        ("run it", ("Bash", {"command": "make notes"})),
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
    payload_dir: Path = Path()
    session_activities: SessionActivities | None = None
    gate_activities: GateActivities | None = None

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
            gate=self.gate_activities,
        )
        return self.agent


@pytest.fixture
async def harness(
    temporal_env: WorkflowEnvironment, connect: Connect, tmp_path: Path
) -> AsyncIterator[Harness]:
    tools.use_manifest(MANIFEST)
    keys = PgPayloadKeys(connect, KEK)
    client = await Client.connect(
        temporal_env.client.service_client.config.target_host,
        namespace=temporal_env.client.namespace,
        data_converter=data_converter(keys, external_storage(tmp_path)),
    )
    stand_in = StandInTools(connect)
    queue = f"agent-{uuid.uuid4().hex[:8]}"
    h = Harness(client, connect, stand_in, queue, None, [], payload_dir=tmp_path)  # type: ignore[arg-type]
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
    await send_next(handle, "again")
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


async def test_a_large_turn_crosses_history_as_references(
    harness: Harness, agent_db: Conn, connect: Connect
) -> None:
    """#2243: the message, the segment's input and its answer, the stream and the turn's
    events all carry the text, and none of history's events does."""
    sid, handle = await harness.session(agent_db)
    text = "a long brief " + "z" * (OFFLOAD_BYTES * 4)
    await send(handle, text)
    assert await settled(connect, sid) == "idle"
    deltas = [
        e["delta"] for e in await events(agent_db, sid) if e["type"] == "assistant.text.delta"
    ]
    [delta] = deltas  # the panel's delta is cut to 32 KiB; the answer was whole
    assert delta.startswith("you said: a long brief zzzz")
    history = await handle.fetch_history()
    # Nothing at the threshold or over it is inline: the message alone is four times it.
    assert max(e.ByteSize() for e in history.events) < OFFLOAD_BYTES + 4096
    assert b"zzzz" not in b"".join(e.SerializeToString() for e in history.events)
    assert any((harness.payload_dir / f"session-{sid}").iterdir())


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


async def test_the_turn_query_names_the_turn_the_workflow_holds(
    harness: Harness, agent_db: Conn, connect: Connect
) -> None:
    """#2078: the agent's sweep gives back a kept claim when the workflow holds no turn."""
    sid, handle = await harness.session(agent_db)
    assert await handle.query(TURN_QUERY) is None
    answer = await send(handle, "print it")
    await _parked(handle)
    assert await handle.query(TURN_QUERY) == answer.turn_id
    await handle.execute_update(CANCEL_INPUT_UPDATE, {"reason": "done"})
    assert await settled(connect, sid) == "idle"
    await until(lambda: _turn_is(handle, None))


async def _turn_is(handle: WorkflowHandle[Any, Any], turn_id: str | None) -> bool:
    return bool(await handle.query(TURN_QUERY) == turn_id)


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


async def test_end_completes_an_idle_session(
    harness: Harness, agent_db: Conn, connect: Connect
) -> None:
    """#1056: a session marked done ends its workflow."""
    sid, handle = await harness.session(agent_db)
    await send(handle, "hi")
    await settled(connect, sid)
    await handle.signal(END_SIGNAL, {})
    await asyncio.wait_for(handle.result(), 30)


async def test_end_during_a_turn_completes_the_session_after_it(
    harness: Harness, agent_db: Conn, connect: Connect
) -> None:
    sid, handle = await harness.session(agent_db)
    await send(handle, "print it")
    await _parked(handle)
    await handle.signal(END_SIGNAL, {})
    await handle.execute_update(CANCEL_INPUT_UPDATE, {"reason": "done"})
    assert await settled(connect, sid) == "idle"
    await asyncio.wait_for(handle.result(), 30)


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
    assert {"interrupt", "end"} <= signals
    assert {"pending_input", "segment_context", "turn"} <= queries
    ours = {
        "send_message",
        "respond",
        "cancel_input",
        "interrupt",
        "end",
        "pending_input",
        "segment_context",
        "turn",
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
        assert (await send_next(handle, "now")).accepted
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


async def test_a_respond_and_an_interrupt_together_end_the_turn_and_the_next_is_taken(
    harness: Harness, agent_db: Conn, connect: Connect
) -> None:
    """Review of #1958: the approval's resolve_input returns after the interrupt ended the
    turn, so its decide finds no call waiting; that must not fail the workflow task."""
    sid, handle = await harness.session(agent_db)
    await send(handle, "print it")
    [entry] = await _parked(handle)
    await harness.agent.shutdown()
    approving = asyncio.ensure_future(
        handle.execute_update(
            RESPOND_UPDATE, _respond(entry, {"kind": "approval", "decision": "approve"})
        )
    )
    await asyncio.sleep(1)
    await handle.signal(INTERRUPT_SIGNAL, {"reason": "stop"})
    async with harness.restart_agent():
        with contextlib.suppress(WorkflowUpdateFailedError):
            await asyncio.wait_for(approving, 60)

        async def ended() -> bool:
            return any(e["type"] == "error" for e in await events(agent_db, sid))

        # The turn's own end, not the idle the cancelled entry's resolve writes.
        await until(ended)
        assert await settled(connect, sid) == "idle"
        await agent_db.execute("UPDATE ai_sessions SET status = 'running' WHERE id = %s", (sid,))
        assert (await asyncio.wait_for(send_next(handle, "hello again"), 60)).accepted

        async def second() -> bool:
            log = await events(agent_db, sid)
            return len([e for e in log if e["type"] == "session.result"]) == 2

        await until(second)
        assert await settled(connect, sid) == "idle"
    texts = [e["delta"] for e in await events(agent_db, sid) if e["type"] == "assistant.text.delta"]
    # The scripted model reports its history's last call: here, the one the interrupt ended.
    assert len(texts) == 1


class HeldSettings(SessionActivities):
    """gate_settings held: the window at a turn's start before its run begins."""

    def __init__(self, connect: Connect) -> None:
        super().__init__(connect)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    @activity.defn(name="gate_settings")
    async def gate_settings(self) -> GateSettings:
        self.entered.set()
        await self.release.wait()
        return await super().gate_settings()


async def test_an_interrupt_as_a_turn_starts_stops_it(
    harness: Harness, agent_db: Conn, connect: Connect
) -> None:
    """Review of #1958: a Stop while gate_settings runs was dropped and the turn ran."""
    held = HeldSettings(connect)
    await harness.agent.shutdown()
    harness.session_activities = held
    async with harness.restart_agent():
        sid, handle = await harness.session(agent_db)
        await send(handle, "print it")
        await asyncio.wait_for(held.entered.wait(), 30)
        await handle.signal(INTERRUPT_SIGNAL, {"reason": "stop"})
        await asyncio.sleep(1)
        held.release.set()
        assert await settled(connect, sid) == "idle"
    log = await events(agent_db, sid)
    assert [e["code"] for e in log if e["type"] == "error"] == ["interrupted"]
    assert not any(e["type"] == "approval.required" for e in log)
    assert harness.stand_in.ran == []


class SlowCancel(GateActivities):
    """Postgres out for longer than the respond path's 30 s: a cancel's first attempt hangs."""

    def __init__(self, connect: Connect) -> None:
        super().__init__(connect)
        self.hung = False

    @activity.defn(name="resolve_input")
    async def resolve_input(self, args: ResolveInput) -> bool:
        if args.outcome == "cancelled" and not self.hung:
            self.hung = True
            await asyncio.sleep(35)
        return await super().resolve_input(args)


async def test_a_stop_survives_a_database_outage_while_it_cancels_a_parked_call(
    harness: Harness, agent_db: Conn, connect: Connect
) -> None:
    """Review of #1958: a system cancel ran under the respond path's 30 s bound, and its
    failure, raised in the interrupt Signal, failed the whole workflow."""
    await harness.agent.shutdown()
    harness.gate_activities = SlowCancel(connect)
    async with harness.restart_agent():
        sid, handle = await harness.session(agent_db)
        await send(handle, "print it")
        await _parked(handle)
        await handle.signal(INTERRUPT_SIGNAL, {"reason": "stop"})

        async def ended() -> bool:
            return any(e["type"] == "error" for e in await events(agent_db, sid))

        await until(ended, timeout=120)
        assert (await handle.describe()).status == WorkflowExecutionStatus.RUNNING
        assert await settled(connect, sid) == "idle"
        await agent_db.execute("UPDATE ai_sessions SET status = 'running' WHERE id = %s", (sid,))
        assert (await send_next(handle, "hello again")).accepted
        assert await settled(connect, sid) == "idle"
    log = await events(agent_db, sid)
    assert [e["code"] for e in log if e["type"] == "error"] == ["interrupted"]


class SlowOpen(GateActivities):
    """open_input writes its row, then its result is slow to reach the workflow."""

    def __init__(self, connect: Connect) -> None:
        super().__init__(connect)
        self.written = asyncio.Event()
        self.release = asyncio.Event()

    @activity.defn(name="open_input")
    async def open_input(self, args: OpenInput) -> bool:
        opened = await super().open_input(args)
        self.written.set()
        await self.release.wait()
        return opened


async def test_a_respond_as_soon_as_the_row_is_written_is_taken(
    harness: Harness, agent_db: Conn, connect: Connect
) -> None:
    """A respond read from ai_pending_input before open_input's result reached the
    workflow was refused stale (flowSessions.e2e on #2582's CI). It now waits for the
    open, then resolves the call."""
    await harness.agent.shutdown()
    gate = SlowOpen(connect)
    harness.gate_activities = gate
    async with harness.restart_agent():
        sid, handle = await harness.session(agent_db)
        await send(handle, "print it")
        await asyncio.wait_for(gate.written.wait(), 60)
        cur = await agent_db.execute(
            "SELECT request_id FROM ai_pending_input WHERE session_id = %s", (sid,)
        )
        [(request_id,)] = await cur.fetchall()
        answer = asyncio.create_task(
            handle.execute_update(
                RESPOND_UPDATE,
                _respond({"id": request_id}, {"kind": "approval", "decision": "deny"}),
            )
        )
        await asyncio.sleep(1)
        # Taken, and waiting for the open: no response was written before the row's open ended.
        assert not answer.done()
        gate.release.set()
        assert await answer == {"kind": "approval", "outcome": "denied"}
        assert await settled(connect, sid) == "idle"
    assert harness.stand_in.ran == []
    resolved = [e for e in await events(agent_db, sid) if e["type"] == "input.resolved"]
    assert [e["outcome"] for e in resolved] == ["denied"]


# ---- Settings → Plugins overrides (#2088) ----


async def _overrides(conn: Conn, tool_tiers: dict[str, str], disabled: list[str]) -> None:
    await conn.execute(
        "INSERT INTO ai_settings (key, value) VALUES (%s, %s)"
        " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (
            tools.OVERRIDES_SETTING,
            json.dumps({"tool_tiers": tool_tiers, "disabled_tools": disabled}),
        ),
    )


async def _replays(harness: Harness, handle: WorkflowHandle[Any, Any]) -> None:
    """The history replays on this code: the gates came from recorded results only."""
    history = await handle.fetch_history()
    await Replayer(
        workflows=[DurableSession],
        plugins=[ClaudeAgentPlugin(ScriptedClaude(policy))],
        data_converter=harness.client.data_converter,
        workflow_runner=SandboxedWorkflowRunner(
            restrictions=SandboxRestrictions.default.with_passthrough_modules(
                "scadbuddy_durable.session.tools"
            )
        ),
    ).replay_workflow(history)


async def test_a_tool_raised_to_outward_parks_and_runs_once_approved(
    harness: Harness, agent_db: Conn, connect: Connect
) -> None:
    await _overrides(agent_db, {"render_preview": "outward"}, [])
    sid, handle = await harness.session(agent_db)
    await send(handle, "render it")
    [entry] = await _parked(handle)
    assert entry["kind"] == "approval"
    assert entry["summary"] == "render_preview call"
    assert harness.stand_in.ran == []
    result = await handle.execute_update(
        RESPOND_UPDATE, _respond(entry, {"kind": "approval", "decision": "approve"})
    )
    assert result == {"kind": "approval", "outcome": "approved"}
    assert await settled(connect, sid) == "idle"
    assert [r[0] for r in harness.stand_in.ran] == ["render_preview"]
    texts = [e["delta"] for e in await events(agent_db, sid) if e["type"] == "assistant.text.delta"]
    assert texts == ["render_preview: ok: rendered"]
    log = await events(agent_db, sid)
    assert _types(log).count("approval.required") == 1
    await _replays(harness, handle)


async def test_an_override_never_lowers_a_tool_below_its_own_tier(
    harness: Harness, agent_db: Conn, connect: Connect
) -> None:
    """TIGHTEN ONLY (builtInTools.ts): an outward tool set to read still parks."""
    await _overrides(agent_db, {"print_output": "read"}, [])
    sid, handle = await harness.session(agent_db)
    await send(handle, "print it")
    [entry] = await _parked(handle)
    assert entry["kind"] == "approval"
    await handle.execute_update(
        RESPOND_UPDATE, _respond(entry, {"kind": "approval", "decision": "deny"})
    )
    assert await settled(connect, sid) == "idle"
    assert harness.stand_in.ran == []


async def test_a_disabled_tool_is_refused_naming_settings_and_never_parks(
    harness: Harness, agent_db: Conn, connect: Connect
) -> None:
    # Raised as well: a disabled tool is refused as disabled, never parked for approval.
    await _overrides(agent_db, {"render_preview": "outward"}, ["render_preview"])
    sid, handle = await harness.session(agent_db)
    before = await handle.query(SEGMENT_CONTEXT_QUERY, result_type=SegmentContext)
    assert before.disabled_tools == []  # no turn has read the settings yet
    await send(handle, "render it")
    assert await settled(connect, sid) == "idle"
    assert harness.stand_in.ran == []
    log = await events(agent_db, sid)
    assert "approval.required" not in _types(log)
    texts = [e["delta"] for e in log if e["type"] == "assistant.text.delta"]
    assert len(texts) == 1
    assert texts[0].startswith("render_preview: error")
    assert "disabled in Settings → Plugins" in texts[0]
    # What the segment runner reads, to leave the tool undeclared.
    context = await handle.query(SEGMENT_CONTEXT_QUERY, result_type=SegmentContext)
    assert context.disabled_tools == ["render_preview"]
    await _replays(harness, handle)


class OlderSettings(SessionActivities):
    """gate_settings as a worker before #2088 answered it: the timers alone."""

    @activity.defn(name="gate_settings")
    async def gate_settings(self) -> dict[str, int]:  # type: ignore[override]
        return {"approval_expiry_s": 600, "question_expiry_s": 3600}


async def test_a_turn_an_older_worker_started_parks_by_the_manifest_and_replays(
    harness: Harness, agent_db: Conn, connect: Connect
) -> None:
    """A history whose gate_settings result has no overrides keeps its old commands: the
    raised call runs unparked (and is refused, as before #2088), and replays here."""
    await harness.agent.shutdown()
    harness.session_activities = OlderSettings(connect)
    await _overrides(agent_db, {"render_preview": "outward"}, [])
    async with harness.restart_agent():
        sid, handle = await harness.session(agent_db)
        await send(handle, "render it")
        assert await settled(connect, sid) == "idle"
    log = await events(agent_db, sid)
    assert "approval.required" not in _types(log)
    texts = [e["delta"] for e in log if e["type"] == "assistant.text.delta"]
    assert texts and texts[0].startswith("render_preview: error")
    await _replays(harness, handle)


NOTES_POST = {
    "name": "notes__post",
    "description": "posts a note",
    "input_schema": {
        "type": "object",
        "properties": {"method": {"type": "string"}, "body": {"type": "string"}},
    },
    "may_park": True,
}


async def test_a_turn_tool_whose_call_is_read_runs_without_parking(
    harness: Harness, agent_db: Conn, connect: Connect
) -> None:
    """#2320: a plugin's or http_request's tool is offered as a durable tool, and a call
    the agent tiers read (an http GET) goes straight through, as in a classic turn."""
    harness.stand_in.turn_offered = [NOTES_POST]
    sid, handle = await harness.session(agent_db)
    await send(handle, "get it")
    assert await settled(connect, sid) == "idle"
    assert harness.stand_in.ran == [("turn:notes__post", {"method": "GET", "body": "y"})]
    log = await events(agent_db, sid)
    assert "approval.required" not in _types(log)
    texts = [e["delta"] for e in log if e["type"] == "assistant.text.delta"]
    assert texts == ["notes__post: ok: turn:notes__post did GET"]
    await _replays(harness, handle)


async def test_an_outward_turn_tool_call_parks_and_runs_once_approved(
    harness: Harness, agent_db: Conn, connect: Connect
) -> None:
    harness.stand_in.turn_offered = [NOTES_POST]
    sid, handle = await harness.session(agent_db)
    await send(handle, "post it")
    [entry] = await _parked(handle)
    assert entry["kind"] == "approval"
    assert harness.stand_in.ran == []
    await handle.execute_update(
        RESPOND_UPDATE, _respond(entry, {"kind": "approval", "decision": "approve"})
    )
    assert await settled(connect, sid) == "idle"
    assert harness.stand_in.ran == [("turn:notes__post", {"method": "POST", "body": "x"})]
    await _replays(harness, handle)


NOTES_PACKAGE = {
    "name": "notes",
    "dir": "a" * 64,
    "content_hash": "sha256:" + "a" * 64,
    "allowed": True,
    "builtin_tools": ["Bash"],
}


async def test_a_package_built_in_parks_runs_once_approved_and_the_runner_gets_the_package(
    harness: Harness, agent_db: Conn, connect: Connect
) -> None:
    """#2320: an allowed package's Bash, outward in a classic turn, runs here as its own
    activity once a human approved the call; the segment runner is told the package."""
    harness.stand_in.turn_packages = [NOTES_PACKAGE]
    sid, handle = await harness.session(agent_db)
    await send(handle, "run it")
    [entry] = await _parked(handle)
    assert (entry["kind"], entry["tool"]) == ("approval", "Bash")
    context = await handle.query(SEGMENT_CONTEXT_QUERY, result_type=SegmentContext)
    assert [(p.name, p.dir, p.allowed) for p in context.packages] == [("notes", "a" * 64, True)]
    await handle.execute_update(
        RESPOND_UPDATE, _respond(entry, {"kind": "approval", "decision": "approve"})
    )
    assert await settled(connect, sid) == "idle"
    texts = [e["delta"] for e in await events(agent_db, sid) if e["type"] == "assistant.text.delta"]
    assert len(texts) == 1 and texts[0].startswith("Bash: ok"), texts
    await _replays(harness, handle)


async def test_a_turn_recalls_memories_into_its_prompt_and_retains_before_it_ends(
    harness: Harness, agent_db: Conn, connect: Connect
) -> None:
    """#2320: Hindsight's block follows the prompt as a classic turn's recall hook adds
    it, and the transcript is retained before the turn is written."""
    harness.stand_in.memory_context = "<hindsight_memories>1. red</hindsight_memories>"
    sid, handle = await harness.session(agent_db)
    answer = await send(handle, "hello there")
    assert await settled(connect, sid) == "idle"
    texts = [e["delta"] for e in await events(agent_db, sid) if e["type"] == "assistant.text.delta"]
    assert texts == ["you said: hello there\n\n<hindsight_memories>1. red</hindsight_memories>"]
    assert harness.stand_in.memory == [
        ("memory_recall", answer.turn_id),
        ("memory_retain", answer.turn_id),
    ]
    await _replays(harness, handle)


async def test_a_failed_recall_or_retain_never_fails_the_turn(
    harness: Harness, agent_db: Conn, connect: Connect
) -> None:
    harness.stand_in.memory_fails = True
    sid, handle = await harness.session(agent_db)
    await send(handle, "hello there")
    assert await settled(connect, sid) == "idle"
    texts = [e["delta"] for e in await events(agent_db, sid) if e["type"] == "assistant.text.delta"]
    assert texts == ["you said: hello there"]
    assert [name for name, _ in harness.stand_in.memory] == ["memory_recall", "memory_retain"]
    await _replays(harness, handle)


async def test_a_turn_tool_call_the_agent_cannot_tier_parks(
    harness: Harness, agent_db: Conn
) -> None:
    """Fail toward asking: a may_park tool whose turn_tool_tier fails is asked about, never
    let through to be refused as unapproved without the user seeing it."""
    harness.stand_in.turn_offered = [NOTES_POST]
    harness.stand_in.tier_refuses = True
    _, handle = await harness.session(agent_db)
    await send(handle, "get it")
    [entry] = await _parked(handle)
    assert entry["kind"] == "approval"
    assert harness.stand_in.ran == []
    await _replays(harness, handle)


async def test_a_turn_whose_other_tools_cannot_be_listed_goes_on_with_its_own(
    harness: Harness, agent_db: Conn, connect: Connect
) -> None:
    """A failed turn_tools never fails the session: the turn runs with the manifest's tools
    and offers none of last turn's others, as a classic turn reports the plugin and goes on."""
    harness.stand_in.turn_offered = [NOTES_POST]
    sid, handle = await harness.session(agent_db)
    await send(handle, "get it")
    assert await settled(connect, sid) == "idle"
    harness.stand_in.turn_tools_fails = True
    await agent_db.execute("UPDATE ai_sessions SET status = 'running' WHERE id = %s", (sid,))
    await send_next(handle, "hello there")
    assert await settled(connect, sid) == "idle"
    # The second turn ran to its result, offered no turn tool, and called none.
    types = _types(await events(agent_db, sid))
    assert types.count("session.result") == 2
    assert types.count("tool.call") == 1
    assert harness.stand_in.ran == [("turn:notes__post", {"method": "GET", "body": "y"})]
    await _replays(harness, handle)
