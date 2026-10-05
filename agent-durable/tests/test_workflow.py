"""The DurableSession workflow (plan task 9) on ScriptedClaude, with stub tools on agent-tools."""

from __future__ import annotations

import asyncio
import dataclasses
import os
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from temporalio import activity
from temporalio.api.enums.v1 import EventType
from temporalio.claude_agent_sdk import AgentState, ClaudeAgentPlugin, ToolOutcome, follow_agent
from temporalio.claude_agent_sdk.testing import Final, HistoryItem, ScriptedClaude, ToolCall
from temporalio.client import (
    Client,
    WithStartWorkflowOperation,
    WorkflowHandle,
    WorkflowHistory,
    WorkflowUpdateFailedError,
)
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from scadbuddy_durable.models import (
    DECISIONS_QUERY,
    EXPIRED_BY,
    PENDING_QUERY,
    REVIEW_UPDATE,
    SEND_UPDATE,
    TASK_QUEUE,
    WORKFLOW_NAME,
    InFlight,
    Message,
    Restored,
    SessionInput,
    SnapshotInput,
    render_prompt,
    restore_state,
)
from scadbuddy_durable.segments import SAVE_SNAPSHOT
from scadbuddy_durable.tools import TOOL_QUEUE, TOOLS
from scadbuddy_durable.workflow import DurableSession
from tests.short_runs import ShortRuns

HISTORIES = Path(__file__).parent / "histories"
BROWSER = "browser:browser"
STOPPED = "the previous run of this session stopped unexpectedly"
INTERRUPTED = (
    "This tool call was interrupted ({}); whether it took effect is unknown. Check before running it again."
)
CANCELLED_WAITING = "This tool call did not run: the Workflow was cancelled."
REJECTED = "A human reviewer rejected this action. Do not retry it."
LOST = (
    f"This tool call ran, but its result was lost when {STOPPED}. Check its effect before running it again."
)
DID_NOT_RUN = f"This tool call did not run: {STOPPED}."
WAIT = 60.0

temporal = pytest.mark.requires_temporal


# ---- the scripted Claude --------------------------------------------------------------


@dataclass
class Seen:
    """Every (prompt, history) the policy was asked about, in order."""

    calls: list[tuple[str, list[HistoryItem]]] = field(default_factory=list)

    def last_for(self, prompt: str) -> list[HistoryItem]:
        return [h for p, h in self.calls if p == prompt][-1]

    def first_for(self, prompt: str) -> list[HistoryItem]:
        return [h for p, h in self.calls if p == prompt][0]


def make_policy(seen: Seen) -> Callable[[str, list[HistoryItem]], ToolCall | list[ToolCall] | Final]:
    """`read …` calls get_model, `print …` calls print_output (outward), `both …` calls
    get_model and update_source in one message; anything else is answered at once. A
    task's calls carry its prompt as `tag`, so the policy knows which ones are its own."""

    def policy(prompt: str, history: list[HistoryItem]) -> ToolCall | list[ToolCall] | Final:
        seen.calls.append((prompt, list(history)))
        mine = [h for h in history if h.input.get("tag") == prompt]
        tag = {"tag": prompt}
        if prompt.startswith("read") and not mine:
            return ToolCall("get_model", tag)
        if prompt.startswith("print") and not mine:
            return ToolCall("print_output", tag)
        if prompt.startswith("both") and not mine:
            return [ToolCall("get_model", tag), ToolCall("update_source", tag)]
        return Final(f"answer to {prompt}")

    return policy


# ---- stub tools and the snapshot recorder ---------------------------------------------


