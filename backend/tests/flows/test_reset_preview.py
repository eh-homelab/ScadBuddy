"""What a Reset would run again (#1057, plan 2026-10-09-durable-phase-6-flows.md Task
E1): the outward calls after a point of a recorded history.

`two_prints.json` was recorded on 6e's workflow (`SCADBUDDY_RECORD_HISTORIES=1`): a
`render`, a `wait_for_human` answered, then two approved `queue_print`s. Its payloads
are sealed; the preview reads only event types and activity ids.
"""

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from temporal_agent_harness.harness.agent_client import AgentClient
from temporalio.api.enums.v1 import EventType
from temporalio.api.history.v1 import History, HistoryEvent
from temporalio.api.workflowservice.v1 import (
    GetWorkflowExecutionHistoryReverseRequest,
    GetWorkflowExecutionHistoryReverseResponse,
)
from temporalio.client import Client, WorkflowHistory

from scadbuddy.flows import history
from scadbuddy.flows.history import outward_since, points_of, preview_of
from scadbuddy.flows.models import Run
from scadbuddy.workflows.flow_activities import FLOW_CLOSE
from scadbuddy.workflows.flow_models import FlowStep
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


def _closes(events: list[HistoryEvent]) -> int:
    return next(
        e.event_id
        for e in events
        if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
        and e.activity_task_scheduled_event_attributes.activity_type.name == FLOW_CLOSE
    )


def test_a_finished_run_resets_only_to_a_point_before_it_recorded_its_end() -> None:
    """#2378: the end's write and `flow_close` are one workflow task's commands. A point
    after them replays the end without writing it, so the run would read `running`."""
    events = _events()
    closes = _closes(events)
    before = _task_before(events, closes)
    after = [
        e.event_id
        for e in events
        if e.event_type == COMPLETED and closes < e.event_id < events[-1].event_id
    ]
    assert after
    assert preview_of(events, before, ended=True).valid
    for point in after:
        assert not preview_of(events, point, ended=True).valid
        assert preview_of(events, point).valid
    offered = [p.event_id for p in points_of(events, [], ended=True).points]
    assert offered == [
        e.event_id for e in events if preview_of(events, e.event_id, ended=True).valid
    ]
    assert before in offered
    assert not set(after) & set(offered)


async def test_a_finished_runs_preview_refuses_a_point_after_its_end() -> None:
    events = _events()
    point = _task_before(events, events[-1].event_id)
    for status, valid in (("waiting", True), ("succeeded", False), ("failed", False)):
        run = cast(Run, SimpleNamespace(workflow_id="flow-two-prints", steps=[], status=status))
        preview = await outward_since(cast(Client, _Served(events)), run, point, run_id="r1")
        assert preview.valid is valid, status


def test_only_the_execute_update_starts_the_script() -> None:
    """#2303: an Update other than `execute` (`send_agent_message`) accepted before the
    point does not make it valid."""
    events = _events()
    first, _ = _sends(events)
    point = _task_before(events, first.event_id)
    assert preview_of(events, point).valid
    renamed = []
    for event in events:
        copy = HistoryEvent()
        copy.CopyFrom(event)
        if copy.event_type == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_UPDATE_ACCEPTED:
            copy.workflow_execution_update_accepted_event_attributes.accepted_request.input.name = (
                "provide_callback_result"
            )
        renamed.append(copy)
    assert not preview_of(renamed, point).valid


class _Served:
    """A client over the fixture's history that counts the events it hands out."""

    def __init__(self, events: list[HistoryEvent]) -> None:
        self.events = events
        self.served = 0
        self.namespace = "default"
        self.workflow_service = self
        self.data_converter = None

    def get_workflow_handle(self, workflow_id: str, *, run_id: str | None = None) -> Any:
        served = self

        async def fetch_history_events(page_size: int | None = None) -> AsyncIterator[HistoryEvent]:
            for event in served.events:
                served.served += 1
                yield event

        async def fetch_history() -> WorkflowHistory:
            served.served += len(served.events)
            return WorkflowHistory(workflow_id, served.events)

        return SimpleNamespace(
            fetch_history_events=fetch_history_events, fetch_history=fetch_history
        )

    async def get_workflow_execution_history_reverse(
        self, request: GetWorkflowExecutionHistoryReverseRequest
    ) -> GetWorkflowExecutionHistoryReverseResponse:
        newest = self.events[::-1]
        start = int(request.next_page_token or b"0")
        page = newest[start : start + request.maximum_page_size]
        self.served += len(page)
        end = start + len(page)
        return GetWorkflowExecutionHistoryReverseResponse(
            history=History(events=page),
            next_page_token=str(end).encode() if end < len(newest) else b"",
        )


