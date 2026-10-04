"""Housekeeping as a Temporal Schedule (#1054, spec 2026-10-01 §4.4)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import timedelta

import pytest
from temporalio import activity
from temporalio.client import (
    Client,
    ScheduleActionExecutionStartWorkflow,
    ScheduleActionStartWorkflow,
    ScheduleOverlapPolicy,
)
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
    housekeeping_timeout,
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
    # The prune and the claims sweep (review 3c M1) are short, so they share a timeout.
    for sweep in SWEEPS:
        expected = (
            (PRUNE_TIMEOUT, None) if sweep in PRUNE_SWEEPS else (SWEEP_TIMEOUT, HEARTBEAT_TIMEOUT)
        )
        assert fake.timeouts[sweep] == expected


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
    # Review 3c M1: claims are swept even with the sweeps off, or they pile up for good.
    action = described.schedule.action
    assert isinstance(action, ScheduleActionStartWorkflow)
    (sweeps,) = await client.data_converter.decode(action.args)
    assert list(sweeps) == ["housekeeping_prune_jobs", "housekeeping_sweep_claims"]


async def _actions(client: Client, schedule_id: str, count: int) -> int:
    """The Schedule's action count once it reaches ``count``, or after ~5 s."""
    for _ in range(50):
        described = await client.get_schedule_handle(schedule_id).describe()
        if described.info.num_actions >= count:
            break
        await asyncio.sleep(0.1)
    return described.info.num_actions


async def test_the_boots_trigger_queues_behind_an_open_run(client: Client) -> None:
    """Review #1095 1a: a start while a run is still open (a rollout stopped the old
    pod mid-sweep) still gets its converging sweep, after that run."""
    schedule_id = f"housekeeping-test-{uuid.uuid4().hex[:8]}"
    queue = f"library-{uuid.uuid4().hex[:8]}"
    started = asyncio.Event()
    release = asyncio.Event()
    ran: list[str] = []

    @activity.defn(name=PRUNE_SWEEPS[0])
    async def prune() -> None:
        ran.append(PRUNE_SWEEPS[0])
        started.set()
        await release.wait()

    try:
        async with Worker(client, task_queue=queue, workflows=[Housekeeping], activities=[prune]):
            await ensure_schedule(
                client, queue, 3600.0, schedule_id=schedule_id, sweeps=PRUNE_SWEEPS
            )
            await asyncio.wait_for(started.wait(), 10)
            await ensure_schedule(
                client, queue, 3600.0, schedule_id=schedule_id, sweeps=PRUNE_SWEEPS
            )
            release.set()
            assert await _actions(client, schedule_id, 2) == 2
            for _ in range(50):
                if len(ran) == 2:
                    break
                await asyncio.sleep(0.1)
    finally:
        await client.get_schedule_handle(schedule_id).delete()
        await terminate_open_workflows(client, queue)
    assert ran == [PRUNE_SWEEPS[0]] * 2


async def test_a_run_is_bounded_by_its_sweeps_timeouts(client: Client) -> None:
    """Review #1095 1b: a run that can never finish (a workflow task that fails on
    replay) ends, so the Schedule's overlap SKIP does not hold back every later tick."""
    queue = f"library-{uuid.uuid4().hex[:8]}"
    prune = client.get_schedule_handle(prune_schedule_id_for(queue))
    sweeps = client.get_schedule_handle(schedule_id_for(queue))
    try:
        await ensure_schedules(client, queue, 600.0)
        prune_action = (await prune.describe()).schedule.action
        sweeps_action = (await sweeps.describe()).schedule.action
    finally:
        await prune.delete()
        await sweeps.delete()
        await terminate_open_workflows(client, queue)
    assert isinstance(prune_action, ScheduleActionStartWorkflow)
    assert isinstance(sweeps_action, ScheduleActionStartWorkflow)
    assert prune_action.execution_timeout == housekeeping_timeout(PRUNE_SWEEPS)
    assert timedelta(minutes=2) < prune_action.execution_timeout <= timedelta(minutes=10)
    assert sweeps_action.execution_timeout == housekeeping_timeout(SWEEPS)
    assert sweeps_action.execution_timeout > 3 * SWEEP_TIMEOUT + PRUNE_TIMEOUT


async def test_a_paused_schedule_stays_paused_and_is_not_triggered(client: Client) -> None:
    """Review #1095 1c: a restart neither resumes a Schedule an operator paused nor
    runs it."""
    schedule_id = f"housekeeping-test-{uuid.uuid4().hex[:8]}"
    queue = f"library-{uuid.uuid4().hex[:8]}"
    handle = client.get_schedule_handle(schedule_id)
    try:
        assert not await ensure_schedule(client, queue, 600.0, schedule_id=schedule_id)
        assert await _actions(client, schedule_id, 1) == 1
        # The boot's run, ended: no open run that a trigger could queue behind.
        started = (await handle.describe()).info.recent_actions[-1].action
        assert isinstance(started, ScheduleActionExecutionStartWorkflow)
        await client.get_workflow_handle(
            started.workflow_id, run_id=started.first_execution_run_id
        ).terminate("ended by the test")
        await handle.pause(note="incident")
        # Said, so the boot converges the uploads itself (review #1095 2).
        assert await ensure_schedule(client, queue, 120.0, schedule_id=schedule_id)
        actions = await _actions(client, schedule_id, 2)
        described = await handle.describe()
    finally:
        await handle.delete()
        await terminate_open_workflows(client, queue)
    assert described.schedule.state.paused
    assert described.schedule.state.note == "incident"
    assert described.schedule.spec.intervals[0].every == timedelta(seconds=120)
    assert actions == 1
