"""DurableSession on a dev server: turns, the gate, interrupt, continue-as-new (§6, §8).

The model is the plugin's ScriptedClaude; agent-tools is a stand-in (session_support).

``fixtures/durable_session_histories/raised.json`` is the history of
``test_a_tool_raised_to_outward_parks_and_runs_once_approved`` as recorded on the code
before ``tool-access-2171`` (#2171), payloads unsealed; it is never re-recorded.
``bash.json`` is the history of
``test_a_package_built_in_parks_runs_once_approved_and_the_runner_gets_the_package``,
recorded the same way on the code before ``builtin-access-4757`` (#4757). ``skill.json`` is
``test_an_outward_package_skill_parks_and_runs_once_approved``'s, recorded the same way when
the hooked built-in path was added (#4757), to keep it replaying. ``bash_access.json`` is
``test_an_off_bash_is_refused_then_an_outward_one_parks_and_runs_once_approved``'s, recorded
the same way under ``builtin-access-4757`` (an engine-parked Bash: off, then outward).
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from session_support import (
    MANIFEST,
    ME,
    SCHEMA,
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
from temporalio.claude_agent_sdk import (
    AgentState,
    ClaudeAgentPlugin,
    SegmentInput,
    SegmentOutput,
    follow_agent,
)
from temporalio.claude_agent_sdk.testing import Final, HistoryItem, ScriptedClaude, ToolCall
from temporalio.client import (
    Client,
    WorkflowExecutionStatus,
    WorkflowHandle,
    WorkflowHistory,
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
from scadbuddy_durable.session import subagents, tools
from scadbuddy_durable.session.access import REGISTRY_SOURCES, registry_source
from scadbuddy_durable.session.activities import SessionActivities
from scadbuddy_durable.session.models import (
    BUDGET_EXHAUSTED,
    BUILTIN_CALL_UPDATE,
    BUILTIN_RELEASE_UPDATE,
    FORK_POINT_QUERY,
    MESSAGE_MAX,
    SEGMENT_CONTEXT_QUERY,
    SEGMENT_TIMEOUT,
    TOOLS_QUEUE,
    TURN_QUERY,
    FinishTurn,
    ForkPoint,
    ForkPointArgs,
    GateSettings,
    SegmentContext,
    SessionStart,
)
from scadbuddy_durable.session.workflow import FOLLOW_DRAIN, DurableSession, refusal
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
        ("watch it", ("get_printer_camera", {"printer_id": 1})),
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
    claude: ScriptedClaude | None = None

    async def session(
        self, conn: Conn, execution_timeout: timedelta = timedelta(minutes=10)
    ) -> tuple[str, WorkflowHandle[Any, Any]]:
        sid = await insert_session(conn)
        handle = await start_session(
            self.client, sid, self.queue, execution_timeout=execution_timeout
        )
        self.handles.append(handle)
        return sid, handle

    def restart_agent(self) -> Worker:
        self.agent = build_worker(
            self.client,
            self.connect,
            self.claude or ScriptedClaude(policy),
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


async def test_a_segment_may_run_as_long_as_a_subagent_needs(
    harness: Harness, agent_db: Conn, connect: Connect
) -> None:
    # A subagent runs inside its parent's segment: at the plugin's 10 minutes an authoring
    # subagent never finished, and every retry started it over (#4026).
    # The server caps an activity's timeout at the run's, so the run outlasts the segment.
    sid, handle = await harness.session(agent_db, execution_timeout=timedelta(hours=2))
    await send(handle, "a long one")
    await settled(connect, sid)
    history = await handle.fetch_history()
    segments = [
        e.activity_task_scheduled_event_attributes
        for e in history.events
        if e.activity_task_scheduled_event_attributes.activity_type.name == "run_claude_segment"
    ]
    assert segments
    for s in segments:
        assert s.start_to_close_timeout.ToTimedelta() == SEGMENT_TIMEOUT
        assert s.heartbeat_timeout.ToTimedelta() == timedelta(seconds=30)
    assert SEGMENT_TIMEOUT.total_seconds() < subagents.TOKEN_TTL_S


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
    # builtin_call and builtin_release: the segment runner's hook asks the gate about a hooked
    # built-in, and ends its entry when the segment ends first (#4757).
    assert {"send_message", "respond", "cancel_input", "builtin_call", "builtin_release"} <= updates
    assert {"interrupt", "end"} <= signals
    assert {"pending_input", "segment_context", "turn", "fork_point"} <= queries
    ours = {
        "send_message",
        "respond",
        "cancel_input",
        "builtin_call",
        "builtin_release",
        "interrupt",
        "end",
        "pending_input",
        "segment_context",
        "turn",
        "fork_point",
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


class FailingClaude(ScriptedClaude):
    """A runner whose segment answers a failure with the prompt's text after "fail: "."""

    async def run(self, inp: SegmentInput, attempt: int) -> SegmentOutput:
        _, asked, error = (inp.prompt or "").partition("fail: ")
        if asked:
            return SegmentOutput(session_id=inp.session_id, is_error=True, error=error)
        return await super().run(inp, attempt)


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (BUDGET_EXHAUSTED, "error_max_budget_usd"),
        (f"the notes say {BUDGET_EXHAUSTED} last time", "turn_failed"),
        (f"{BUDGET_EXHAUSTED} was quoted first", "turn_failed"),
    ],
)
async def test_only_the_budgets_own_failure_ends_a_turn_as_the_budgets(
    harness: Harness, agent_db: Conn, connect: Connect, error: str, code: str
) -> None:
    """#3353: a segment's failure that only quotes the budget's words, anywhere in its
    text, is the turn's failure, not the budget's."""
    await harness.agent.shutdown()
    harness.claude = FailingClaude(policy)
    async with harness.restart_agent():
        sid, handle = await harness.session(agent_db)
        await send(handle, f"fail: {error}")
        await settled(connect, sid)
    log = await events(agent_db, sid)
    errors = [e for e in log if e["type"] == "error"]
    assert [e["code"] for e in errors] == [code]
    if code == "turn_failed":
        # The session's own text survives in the failure, quote and all (#3568). Whole,
        # since finish_turn keeps only the first MESSAGE_MAX characters (#3915).
        assert len(error) < MESSAGE_MAX
        assert error in errors[0]["message"]


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


