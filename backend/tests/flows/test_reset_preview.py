"""What a Reset would run again (#1057, plan 2026-10-09-durable-phase-6-flows.md Task
E1): the outward calls after a point of a recorded history.

`two_prints.json` was recorded on 6e's workflow (`SCADBUDDY_RECORD_HISTORIES=1`): a
`render`, a `wait_for_human` answered, then two approved `queue_print`s. Its payloads
are sealed; the preview reads only event types and activity ids.
"""

import os
from pathlib import Path

import pytest
from temporal_agent_harness.harness.agent_client import AgentClient
from temporalio.api.enums.v1 import EventType
from temporalio.api.history.v1 import HistoryEvent
from temporalio.client import WorkflowHistory

from scadbuddy.flows.history import outward_since, preview_of
from tests.flows.fake_api import PLAN, Outward
from tests.flows.flows_support import script

FIXTURE = Path(__file__).parent.parent / "fixtures" / "flow_reset_histories" / "two_prints.json"
COMPLETED = EventType.EVENT_TYPE_WORKFLOW_TASK_COMPLETED


def _events() -> list[HistoryEvent]:
    return list(WorkflowHistory.from_json("flow-two-prints", FIXTURE.read_text()).events)


def _sends(events: list[HistoryEvent]) -> list[HistoryEvent]:
    return [
        e
        for e in events
        if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
        and e.activity_task_scheduled_event_attributes.activity_id.startswith("outward-")
    ]


def _task_before(events: list[HistoryEvent], event_id: int) -> int:
    """The last completed workflow task before `event_id`: a point Reset takes."""
    return max(e.event_id for e in events if e.event_type == COMPLETED and e.event_id < event_id)


def test_the_fixture_has_two_outward_sends() -> None:
    assert [
        e.activity_task_scheduled_event_attributes.activity_id.split("-")[1]
        for e in _sends(_events())
    ] == ["queue_print", "queue_print"]


def test_a_point_before_both_prints_lists_both() -> None:
    events = _events()
    first, second = _sends(events)
    preview = preview_of(events, _task_before(events, first.event_id))
    assert preview.valid
    assert [(c.fn, c.scheduled_event_id) for c in preview.calls] == [
        ("queue_print", first.event_id),
        ("queue_print", second.event_id),
    ]
    assert preview.as_of_event_id == events[-1].event_id


def test_a_point_between_the_prints_lists_the_second() -> None:
    events = _events()
    first, second = _sends(events)
    preview = preview_of(events, _task_before(events, second.event_id))
    assert first.event_id < preview.event_id
    assert [c.scheduled_event_id for c in preview.calls] == [second.event_id]
    attrs = second.activity_task_scheduled_event_attributes
    assert attrs.activity_id == f"outward-queue_print-{preview.calls[0].call_id}"


def test_a_point_after_the_prints_lists_none() -> None:
    events = _events()
    point = _task_before(events, events[-1].event_id)
    preview = preview_of(events, point)
    assert (preview.valid, preview.calls) == (True, [])


def test_only_a_completed_workflow_task_before_the_end_is_valid() -> None:
    events = _events()
    started = next(e.event_id for e in events if e.event_type != COMPLETED and e.event_id > 1)
    assert not preview_of(events, started).valid
    assert not preview_of(events, events[-1].event_id).valid
    assert not preview_of(events, events[-1].event_id + 10).valid


@pytest.mark.requires_temporal
@pytest.mark.requires_postgres
async def test_two_prints(outward: Outward) -> None:
    """Runs the fixture's script live; records it with `SCADBUDDY_RECORD_HISTORIES=1`."""
    run_id = await outward.run(
        script(
            "await render('box', {'width': 20})",
            "await wait_for_human('Go?', 600)",
            f"await queue_print({{'output_id': 'o1'}}, {PLAN!r})",
            f"await queue_print({{'output_id': 'o2'}}, {PLAN!r})",
            "return 'printed'",
        )
    )
    run = await outward.row(run_id, lambda r: r.status == "waiting")
    await AgentClient(outward.client, f"flow-{run_id}").provide_callback_result(
        run.waiting_on[0].call_id, result={"answer": "yes"}
    )
    await outward.decide(run_id, approved=True)
    await outward.row(run_id, lambda r: len(r.steps) == 4)
    await outward.decide(run_id, approved=True)
    done = await outward.finished(run_id)
    assert done.result == "result: 'printed'"
    workflow_id = f"flow-{run_id}"
    history = await outward.client.get_workflow_handle(workflow_id).fetch_history()
    events = list(history.events)
    first, _ = _sends(events)
    live = await outward_since(outward.client, workflow_id, _task_before(events, first.event_id))
    assert [c.fn for c in live.calls] == ["queue_print", "queue_print"]
    if os.environ.get("SCADBUDDY_RECORD_HISTORIES") == "1":
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE.write_text(history.to_json())
