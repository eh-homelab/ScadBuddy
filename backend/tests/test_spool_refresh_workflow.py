"""The spool providers' refresh on its own Schedule (#3818 tier A, plan 2026-10-18 Task
6b): off the hot path, on the ``library`` queue."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import timedelta

import pytest
from temporalio import activity
from temporalio.client import (
    Client,
    ScheduleActionStartWorkflow,
    ScheduleOverlapPolicy,
    WorkflowFailureError,
)
from temporalio.service import RPCError
from temporalio.worker import Worker

from scadbuddy.workflows.spool_refresh import (
    REFRESH_ACTIVITY,
    RUN_TIMEOUT,
    SPOOL_REFRESH_WORKFLOW,
    SpoolRefresh,
    ensure_spool_schedule,
    spool_schedule_id_for,
)
from tests.support.temporal import temporal_client, terminate_open_workflows

pytestmark = pytest.mark.requires_temporal


@pytest.fixture
async def client() -> AsyncIterator[Client]:
    async with temporal_client() as connected:
        yield connected


def _queue() -> str:
    return f"library-{uuid.uuid4().hex[:8]}"


async def test_a_run_refreshes_once_and_answers_each_providers_outcome(client: Client) -> None:
    calls: list[None] = []

    @activity.defn(name=REFRESH_ACTIVITY)
    async def refresh() -> dict[str, str]:
        calls.append(None)
        return {"bambuddy": "refreshed"}

    queue = _queue()
    async with Worker(client, task_queue=queue, workflows=[SpoolRefresh], activities=[refresh]):
        outcome = await client.execute_workflow(
            SpoolRefresh.run, id=f"spools-{uuid.uuid4().hex}", task_queue=queue
        )
    assert outcome == {"bambuddy": "refreshed"}
    assert len(calls) == 1


async def test_a_failed_refresh_is_not_retried_in_the_run(client: Client) -> None:
    """The next tick is the retry, as it is for the sweeps."""
    calls: list[None] = []

    @activity.defn(name=REFRESH_ACTIVITY)
    async def refresh() -> dict[str, str]:
        calls.append(None)
        raise RuntimeError("the store is down")

    queue = _queue()
    async with Worker(client, task_queue=queue, workflows=[SpoolRefresh], activities=[refresh]):
        with pytest.raises(WorkflowFailureError):
            await client.execute_workflow(
                SpoolRefresh.run, id=f"spools-{uuid.uuid4().hex}", task_queue=queue
            )
    assert len(calls) == 1


async def test_the_schedule_runs_the_refresh_every_interval(client: Client) -> None:
    queue = _queue()
    handle = client.get_schedule_handle(spool_schedule_id_for(queue))
    try:
        await ensure_spool_schedule(client, queue, 300)
        described = await handle.describe()
    finally:
        await handle.delete()
        await terminate_open_workflows(client, queue)
    assert described.schedule.spec.intervals[0].every == timedelta(seconds=300)
    assert described.schedule.policy.overlap == ScheduleOverlapPolicy.SKIP
    action = described.schedule.action
    assert isinstance(action, ScheduleActionStartWorkflow)
    assert action.workflow == SPOOL_REFRESH_WORKFLOW
    assert action.task_queue == queue
    assert action.execution_timeout == RUN_TIMEOUT


async def test_zero_deletes_the_schedule(client: Client) -> None:
    queue = _queue()
    handle = client.get_schedule_handle(spool_schedule_id_for(queue))
    try:
        await ensure_spool_schedule(client, queue, 300)
        await ensure_spool_schedule(client, queue, 0)
        with pytest.raises(RPCError):
            await handle.describe()
        await ensure_spool_schedule(client, queue, 0)
    finally:
        with suppress(RPCError):
            await handle.delete()
        await terminate_open_workflows(client, queue)