def _refused_then_asks(prompt: str, history: list[HistoryItem]) -> ToolCall | Final:
    """Calls print_output, then asks a question, which holds the turn open to be read."""
    if not history:
        return ToolCall("print_output", {"output_id": "o1"})
    if len(history) == 1:
        return ToolCall("ask_user", QUESTION)
    return Final("done")


async def test_a_call_the_agent_refuses_to_describe_tells_the_model_why(
    harness: Harness, agent_db: Conn, connect: Connect
) -> None:
    """#4442: a print_output naming a printer that no longer links its printer_id is
    refused before it parks, and the segment runner is given the agent's reason in place
    of the plugin's rejection (`with_refusals`), so the model never reads that a person
    rejected it."""
    why = "Garage is Bambuddy printer 2, not printer_id 3."
    harness.stand_in.describe_tool_error = why
    await harness.agent.shutdown()
    harness.claude = ScriptedClaude(_refused_then_asks)
    async with harness.restart_agent():
        _, handle = await harness.session(agent_db)
        await send(handle, "print it")
        [entry] = await _parked(handle)
        assert entry["kind"] == "answer"
        context = await handle.query(SEGMENT_CONTEXT_QUERY, result_type=SegmentContext)
        assert list(context.replaced.values()) == [why]
        assert context.refusals == {}
        assert harness.stand_in.ran == []
        await _replays(harness, handle)


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
        started = time.monotonic()
        assert await settled(connect, sid) == "idle"
        # #2123: a run cancelled before its first step publishes nothing, so the turn
        # must not wait out FOLLOW_DRAIN for an end that never comes.
        assert time.monotonic() - started < FOLLOW_DRAIN.total_seconds() / 2
    log = await events(agent_db, sid)
    assert [e["code"] for e in log if e["type"] == "error"] == ["interrupted"]
    assert not any(e["type"] == "approval.required" for e in log)
    assert harness.stand_in.ran == []
    await _replays(harness, handle)


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


class Outage(GateActivities):
    """Postgres out for one resolve outcome: every attempt fails until the test lets it go."""

    def __init__(self, connect: Connect, outcome: str) -> None:
        super().__init__(connect)
        self.outcome = outcome
        self.tried = asyncio.Event()
        self.release = asyncio.Event()

    @activity.defn(name="resolve_input")
    async def resolve_input(self, args: ResolveInput) -> bool:
        if args.outcome == self.outcome and not self.release.is_set():
            self.tried.set()
            raise RuntimeError("database unreachable")
        return await super().resolve_input(args)


async def _open_rows(conn: Conn, sid: str) -> int:
    cur = await conn.execute("SELECT count(*) FROM ai_pending_input WHERE session_id = %s", (sid,))
    row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def test_a_stop_stops_the_run_while_the_database_is_down(
    harness: Harness, agent_db: Conn, connect: Connect
) -> None:
    """#2123: Stop waited for the parked call's cancel to be written before it cancelled
    the run, so with Postgres down the agent kept running until it was back."""
    outage = Outage(connect, "cancelled")
    await harness.agent.shutdown()
    harness.gate_activities = outage
    async with harness.restart_agent():
        sid, handle = await harness.session(agent_db)
        await send(handle, "print it")
        await _parked(handle)
        await handle.signal(INTERRUPT_SIGNAL, {"reason": "stop"})

        async def stopped() -> bool:
            async for event in follow_agent(harness.client, handle.id):
                if event["type"] == "cancelled":
                    return True
            return False

        # The run stops while the cancel cannot be written; the turn's end then retries it.
        assert await asyncio.wait_for(stopped(), 20)
        await asyncio.wait_for(outage.tried.wait(), 30)
        outage.release.set()
        assert await settled(connect, sid, timeout=120) == "idle"
    log = await events(agent_db, sid)
    assert [e["code"] for e in log if e["type"] == "error"] == ["interrupted"]
    resolved = [e for e in log if e["type"] == "input.resolved"]
    assert [(e["outcome"], e.get("reason")) for e in resolved] == [
        ("cancelled", "the turn was interrupted")
    ]
    assert await _open_rows(agent_db, sid) == 0
    assert harness.stand_in.ran == []
    await _replays(harness, handle)


async def test_an_expiry_in_flight_as_the_turn_ends_is_still_written(
    harness: Harness, agent_db: Conn, connect: Connect
) -> None:
    """#2123: the turn's end skipped an entry whose timer had fired and then cancelled
    its park, and with it the expiry's resolve_input, so the row stayed pending."""
    outage = Outage(connect, "expired")
    await _setting(agent_db, "approval_expiry_seconds", 10)
    await harness.agent.shutdown()
    harness.gate_activities = outage
    async with harness.restart_agent():
        sid, handle = await harness.session(agent_db)
        await send(handle, "print it")
        await _parked(handle)
        await asyncio.wait_for(outage.tried.wait(), 60)
        await handle.signal(INTERRUPT_SIGNAL, {"reason": "stop"})
        await asyncio.sleep(3)
        outage.release.set()
        assert await settled(connect, sid, timeout=120) == "idle"

        async def closed() -> bool:
            return await _open_rows(agent_db, sid) == 0

        await until(closed, timeout=60)
    log = await events(agent_db, sid)
    resolved = [e for e in log if e["type"] == "input.resolved"]
    assert [e["outcome"] for e in resolved] == ["expired"]
    assert harness.stand_in.ran == []
    await _replays(harness, handle)


# ---- Settings → Tools & plugins overrides (#2088) ----


async def _overrides(conn: Conn, tool_tiers: dict[str, str], disabled: list[str]) -> None:
    """The registry tools' ai_access rows as rowsFromOverrides (agent test/support/
    memoryAccess.ts) writes them: a disabled tool is off, whatever its tier."""
    rows = [(t, "off") for t in disabled]
    rows += [(t, a) for t, a in tool_tiers.items() if t not in disabled]
    await conn.execute("DELETE FROM ai_access WHERE source = ANY(%s)", (list(REGISTRY_SOURCES),))
    for item, value in rows:
        await conn.execute(
            "INSERT INTO ai_access (source, item, access, updated_by) VALUES (%s, %s, %s, %s)",
            (registry_source(item), item, value, "browser:browser"),
        )


