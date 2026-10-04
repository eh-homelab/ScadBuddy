"""Housekeeping as a Temporal Schedule (#1054, spec 2026-10-01 §4.4)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import timedelta

import pytest
from temporalio import activity
from temporalio.client import Client, ScheduleOverlapPolicy
from temporalio.service import RPCError
from temporalio.worker import Worker

from scadbuddy.workflows.housekeeping import (
    HEARTBEAT_TIMEOUT,
    PRUNE_SWEEPS,
    PRUNE_TIMEOUT,
    SWEEP_TIMEOUT,
    SWEEPS,
    Housekeeping,
    ensure_schedule,
    ensure_schedules,
    prune_schedule_id_for,
    schedule_id_for,
)
from tests.support.temporal import temporal_client, terminate_open_workflows

pytestmark = pytest.mark.requires_temporal


class FakeSweeps:
    def __init__(self, failing: str | None = None) -> None:
        self.ran: list[str] = []
        self.failing = failing
        self.timeouts: dict[str, tuple[timedelta | None, timedelta | None]] = {}

    def all(self) -> list[object]:
        def make(name: str) -> object:
            @activity.defn(name=name)
            async def sweep() -> None:
                self.ran.append(name)
                info = activity.info()
                self.timeouts[name] = (info.start_to_close_timeout, info.heartbeat_timeout)
                if name == self.failing:
                    raise RuntimeError(f"{name} broke")

            return sweep

        return [make(name) for name in SWEEPS]


@pytest.fixture
async def client() -> AsyncIterator[Client]:
    async with temporal_client() as connected:
        yield connected


async def test_housekeeping_runs_every_sweep_in_order(client: Client) -> None:
    queue = f"library-{uuid.uuid4().hex[:8]}"
    fake = FakeSweeps()
    async with Worker(client, task_queue=queue, workflows=[Housekeeping], activities=fake.all()):  # type: ignore[arg-type]
        await client.execute_workflow(
            Housekeeping.run, id=f"housekeeping-{uuid.uuid4().hex}", task_queue=queue
        )
    assert fake.ran == list(SWEEPS)


async def test_a_failing_sweep_does_not_stop_the_rest(client: Client) -> None:
    queue = f"library-{uuid.uuid4().hex[:8]}"
    fake = FakeSweeps(failing=SWEEPS[1])
    async with Worker(client, task_queue=queue, workflows=[Housekeeping], activities=fake.all()):  # type: ignore[arg-type]
        failed = await client.execute_workflow(
            Housekeeping.run, id=f"housekeeping-{uuid.uuid4().hex}", task_queue=queue
        )
    assert fake.ran == list(SWEEPS)
    assert failed == [SWEEPS[1]]


async def test_the_prune_is_short_and_the_long_sweeps_heartbeat(client: Client) -> None:
    """Review #1095 2: a worker lost mid-sweep is noticed within the heartbeat
    timeout, and a lost prune within its own short one, not after `SWEEP_TIMEOUT`."""
    queue = f"library-{uuid.uuid4().hex[:8]}"
    fake = FakeSweeps()
    async with Worker(client, task_queue=queue, workflows=[Housekeeping], activities=fake.all()):  # type: ignore[arg-type]
        await client.execute_workflow(
            Housekeeping.run, id=f"housekeeping-{uuid.uuid4().hex}", task_queue=queue
        )
    assert fake.timeouts[SWEEPS[0]] == (PRUNE_TIMEOUT, None)
    for sweep in SWEEPS[1:]:
        assert fake.timeouts[sweep] == (SWEEP_TIMEOUT, HEARTBEAT_TIMEOUT)


async def test_ensure_schedule_creates_then_updates_the_interval(client: Client) -> None:
    schedule_id = f"housekeeping-test-{uuid.uuid4().hex[:8]}"
    queue = f"library-{uuid.uuid4().hex[:8]}"
    try:
        await ensure_schedule(client, queue, 600.0, schedule_id=schedule_id)
        first = await client.get_schedule_handle(schedule_id).describe()
        await ensure_schedule(client, queue, 120.0, schedule_id=schedule_id)
        second = await client.get_schedule_handle(schedule_id).describe()
    finally:
        await client.get_schedule_handle(schedule_id).delete()
        await terminate_open_workflows(client, queue)
    assert first.schedule.spec.intervals[0].every == timedelta(seconds=600)
    assert second.schedule.spec.intervals[0].every == timedelta(seconds=120)
    assert second.schedule.policy.overlap == ScheduleOverlapPolicy.SKIP


async def test_an_interval_under_temporals_minimum_is_the_minimum(client: Client) -> None:
    """Temporal refuses a Schedule interval under a second; a smaller setting is not
    a Schedule that never gets made."""
    schedule_id = f"housekeeping-test-{uuid.uuid4().hex[:8]}"
    queue = f"library-{uuid.uuid4().hex[:8]}"
    try:
        await ensure_schedule(client, queue, 0.05, schedule_id=schedule_id)
        described = await client.get_schedule_handle(schedule_id).describe()
    finally:
        await client.get_schedule_handle(schedule_id).delete()
        await terminate_open_workflows(client, queue)
    assert described.schedule.spec.intervals[0].every == timedelta(seconds=1)


async def test_an_interval_of_zero_deletes_the_schedule(client: Client) -> None:
    schedule_id = f"housekeeping-test-{uuid.uuid4().hex[:8]}"
    queue = f"library-{uuid.uuid4().hex[:8]}"
    handle = client.get_schedule_handle(schedule_id)
    try:
        await ensure_schedule(client, queue, 600.0, schedule_id=schedule_id)
        await ensure_schedule(client, queue, 0.0, schedule_id=schedule_id)
        with pytest.raises(RPCError):
            await handle.describe()
        # And zero with no Schedule is not an error.
        await ensure_schedule(client, queue, 0.0, schedule_id=schedule_id)
    finally:
        with suppress(RPCError):  # gone, as it should be
            await handle.delete()
        await terminate_open_workflows(client, queue)


async def test_housekeeping_runs_only_the_sweeps_it_is_given(client: Client) -> None:
    queue = f"library-{uuid.uuid4().hex[:8]}"
    fake = FakeSweeps()
    async with Worker(client, task_queue=queue, workflows=[Housekeeping], activities=fake.all()):  # type: ignore[arg-type]
        await client.execute_workflow(
            Housekeeping.run,
            list(PRUNE_SWEEPS),
            id=f"housekeeping-{uuid.uuid4().hex}",
            task_queue=queue,
        )
    assert fake.ran == list(PRUNE_SWEEPS)


async def test_the_prune_keeps_its_own_cadence_when_the_sweeps_are_off(client: Client) -> None:
    """Review I1: settled jobs (and the blob refs they hold) are pruned every
    `PRUNE_INTERVAL`, as the loop did, whatever the sweep interval, even 0."""
    queue = f"library-{uuid.uuid4().hex[:8]}"
    prune = client.get_schedule_handle(prune_schedule_id_for(queue))
    try:
        await ensure_schedules(client, queue, 0.0)
        described = await prune.describe()
        with pytest.raises(RPCError):
            await client.get_schedule_handle(schedule_id_for(queue)).describe()
    finally:
        await prune.delete()
        await terminate_open_workflows(client, queue)
    assert described.schedule.spec.intervals[0].every == timedelta(seconds=300)
