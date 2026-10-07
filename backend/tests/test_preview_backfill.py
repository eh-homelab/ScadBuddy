"""The preview backfill as a Schedule-triggered workflow (#1054, spec 2026-10-01 §4.4)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import timedelta

import pytest
from temporalio import activity, workflow
from temporalio.api.enums.v1 import EventType
from temporalio.client import (
    Client,
    ScheduleActionStartWorkflow,
    ScheduleOverlapPolicy,
    WorkflowExecutionStatus,
)
from temporalio.exceptions import ApplicationError
from temporalio.service import RPCError
from temporalio.worker import Worker

from scadbuddy.workflows.previews import (
    BACKFILL_TIMEOUT,
    BATCH,
    DUE_ACTIVITY,
    PREVIEW_BACKFILL_WORKFLOW,
    REFRESH_ACTIVITY,
    REFRESH_TIMEOUT,
    UNRUN_FAILURE,
    PreviewBackfill,
    ensure_preview_schedule,
    preview_schedule_id_for,
    refresh_timeout_for,
)
from tests.support.temporal import temporal_client, terminate_open_workflows

pytestmark = pytest.mark.requires_temporal


class FakePreviews:
    def __init__(
        self,
        due: list[str],
        *,
        rendered: bool = True,
        failing: str | None = None,
        unrun: str | None = None,
    ) -> None:
        self.due = due
        self.rendered = rendered
        self.failing = failing
        self.unrun = unrun
        self.listed = 0
        self.refreshed: list[str] = []

    def activities(self) -> list[object]:
        @activity.defn(name=DUE_ACTIVITY)
        async def previews_due() -> list[str]:
            self.listed += 1
            return self.due

        @activity.defn(name=REFRESH_ACTIVITY)
        async def preview_refresh(slug: str) -> bool:
            self.refreshed.append(slug)
            if slug == self.failing:
                raise RuntimeError(f"{slug} broke")
            if slug == self.unrun:
                raise ApplicationError("no render worker", type=UNRUN_FAILURE)
            return self.rendered

        return [previews_due, preview_refresh]


@pytest.fixture
async def client() -> AsyncIterator[Client]:
    async with temporal_client() as connected:
        yield connected


def _queue() -> str:
    return f"library-{uuid.uuid4().hex[:8]}"


async def _timers(client: Client, workflow_id: str) -> int:
    history = await client.get_workflow_handle(workflow_id).fetch_history()
    return sum(
        1 for event in history.events if event.event_type == EventType.EVENT_TYPE_TIMER_STARTED
    )


async def _refresh_timeouts(
    client: Client, workflow_id: str, run_id: str | None = None
) -> set[float]:
    history = await client.get_workflow_handle(workflow_id, run_id=run_id).fetch_history()
    return {
        event.activity_task_scheduled_event_attributes.start_to_close_timeout.ToTimedelta().total_seconds()
        for event in history.events
        if event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
        and event.activity_task_scheduled_event_attributes.activity_type.name == REFRESH_ACTIVITY
    }


async def test_backfill_refreshes_each_due_model_in_order(client: Client) -> None:
    queue = _queue()
    fake = FakePreviews(["a", "b", "c"])
    workflow_id = f"previews-{uuid.uuid4().hex}"
    async with Worker(
        client,
        task_queue=queue,
        workflows=[PreviewBackfill],
        activities=fake.activities(),  # type: ignore[arg-type]
    ):
        failed = await client.execute_workflow(
            PreviewBackfill.run, id=workflow_id, task_queue=queue
        )
    assert failed == []
    assert fake.refreshed == ["a", "b", "c"]
    # The pause after each render: one timer per refresh that rendered.
    assert await _timers(client, workflow_id) == 3


async def test_a_failing_refresh_does_not_stop_the_rest(client: Client) -> None:
    queue = _queue()
    fake = FakePreviews(["a", "b", "c"], rendered=False, failing="b")
    workflow_id = f"previews-{uuid.uuid4().hex}"
    async with Worker(
        client,
        task_queue=queue,
        workflows=[PreviewBackfill],
        activities=fake.activities(),  # type: ignore[arg-type]
    ):
        failed = await client.execute_workflow(
            PreviewBackfill.run, id=workflow_id, task_queue=queue
        )
    assert failed == ["b"]
    assert fake.refreshed == ["a", "b", "c"]
    # Nothing rendered, so nothing paused.
    assert await _timers(client, workflow_id) == 0


async def test_a_render_that_could_not_run_ends_the_run(client: Client) -> None:
    """Review #1195 2: the render queue is unreachable, so each later refresh would
    only wait out the same timeout; the run ends with that model failed, and the next
    tick is the retry."""
    queue = _queue()
    fake = FakePreviews(["a", "b", "c"], unrun="b")
    workflow_id = f"previews-{uuid.uuid4().hex}"
    async with Worker(
        client,
        task_queue=queue,
        workflows=[PreviewBackfill],
        activities=fake.activities(),  # type: ignore[arg-type]
    ):
        failed = await client.execute_workflow(
            PreviewBackfill.run, id=workflow_id, task_queue=queue
        )
    assert failed == ["b"]
    assert fake.refreshed == ["a", "b"]


async def test_a_long_backlog_continues_as_new(client: Client) -> None:
    """The first boot after the upgrade may find hundreds of models to render: the run
    carries the rest into a new run rather than growing one history without bound."""
    queue = _queue()
    slugs = [f"m{index}" for index in range(BATCH + 1)]
    fake = FakePreviews(slugs, rendered=False)
    workflow_id = f"previews-{uuid.uuid4().hex}"
    async with Worker(
        client,
        task_queue=queue,
        workflows=[PreviewBackfill],
        activities=fake.activities(),  # type: ignore[arg-type]
    ):
        handle = await client.start_workflow(PreviewBackfill.run, id=workflow_id, task_queue=queue)
        assert await handle.result() == []
    first = await client.get_workflow_handle(
        workflow_id, run_id=handle.first_execution_run_id
    ).describe()
    assert first.status == WorkflowExecutionStatus.CONTINUED_AS_NEW
    assert fake.refreshed == slugs
    assert fake.listed == 1


async def test_continue_as_new_when_temporal_suggests_it(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Well short of `BATCH`, a history Temporal finds too large is carried on in a new
    run, with the refresh bound it was given."""
    # Suggested in the first run only, so the new run finishes.
    monkeypatch.setattr(
        workflow.Info,
        "is_continue_as_new_suggested",
        lambda self: self.continued_run_id is None,
    )
    queue = _queue()
    fake = FakePreviews(["a", "b"], rendered=False)
    workflow_id = f"previews-{uuid.uuid4().hex}"
    async with Worker(
        client,
        task_queue=queue,
        workflows=[PreviewBackfill],
        activities=fake.activities(),  # type: ignore[arg-type]
    ):
        handle = await client.start_workflow(
            PreviewBackfill.run, args=[None, 5400.0], id=workflow_id, task_queue=queue
        )
        assert await handle.result() == []
    first = await client.get_workflow_handle(
        workflow_id, run_id=handle.first_execution_run_id
    ).describe()
    assert first.status == WorkflowExecutionStatus.CONTINUED_AS_NEW
    assert fake.refreshed == ["a", "b"]
    assert fake.listed == 1
    assert await _refresh_timeouts(client, workflow_id) == {5400.0}


