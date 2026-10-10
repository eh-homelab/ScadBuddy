"""What a Reset of a flow run would run again (#1057, spec 2026-10-01 §7.4, plan
2026-10-09-durable-phase-6-flows.md Task E1).

A Reset to a workflow task replays the history up to it and runs every host call after
it again, as new effects (Ruling 4). The preview lists the outward ones, read from the
current execution's history: an outward send's input is sealed, so it is found by its
activity id (`flow_tools.outward_activity_id`, Ruling 14). The preview is pinned to the
history's last event, `as_of_event_id`, so a Reset can refuse when more outward calls
landed after the person looked.
"""

from __future__ import annotations

from collections.abc import Iterable

from pydantic import BaseModel
from temporalio.api.enums.v1 import EventType
from temporalio.api.history.v1 import HistoryEvent
from temporalio.client import Client

from scadbuddy.workflows.flow_tools import OUTWARD_PREFIX


class OutwardCall(BaseModel):
    """One outward host call a Reset would send again."""

    fn: str
    call_id: str
    scheduled_event_id: int


class ResetPreview(BaseModel):
    event_id: int
    #: The history's last event when this was read.
    as_of_event_id: int
    #: `event_id` is a completed workflow task before the last event: what Reset takes.
    valid: bool
    calls: list[OutwardCall]


def preview_of(events: Iterable[HistoryEvent], event_id: int) -> ResetPreview:
    """The preview over one execution's events."""
    last = 0
    valid = False
    calls: list[OutwardCall] = []
    for event in events:
        last = event.event_id
        if event.event_id == event_id:
            valid = event.event_type == EventType.EVENT_TYPE_WORKFLOW_TASK_COMPLETED
        if event.event_id <= event_id:
            continue
        if event.event_type != EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
            continue
        activity_id = event.activity_task_scheduled_event_attributes.activity_id
        if not activity_id.startswith(OUTWARD_PREFIX):
            continue
        fn, _, call_id = activity_id.removeprefix(OUTWARD_PREFIX).partition("-")
        calls.append(OutwardCall(fn=fn, call_id=call_id, scheduled_event_id=event.event_id))
    return ResetPreview(
        event_id=event_id, as_of_event_id=last, valid=valid and event_id < last, calls=calls
    )


async def outward_since(client: Client, workflow_id: str, event_id: int) -> ResetPreview:
    """The preview of the workflow's current execution."""
    history = await client.get_workflow_handle(workflow_id).fetch_history()
    return preview_of(history.events, event_id)