async def _replays(harness: Harness, handle: WorkflowHandle[Any, Any]) -> None:
    """The history replays on this code: the gates came from recorded results only."""
    await _replay(harness, await handle.fetch_history())


async def _replay(harness: Harness, history: WorkflowHistory) -> None:
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


def _twice(
    tool: str, args: dict[str, Any], between: Callable[[], None]
) -> Callable[[str, list[HistoryItem]], ToolCall | Final]:
    """Calls ``tool`` twice, running ``between`` before the second call (Settings changed
    while the turn runs), then reports the last call's result."""

    def scripted(prompt: str, history: list[HistoryItem]) -> ToolCall | Final:
        if not history:
            return ToolCall(tool, args)
        if len(history) == 1:
            between()
            return ToolCall(tool, args)
        last = history[-1]
        return Final(f"{last.name}: {'error' if last.is_error else 'ok'}: {last.content}")

    return scripted


async def _approve_next(handle: WorkflowHandle[Any, Any]) -> None:
    [entry] = await _parked(handle)
    assert entry["kind"] == "approval"
    await handle.execute_update(
        RESPOND_UPDATE, _respond(entry, {"kind": "approval", "decision": "approve"})
    )


async def test_a_tier_lowered_mid_turn_stops_parking_on_the_next_call(
    harness: Harness, agent_db: Conn, connect: Connect
) -> None:
    """#2171 §4.4: each call is decided by its access as it is now, not as the turn began."""
    stand_in = harness.stand_in
    await harness.agent.shutdown()
    harness.claude = ScriptedClaude(
        _twice(
            "print_output", {"output_id": "o1"}, lambda: stand_in.tiers.update(print_output="write")
        )
    )
    async with harness.restart_agent():
        sid, handle = await harness.session(agent_db)
        await send(handle, "print it")
        await _approve_next(handle)
        assert await settled(connect, sid) == "idle"
        log = await events(agent_db, sid)
        assert _types(log).count("approval.required") == 1
        assert [r[0] for r in stand_in.ran] == ["print_output", "print_output"]
        texts = [e["delta"] for e in log if e["type"] == "assistant.text.delta"]
        assert texts == ["print_output: ok: printed"]
        await _replays(harness, handle)


async def test_a_tier_raised_mid_turn_parks_the_next_call(
    harness: Harness, agent_db: Conn, connect: Connect
) -> None:
    stand_in = harness.stand_in
    await harness.agent.shutdown()
    harness.claude = ScriptedClaude(
        _twice(
            "render_preview",
            {"model": "m"},
            lambda: stand_in.tiers.update(render_preview="outward"),
        )
    )
    async with harness.restart_agent():
        sid, handle = await harness.session(agent_db)
        await send(handle, "render it")
        await _approve_next(handle)
        assert await settled(connect, sid) == "idle"
        log = await events(agent_db, sid)
        assert _types(log).count("approval.required") == 1
        assert [r[0] for r in stand_in.ran] == ["render_preview", "render_preview"]
        texts = [e["delta"] for e in log if e["type"] == "assistant.text.delta"]
        assert texts == ["render_preview: ok: rendered"]
        # The first ran as a read (retried on a lost worker), the raised one at most once.
        assert _scheduled_attempts(await handle.fetch_history(), "render_preview") == [3, 1]
        await _replays(harness, handle)


def _scheduled_attempts(history: WorkflowHistory, tool: str) -> list[int]:
    return [
        e.activity_task_scheduled_event_attributes.retry_policy.maximum_attempts
        for e in history.events
        if e.HasField("activity_task_scheduled_event_attributes")
        and e.activity_task_scheduled_event_attributes.activity_type.name == tool
    ]


def _both(prompt: str, history: list[HistoryItem]) -> ToolCall | list[ToolCall] | Final:
    """Two render_preview calls at once, then the last result."""
    if not history:
        return [
            ToolCall("render_preview", {"model": "a"}),
            ToolCall("render_preview", {"model": "b"}),
        ]
    return Final(f"{len(history)} results")


async def test_a_parked_call_runs_once_though_a_parallel_call_was_read_lower(
    harness: Harness, agent_db: Conn, connect: Connect
) -> None:
    """The tool object is shared by parallel calls: the one lowered after the first parked
    must not leave the parked one to be scheduled with retries (a second send)."""
    stand_in = harness.stand_in
    await _overrides(agent_db, {"render_preview": "outward"}, [])
    stand_in.tier_answers = ["outward", "read"]
    await harness.agent.shutdown()
    harness.claude = ScriptedClaude(_both)
    async with harness.restart_agent():
        sid, handle = await harness.session(agent_db)
        await send(handle, "render both")
        await _approve_next(handle)
        assert await settled(connect, sid) == "idle"
        assert stand_in.tiers_asked == ["render_preview", "render_preview"]
        assert _types(await events(agent_db, sid)).count("approval.required") == 1
        assert len(stand_in.ran) == 2
        assert _scheduled_attempts(await handle.fetch_history(), "render_preview") == [1, 1]
        await _replays(harness, handle)


async def test_a_call_lowered_while_parked_runs_once_approved(
    harness: Harness, agent_db: Conn, connect: Connect
) -> None:
    await _overrides(agent_db, {"render_preview": "outward"}, [])
    sid, handle = await harness.session(agent_db)
    await send(handle, "render it")
    [entry] = await _parked(handle)
    harness.stand_in.tiers["render_preview"] = "read"
    await handle.execute_update(
        RESPOND_UPDATE, _respond(entry, {"kind": "approval", "decision": "approve"})
    )
    assert await settled(connect, sid) == "idle"
    assert [r[0] for r in harness.stand_in.ran] == ["render_preview"]
    assert _scheduled_attempts(await handle.fetch_history(), "render_preview") == [1]
    await _replays(harness, handle)


