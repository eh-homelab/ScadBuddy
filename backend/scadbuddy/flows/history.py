"""What a Reset of a flow run would run again (#1057, spec 2026-10-01 §7.4, plan
2026-10-09-durable-phase-6-flows.md Task E1).

A Reset to a workflow task replays the history up to it and runs every host call after
it again, as new effects (Ruling 4). The preview lists the outward ones, read from the
current execution's history: an outward send's input is sealed, so it is found by its
activity id (`flow_tools.outward_activity_id`, Ruling 14). An `agent`/`ask_session` turn
counts when it made an outward call, which its wait's result says (decoded with the
flows codec); a turn that has not answered may have, so it counts. The preview is pinned to the
history's last event, `as_of_event_id`, so a Reset can refuse when more outward calls
landed after the person looked.

The history is not read whole (#2268): backwards from its last event to the point, and
forwards only until the run's `execute` Update was accepted, which is in its first
workflow task. A preview of a recent point in a long run reads only the tail.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence

from pydantic import BaseModel
from temporal_agent_harness.harness.agent_protocol.agent_interface import (
    SEND_AGENT_MESSAGE_UPDATE,
)
from temporalio.api.common.v1 import WorkflowExecution
from temporalio.api.enums.v1 import EventType
from temporalio.api.history.v1 import HistoryEvent
from temporalio.api.workflowservice.v1 import GetWorkflowExecutionHistoryReverseRequest
from temporalio.client import Client

from scadbuddy.flows.models import Run
from scadbuddy.workflows.flow_tools import OUTWARD_PREFIX, TOOL_PREFIX, TURN_PREFIX

#: Events per page of a history read.
PAGE_SIZE = 1000


class OutwardCall(BaseModel):
    """One outward host call a Reset would send again."""

    fn: str
    call_id: str
    scheduled_event_id: int


class ResetPreview(BaseModel):
    event_id: int
    #: The execution the preview was read from: past a Reset's point, another
    #: execution's event ids describe other events.
    workflow_run_id: str = ""
    #: The history's last event when this was read.
    as_of_event_id: int
    #: `event_id` is a completed workflow task after the script started and before the
    #: last event: what a flow's Reset takes.
    valid: bool
    calls: list[OutwardCall]


def _executes(event: HistoryEvent) -> bool:
    """Whether `event` accepted the run's `execute`: the harness's `send_agent_message`
    Update (#2303). No other Update starts the script, so no other one makes a point
    after it valid."""
    return (
        event.event_type == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_UPDATE_ACCEPTED
        and event.workflow_execution_update_accepted_event_attributes.accepted_request.input.name
        == SEND_AGENT_MESSAGE_UPDATE
    )


def preview_of(
    events: Iterable[HistoryEvent],
    event_id: int,
    outward_tools: Collection[str] = (),
    turns: Mapping[int, int] | None = None,
) -> ResetPreview:
    """The preview over one execution's events. `outward_tools` are the call ids of the
    run's outward `tool(...)` steps: their activity id alone does not say. `turns` maps
    an `agent`/`ask_session` turn's wait (scheduled event id) to the outward calls the
    turn made, once it answered; a turn not in it has not, so it may have made one, and
    is listed."""
    turns = turns or {}
    last = 0
    valid = False
    started = False
    calls: list[OutwardCall] = []
    for event in events:
        last = event.event_id
        if _executes(event):
            started = started or event.event_id < event_id
        if event.event_id == event_id:
            # After the script started: a Reset reapplies no Update (`flow_reset`), so
            # one to before `execute` was accepted would leave a run with no script.
            valid = event.event_type == EventType.EVENT_TYPE_WORKFLOW_TASK_COMPLETED
        if event.event_id <= event_id:
            continue
        if event.event_type != EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
            continue
        scheduled = event.activity_task_scheduled_event_attributes
        activity_id = scheduled.activity_id
        if activity_id.startswith(OUTWARD_PREFIX):
            fn, _, call_id = activity_id.removeprefix(OUTWARD_PREFIX).partition("-")
        elif activity_id.removeprefix(TOOL_PREFIX) in outward_tools:
            # An outward agent tool (`tool(...)`), by the run's own step for it.
            fn, call_id = f"tool:{scheduled.activity_type.name}", activity_id[len(TOOL_PREFIX) :]
        elif activity_id.startswith(TURN_PREFIX) and turns.get(event.event_id, 1) > 0:
            # An agent session's turn that made an outward call (§7.4).
            fn, _, call_id = activity_id.removeprefix(TURN_PREFIX).partition("-")
        else:
            continue
        calls.append(OutwardCall(fn=fn, call_id=call_id, scheduled_event_id=event.event_id))
    return ResetPreview(
        event_id=event_id,
        as_of_event_id=last,
        valid=valid and started and event_id < last,
        calls=calls,
    )


async def outward_since(
    client: Client, run: Run, event_id: int, *, run_id: str | None = None
) -> ResetPreview:
    """The preview of the run's execution `run_id`, else its current one."""
    if run_id is None:
        run_id = (await client.get_workflow_handle(run.workflow_id).describe()).run_id
    tail = await _tail(client, run.workflow_id, run_id, event_id)
    head = await _head(client, run.workflow_id, run_id, event_id)
    # Ascending, each once: the tail's last event can fall inside a short head.
    events = sorted({e.event_id: e for e in [*head, *tail]}.values(), key=lambda e: e.event_id)
    tools = {s.call_id for s in run.steps if s.outward and s.fn.startswith("tool:")}
    preview = preview_of(events, event_id, tools, await _turns(client, tail))
    preview.workflow_run_id = run_id
    return preview


async def _head(client: Client, workflow_id: str, run_id: str, before: int) -> list[HistoryEvent]:
    """The events before `before`, read forwards only until `execute` was accepted."""
    events: list[HistoryEvent] = []
    handle = client.get_workflow_handle(workflow_id, run_id=run_id)
    async for event in handle.fetch_history_events(page_size=PAGE_SIZE):
        if event.event_id >= before:
            break
        events.append(event)
        if _executes(event):
            break
    return events


async def _tail(client: Client, workflow_id: str, run_id: str, since: int) -> list[HistoryEvent]:
    """The events from `since` to the last, read backwards from the last; at least the
    last, which pins the preview."""
    events: list[HistoryEvent] = []
    token = b""
    while True:
        page = await client.workflow_service.get_workflow_execution_history_reverse(
            GetWorkflowExecutionHistoryReverseRequest(
                namespace=client.namespace,
                execution=WorkflowExecution(workflow_id=workflow_id, run_id=run_id),
                maximum_page_size=PAGE_SIZE,
                next_page_token=token,
            )
        )
        for event in page.history.events:
            if event.event_id < since and events:
                return events[::-1]
            events.append(event)
        token = page.next_page_token
        if not token:
            return events[::-1]


async def _turns(client: Client, events: Sequence[HistoryEvent]) -> dict[int, int]:
    """Each answered turn's outward calls, from its wait's result: sealed, so decoded with
    the client's converter (the flows codec)."""
    waits = {
        e.event_id
        for e in events
        if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
        and e.activity_task_scheduled_event_attributes.activity_id.startswith(TURN_PREFIX)
    }
    turns: dict[int, int] = {}
    for e in events:
        if e.event_type != EventType.EVENT_TYPE_ACTIVITY_TASK_COMPLETED:
            continue
        done = e.activity_task_completed_event_attributes
        if done.scheduled_event_id not in waits:
            continue
        [answer] = await client.data_converter.decode(list(done.result.payloads), [dict])
        calls = answer.get("outward_calls") if isinstance(answer, dict) else None
        turns[done.scheduled_event_id] = calls if isinstance(calls, int) else 1
    return turns
