"""Housekeeping as a Temporal Schedule (#1054, spec 2026-10-01 §4.4)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from temporalio import activity
from temporalio.client import Client, ScheduleOverlapPolicy
from temporalio.service import RPCError, RPCStatusCode
from temporalio.worker import Worker

from scadbuddy import main
from scadbuddy.workflows.housekeeping import (
    PRUNE_INTERVAL,
    PRUNE_SWEEPS,
    SWEEPS,
    Housekeeping,
    ensure_schedule,
    ensure_schedules,
    prune_schedule_id_for,
    schedule_id_for,
)
from tests.support.temporal import temporal_client

pytestmark = pytest.mark.requires_temporal


class FakeSweeps:
    def __init__(self, failing: str | None = None) -> None:
        self.ran: list[str] = []
        self.failing = failing

    def all(self) -> list[object]:
        def make(name: str) -> object:
            @activity.defn(name=name)
            async def sweep() -> None:
                self.ran.append(name)
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
        await client.execute_workflow(
            Housekeeping.run, id=f"housekeeping-{uuid.uuid4().hex}", task_queue=queue
        )
    assert fake.ran == list(SWEEPS)


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
    assert first.schedule.spec.intervals[0].every == timedelta(seconds=600)
    assert second.schedule.spec.intervals[0].every == timedelta(seconds=120)
    assert second.schedule.policy.overlap == ScheduleOverlapPolicy.SKIP


async def test_an_interval_of_zero_deletes_the_schedule(client: Client) -> None:
    schedule_id = f"housekeeping-test-{uuid.uuid4().hex[:8]}"
    queue = f"library-{uuid.uuid4().hex[:8]}"
    await ensure_schedule(client, queue, 600.0, schedule_id=schedule_id)
    await ensure_schedule(client, queue, 0.0, schedule_id=schedule_id)
    with pytest.raises(RPCError):
        await client.get_schedule_handle(schedule_id).describe()
    # And zero with no Schedule is not an error.
    await ensure_schedule(client, queue, 0.0, schedule_id=schedule_id)


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
    assert described.schedule.spec.intervals[0].every == timedelta(seconds=PRUNE_INTERVAL)
    assert PRUNE_INTERVAL == 300.0


async def test_the_schedules_are_set_up_once_temporal_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review I2: a create that fails after the connect (a frontend up before its
    history service) is retried, not left to the next restart."""
    calls: list[float] = []

    async def flaky(client: object, queue: str, interval: float) -> None:
        calls.append(interval)
        if len(calls) == 1:
            raise RPCError("unavailable", RPCStatusCode.UNAVAILABLE, b"")

    monkeypatch.setattr(main, "ensure_schedules", flaky)
    monkeypatch.setattr(main, "PRINT_WORKER_RECONNECT", 0.01)
    stop = asyncio.Event()
    await asyncio.wait_for(main._set_up_housekeeping(object(), "library", 600.0, stop), 5)  # type: ignore[arg-type]
    assert calls == [600.0, 600.0]