async def test_a_tool_disabled_mid_turn_is_refused_naming_settings(
    harness: Harness, agent_db: Conn, connect: Connect
) -> None:
    stand_in = harness.stand_in
    await harness.agent.shutdown()
    harness.claude = ScriptedClaude(
        _twice("print_output", {"output_id": "o1"}, lambda: stand_in.disabled.add("print_output"))
    )
    async with harness.restart_agent():
        sid, handle = await harness.session(agent_db)
        await send(handle, "print it")
        await _approve_next(handle)
        assert await settled(connect, sid) == "idle"
        log = await events(agent_db, sid)
        # Never parked: its activity refuses it by name.
        assert _types(log).count("approval.required") == 1
        assert [r[0] for r in stand_in.ran] == ["print_output"]
        [text] = [e["delta"] for e in log if e["type"] == "assistant.text.delta"]
        assert text.startswith("print_output: error")
        assert "disabled in Settings → Tools & plugins" in text
        await _replays(harness, handle)


async def test_a_call_whose_access_cannot_be_read_parks(
    harness: Harness, agent_db: Conn, connect: Connect, caplog: pytest.LogCaptureFixture
) -> None:
    """Fail toward asking: a read tool whose tool_tier fails is asked about, and the
    workflow says once why (review of #4838: an agent not yet rolled out)."""
    caplog.set_level(logging.WARNING)
    harness.stand_in.tool_tier_refuses = True
    sid, handle = await harness.session(agent_db)
    await send(handle, "render it")
    await _approve_next(handle)
    assert await settled(connect, sid) == "idle"
    assert [r[0] for r in harness.stand_in.ran] == ["render_preview"]
    assert harness.stand_in.tiers_asked == ["render_preview"]
    said = [
        r.getMessage() for r in caplog.records if "no tool_tier on agent-tools" in r.getMessage()
    ]
    assert len(said) == 1, said
    assert "is the agent rolled out?" in said[0]
    assert "ApplicationError" in said[0]
    await _replays(harness, handle)


async def test_a_turn_from_before_tool_access_replays(harness: Harness) -> None:
    """A history recorded before tool-access-2171 keeps its turn-start gates on this code."""
    path = Path(__file__).parent / "fixtures" / "durable_session_histories" / "raised.json"
    await _replay(harness, WorkflowHistory.from_json("raised", path.read_text(encoding="utf-8")))


async def test_an_outward_tool_lowered_to_read_runs_without_parking(
    harness: Harness, agent_db: Conn, connect: Connect
) -> None:
    """#2171: Settings may lower a tool. print_output at read runs with no approval."""
    await _overrides(agent_db, {"print_output": "read"}, [])
    sid, handle = await harness.session(agent_db)
    await send(handle, "print it")
    assert await settled(connect, sid) == "idle"
    log = await events(agent_db, sid)
    assert "approval.required" not in _types(log)
    assert [r[0] for r in harness.stand_in.ran] == ["print_output"]
    texts = [e["delta"] for e in log if e["type"] == "assistant.text.delta"]
    assert texts == ["print_output: ok: printed"]
    await _replays(harness, handle)


async def test_gate_settings_reads_lowered_raised_and_off_from_ai_access(
    agent_db: Conn, connect: Connect
) -> None:
    tools.use_manifest(MANIFEST)
    acts = SessionActivities(connect)
    await _overrides(agent_db, {"render_preview": "outward", "print_output": "write"}, [])
    await agent_db.execute(
        "INSERT INTO ai_access (source, item, access, updated_by) VALUES"
        " ('builtin:scadbuddy', 'wait_for_user', 'off', 'browser:browser'),"
        " ('remote:hs', 'print_output', 'off', 'browser:browser')"
    )
    read = await acts.gate_settings()
    assert read.tool_tiers == {"render_preview": "outward", "print_output": "write"}
    assert read.disabled_tools == []  # an answer tool is in no source; remote:hs is not ours
    await _overrides(agent_db, {}, ["print_output"])
    off = await acts.gate_settings()
    assert (off.tool_tiers, off.disabled_tools) == ({}, ["print_output"])


async def test_the_camera_off_is_refused_by_the_durable_gate_and_never_parks(
    harness: Harness, agent_db: Conn, connect: Connect
) -> None:
    """The camera switch is the camera's row (#2171): off, a durable call of it is refused
    naming Settings, never parked, and the segment runner leaves it undeclared."""
    camera = tools.ManifestEntry("get_printer_camera", "A frame.", SCHEMA, "read", None)
    tools.use_manifest([*MANIFEST, camera])
    await _overrides(agent_db, {}, ["get_printer_camera"])
    sid, handle = await harness.session(agent_db)
    await send(handle, "watch it")
    assert await settled(connect, sid) == "idle"
    assert harness.stand_in.ran == []
    log = await events(agent_db, sid)
    assert "approval.required" not in _types(log)
    [text] = [e["delta"] for e in log if e["type"] == "assistant.text.delta"]
    assert text.startswith("get_printer_camera: error")
    assert "disabled in Settings → Tools & plugins" in text
    context = await handle.query(SEGMENT_CONTEXT_QUERY, result_type=SegmentContext)
    assert context.disabled_tools == ["get_printer_camera"]
    await _replays(harness, handle)


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
    assert "disabled in Settings → Tools & plugins" in texts[0]
    # What the segment runner reads, to leave the tool undeclared.
    context = await handle.query(SEGMENT_CONTEXT_QUERY, result_type=SegmentContext)
    assert context.disabled_tools == ["render_preview"]
    await _replays(harness, handle)


class OlderSettings(SessionActivities):
    """gate_settings as a worker before #2088 answered it: the timers alone."""

    @activity.defn(name="gate_settings")
    async def gate_settings(self) -> dict[str, int]:  # type: ignore[override]
        return {"approval_expiry_s": 600, "question_expiry_s": 3600}


