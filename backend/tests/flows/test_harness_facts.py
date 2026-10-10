"""The temporal-agent-harness facts phase 6 relies on (plan
2026-10-09-durable-phase-6-flows.md, "What the pinned harness is", Task A3).

Each test pins one fact against the pinned harness on a real Temporal. A pin bump
that changes a fact fails here first; a failure is a reason to stop and ask, not to
work around the harness (spec 2026-10-01 §9).
"""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from temporal_agent_harness.harness import agent
from temporal_agent_harness.harness.agent_client import (
    AgentClient,
    CallbackResultError,
    ToolApprovalError,
)
from temporal_agent_harness.harness.agent_protocol import (
    TURN_EVENTS_TOPIC,
    AgentConfig,
    AgentEvent,
    AgentEventType,
)
from temporalio import activity
from temporalio.api.common.v1 import WorkflowExecution
from temporalio.api.enums.v1 import EventType
from temporalio.api.history.v1 import HistoryEvent
from temporalio.api.sdk.v1 import WorkflowMetadata
from temporalio.api.workflowservice.v1 import ResetWorkflowExecutionRequest
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.contrib.workflow_streams import WorkflowStreamClient
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker

from scadbuddy.workflows.flow_entries import (
    FLOW_ENTRY_TIMEOUT,
    EntryTimeout,
    flow_entry_timeout,
)
from scadbuddy.workflows.flows_client import connect_flows, harness_plugins
from scadbuddy.workflows.payload_codec import SubjectForgottenError
from tests.flows.harness_probe import (
    EFFECTS_ENV,
    PROBE_TOOLS,
    PROBE_WORKFLOW,
    SCRIPT_TOOLS,
    ProbeChild,
    ProbeWorkflow,
    probe_close,
)

pytestmark = pytest.mark.requires_temporal

#: Every handler an `@agent.defn` workflow registers (plan, "What the pinned harness
#: is"). ScadBuddy adds none to ProjectWorkflow; a new name here is a harness change
#: to read before bumping the pin (spec §8: no handler but the harness's decides).
HARNESS_UPDATES = {
    "send_agent_message",
    "tool_approval",
    "provide_callback_result",
    "__temporal_workflow_stream_poll",
}
HARNESS_QUERIES = {"agent_status", "agent_interface", "__temporal_workflow_stream_offset"}
HARNESS_SIGNALS = {"close", "__temporal_workflow_stream_publish"}


class _Keys:
    """One fixed data key per subject: the codec runs, Postgres is not needed."""

    def __init__(self) -> None:
        self.keys: dict[str, bytes] = {}

    async def key_for(self, subject: str, create: bool) -> bytes:
        if subject not in self.keys:
            if not create:
                raise SubjectForgottenError(subject)
            self.keys[subject] = uuid.uuid4().bytes * 2
        return self.keys[subject]


@pytest.fixture
def effects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "effects.jsonl"
    path.touch()
    monkeypatch.setenv(EFFECTS_ENV, str(path))
    return path