async def test_a_recent_point_reads_only_the_tail(monkeypatch: pytest.MonkeyPatch) -> None:
    """#2268: the preview reads the history back to the point and forward only to
    `execute`, not whole, and says what reading it whole says."""
    monkeypatch.setattr(history, "PAGE_SIZE", 5)
    events = _events()
    _, second = _sends(events)
    point = _task_before(events, second.event_id)
    served = _Served(events)
    run = cast(Run, SimpleNamespace(workflow_id="flow-two-prints", steps=[], status="waiting"))
    preview = await outward_since(cast(Client, served), run, point, run_id="r1")
    whole = preview_of(events, point)
    assert (preview.valid, preview.calls, preview.as_of_event_id) == (
        whole.valid,
        whole.calls,
        whole.as_of_event_id,
    )
    assert served.served < len(events) // 2


async def test_a_point_past_the_end_is_pinned_to_the_last_event() -> None:
    events = _events()
    run = cast(Run, SimpleNamespace(workflow_id="flow-two-prints", steps=[], status="waiting"))
    preview = await outward_since(
        cast(Client, _Served(events)), run, events[-1].event_id + 10, run_id="r1"
    )
    assert (preview.valid, preview.as_of_event_id) == (False, events[-1].event_id)


def _step(seq: int, history_length: int) -> FlowStep:
    return FlowStep(
        seq=seq,
        fn="queue_print",
        call_id=f"c{seq}",
        status="succeeded",
        outward=True,
        started_at=datetime(2026, 10, 11, tzinfo=UTC),
        history_length=history_length,
    )


def test_the_reset_points_are_the_valid_points_with_the_first_step_each_runs_again() -> None:
    """#2350: the run page lists the points a Reset takes, rather than probing ids."""
    events = _events()
    first, second = _sends(events)
    # Each print's step started in the task that scheduled it: its started event.
    before_first, before_second = (_task_before(events, s.event_id) for s in (first, second))
    steps = [_step(1, before_first - 1), _step(2, before_second - 1)]
    found = points_of(events, steps)
    assert found.as_of_event_id == events[-1].event_id
    assert [p.event_id for p in found.points] == [
        e.event_id for e in events if preview_of(events, e.event_id).valid
    ]
    first_step = {p.event_id: p.first_step for p in found.points}
    assert first_step[before_first] == 1
    assert first_step[before_second] == 2
    assert first_step[_task_before(events, events[-1].event_id)] is None
    # A step that ended in the task done again is reopened: it waits or runs again.
    steps[0].ended_history_length = before_second - 1
    reopened = {p.event_id: p.first_step for p in points_of(events, steps).points}
    assert reopened[before_second] == 1


def test_the_reset_points_leave_out_those_before_the_runs_floor() -> None:
    """A Reset refuses a point at or before the run's floor (`check_reset`), so the list
    does not offer one."""
    events = _events()
    _, second = _sends(events)
    floor = _task_before(events, second.event_id) - 1
    every = [p.event_id for p in points_of(events, []).points]
    offered = [p.event_id for p in points_of(events, [], floor=floor).points]
    assert offered == [e for e in every if e - 1 > floor]
    assert 0 < len(offered) < len(every)


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
    live = await outward_since(outward.client, done, _task_before(events, first.event_id))
    assert [c.fn for c in live.calls] == ["queue_print", "queue_print"]
    if os.environ.get("SCADBUDDY_RECORD_HISTORIES") == "1":
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE.write_text(history.to_json())