async def test_a_gate_settings_result_without_overrides_still_parks_a_raised_call(
    harness: Harness, agent_db: Conn, connect: Connect
) -> None:
    """A gate_settings result as before #2088 (no overrides) no longer decides a registry
    call under tool-access-2171: the raised call parks by its access as it is now (#2171).
    A history recorded before the patch keeps its old commands:
    test_a_turn_from_before_tool_access_replays."""
    await harness.agent.shutdown()
    harness.session_activities = OlderSettings(connect)
    await _overrides(agent_db, {"render_preview": "outward"}, [])
    async with harness.restart_agent():
        sid, handle = await harness.session(agent_db)
        await send(handle, "render it")
        await _approve_next(handle)
        assert await settled(connect, sid) == "idle"
    assert [r[0] for r in harness.stand_in.ran] == ["render_preview"]
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


def _bash_then_asks(prompt: str, history: list[HistoryItem]) -> ToolCall | Final:
    """Calls Bash, then asks a question, which holds the turn open to be read."""
    if not history:
        return ToolCall("Bash", {"command": "make notes"})
    if len(history) == 1:
        return ToolCall("ask_user", QUESTION)
    return Final("done")


async def test_bash_off_is_refused_and_lowered_runs_unasked(
    harness: Harness, agent_db: Conn, connect: Connect
) -> None:
    """#4757: an allowed package's Bash takes its access as each call comes (builtin_tier):
    off, it is refused in a classic turn's words and never parks; lowered to write, it runs
    with no one asked; outward, it parks as before."""
    harness.stand_in.turn_packages = [NOTES_PACKAGE]
    harness.stand_in.builtin_answers = ["off", "write", "outward"]
    await harness.agent.shutdown()
    harness.claude = ScriptedClaude(_bash_then_asks)
    async with harness.restart_agent():
        sid, handle = await harness.session(agent_db)
        await send(handle, "run it")
        [entry] = await _parked(handle)
        assert entry["kind"] == "answer"  # the question: Bash itself never parked
        context = await handle.query(SEGMENT_CONTEXT_QUERY, result_type=SegmentContext)
        assert list(context.replaced.values()) == [
            "Bash is disabled in Settings → Tools & plugins; the user can turn it back on there"
        ]
        assert await _open_rows(agent_db, sid) == 1
        assert harness.stand_in.builtin_asked == [{"tool": "Bash", "packages": ["notes"]}]
        await _replays(harness, handle)
    harness.claude = None
    async with harness.restart_agent():
        sid, handle = await harness.session(agent_db)
        await send(handle, "run it")
        assert await settled(connect, sid) == "idle"
        log = await events(agent_db, sid)
        assert "approval.required" not in _types(log)
        assert await _open_rows(agent_db, sid) == 0
        texts = [e["delta"] for e in log if e["type"] == "assistant.text.delta"]
        assert len(texts) == 1 and texts[0].startswith("Bash: ok"), texts
        await _replays(harness, handle)
        sid, handle = await harness.session(agent_db)
        await send(handle, "run it")
        [entry] = await _parked(handle)
        assert (entry["kind"], entry["tool"]) == ("approval", "Bash")
        await _replays(harness, handle)


def _bash_twice(prompt: str, history: list[HistoryItem]) -> ToolCall | Final:
    """Calls Bash, then Bash again, then ends."""
    if len(history) < 2:
        return ToolCall("Bash", {"command": "make notes"})
    return Final("done")


async def test_an_off_bash_is_refused_then_an_outward_one_parks_and_runs_once_approved(
    harness: Harness, agent_db: Conn, connect: Connect
) -> None:
    """#4757 final review M4: ``bash_access.json`` is this history, recorded under
    builtin-access-4757: builtin_tier answers off (refused in a classic turn's words), then
    outward (an approval entry; it runs once approved)."""
    harness.stand_in.turn_packages = [NOTES_PACKAGE]
    harness.stand_in.builtin_answers = ["off", "outward"]
    await harness.agent.shutdown()
    harness.claude = ScriptedClaude(_bash_twice)
    async with harness.restart_agent():
        sid, handle = await harness.session(agent_db)
        await send(handle, "run it")
        [entry] = await _parked(handle)
        assert (entry["kind"], entry["tool"]) == ("approval", "Bash")
        context = await handle.query(SEGMENT_CONTEXT_QUERY, result_type=SegmentContext)
        assert list(context.replaced.values()) == [
            "Bash is disabled in Settings → Tools & plugins; the user can turn it back on there"
        ]
        await handle.execute_update(
            RESPOND_UPDATE, _respond(entry, {"kind": "approval", "decision": "approve"})
        )
        assert await settled(connect, sid) == "idle"
        assert harness.stand_in.builtin_asked == [{"tool": "Bash", "packages": ["notes"]}] * 2
        await _replays(harness, handle)


async def test_a_bash_turn_under_builtin_access_from_its_fixture_replays(harness: Harness) -> None:
    """``bash_access.json``: the history of
    test_an_off_bash_is_refused_then_an_outward_one_parks_and_runs_once_approved."""
    path = Path(__file__).parent / "fixtures" / "durable_session_histories" / "bash_access.json"
    await _replay(harness, WorkflowHistory.from_json("bash", path.read_text(encoding="utf-8")))


async def test_builtin_tier_failing_parks_after_three_attempts(
    harness: Harness, agent_db: Conn, connect: Connect
) -> None:
    """An agent that cannot answer builtin_tier (an older one, mid-rollout) leaves the call
    to a person: three attempts, then it parks."""
    harness.stand_in.turn_packages = [NOTES_PACKAGE]
    harness.stand_in.builtin_fails = "unregistered"
    _, handle = await harness.session(agent_db)
    await send(handle, "run it")
    [entry] = await _parked(handle)
    assert (entry["kind"], entry["tool"]) == ("approval", "Bash")
    assert len(harness.stand_in.builtin_asked) == 3
    await _replays(harness, handle)