class Stubs:
    """Activities named like the sample tools; a gated tool waits until released."""

    def __init__(self) -> None:
        self.runs: list[tuple[str, str, dict[str, Any]]] = []  # (call id, tool, input)
        self.gates: dict[str, asyncio.Event] = {}
        self.started: dict[str, asyncio.Event] = {t.name: asyncio.Event() for t in TOOLS}

    def gate(self, name: str) -> asyncio.Event:
        self.gates[name] = asyncio.Event()
        return self.gates[name]

    def ids(self, name: str | None = None) -> list[str]:
        return [i for i, n, _ in self.runs if name is None or n == name]

    def activities(self) -> list[Callable[..., Any]]:
        def make(name: str) -> Callable[..., Any]:
            async def stub(args: dict[str, Any]) -> dict[str, Any]:
                self.runs.append((activity.info().activity_id.removeprefix("tool-"), name, args))
                self.started[name].set()
                gate = self.gates.get(name)
                if gate is not None:
                    await gate.wait()
                return {"ran": name, "tag": args.get("tag")}

            return activity.defn(name=name)(stub)

        return [make(t.name) for t in TOOLS]

    def release(self) -> None:
        for gate in self.gates.values():
            gate.set()


class Snaps:
    def __init__(self) -> None:
        self.saved: list[SnapshotInput] = []

        @activity.defn(name=SAVE_SNAPSHOT)
        async def save_snapshot(inp: SnapshotInput) -> None:
            self.saved.append(inp)

        self.activity = save_snapshot

    def latest(self, session_id: str) -> SnapshotInput:
        return [s for s in self.saved if s.session_id == session_id][-1]


@dataclass
class Rig:
    client: Client
    seen: Seen
    stubs: Stubs
    snaps: Snaps
    ids: list[str] = field(default_factory=list)

    def new(self, *, expiry: int = 3600) -> tuple[str, SessionInput]:
        sid = str(uuid.uuid4())
        self.ids.append(f"session-{sid}")
        return f"session-{sid}", SessionInput(session_id=sid, max_turns=7, approval_expiry_seconds=expiry)

    def handle(self, wid: str) -> WorkflowHandle[Any, AgentState]:
        return self.client.get_workflow_handle_for(DurableSession.run, wid)

    async def send(
        self,
        wid: str,
        inp: SessionInput,
        text: str,
        state: AgentState | None = None,
        *,
        context: str | None = None,
        workflow: Any = DurableSession,
    ) -> WorkflowHandle[Any, AgentState]:
        """Update-with-start, as the agent service sends every message (ruling 6)."""
        op: WithStartWorkflowOperation[Any, AgentState] = WithStartWorkflowOperation(
            workflow.run,
            args=[inp, state, None],
            id=wid,
            task_queue=TASK_QUEUE,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        )
        await self.client.execute_update_with_start_workflow(
            DurableSession.send_message, Message(text, context), start_workflow_operation=op
        )
        return await op.workflow_handle()

    async def event(self, wid: str, kind: str, count: int = 1, from_offset: int = 0) -> dict[str, Any]:
        """The `count`-th live event of `kind` from `from_offset` (the run's events)."""

        async def find() -> dict[str, Any]:
            n = 0
            async for ev in follow_agent(self.client, wid, from_offset=from_offset):
                if ev["type"] == kind:
                    n += 1
                    if n == count:
                        return ev
            raise AssertionError(f"{wid} closed before {count} {kind} event(s)")

        return await asyncio.wait_for(find(), WAIT)

    async def pending(self, wid: str) -> dict[str, Any]:
        async def poll() -> dict[str, Any]:
            while True:
                items: list[dict[str, Any]] = await self.handle(wid).query(PENDING_QUERY)
                if items:
                    return items[0]
                await asyncio.sleep(0.1)

        return await asyncio.wait_for(poll(), WAIT)

    async def started(self, name: str) -> None:
        await asyncio.wait_for(self.stubs.started[name].wait(), WAIT)

    async def snapshot_where(self, sid: str, pred: Callable[[SnapshotInput], bool]) -> SnapshotInput:
        async def poll() -> SnapshotInput:
            while True:
                hits = [s for s in self.snaps.saved if s.session_id == sid and pred(s)]
                if hits:
                    return hits[-1]
                await asyncio.sleep(0.05)

        return await asyncio.wait_for(poll(), WAIT)