def _effects(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


async def _read_effects(path: Path) -> list[dict[str, Any]]:
    return _effects(path)


class Probe:
    def __init__(self, client: Client, queue: str) -> None:
        self.client = client
        self.queue = queue

    async def start(
        self, script: str, *, close: bool = False, update_id: str | None = None
    ) -> tuple[str, Any]:
        wf_id = f"flow-{uuid.uuid4()}"
        reply = await self.send(wf_id, script, close=close, update_id=update_id)
        return wf_id, reply

    async def send(
        self, wf_id: str, script: str, *, close: bool = False, update_id: str | None = None
    ) -> Any:
        return await AgentClient(self.client, wf_id).start_and_submit_message(
            "execute",
            {"script": script, "close": close},
            workflow_name=PROBE_WORKFLOW,
            task_queue=self.queue,
            start_config=AgentConfig(),
            update_id=update_id,
        )

    async def reply(self, wf_id: str, timeout: float = 40) -> str | None:
        """The text of the next turn's reply, read from the harness's event stream."""

        async def read() -> str | None:
            stream = WorkflowStreamClient.create(self.client, wf_id)
            text: str | None = None
            async for item in stream.subscribe(
                topics=[TURN_EVENTS_TOPIC], from_offset=0, result_type=AgentEvent
            ):
                event = item.data.event
                if event.type == AgentEventType.MESSAGE_HANDLER_END:
                    text = event.output.get("text")
                if event.type == AgentEventType.TURN_END:
                    return text
            return text

        return await asyncio.wait_for(read(), timeout)

    async def history(self, wf_id: str, run_id: str | None = None) -> list[HistoryEvent]:
        history = await self.client.get_workflow_handle(wf_id, run_id=run_id).fetch_history()
        return list(history.events)

    async def reset(self, wf_id: str, event_id: int) -> str:
        response = await self.client.workflow_service.reset_workflow_execution(
            ResetWorkflowExecutionRequest(
                namespace=self.client.namespace,
                workflow_execution=WorkflowExecution(workflow_id=wf_id),
                reason="harness facts",
                workflow_task_finish_event_id=event_id,
                request_id=str(uuid.uuid4()),
            )
        )
        return response.run_id


async def _until(read: Any, check: Any, timeout: float = 20) -> Any:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        value = await read()
        if check(value):
            return value
        if loop.time() > deadline:
            raise AssertionError(f"timed out; last value {value!r}")
        await asyncio.sleep(0.1)


@asynccontextmanager
async def _probe(address: str, timeout_activity: Any) -> AsyncIterator[Probe]:
    client = await connect_flows(address, "default", _Keys())
    queue = f"probe-{uuid.uuid4().hex[:8]}"
    async with Worker(
        client,
        task_queue=queue,
        workflows=[ProbeWorkflow, ProbeChild],
        activities=[probe_close, timeout_activity],
        plugins=harness_plugins(PROBE_TOOLS),
    ):
        yield Probe(client, queue)


@pytest.fixture
async def probe(temporal_address: str) -> AsyncIterator[Probe]:
    async with _probe(temporal_address, flow_entry_timeout) as p:
        yield p


@activity.defn(name=FLOW_ENTRY_TIMEOUT)
async def answer_lands_first(entry: EntryTimeout) -> bool:
    """The timer fired, but a person's answer reaches the harness before the timeout's
    Update does: the real activity then finds the entry resolved."""
    client = AgentClient(activity.client(), entry.workflow_id)
    await client.provide_callback_result(entry.call_id, result={"answer": "just in time"})
    return await flow_entry_timeout(entry)


def _script(*body: str) -> str:
    lines = ["import asyncio", "async def main():", *(f"    {b}" for b in body)]
    return "\n".join([*lines, "asyncio.run(main())"])


# 1
async def test_type_check_runs_outside_a_workflow_and_reports_by_line() -> None:
    tool = agent.code_mode_tool(SCRIPT_TOOLS, name="run_flow")
    good = _script("r = await step(1)", "return r['n']")
    bad = _script("r = await step('x')", "return r['nope']")
    assert await agent.code_mode_type_check(tool, good) is None
    report = await agent.code_mode_type_check(tool, bad)
    assert report is not None and report.startswith("MontyTypingError")
    assert "main.py:3" in report


# 2
async def test_gather_runs_host_calls_together(probe: Probe, effects: Path) -> None:
    wf_id, _ = await probe.start(
        _script("a, b = await asyncio.gather(step(1), step(2))", "return a['n'] + b['n']")
    )
    assert await probe.reply(wf_id) == "result: 3"
    assert sorted(e["n"] for e in _effects(effects)) == [1, 2]


# 3
async def test_the_same_update_id_runs_one_execute(probe: Probe, effects: Path) -> None:
    script = _script("r = await step(1)", "return r['n']")
    wf_id, first = await probe.start(script, update_id="key-1")
    again = await probe.send(wf_id, script, update_id="key-1")
    assert again.turn_number == first.turn_number
    assert await probe.reply(wf_id) == "result: 1"
    await _until(lambda: _read_effects(effects), bool)
    # The first effect is in; a duplicate execute would add a second within this window.
    for _ in range(20):
        assert _effects(effects) == [{"tool": "step", "n": 1}]
        await asyncio.sleep(0.1)


# 4
async def test_a_gated_call_parks_and_runs_once_approved(probe: Probe, effects: Path) -> None:
    wf_id, _ = await probe.start(_script("return await outward('pink')"))
    client = AgentClient(probe.client, wf_id)
    pending = await _until(client.get_pending_approvals, bool)
    assert [p.tool_name for p in pending] == ["outward"]
    assert _effects(effects) == []
    await client.approve_tool(pending[0].tool_id, approved=True)
    assert await probe.reply(wf_id) == "result: 'sent pink'"
    with pytest.raises(ToolApprovalError) as err:
        await client.approve_tool(pending[0].tool_id, approved=True)
    assert err.value.error_type == "ToolApprovalAlreadyResolved"
    assert _effects(effects) == [{"tool": "outward", "what": "pink"}]


# 5
async def test_a_nested_run_tool_gets_the_inner_tools_gate(probe: Probe, effects: Path) -> None:
    wf_id, _ = await probe.start(_script("return await gated_via_nested('blue')"))
    client = AgentClient(probe.client, wf_id)
    pending = await _until(client.get_pending_approvals, bool)
    assert [p.tool_name for p in pending] == ["outward"]
    await client.approve_tool(pending[0].tool_id, approved=True)
    assert await probe.reply(wf_id) == "result: 'sent blue'"
    assert _effects(effects) == [{"tool": "outward", "what": "blue"}]


# 6
async def test_a_denial_reaches_the_script_as_an_exception(probe: Probe, effects: Path) -> None:
    wf_id, _ = await probe.start(
        _script(
            "try:", "    return await outward('x')", "except Exception as e:", "    return str(e)"
        )
    )
    client = AgentClient(probe.client, wf_id)
    pending = await _until(client.get_pending_approvals, bool)
    await client.approve_tool(pending[0].tool_id, approved=False, reason="not now")
    reply = await probe.reply(wf_id)
    assert reply is not None
    assert reply.removeprefix("result: ").strip("'\"").startswith("ToolApprovalDenied: ")
    assert _effects(effects) == []


# 7: the harness's callback gate keeps a cancelled call's entry, and accepts a late
# answer to it, so a per-call timeout resolves the entry with the harness's own Update
# (scadbuddy/workflows/flow_entries.py, plan Ruling 11 as revised).
async def test_a_timed_out_wait_leaves_no_pending_callback(probe: Probe) -> None:
    wf_id, _ = await probe.start(
        _script(
            "try:",
            "    await wait('anyone?', 3)",
            "except TimeoutError:",
            "    return 'timed out'",
        )
    )
    client = AgentClient(probe.client, wf_id)
    pending = await _until(client.get_pending_callbacks, bool)
    assert [p.tool_name for p in pending] == ["human_answer"]
    assert await probe.reply(wf_id) == "result: 'timed out'"
    assert (await client.get_status()).pending_callbacks == []
    with pytest.raises(CallbackResultError) as err:
        await client.provide_callback_result(pending[0].tool_id, result={"answer": "late"})
    assert err.value.error_type in {"UnknownCallback", "CallbackAlreadyResolved"}


# 7, the race: an answer that lands while the timeout activity runs is the result.
async def test_an_answer_that_beats_the_timeout_update_is_the_result(
    temporal_address: str,
) -> None:
    async with _probe(temporal_address, answer_lands_first) as probe:
        wf_id, _ = await probe.start(_script("a = await wait('anyone?', 2)", "return a['answer']"))
        assert await probe.reply(wf_id) == "result: 'just in time'"
        assert (await AgentClient(probe.client, wf_id).get_status()).pending_callbacks == []


@activity.defn(name=FLOW_ENTRY_TIMEOUT)
async def timeout_refused(entry: EntryTimeout) -> bool:
    """The harness refuses the timeout's Update: the activity fails for good."""
    raise ApplicationError("refused", type="EntryRefused", non_retryable=True)


# 7, the timeout's own failure: the script still sees one TimeoutError (#2095).
async def test_a_failed_timeout_reaches_the_script_as_a_timeout(temporal_address: str) -> None:
    async with _probe(temporal_address, timeout_refused) as probe:
        wf_id, _ = await probe.start(
            _script(
                "try:",
                "    await wait('anyone?', 2)",
                "except TimeoutError:",
                "    return 'timed out'",
            )
        )
        assert await probe.reply(wf_id) == "result: 'timed out'"


# 8
async def test_an_answer_is_typed_and_a_malformed_one_can_be_corrected(probe: Probe) -> None:
    wf_id, _ = await probe.start(_script("a = await wait('swap spool?', 60)", "return a['answer']"))
    client = AgentClient(probe.client, wf_id)
    [pending] = await _until(client.get_pending_callbacks, bool)
    assert pending.tool_input == {"question": "swap spool?"}
    with pytest.raises(CallbackResultError) as err:
        await client.provide_callback_result(pending.tool_id, result={"nope": 1})
    assert err.value.error_type == "MalformedCallbackResult"
    await client.provide_callback_result(pending.tool_id, result={"answer": "pink"})
    assert await probe.reply(wf_id) == "result: 'pink'"


# 9
async def test_the_workflow_stays_open_until_close(probe: Probe) -> None:
    open_id, _ = await probe.start(_script("return 1"))
    assert await probe.reply(open_id) == "result: 1"
    described = await probe.client.get_workflow_handle(open_id).describe()
    assert described.status == WorkflowExecutionStatus.RUNNING
    closed_id, _ = await probe.start(_script("return 2"), close=True)
    handle = probe.client.get_workflow_handle(closed_id)
    await asyncio.wait_for(handle.result(), 30)
    assert (await handle.describe()).status == WorkflowExecutionStatus.COMPLETED


def _first(events: list[HistoryEvent], kind: int, after: int = 0) -> int:
    return next(e.event_id for e in events if e.event_type == kind and e.event_id > after)


def _last_wft_before(events: list[HistoryEvent], event_id: int) -> int:
    return max(
        e.event_id
        for e in events
        if e.event_type == EventType.EVENT_TYPE_WORKFLOW_TASK_COMPLETED and e.event_id < event_id
    )


# 10
async def test_reset_runs_again_only_what_is_past_the_point(probe: Probe, effects: Path) -> None:
    script = _script(
        "a = await step(1)",
        "await asyncio.sleep(2)",
        "b = await step(2)",
        "return [a['n'], b['n']]",
    )
    # After step(1) completed: step(1) is not run again, step(2) is.
    wf_id, _ = await probe.start(script)
    assert await probe.reply(wf_id) == "result: [1, 2]"
    events = await probe.history(wf_id)
    done = _first(events, EventType.EVENT_TYPE_ACTIVITY_TASK_COMPLETED)
    point = _first(events, EventType.EVENT_TYPE_WORKFLOW_TASK_COMPLETED, after=done)
    await probe.reset(wf_id, point)
    await _until(
        lambda: asyncio.sleep(0, _effects(effects)),
        lambda e: len(e) == 3,
    )
    assert [e["n"] for e in _effects(effects)] == [1, 2, 2]
    # Before the Update was accepted: the server applies it again, the whole script runs.
    effects.write_text("")
    wf_id, _ = await probe.start(
        _script("a = await step(1)", "await asyncio.sleep(15)", "return a['n']")
    )
    await _until(lambda: asyncio.sleep(0, _effects(effects)), bool)
    events = await probe.history(wf_id)
    accepted = _first(events, EventType.EVENT_TYPE_WORKFLOW_EXECUTION_UPDATE_ACCEPTED)
    await probe.reset(wf_id, _last_wft_before(events, accepted))
    assert await probe.reply(wf_id, timeout=60) == "result: 1"
    assert [e["n"] for e in _effects(effects)] == [1, 1]


# 11
async def test_a_reset_before_a_running_childs_start_starts_a_new_child(probe: Probe) -> None:
    wf_id, _ = await probe.start(_script("return await child(1)"))
    events = await _until(
        lambda: probe.history(wf_id),
        lambda evs: any(
            e.event_type == EventType.EVENT_TYPE_CHILD_WORKFLOW_EXECUTION_STARTED for e in evs
        ),
    )
    initiated = _first(events, EventType.EVENT_TYPE_START_CHILD_WORKFLOW_EXECUTION_INITIATED)
    attrs = next(
        e.start_child_workflow_execution_initiated_event_attributes
        for e in events
        if e.event_id == initiated
    )
    first_child = attrs.workflow_id
    new_run = await probe.reset(wf_id, _last_wft_before(events, initiated))
    # The replayed start names the new run, so it cannot collide with the old child,
    # which is still running.
    assert await probe.reply(wf_id, timeout=60) == "result: 10"
    old = await probe.client.get_workflow_handle(first_child).describe()
    assert old.status in {WorkflowExecutionStatus.RUNNING, WorkflowExecutionStatus.COMPLETED}
    after = await probe.history(wf_id, new_run)
    started = [
        e.start_child_workflow_execution_initiated_event_attributes.workflow_id
        for e in after
        if e.event_type == EventType.EVENT_TYPE_START_CHILD_WORKFLOW_EXECUTION_INITIATED
    ]
    assert len(started) == 1 and started[0] != first_child and new_run in started[0]
    assert not any(
        e.event_type == EventType.EVENT_TYPE_START_CHILD_WORKFLOW_EXECUTION_FAILED for e in after
    )


# 12
async def test_the_workflow_registers_only_the_harness_handlers(probe: Probe) -> None:
    wf_id, _ = await probe.start(_script("await asyncio.sleep(30)", "return 0"))
    metadata = await probe.client.get_workflow_handle(wf_id).query(
        "__temporal_workflow_metadata", result_type=WorkflowMetadata
    )
    definition = metadata.definition
    builtin = {"__stack_trace", "__enhanced_stack_trace", "__temporal_workflow_metadata"}
    assert {u.name for u in definition.update_definitions} == HARNESS_UPDATES
    assert {q.name for q in definition.query_definitions} - builtin == HARNESS_QUERIES
    assert {s.name for s in definition.signal_definitions} == HARNESS_SIGNALS