async def test_builtin_tier_whose_read_fails_refuses_the_call_unparked(
    harness: Harness, agent_db: Conn, connect: Connect
) -> None:
    """PR #5001 review: an agent that serves builtin_tier but cannot read ai_access refuses
    the call, as a classic turn's seam does (permissions.ts decideLive); only an agent that
    does not serve the activity leaves it to a person."""
    harness.stand_in.turn_packages = [NOTES_PACKAGE]
    harness.stand_in.builtin_fails = "read"
    await harness.agent.shutdown()
    harness.claude = ScriptedClaude(_bash_then_asks)
    async with harness.restart_agent():
        _, handle = await harness.session(agent_db)
        await send(handle, "run it")
        [entry] = await _parked(handle)
        assert entry["kind"] == "answer"  # the question: Bash itself never parked
        context = await handle.query(SEGMENT_CONTEXT_QUERY, result_type=SegmentContext)
        assert list(context.replaced.values()) == [
            "Bash is refused: its Settings → Tools & plugins entry cannot be read"
        ]
        assert len(harness.stand_in.builtin_asked) == 3
        await _replays(harness, handle)
    harness.claude = None


async def test_a_turn_from_before_builtin_access_replays(harness: Harness) -> None:
    """A history recorded before builtin-access-4757 (an allowed package's Bash call that
    parked, then ran once approved) replays on this code without asking builtin_tier."""
    path = Path(__file__).parent / "fixtures" / "durable_session_histories" / "bash.json"
    await _replay(harness, WorkflowHistory.from_json("bash", path.read_text(encoding="utf-8")))


SKILL_PACKAGE = {**NOTES_PACKAGE, "builtin_tools": ["Skill"], "named_builtins": ["Skill"]}


async def _hooked_skill(
    harness: Harness, agent_db: Conn, answers: list[str]
) -> tuple[str, WorkflowHandle[Any, Any], asyncio.Task[Any]]:
    """A turn held open by a question, and the segment hook's ask about a Skill call."""
    harness.stand_in.turn_packages = [SKILL_PACKAGE]
    harness.stand_in.builtin_answers = answers
    sid, handle = await harness.session(agent_db)
    await send(handle, "ask me")
    await _parked(handle)  # the question: the turn runs
    asked = asyncio.ensure_future(
        handle.execute_update(
            BUILTIN_CALL_UPDATE,
            {"call_id": "toolu_skill", "tool": "Skill", "input": {"skill": "notes:jot"}},
            result_type=dict,
        )
    )
    return sid, handle, asked


async def test_an_outward_package_skill_parks_and_runs_once_approved(
    harness: Harness, agent_db: Conn, connect: Connect
) -> None:
    """#4757 (Task 6 review round 2): a Skill call an allowed package's built-in makes
    outward parks as its Bash would: one approval entry, same request id shape, run on approve."""
    sid, handle, asked = await _hooked_skill(harness, agent_db, ["outward"])

    async def two() -> bool:
        return len(await handle.query(PENDING_INPUT_QUERY)) == 2

    await until(two)
    [entry] = [e for e in await handle.query(PENDING_INPUT_QUERY) if e["tool"] == "Skill"]
    assert entry["kind"] == "approval"
    assert entry["id"].endswith(":toolu_skill") and entry["id"].startswith(f"durable:{sid}:")
    assert not asked.done()
    await handle.execute_update(
        RESPOND_UPDATE, _respond(entry, {"kind": "approval", "decision": "approve"})
    )
    assert await asked == {"go": True, "reason": None}
    assert harness.stand_in.builtin_asked == [{"tool": "Skill", "packages": ["notes"]}]
    await _replays(harness, handle)


async def test_an_outward_package_skill_denied_is_refused_with_why(
    harness: Harness, agent_db: Conn, connect: Connect
) -> None:
    _, handle, asked = await _hooked_skill(harness, agent_db, ["outward"])

    async def two() -> bool:
        return len(await handle.query(PENDING_INPUT_QUERY)) == 2

    await until(two)
    [entry] = [e for e in await handle.query(PENDING_INPUT_QUERY) if e["tool"] == "Skill"]
    await handle.execute_update(
        RESPOND_UPDATE, _respond(entry, {"kind": "approval", "decision": "deny"})
    )
    answer = await asked
    assert answer["go"] is False
    assert answer["reason"] == refusal("Skill", entry["id"], "denied", None)
    await _replays(harness, handle)


@pytest.mark.parametrize(("tier", "go"), [("read", True), ("write", True), ("off", False)])
async def test_a_package_skill_read_or_off_is_answered_without_parking(
    harness: Harness, agent_db: Conn, connect: Connect, tier: str, go: bool
) -> None:
    sid, handle, asked = await _hooked_skill(harness, agent_db, [tier])
    answer = await asked
    assert answer["go"] is go
    if not go:
        assert answer["reason"] == (
            "Skill is disabled in Settings → Tools & plugins; the user can turn it back on there"
        )
    assert [e["tool"] for e in await handle.query(PENDING_INPUT_QUERY)] == ["ask_user"]
    assert await _open_rows(agent_db, sid) == 1
    await _replays(harness, handle)


@pytest.mark.parametrize("decision", ["approve", "deny"])
async def test_a_hooked_call_over_the_offload_size_parks_with_history_holding_a_reference(
    harness: Harness, agent_db: Conn, connect: Connect, decision: str
) -> None:
    """#5000: External Storage takes the ``builtin_call`` Update's argument out of history as
    it takes every other payload over OFFLOAD_BYTES, so a hooked call's large input parks
    (as a classic turn's does) and is answered by its decision; no event holds the input."""
    harness.stand_in.turn_packages = [SKILL_PACKAGE]
    harness.stand_in.builtin_answers = ["outward"]
    sid, handle = await harness.session(agent_db)
    await send(handle, "ask me")
    await _parked(handle)
    big = {"skill": "notes:jot", "args": "a long note " + "q" * (OFFLOAD_BYTES * 2)}
    asked = asyncio.ensure_future(
        handle.execute_update(
            BUILTIN_CALL_UPDATE,
            {"call_id": "toolu_big", "tool": "Skill", "input": big},
            result_type=dict,
        )
    )

    async def two() -> bool:
        return len(await handle.query(PENDING_INPUT_QUERY)) == 2

    await until(two)
    [entry] = [e for e in await handle.query(PENDING_INPUT_QUERY) if e["tool"] == "Skill"]
    assert entry["id"].endswith(":toolu_big")
    await handle.execute_update(
        RESPOND_UPDATE, _respond(entry, {"kind": "approval", "decision": decision})
    )
    answer = await asked
    if decision == "approve":
        assert answer == {"go": True, "reason": None}
    else:
        assert answer["go"] is False
        assert answer["reason"] == refusal("Skill", entry["id"], "denied", None)
    history = await handle.fetch_history()
    [accepted] = [
        e.workflow_execution_update_accepted_event_attributes.accepted_request.input
        for e in history.events
        if e.HasField("workflow_execution_update_accepted_event_attributes")
        and e.workflow_execution_update_accepted_event_attributes.accepted_request.input.name
        == BUILTIN_CALL_UPDATE
    ]
    [arg] = accepted.args.payloads
    assert len(arg.external_payloads) == 1 and arg.ByteSize() < 4096  # a reference only
    assert max(e.ByteSize() for e in history.events) < OFFLOAD_BYTES + 4096
    assert any((harness.payload_dir / f"session-{sid}").iterdir())
    await _replays(harness, handle)


