"""The spool refresh on its own Schedule (#3818 tier A, plan 2026-10-18 Task 6b): off the
hot path, on the ``library`` queue. Since #6262 Task 12 it keeps Bambuddy's local presets
and pulls Bambuddy's spools; the spool providers and their overlay are gone."""

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
)
from temporalio.service import RPCError
from temporalio.worker import Worker

from scadbuddy.spools.activities import spool_activities
from scadbuddy.workflows.spool_refresh import (
    FILAMENT_PULL_ACTIVITY,
    HELD_ACTIVITY,
    LIST_ACTIVITY,
    PROVIDER_ACTIVITY,
    RUN_TIMEOUT,
    SEEN_ACTIVITY,
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


def fakes(*, held_fails: bool = False) -> tuple[list[object], list[str]]:
    calls: list[str] = []

    @activity.defn(name=LIST_ACTIVITY)
    async def names() -> list[str]:
        calls.append("list")
        return []

    @activity.defn(name=PROVIDER_ACTIVITY)
    async def refresh(name: str) -> str:
        calls.append(name)
        return "skipped"

    @activity.defn(name=SEEN_ACTIVITY)
    async def seen() -> None:
        calls.append("seen")

    @activity.defn(name=HELD_ACTIVITY)
    async def held() -> str:
        calls.append("held")
        if held_fails:
            raise RuntimeError("Bambuddy is down")
        return "refreshed"

    @activity.defn(name=FILAMENT_PULL_ACTIVITY)
    async def pull() -> str:
        calls.append("pull")
        return "pulled"

    return [names, refresh, seen, held, pull], calls


async def test_a_run_keeps_the_presets_then_pulls(client: Client) -> None:
    """#6262 Task 12: no provider is listed or refreshed, and no tray recorded (the
    overlay is gone); Bambuddy's local presets are kept (#4645), then its spools pulled
    (#6261)."""
    activities, calls = fakes()
    queue = _queue()
    async with Worker(client, task_queue=queue, workflows=[SpoolRefresh], activities=activities):  # type: ignore[arg-type]
        outcome = await client.execute_workflow(
            SpoolRefresh.run, id=f"spools-{uuid.uuid4().hex}", task_queue=queue
        )
    assert outcome == {}
    assert calls == ["held", "pull"]


async def test_a_failed_step_never_stops_the_next(client: Client) -> None:
    """Nothing is retried within the run: the next tick is the retry, as it is for the
    sweeps."""
    activities, calls = fakes(held_fails=True)
    queue = _queue()
    async with Worker(client, task_queue=queue, workflows=[SpoolRefresh], activities=activities):  # type: ignore[arg-type]
        outcome = await client.execute_workflow(
            SpoolRefresh.run, id=f"spools-{uuid.uuid4().hex}", task_queue=queue
        )
    assert outcome == {}
    assert calls == ["held", "pull"]


async def test_the_retired_activities_answer_as_an_open_run_expects() -> None:
    """A run opened before ``spool-providers-gone`` still calls them on the new code: no
    providers, so no refresh, and no trays to record."""
    providers, refresh, seen = spool_activities()
    assert await providers() == []
    assert await refresh("bambuddy") == "skipped"
    assert await seen() is None


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