@asynccontextmanager
async def rig_on(client: Client, state_dir: Path) -> AsyncIterator[Rig]:
    seen, stubs, snaps = Seen(), Stubs(), Snaps()
    runner = ScriptedClaude(make_policy(seen), state_dir)
    rig = Rig(client, seen, stubs, snaps)
    try:
        async with (
            Worker(
                client,
                task_queue=TASK_QUEUE,
                workflows=[DurableSession, ShortRuns],
                activities=[snaps.activity],
                plugins=[ClaudeAgentPlugin(runner, heartbeat_every=1.0)],
            ),
            Worker(client, task_queue=TOOL_QUEUE, activities=stubs.activities()),
        ):
            try:
                yield rig
            finally:
                stubs.release()
                for wid in rig.ids:
                    try:
                        await client.get_workflow_handle(wid).terminate("test over")
                    except Exception:
                        pass
    finally:
        stubs.release()


@pytest_asyncio.fixture
async def rig(temporal_env: Client, tmp_path: Path) -> AsyncIterator[Rig]:
    async with rig_on(temporal_env, tmp_path / "sessions") as r:
        yield r


async def accepted(handle: WorkflowHandle[Any, Any], update: str) -> int:
    """How many `update` Updates the history accepted (Workflow Streams polls are Updates too)."""
    return sum(
        1
        for e in (await handle.fetch_history()).events
        if e.event_type == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_UPDATE_ACCEPTED
        and e.workflow_execution_update_accepted_event_attributes.accepted_request.input.name == update
    )


# ---- models (no Temporal) ----------------------------------------------------------------


def test_names() -> None:
    assert (WORKFLOW_NAME, TASK_QUEUE) == ("DurableSession", "agent")
    assert (SEND_UPDATE, REVIEW_UPDATE) == ("send_message", "review")
    assert (PENDING_QUERY, DECISIONS_QUERY, EXPIRED_BY) == (
        "pending_approvals",
        "decisions",
        "system:expired",
    )


def test_render_prompt_keeps_the_page_context_for_the_model() -> None:
    assert render_prompt(Message("hello")) == "hello"
    assert render_prompt(Message("hello", "page: /models/x")) == "hello\n\npage: /models/x"


def _state(**kw: Any) -> AgentState:
    base: dict[str, Any] = {
        "session_id": "s1",
        "checkpoint": "cp_1",
        "task_prompt": "do it",
        "task_segments": 2,
        "pending": {"old": ToolOutcome(content="x")},
        "recent_call_ids": ["a", "b"],
    }
    return AgentState(**{**base, **kw})


def test_restore_state_owes_each_in_flight_call_its_outcome() -> None:
    before = _state()
    after = restore_state(
        before,
        [InFlight("c1", "get_model", "started"), InFlight("c2", "print_output", "waiting for approval")],
    )
    assert after.pending["c1"] == ToolOutcome(content=INTERRUPTED.format(STOPPED), is_error=True)
    assert after.pending["c2"] == ToolOutcome(
        content=f"This tool call did not run: {STOPPED}.", is_error=True
    )
    assert after.recent_call_ids == ["a", "b", "c1", "c2"]
    assert after.task_prompt is None and after.task_segments == 0 and after.fork_next is True
    assert after.session_id == "s1" and after.checkpoint == "cp_1"


def test_restore_state_owes_every_status_of_the_unanswered_batch() -> None:
    statuses = ["started", "cancelled", "waiting for approval", "done", "failed", "rejected", "unknown tool"]
    after = restore_state(_state(), [InFlight(f"c{i}", "t", st) for i, st in enumerate(statuses)])
    contents = [after.pending[f"c{i}"].content for i in range(len(statuses))]
    assert contents == [
        INTERRUPTED.format(STOPPED),
        INTERRUPTED.format(STOPPED),
        DID_NOT_RUN,
        LOST,
        LOST,
        REJECTED,
        DID_NOT_RUN,
    ]
    assert all(after.pending[f"c{i}"].is_error for i in range(len(statuses)))
    assert after.recent_call_ids == ["a", "b", *(f"c{i}" for i in range(len(statuses)))]