async def test_a_hooked_call_outside_a_turn_is_refused(
    harness: Harness, agent_db: Conn, connect: Connect
) -> None:
    _, handle = await harness.session(agent_db)
    answer = await handle.execute_update(
        BUILTIN_CALL_UPDATE,
        {"call_id": "t1", "tool": "Skill", "input": {"skill": "x"}},
        result_type=dict,
    )
    assert answer["go"] is False


async def _accepted_updates(handle: WorkflowHandle[Any, Any]) -> list[str]:
    history = await handle.fetch_history()
    return [
        e.workflow_execution_update_accepted_event_attributes.accepted_request.input.name
        for e in history.events
        if e.HasField("workflow_execution_update_accepted_event_attributes")
    ]


@pytest.mark.parametrize(
    "args",
    [
        {"call_id": "t2", "tool": "Bash", "input": {}},
        {"call_id": "", "tool": "Skill", "input": {}},
        {"call_id": "t3", "tool": "Skill", "input": "not an object"},
        {"tool": "Skill", "input": {}},
    ],
)
async def test_a_malformed_hooked_call_is_rejected_leaving_no_history(
    harness: Harness, agent_db: Conn, connect: Connect, args: dict[str, Any]
) -> None:
    """Task 6 re-review R3: the static refusals are the validator's, so none is recorded."""
    _, handle = await harness.session(agent_db)
    before = await _accepted_updates(handle)
    with pytest.raises(WorkflowUpdateFailedError):
        await handle.execute_update(BUILTIN_CALL_UPDATE, args, result_type=dict)
    with pytest.raises(WorkflowUpdateFailedError):
        await handle.execute_update(BUILTIN_RELEASE_UPDATE, {"call_id": "nobody"})
    assert await _accepted_updates(handle) == before


async def test_a_hooked_call_whose_segment_ended_is_cancelled_and_leaves_the_panel(
    harness: Harness, agent_db: Conn, connect: Connect
) -> None:
    """Task 6 re-review R5: the segment's end releases its waiting call: the entry is
    cancelled, leaves the panel, and the ask is answered as refused."""
    sid, handle, asked = await _hooked_skill(harness, agent_db, ["outward"])

    async def two() -> bool:
        return len(await handle.query(PENDING_INPUT_QUERY)) == 2

    await until(two)
    await handle.execute_update(BUILTIN_RELEASE_UPDATE, {"call_id": "toolu_skill"})
    assert [e["tool"] for e in await handle.query(PENDING_INPUT_QUERY)] == ["ask_user"]
    answer = await asked
    assert answer["go"] is False
    assert await _open_rows(agent_db, sid) == 1
    await _replays(harness, handle)


async def test_a_hooked_skill_from_its_fixture_replays(harness: Harness) -> None:
    """``skill.json``: test_an_outward_package_skill_parks_and_runs_once_approved's history."""
    path = Path(__file__).parent / "fixtures" / "durable_session_histories" / "skill.json"
    await _replay(harness, WorkflowHistory.from_json("skill", path.read_text(encoding="utf-8")))


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


async def test_a_session_stays_on_the_replica_its_plugin_calls_are_pinned_to(
    harness: Harness, agent_db: Conn, connect: Connect
) -> None:
    """#2581: the replica that holds the session's plugin MCP clients lists its next turn
    too, so the pin (and a browser's page) outlives the turn; the session's end closes
    them there."""
    host = f"agent-tools.pod-{uuid.uuid4().hex[:6]}"
    harness.stand_in.turn_offered = [NOTES_POST]
    harness.stand_in.host_queue = host
    async with Worker(harness.client, task_queue=host, activities=harness.stand_in.activities()):
        sid, handle = await harness.session(agent_db)
        await send(handle, "get it")
        assert await settled(connect, sid) == "idle"
        await agent_db.execute("UPDATE ai_sessions SET status = 'running' WHERE id = %s", (sid,))
        await send_next(handle, "hello there")
        assert await settled(connect, sid) == "idle"
        await handle.signal(END_SIGNAL, {})
        await asyncio.wait_for(handle.result(), 30)
    assert harness.stand_in.called_on == [host]
    assert harness.stand_in.listed_on == [TOOLS_QUEUE, host]
    assert harness.stand_in.closed_on == [host]
    await _replays(harness, handle)


async def test_a_session_whose_replica_is_gone_lists_its_next_turn_anywhere(
    harness: Harness, agent_db: Conn, connect: Connect
) -> None:
    """#2581: a pinned replica that does not pick the listing up is gone: the turn is
    listed on agent-tools (its plugins start new sessions there), and goes on."""
    harness.stand_in.turn_offered = [NOTES_POST]
    harness.stand_in.host_queue = f"agent-tools.gone-{uuid.uuid4().hex[:6]}"
    sid, handle = await harness.session(agent_db)
    await send(handle, "hello there")
    assert await settled(connect, sid) == "idle"
    harness.stand_in.host_queue = None
    await agent_db.execute("UPDATE ai_sessions SET status = 'running' WHERE id = %s", (sid,))
    await send_next(handle, "get it")
    assert await settled(connect, sid, timeout=60) == "idle"
    assert harness.stand_in.listed_on == [TOOLS_QUEUE, TOOLS_QUEUE]
    assert harness.stand_in.ran == [("turn:notes__post", {"method": "GET", "body": "y"})]
    await handle.signal(END_SIGNAL, {})
    await asyncio.wait_for(handle.result(), 30)
    # Nothing was pinned last: nothing to close.
    assert harness.stand_in.closed_on == []
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


