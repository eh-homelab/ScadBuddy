"""Housekeeping as a Temporal Schedule (#1054, spec 2026-10-01 §4.4)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from temporalio import activity
from temporalio.client import Client, ScheduleOverlapPolicy
from temporalio.service import RPCError
from temporalio.worker import Worker

from scadbuddy.workflows.housekeeping import (
    SWEEPS,
    Housekeeping,
    ensure_schedule,
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