def test_restore_state_caps_recent_call_ids_keeping_the_newest() -> None:
    before = _state(recent_call_ids=[f"r{i}" for i in range(256)])
    after = restore_state(before, [InFlight("new", "get_model", "started")])
    assert len(after.recent_call_ids) == 256
    assert after.recent_call_ids[-1] == "new" and after.recent_call_ids[0] == "r1"


def test_restore_state_without_a_checkpoint_starts_a_new_session() -> None:
    after = restore_state(_state(checkpoint=None), [])
    assert after.session_id is None


def test_restore_state_does_not_mutate_its_input() -> None:
    before = _state()
    restore_state(before, [InFlight("c1", "get_model", "started")])
    assert before == _state()


# ---- the workflow ------------------------------------------------------------------------


@temporal
async def test_a_message_is_answered_and_the_next_continues_the_session(rig: Rig) -> None:
    wid, inp = rig.new()
    await rig.send(wid, inp, "read 1", context="page: /models/x")
    done = await rig.event(wid, "done")
    assert done["result"] == "answer to read 1\n\npage: /models/x"
    await rig.send(wid, inp, "what did you read?")
    assert (await rig.event(wid, "done", 2))["result"] == "answer to what did you read?"
    earlier = rig.seen.last_for("what did you read?")
    assert [(h.name, h.content) for h in earlier] == [
        ("get_model", {"ran": "get_model", "tag": "read 1\n\npage: /models/x"})
    ]


@temporal
async def test_a_second_message_while_busy_is_refused_before_history(rig: Rig) -> None:
    gate = rig.stubs.gate("get_model")
    wid, inp = rig.new()
    handle = await rig.send(wid, inp, "read slow")
    await rig.started("get_model")
    with pytest.raises(WorkflowUpdateFailedError) as err:
        await handle.execute_update(DurableSession.send_message, Message("another"))
    assert "the session is busy" in str(err.value.cause)
    gate.set()
    await rig.event(wid, "done")
    assert await accepted(handle, SEND_UPDATE) == 1
    assert all(p != "another" for p, _ in rig.seen.calls)


@temporal
async def test_an_empty_message_is_refused(rig: Rig) -> None:
    wid, inp = rig.new()
    with pytest.raises(WorkflowUpdateFailedError) as err:
        await rig.send(wid, inp, "   ")
    assert "the message is empty" in str(err.value.cause)


@temporal
async def test_approve_runs_the_outward_call_once(rig: Rig) -> None:
    wid, inp = rig.new()
    handle = await rig.send(wid, inp, "print 1")
    pending = await rig.pending(wid)
    assert pending["name"] == "print_output"
    await handle.execute_update(DurableSession.review, args=[pending["id"], True, BROWSER])
    assert (await rig.event(wid, "done"))["result"] == "answer to print 1"
    assert rig.stubs.ids("print_output") == [pending["id"]]
    decisions: dict[str, str] = await handle.query(DECISIONS_QUERY)
    assert decisions[pending["id"]] == BROWSER
    if os.environ.get("SCADBUDDY_RECORD_HISTORIES"):
        HISTORIES.mkdir(exist_ok=True)
        (HISTORIES / "approve.json").write_text((await handle.fetch_history()).to_json(), encoding="utf-8")


@temporal
async def test_deny_never_runs_the_call(rig: Rig) -> None:
    wid, inp = rig.new()
    handle = await rig.send(wid, inp, "print 1")
    pending = await rig.pending(wid)
    await handle.execute_update(DurableSession.review, args=[pending["id"], False, BROWSER])
    await rig.event(wid, "done")
    assert rig.stubs.ids("print_output") == []
    last = rig.seen.last_for("print 1")[-1]
    assert last.id == pending["id"] and last.is_error and last.content == REJECTED