async def test_a_refresh_is_bounded_by_the_timeout_it_is_given(client: Client) -> None:
    """Final review M1: a refresh may wait out the scheduler's render, then run its own,
    each up to 3 x `render_timeout`; the bound comes from the Schedule, not a fixed hour."""
    queue = _queue()
    fake = FakePreviews(["a"], rendered=False)
    workflow_id = f"previews-{uuid.uuid4().hex}"
    async with Worker(
        client,
        task_queue=queue,
        workflows=[PreviewBackfill],
        activities=fake.activities(),  # type: ignore[arg-type]
    ):
        await client.execute_workflow(
            PreviewBackfill.run, args=[None, 7200.0], id=workflow_id, task_queue=queue
        )
    assert await _refresh_timeouts(client, workflow_id) == {7200.0}


def test_the_refresh_bound_scales_with_the_render_bound() -> None:
    # A render bound of 3 x 600 s: two of them no longer fit in an hour.
    assert refresh_timeout_for(1800.0) > 2 * 1800.0
    # The default (3 x 120 s) keeps the hour.
    assert refresh_timeout_for(360.0) == REFRESH_TIMEOUT.total_seconds()


async def test_the_preview_schedule_runs_the_backfill_hourly(client: Client) -> None:
    queue = _queue()
    handle = client.get_schedule_handle(preview_schedule_id_for(queue))
    try:
        await ensure_preview_schedule(client, queue, 1800.0)
        described = await handle.describe()
    finally:
        await handle.delete()
        await terminate_open_workflows(client, queue)
    assert described.schedule.spec.intervals[0].every == timedelta(hours=1)
    assert described.schedule.policy.overlap == ScheduleOverlapPolicy.SKIP
    action = described.schedule.action
    assert isinstance(action, ScheduleActionStartWorkflow)
    assert action.workflow == PREVIEW_BACKFILL_WORKFLOW
    assert action.task_queue == queue
    # Its refreshes bounded for the render bound it was given (final review M1).
    assert await client.data_converter.decode(list(action.args)) == [
        None,
        refresh_timeout_for(1800.0),
    ]
    assert action.execution_timeout == BACKFILL_TIMEOUT


async def test_previews_off_deletes_the_schedule(client: Client) -> None:
    queue = _queue()
    handle = client.get_schedule_handle(preview_schedule_id_for(queue))
    try:
        await ensure_preview_schedule(client, queue, 360.0)
        await ensure_preview_schedule(client, queue, None)
        with pytest.raises(RPCError):
            await handle.describe()
        # And off with no Schedule is not an error.
        await ensure_preview_schedule(client, queue, None)
    finally:
        with suppress(RPCError):  # gone, as it should be
            await handle.delete()
        await terminate_open_workflows(client, queue)
