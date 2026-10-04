"""The preview backfill as a Schedule-triggered workflow (#1054, spec 2026-10-01 §4.4)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import timedelta

import pytest
from temporalio import activity
from temporalio.api.enums.v1 import EventType
from temporalio.client import (
    Client,
    ScheduleActionStartWorkflow,
    ScheduleOverlapPolicy,
    WorkflowExecutionStatus,
)
from temporalio.service import RPCError
from temporalio.worker import Worker

from scadbuddy.workflows.previews import (
    BACKFILL_TIMEOUT,
    BATCH,
    DUE_ACTIVITY,
    PREVIEW_BACKFILL_WORKFLOW,
    REFRESH_ACTIVITY,
    PreviewBackfill,
    ensure_preview_schedule,
    preview_schedule_id_for,
)
from tests.support.temporal import temporal_client, terminate_open_workflows

pytestmark = pytest.mark.requires_temporal


class FakePreviews:
    def __init__(
        self, due: list[str], *, rendered: bool = True, failing: str | None = None
    ) -> None:
        self.due = due
        self.rendered = rendered
        self.failing = failing
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


async def test_the_preview_schedule_runs_the_backfill_hourly(client: Client) -> None:
    queue = _queue()
    handle = client.get_schedule_handle(preview_schedule_id_for(queue))
    try:
        await ensure_preview_schedule(client, queue, True)
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
    assert action.args == []
    assert action.execution_timeout == BACKFILL_TIMEOUT


async def test_previews_off_deletes_the_schedule(client: Client) -> None:
    queue = _queue()
    handle = client.get_schedule_handle(preview_schedule_id_for(queue))
    try:
        await ensure_preview_schedule(client, queue, True)
        await ensure_preview_schedule(client, queue, False)
        with pytest.raises(RPCError):
            await handle.describe()
        # And off with no Schedule is not an error.
        await ensure_preview_schedule(client, queue, False)
    finally:
        with suppress(RPCError):  # gone, as it should be
            await handle.delete()
        await terminate_open_workflows(client, queue)