@temporal
async def test_review_twice_is_refused_the_second_time(rig: Rig) -> None:
    wid, inp = rig.new()
    handle = await rig.send(wid, inp, "print 1")
    pending = await rig.pending(wid)
    await handle.execute_update(DurableSession.review, args=[pending["id"], True, BROWSER])
    with pytest.raises(WorkflowUpdateFailedError) as err:
        await handle.execute_update(DurableSession.review, args=[pending["id"], True, BROWSER])
    assert "already decided" in str(err.value.cause) or "is waiting" in str(err.value.cause)
    await rig.event(wid, "done")
    assert rig.stubs.ids("print_output") == [pending["id"]]


@pytest_asyncio.fixture
async def skipping(_temporal_server: str) -> AsyncIterator[WorkflowEnvironment]:
    """A time-skipping test server (the controller's ruling for the expiry tests)."""
    env = await asyncio.wait_for(WorkflowEnvironment.start_time_skipping(), 120)
    try:
        yield env
    finally:
        await env.shutdown()


@temporal
async def test_an_unanswered_approval_expires(skipping: WorkflowEnvironment, tmp_path: Path) -> None:
    async with rig_on(skipping.client, tmp_path / "sessions") as rig:
        wid, inp = rig.new(expiry=300)
        handle = await rig.send(wid, inp, "print 1")
        pending = await rig.pending(wid)
        await skipping.sleep(timedelta(seconds=301))

        async def decided() -> dict[str, str]:
            while True:
                d: dict[str, str] = await handle.query(DECISIONS_QUERY)
                if d:
                    return d
                await asyncio.sleep(0.1)

        assert (await asyncio.wait_for(decided(), WAIT)) == {pending["id"]: EXPIRED_BY}
        # Review Focus 2: a review after expiry never runs the call.
        with pytest.raises(WorkflowUpdateFailedError) as err:
            await handle.execute_update(DurableSession.review, args=[pending["id"], True, BROWSER])
        assert "already decided" in str(err.value.cause) or "is waiting" in str(err.value.cause)
        await rig.event(wid, "done")
        last = rig.seen.last_for("print 1")[-1]
        assert last.is_error and last.content == REJECTED
        assert rig.stubs.ids("print_output") == []


@temporal
async def test_continue_as_new_keeps_the_conversation(rig: Rig) -> None:
    wid, inp = rig.new()
    for n in range(1, 5):
        await rig.send(wid, inp, f"read {n}", workflow=ShortRuns)
        await rig.event(wid, "done", n)
    assert rig.snaps.latest(inp.session_id).state.runs > 1
    assert [h.input["tag"] for h in rig.seen.first_for("read 4")] == ["read 1", "read 2", "read 3"]


@temporal
async def test_a_message_carried_in_the_inbox_is_answered(rig: Rig) -> None:
    wid, inp = rig.new()
    await rig.client.start_workflow(
        DurableSession.run, args=[inp, None, [Message("hello")]], id=wid, task_queue=TASK_QUEUE
    )
    assert (await rig.event(wid, "done"))["result"] == "answer to hello"


@temporal
async def test_parallel_calls_run_and_both_results_arrive(rig: Rig) -> None:
    wid, inp = rig.new()
    await rig.send(wid, inp, "both 1")
    await rig.event(wid, "done")
    assert sorted(n for _, n, _ in rig.stubs.runs) == ["get_model", "update_source"]
    history = rig.seen.last_for("both 1")
    assert sorted(h.name for h in history) == ["get_model", "update_source"]
    assert all(not h.is_error for h in history)


# ---- Stop and resume (deviation 4) --------------------------------------------------------


async def cancel_result(rig: Rig, wid: str) -> AgentState:
    handle = rig.handle(wid)
    await handle.cancel()
    return await asyncio.wait_for(handle.result(), WAIT)


