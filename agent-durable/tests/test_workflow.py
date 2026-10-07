"""The DurableSession workflow (plan task 9) on ScriptedClaude, with stub tools on agent-tools."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from temporalio.api.enums.v1 import EventType
from temporalio.claude_agent_sdk import AgentState, ClaudeAgentPlugin, ToolOutcome, follow_agent
from temporalio.claude_agent_sdk.testing import ScriptedClaude
from temporalio.client import (
    Client,
    WithStartWorkflowOperation,
    WorkflowHandle,
    WorkflowHistory,
    WorkflowUpdateFailedError,
)
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.service import RPCError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer

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
    Nudge,
    Restored,
    SessionInput,
    render_prompt,
    restore_state,
)
from scadbuddy_durable.workflow import DurableSession
from tests.conftest import free_port, start_dev_server, stop_dev_server, temporal_cli
from tests.short_runs import LongStop, ShortRuns
from tests.support import WAIT, FakeInputs, Rig, Seen, make_policy, rig_on

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

temporal = pytest.mark.requires_temporal


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
    assert render_prompt(Message("m", "hello")) == "hello"
    assert render_prompt(Message("m", "hello", "page: /models/x")) == "hello\n\npage: /models/x"
    assert render_prompt(Message("m", "hello", None, "lost: x")) == "hello\n\nlost: x"


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
    another = await rig.inputs.commit(inp.session_id, "another")
    with pytest.raises(WorkflowUpdateFailedError) as err:
        await handle.execute_update(SEND_UPDATE, Nudge(another))
    assert "the session is busy" in str(err.value.cause)
    gate.set()
    await rig.event(wid, "done")
    assert await accepted(handle, SEND_UPDATE) == 1
    assert all(p != "another" for p, _ in rig.seen.calls)


@temporal
async def test_a_nudge_for_no_committed_message_is_refused(rig: Rig) -> None:
    wid, inp = rig.new()
    with pytest.raises(WorkflowUpdateFailedError) as err:
        await rig.nudge(wid, inp, "never-committed")
    assert "no message with this id was committed" in str(err.value.cause)


@temporal
async def test_a_repeated_nudge_never_runs_the_message_twice(rig: Rig) -> None:
    # The agent service sends the same id again after a timeout or an UNAVAILABLE: the
    # Update id dedupes it in a run, the message id across runs.
    wid, inp = rig.new()
    message_id = await rig.inputs.commit(inp.session_id, "read once")
    handle = await rig.nudge(wid, inp, message_id)
    await rig.event(wid, "done")
    await rig.nudge(wid, inp, message_id)
    await handle.execute_update(SEND_UPDATE, Nudge(message_id), id=f"{message_id}-other")
    state = await cancel_result(rig, wid)
    await rig.nudge(wid, inp, message_id, state)  # a new run: the record says it ran
    assert [p for p, _ in rig.seen.calls].count("read once") == 2  # the call, then the answer
    assert rig.fake.takes == [message_id]
    assert rig.stubs.ids("get_model") and len(rig.stubs.ids("get_model")) == 1


@temporal
async def test_an_abandoned_message_never_runs(rig: Rig) -> None:
    wid, inp = rig.new()
    message_id = await rig.inputs.commit(inp.session_id, "read never")
    rig.fake.abandon(message_id)  # a Stop before any run took it
    with pytest.raises(WorkflowUpdateFailedError) as err:
        await rig.nudge(wid, inp, message_id)
    assert "abandoned" in str(err.value.cause)
    assert rig.seen.calls == [] and rig.fake.takes == []


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


@temporal
async def test_pending_approvals_say_when_this_runs_expiry_denies_them(rig: Rig) -> None:
    """The agent service shows `expires_at` from the expiry fixed at the run's start (ruling 8)."""

    async def expiry_of(seconds: int) -> float:
        wid, inp = rig.new(expiry=seconds)
        handle = await rig.send(wid, inp, "print 1")
        started = (await handle.describe()).start_time

        async def stamped() -> str:
            while True:
                items: list[dict[str, Any]] = await handle.query(PENDING_QUERY)
                if items and "expires_at" in items[0]:
                    return str(items[0]["expires_at"])
                await asyncio.sleep(0.1)

        expires = datetime.fromisoformat(await asyncio.wait_for(stamped(), WAIT))
        await handle.cancel()
        return (expires - started).total_seconds()

    # Margins of minutes: the timer starts after the run, and this host's clock jumps.
    assert 300 <= await expiry_of(300) < 600
    assert 900 <= await expiry_of(900) < 1200


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
async def test_a_message_whose_nudge_was_lost_runs_when_a_run_starts(rig: Rig) -> None:
    # Committed, and its Update lost (Temporal restarted while no worker ran, say): the next
    # run of the session loads it.
    wid, inp = rig.new()
    message_id = await rig.inputs.commit(inp.session_id, "hello")
    await rig.client.start_workflow(DurableSession.run, args=[inp, None], id=wid, task_queue=TASK_QUEUE)
    assert (await rig.event(wid, "done"))["result"] == "answer to hello"
    assert rig.fake.status(message_id) == "run"


@temporal
async def test_a_message_whose_nudge_was_lost_runs_on_the_next_nudge_of_a_live_run(rig: Rig) -> None:
    # The run was live and idle, and the Update was lost before a worker accepted it: the
    # agent service sends the same id again, and the run loads the message then.
    wid, inp = rig.new()
    await rig.send(wid, inp, "first")
    await rig.event(wid, "done")
    lost = await rig.inputs.commit(inp.session_id, "lost one")
    await rig.nudge(wid, inp, lost)
    await rig.event(wid, "done", 2)
    assert rig.fake.takes[1:] == [lost]
    assert [p for p, _ in rig.seen.calls] == ["first", "lost one"]


