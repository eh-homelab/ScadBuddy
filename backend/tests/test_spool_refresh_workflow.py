"""The spool providers' refresh on its own Schedule (#3818 tier A, plan 2026-10-18 Task
6b): off the hot path, on the ``library`` queue."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import timedelta
from typing import cast

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
from scadbuddy.spools.component import Spools
from scadbuddy.spools.models import SpoolSnapshot
from scadbuddy.spools.providers import SpoolProvider
from scadbuddy.spools.store import SpoolStore
from scadbuddy.workflows.spool_refresh import (
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


def fakes(
    outcomes: dict[str, str],
    *,
    failing: str | None = None,
    seen_fails: bool = False,
    held_fails: bool = False,
) -> tuple[list[object], list[str]]:
    calls: list[str] = []

    @activity.defn(name=LIST_ACTIVITY)
    async def names() -> list[str]:
        calls.append("list")
        return list(outcomes)

    @activity.defn(name=PROVIDER_ACTIVITY)
    async def refresh(name: str) -> str:
        calls.append(name)
        if name == failing:
            raise RuntimeError("could not even record the failure")
        return outcomes[name]

    @activity.defn(name=SEEN_ACTIVITY)
    async def seen() -> None:
        calls.append("seen")
        if seen_fails:
            raise RuntimeError("no reports")

    @activity.defn(name=HELD_ACTIVITY)
    async def held() -> str:
        calls.append("held")
        if held_fails:
            raise RuntimeError("Bambuddy is down")
        return "refreshed"

    return [names, refresh, seen, held], calls


async def test_a_run_refreshes_each_provider_then_the_trays(client: Client) -> None:
    activities, calls = fakes({"a": "refreshed", "b": "skipped"})
    queue = _queue()
    async with Worker(client, task_queue=queue, workflows=[SpoolRefresh], activities=activities):  # type: ignore[arg-type]
        outcome = await client.execute_workflow(
            SpoolRefresh.run, id=f"spools-{uuid.uuid4().hex}", task_queue=queue
        )
    assert outcome == {"a": "refreshed", "b": "skipped"}
    # Then Bambuddy's local presets are kept (#4645), last, behind its patch.
    assert calls == ["list", "a", "b", "seen", "held"]


async def test_one_providers_failure_never_stops_the_others(client: Client) -> None:
    """I3: each provider is its own activity, and the trays' step its own too. Nothing is
    retried within the run: the next tick is the retry, as it is for the sweeps."""
    activities, calls = fakes(
        {"a": "refreshed", "b": "refreshed"}, failing="a", seen_fails=True, held_fails=True
    )
    queue = _queue()
    async with Worker(client, task_queue=queue, workflows=[SpoolRefresh], activities=activities):  # type: ignore[arg-type]
        outcome = await client.execute_workflow(
            SpoolRefresh.run, id=f"spools-{uuid.uuid4().hex}", task_queue=queue
        )
    assert outcome == {"a": "failed", "b": "refreshed"}
    assert calls == ["list", "a", "b", "seen", "held"]


@pytest.mark.parametrize("breaks", ["records", "replace_records"])
async def test_the_real_activities_isolate_each_provider(client: Client, breaks: str) -> None:
    """I3, through `spool_activities`: provider ``a`` fails in its read or in its write,
    and ``b`` is still refreshed."""

    class Provider:
        priority = 100

        def __init__(self, name: str) -> None:
            self.name = name
            self.label = name

        async def configured(self) -> bool:
            return True

        async def records(self) -> list[SpoolSnapshot]:
            if self.name == "a" and breaks == "records":
                raise RuntimeError("the provider is down")
            return [SpoolSnapshot(record_id="1")]

        def printer_key(self, row: object) -> str | None:
            return None

    class Store:
        def __init__(self) -> None:
            self.refreshed: list[str] = []
            self.failures: list[str] = []

        async def replace_records(
            self, name: str, snapshots: object, at: object, *, timeout: float
        ) -> None:
            if name == "a" and breaks == "replace_records":
                raise RuntimeError("the store refused it")
            self.refreshed.append(name)

        async def failed(self, name: str, at: object, failure: str) -> None:
            self.failures.append(name)

    store = Store()
    spools = Spools(
        store=cast(SpoolStore, store),
        providers=cast(list[SpoolProvider], [Provider("a"), Provider("b")]),
    )
    queue = _queue()
    async with Worker(
        client, task_queue=queue, workflows=[SpoolRefresh], activities=spool_activities(spools)
    ):
        outcome = await client.execute_workflow(
            SpoolRefresh.run, id=f"spools-{uuid.uuid4().hex}", task_queue=queue
        )
    assert outcome == {"a": "failed", "b": "refreshed"}
    assert store.refreshed == ["b"]
    assert store.failures == ["a"]


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