@temporal
async def test_stop_during_a_tool_hands_over_the_state_and_resumes(rig: Rig) -> None:
    gate = rig.stubs.gate("get_model")
    wid, inp = rig.new()
    await rig.send(wid, inp, "read slow")
    await rig.started("get_model")
    [call_id] = rig.stubs.ids("get_model")
    session = rig.snaps.latest(inp.session_id).state.session_id
    state = await cancel_result(rig, wid)
    assert state.session_id == session and state.checkpoint is not None and state.fork_next
    assert state.pending[call_id] == ToolOutcome(
        content=INTERRUPTED.format("the Workflow was cancelled"), is_error=True
    )
    gate.set()
    await rig.send(wid, inp, "read after", state)
    await rig.event(wid, "done")
    owed = rig.seen.first_for("read after")
    assert [(h.id, h.content, h.is_error) for h in owed] == [
        (call_id, INTERRUPTED.format("the Workflow was cancelled"), True)
    ]
    assert rig.stubs.ids("get_model").count(call_id) == 1
    assert len(rig.stubs.ids("get_model")) == 2


@temporal
async def test_stop_while_waiting_for_approval_never_runs_the_call(rig: Rig) -> None:
    wid, inp = rig.new()
    await rig.send(wid, inp, "print 1")
    pending = await rig.pending(wid)
    state = await cancel_result(rig, wid)
    assert state.pending[pending["id"]] == ToolOutcome(content=CANCELLED_WAITING, is_error=True)
    await rig.send(wid, inp, "read after", state)
    await rig.event(wid, "done")
    owed = rig.seen.first_for("read after")
    assert [(h.id, h.content) for h in owed] == [(pending["id"], CANCELLED_WAITING)]
    assert rig.stubs.ids("print_output") == []


@temporal
async def test_stop_while_idle_keeps_the_session(rig: Rig) -> None:
    wid, inp = rig.new()
    await rig.send(wid, inp, "read 1")
    await rig.event(wid, "done")
    before = await rig.snapshot_where(inp.session_id, lambda s: s.state.segments == 2)
    state = await cancel_result(rig, wid)
    assert state.session_id == before.state.session_id
    assert state.checkpoint == before.state.checkpoint is not None
    await rig.send(wid, inp, "read again", state)
    await rig.event(wid, "done")
    assert [h.input["tag"] for h in rig.seen.first_for("read again")] == ["read 1"]
    # Without the state, the store alone does not resume: a new Claude session.
    await cancel_result(rig, wid)
    await rig.send(wid, inp, "fresh")
    await rig.event(wid, "done")
    fresh = await rig.snapshot_where(inp.session_id, lambda s: s.state.segments == 1)
    assert fresh.state.session_id not in (None, before.state.session_id)
    assert rig.seen.first_for("fresh") == []


# ---- snapshots and restore (ruling 15) -----------------------------------------------------


@temporal
async def test_snapshots_follow_the_agent(rig: Rig) -> None:
    gate = rig.stubs.gate("get_model")
    wid, inp = rig.new()
    await rig.send(wid, inp, "read 1")
    await rig.started("get_model")
    [call_id] = rig.stubs.ids("get_model")
    during = await rig.snapshot_where(inp.session_id, lambda s: bool(s.in_flight))
    assert during.in_flight == [InFlight(call_id, "get_model", "started")]
    gate.set()
    await rig.event(wid, "done")
    await rig.snapshot_where(inp.session_id, lambda s: s.state.segments == 2 and not s.in_flight)
    state = await cancel_result(rig, wid)
    mine = [s for s in rig.snaps.saved if s.session_id == inp.session_id]
    versions = [s.version for s in mine]
    assert versions == sorted(versions)
    # A Stop within the agent's live-output linger after `done` ends the finished task
    # once more (fork_next), which moves no counter, so no snapshot follows it.
    assert mine[-1].state == dataclasses.replace(state, fork_next=mine[-1].state.fork_next)