@temporal
async def test_a_nudge_lost_with_a_temporal_restart_is_sent_again_and_runs_once(tmp_path: Path) -> None:
    # The live run is idle and no worker polls; the nudge is admitted, which Temporal keeps
    # in memory only, and the server restarts. The message is committed, so the agent
    # service sends the same id again (client.ts `send`), and it runs exactly once.
    cli = temporal_cli()
    if not cli:
        pytest.skip("no Temporal CLI")
    port, db = free_port(), tmp_path / "restart.db"
    address = f"127.0.0.1:{port}"
    queue = f"agent-{uuid.uuid4()}"
    inputs = FakeInputs()
    server = start_dev_server(cli, port, db)
    try:
        client = await Client.connect(address)
        async with rig_on(client, tmp_path / "sessions", task_queue=queue, inputs=inputs) as rig:
            wid, inp = rig.new()
            await rig.send(wid, inp, "first")
            await rig.event(wid, "done")
            rig.ids.remove(wid)  # the run lives on past this worker
        lost = await inputs.commit(inp.session_id, "lost one")
        nudge = asyncio.create_task(rig.nudge(wid, inp, lost))
        await asyncio.sleep(2)  # admitted: no worker polls the queue to accept it
        stop_dev_server(server)
        server = start_dev_server(cli, port, db)
        # The call died with the server, or still retries against the new one: either way
        # the agent service gives it up after its deadline and sends again.
        nudge.cancel()
        with contextlib.suppress(BaseException):
            await nudge
        client = await Client.connect(address)
        async with rig_on(client, tmp_path / "sessions", task_queue=queue, inputs=inputs) as again:
            again.ids.append(wid)
            await again.nudge(wid, inp, lost)  # the same id
            await again.event(wid, "done", 2)
            await again.nudge(wid, inp, lost)  # and again: a no-op
            assert inputs.takes[1:] == [lost]
            assert [p for p, _ in again.seen.calls] == ["lost one"]
    finally:
        stop_dev_server(server)


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
async def test_a_stop_returns_while_a_live_output_subscriber_polls(rig: Rig) -> None:
    # A subscriber's poll is an Update handler that waits as long as the run is open: the
    # Stop must not wait for every handler, only for the nudges it owes an answer.
    rig.stubs.gate("get_model")
    wid, inp = rig.new()
    await rig.send(wid, inp, "read slow")
    await rig.started("get_model")

    async def follow() -> None:
        async for _ in follow_agent(rig.client, wid):
            pass

    following = asyncio.create_task(follow())
    try:
        await asyncio.sleep(1)  # the subscriber is polling
        state = await cancel_result(rig, wid)
        assert state.fork_next
    finally:
        following.cancel()
        with contextlib.suppress(BaseException):
            await following


@temporal
async def test_a_message_sent_while_a_stop_closes_the_run_is_refused(rig: Rig) -> None:
    # Accepted, it would be lost: the run returns its state without reading its inbox.
    rig.stubs.gate("get_model")
    wid, inp = rig.new()
    handle = await rig.send(wid, inp, "read slow", workflow=LongStop)
    await rig.started("get_model")
    await handle.cancel()
    await rig.event(wid, "cancelled")  # the plugin ended the task; the run is still open
    after = await rig.inputs.commit(inp.session_id, "after stop")
    with pytest.raises(WorkflowUpdateFailedError) as err:
        await handle.execute_update(SEND_UPDATE, Nudge(after))
    assert "the session is stopping" in str(err.value.cause)
    await handle.terminate("test over")
    assert rig.fake.status(after) == "pending"  # the agent service abandons it


@temporal
async def test_a_nudge_and_a_stop_in_one_activation_refuse_the_message(
    temporal_env: Client, tmp_path: Path
) -> None:
    # The Update and the cancel reach the run together (no worker polled while both were
    # sent): accepted, the message would be dropped by the Stop. It must be refused.
    queue = f"agent-{uuid.uuid4()}"
    inputs = FakeInputs()
    sid = str(uuid.uuid4())
    wid = f"session-{sid}"
    inp = SessionInput(session_id=sid, max_turns=7, approval_expiry_seconds=3600)
    message_id = await inputs.commit(sid, "read never")
    op: WithStartWorkflowOperation[Any, AgentState] = WithStartWorkflowOperation(
        DurableSession.run,
        args=[inp, None],
        id=wid,
        task_queue=queue,
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
    )
    sending = asyncio.create_task(
        temporal_env.execute_update_with_start_workflow(
            SEND_UPDATE, Nudge(message_id), id=message_id, start_workflow_operation=op
        )
    )
    handle = temporal_env.get_workflow_handle_for(DurableSession.run, wid)

    async def admitted() -> None:
        while True:
            try:
                await handle.describe()
                return
            except RPCError:
                await asyncio.sleep(0.1)

    await asyncio.wait_for(admitted(), WAIT)  # started, with the Update admitted, no worker yet
    await handle.cancel()
    async with rig_on(temporal_env, tmp_path / "sessions", task_queue=queue, inputs=inputs) as rig:
        rig.ids.append(wid)
        with pytest.raises(WorkflowUpdateFailedError) as err:
            await asyncio.wait_for(sending, WAIT)
        assert "the session is stopping" in str(err.value.cause)
        await asyncio.wait_for(handle.result(), WAIT)
        assert rig.seen.calls == [] and inputs.takes == []
        assert inputs.status(message_id) == "pending"


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