async def test_a_switched_off_package_servers_call_is_refused_without_parking(
    harness: Harness, agent_db: Conn, connect: Connect
) -> None:
    """#4757: turn_tool_tier answers a switched-off package server's call as refused, so it
    never parks for an approval that could not run it; its call refuses it with the reason."""
    off = "api of plugin package notes is switched off in Settings → Tools & plugins"
    harness.stand_in.turn_offered = [NOTES_POST]
    harness.stand_in.turn_refused = off
    sid, handle = await harness.session(agent_db)
    await send(handle, "post it")
    assert await settled(connect, sid) == "idle"
    log = await events(agent_db, sid)
    assert "approval.required" not in _types(log)
    assert harness.stand_in.ran == []
    assert off in json.dumps(log, ensure_ascii=False)
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


async def _fork_point(handle: WorkflowHandle[Any, Any], limit: int) -> ForkPoint:
    """Every page of ``fork_point``, as the agent service reads them (sessions/durable.ts)."""
    first: ForkPoint = await handle.query(
        FORK_POINT_QUERY, ForkPointArgs(start=0, limit=limit), result_type=ForkPoint
    )
    entries = list(first.entries)
    while len(entries) < first.total:
        page: ForkPoint = await handle.query(
            FORK_POINT_QUERY, ForkPointArgs(start=len(entries), limit=limit), result_type=ForkPoint
        )
        assert page.total == first.total and page.entries
        entries.extend(page.entries)
    return dataclasses.replace(first, entries=entries)


async def test_a_fork_starts_with_the_parents_conversation_and_goes_on_from_it(
    harness: Harness, agent_db: Conn, connect: Connect
) -> None:
    """#2688: ``fork_point`` serves the committed conversation a page at a time, and a
    session started with it as its agent's state continues it: the model sees the
    parent's tool call. The parent goes on unchanged."""
    sid, handle = await harness.session(agent_db)
    await send(handle, "render it")
    assert await settled(connect, sid) == "idle"
    point = await _fork_point(handle, limit=1)  # one entry a page
    assert point.busy is False
    assert point.session_id and point.checkpoint
    assert point.total == len(point.entries) >= 2
    assert json.loads(point.entries[-1])["uuid"] == point.checkpoint

    child = await insert_session(agent_db)
    seed = AgentState(
        session_id=point.session_id, checkpoint=point.checkpoint, conversation=point.entries
    )
    forked = await harness.client.start_workflow(
        DurableSession.run,
        SessionStart(session_id=child, creator=ME, owner=ME, agent=seed),
        id=f"session-{child}",
        task_queue=harness.queue,
        execution_timeout=timedelta(minutes=10),
    )
    harness.handles.append(forked)
    await send(forked, "and now?")
    assert await settled(connect, child) == "idle"
    texts = [
        e["delta"] for e in await events(agent_db, child) if e["type"] == "assistant.text.delta"
    ]
    # The scripted model reports the last tool call it has seen: the parent's.
    assert texts == ["render_preview: ok: rendered"]
    grown = await _fork_point(forked, limit=1 << 20)
    assert grown.entries[: point.total] == point.entries
    assert grown.total > point.total
    # Sealed under the child's own key: its history holds none of the parent's words.
    raw = b"".join(e.SerializeToString() for e in (await forked.fetch_history()).events)
    assert b"render it" not in raw
    assert (await _fork_point(handle, limit=1 << 20)).entries == point.entries


async def test_a_fork_of_an_interrupted_turn_carries_what_its_parent_still_owes(
    harness: Harness, agent_db: Conn, connect: Connect
) -> None:
    """#4173: a turn stopped at a paused call commits nothing past that pause; its result
    waits in the agent's ``pending`` for the next segment. ``fork_point`` hands that on
    with the checkpoint, so the child's first segment delivers it as the parent's would
    (seeded without it, the engine re-announced the paused call and the segment failed)."""
    sid, handle = await harness.session(agent_db)
    await send(handle, "print it")
    [entry] = await _parked(handle)
    await handle.signal(INTERRUPT_SIGNAL, {"reason": "stop"})
    assert await settled(connect, sid) == "idle"
    point = await _fork_point(handle, limit=1 << 20)
    assert point.busy is False
    call = entry["id"].rsplit(":", 1)[-1]  # durable:<session>:<run>:<tool_use_id>
    assert list(point.pending) == [call]
    assert point.pending[call].is_error
    assert point.fork_next is True

    child = await insert_session(agent_db)
    seed = AgentState(
        session_id=point.session_id,
        checkpoint=point.checkpoint,
        conversation=point.entries,
        pending=point.pending,
        recent_call_ids=point.recent_call_ids,
        fork_next=point.fork_next,
    )
    forked = await harness.client.start_workflow(
        DurableSession.run,
        SessionStart(session_id=child, creator=ME, owner=ME, agent=seed),
        id=f"session-{child}",
        task_queue=harness.queue,
        execution_timeout=timedelta(minutes=10),
    )
    harness.handles.append(forked)
    await send(forked, "and now?")
    assert await settled(connect, child) == "idle"
    log = await events(agent_db, child)
    assert not [e for e in log if e["type"] == "error"]
    texts = [e["delta"] for e in log if e["type"] == "assistant.text.delta"]
    # The scripted model reports its history's last call: the parent's, ended by the stop.
    assert len(texts) == 1 and texts[0].startswith("print_output: error")
    assert harness.stand_in.ran == []
    grown = await _fork_point(forked, limit=1 << 20)
    assert grown.pending == {} and grown.fork_next is False