@temporal
async def test_restore_after_termination_during_a_tool(rig: Rig) -> None:
    rig.stubs.gate("get_model")
    wid, inp = rig.new()
    await rig.send(wid, inp, "read slow")
    await rig.started("get_model")
    [call_id] = rig.stubs.ids("get_model")
    snap = await rig.snapshot_where(inp.session_id, lambda s: bool(s.in_flight))
    await rig.handle(wid).terminate("gone")
    rig.stubs.release()
    restored = SessionInput(
        inp.session_id, inp.max_turns, inp.approval_expiry_seconds, restored=Restored(snap.in_flight)
    )
    await rig.send(wid, restored, "read after", snap.state)
    await rig.event(wid, "done")
    owed = rig.seen.first_for("read after")
    assert [(h.id, h.content, h.is_error) for h in owed] == [(call_id, INTERRUPTED.format(STOPPED), True)]
    latest = rig.snaps.latest(inp.session_id)
    assert call_id in latest.state.recent_call_ids
    assert rig.stubs.ids("get_model").count(call_id) == 1


@temporal
async def test_restore_never_reruns_a_finished_sibling(rig: Rig) -> None:
    """Review finding: a call done before its batch committed is owed "ran, but lost"."""
    rig.stubs.gate("update_source")
    wid, inp = rig.new()
    await rig.send(wid, inp, "both 1")
    await rig.started("update_source")
    snap = await rig.snapshot_where(
        inp.session_id, lambda s: {c.status for c in s.in_flight} == {"done", "started"}
    )
    [read_id] = rig.stubs.ids("get_model")
    [write_id] = rig.stubs.ids("update_source")
    assert sorted((c.id, c.status) for c in snap.in_flight) == sorted(
        [(read_id, "done"), (write_id, "started")]
    )
    await rig.handle(wid).terminate("gone")
    rig.stubs.release()
    restored = SessionInput(
        inp.session_id, inp.max_turns, inp.approval_expiry_seconds, restored=Restored(snap.in_flight)
    )
    await rig.send(wid, restored, "after", snap.state)
    await rig.event(wid, "done")
    owed = {h.id: h.content for h in rig.seen.first_for("after")}
    assert owed == {read_id: LOST, write_id: INTERRUPTED.format(STOPPED)}
    latest = rig.snaps.latest(inp.session_id)
    assert {read_id, write_id} <= set(latest.state.recent_call_ids)
    assert rig.stubs.ids("get_model") == [read_id]


@temporal
async def test_restore_after_termination_while_waiting_for_approval(rig: Rig) -> None:
    wid, inp = rig.new()
    await rig.send(wid, inp, "print 1")
    pending = await rig.pending(wid)
    snap = await rig.snapshot_where(inp.session_id, lambda s: bool(s.in_flight))
    assert snap.in_flight == [InFlight(pending["id"], "print_output", "waiting for approval")]
    await rig.handle(wid).terminate("gone")
    restored = SessionInput(
        inp.session_id, inp.max_turns, inp.approval_expiry_seconds, restored=Restored(snap.in_flight)
    )
    await rig.send(wid, restored, "read after", snap.state)
    await rig.event(wid, "done")
    owed = rig.seen.first_for("read after")
    assert [(h.id, h.content) for h in owed] == [(pending["id"], f"This tool call did not run: {STOPPED}.")]
    assert rig.stubs.ids("print_output") == []


# ---- replay ----------------------------------------------------------------------------------


@pytest.fixture
def approve_history() -> Iterator[WorkflowHistory]:
    path = HISTORIES / "approve.json"
    yield WorkflowHistory.from_json("approve", path.read_text(encoding="utf-8"))


async def test_the_recorded_approval_replays(approve_history: WorkflowHistory) -> None:
    replayer = Replayer(
        workflows=[DurableSession], plugins=[ClaudeAgentPlugin(ScriptedClaude(make_policy(Seen())))]
    )
    await replayer.replay_workflow(approve_history)
