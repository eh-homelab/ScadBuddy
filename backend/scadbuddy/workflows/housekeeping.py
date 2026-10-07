"""Housekeeping on a Temporal Schedule (#1054, spec 2026-10-01 §4.4).

The API process's periodic loops are two Schedules that start ``Housekeeping`` on the
``library`` queue: ``scadbuddy-housekeeping-<queue>`` runs every sweep every
``SCADBUDDY_ASSET_SWEEP_INTERVAL`` seconds (0: no Schedule), and
``scadbuddy-prune-<queue>`` prunes settled render jobs every `PRUNE_INTERVAL`, as the
render service's loop did, whatever that interval. The queue is served in the API
process, which holds the data volume the sweeps read. Each sweep is best effort, as the
loop's were: one that fails is logged, fails its activity (the run returns it, and
Temporal's UI shows it), the rest still run, and the next tick tries again.

The ``library`` worker is unversioned (unlike the render worker), so a run left open
across a deploy replays on the new code: ``Housekeeping.run`` must stay
replay-compatible. A change to its command sequence (a step added, the loop reordered)
goes behind ``workflow.patched``. A run that fails on replay anyway ends at its
`housekeeping_timeout`. Activities are not covered by that: a replica still on the old
build takes a sweep the new one added to `SWEEPS` and fails it as unregistered, and a
sweep is not retried, so it waits for the next tick. A release that adds a sweep is
rolled out with ``Recreate`` (README, "Uploaded files").
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleIntervalSpec,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleSpec,
    ScheduleUpdate,
    ScheduleUpdateInput,
)
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError
from temporalio.service import RPCError, RPCStatusCode
from temporalio.worker import Worker

logger = logging.getLogger(__name__)

HOUSEKEEPING_WORKFLOW = "Housekeeping"
HOUSEKEEPING_SCHEDULE = "scadbuddy-housekeeping"
PRUNE_SCHEDULE = "scadbuddy-prune"
#: The render service's prune loop's interval: a settled job outlives `job_ttl` by this.
PRUNE_INTERVAL = 300.0


def schedule_id_for(task_queue: str) -> str:
    """One Schedule per queue: a test's app (a queue of its own) never moves another's."""
    return f"{HOUSEKEEPING_SCHEDULE}-{task_queue}"


def prune_schedule_id_for(task_queue: str) -> str:
    return f"{PRUNE_SCHEDULE}-{task_queue}"


#: The shortest interval a Temporal Schedule takes ("interval is too small" below it).
MIN_INTERVAL = 1.0

#: Today's order: settled jobs first (they hold blob refs), then what they freed.
SWEEPS = (
    "housekeeping_prune_jobs",
    "housekeeping_sweep_assets",
    "housekeeping_sweep_blobs",
    "housekeeping_sweep_staging",
)
PRUNE_SWEEPS = SWEEPS[:1]
#: An asset sweep converges with the store over Bambuddy, at length.
SWEEP_TIMEOUT = timedelta(minutes=30)
#: A long sweep heartbeats: a worker lost mid-sweep is noticed within this, not after
#: `SWEEP_TIMEOUT`, so the Schedules' overlap SKIP does not hold the next ticks back.
HEARTBEAT_TIMEOUT = timedelta(minutes=1)
#: The prune is two deletes; it fits well inside the prune Schedule's interval.
PRUNE_TIMEOUT = timedelta(minutes=2)
#: What a run spends between its activities: workflow tasks, and a start on a busy worker.
RUN_MARGIN = timedelta(minutes=3)


def housekeeping_timeout(sweeps: tuple[str, ...]) -> timedelta:
    """A run's bound: each sweep's activity timeout, plus a margin. A run that can
    never finish (a workflow task that fails on replay retries forever) then ends,
    and the Schedule's overlap SKIP does not hold back every later tick."""
    return sum(
        (PRUNE_TIMEOUT if sweep in PRUNE_SWEEPS else SWEEP_TIMEOUT for sweep in sweeps),
        RUN_MARGIN,
    )


@workflow.defn(name=HOUSEKEEPING_WORKFLOW)
class Housekeeping:
    @workflow.run
    async def run(self, sweeps: list[str] | None = None) -> list[str]:
        failed: list[str] = []
        for sweep in SWEEPS if sweeps is None else sweeps:
            try:
                prune = sweep in PRUNE_SWEEPS
                await workflow.execute_activity(
                    sweep,
                    start_to_close_timeout=PRUNE_TIMEOUT if prune else SWEEP_TIMEOUT,
                    heartbeat_timeout=None if prune else HEARTBEAT_TIMEOUT,
                    # The next tick is the retry, as it was for the loop.
                    retry_policy=RetryPolicy(maximum_attempts=1),
                )
            except ActivityError:
                workflow.logger.warning("housekeeping sweep %s failed", sweep)
                failed.append(sweep)
        return failed


def library_worker(
    client: Client,
    task_queue: str,
    activities: Sequence[Callable[..., Any]],
    *,
    graceful_shutdown_timeout: timedelta = timedelta(seconds=30),
) -> Worker:
    """The ``library`` worker: ``Housekeeping`` and its sweeps. A stop gives a running
    sweep ``graceful_shutdown_timeout`` to finish (review #1095b 5): a cancelled one
    only stops waiting, its thread goes on while the lifespan closes the stores it
    uses. A sweep longer than that (an asset sweep's converge) is still cancelled."""
    return Worker(
        client,
        task_queue=task_queue,
        workflows=[Housekeeping],
        activities=activities,
        graceful_shutdown_timeout=graceful_shutdown_timeout,
    )


def _schedule(
    schedule_id: str, task_queue: str, interval: float, sweeps: tuple[str, ...]
) -> Schedule:
    return Schedule(
        action=ScheduleActionStartWorkflow(
            HOUSEKEEPING_WORKFLOW,
            list(sweeps),
            id=schedule_id,
            task_queue=task_queue,
            execution_timeout=housekeeping_timeout(sweeps),
        ),
        spec=ScheduleSpec(
            intervals=[ScheduleIntervalSpec(every=timedelta(seconds=max(interval, MIN_INTERVAL)))]
        ),
        policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP),
    )


async def ensure_schedule(
    client: Client,
    task_queue: str,
    interval: float,
    *,
    schedule_id: str | None = None,
    sweeps: tuple[str, ...] = SWEEPS,
) -> bool:
    """The Schedule at ``interval`` seconds (0: none), then one run now: the boot's
    converging sweep. A Schedule an operator paused stays paused, and is not run:
    True says so (review #1095 2), since then nothing converges until it resumes."""
    schedule_id = schedule_id or schedule_id_for(task_queue)
    handle = client.get_schedule_handle(schedule_id)
    if interval <= 0:
        try:
            await handle.delete()
        except RPCError as error:
            if error.status != RPCStatusCode.NOT_FOUND:
                raise
        return False
    schedule = _schedule(schedule_id, task_queue, interval, sweeps)
    try:
        await client.create_schedule(schedule_id, schedule)
    except ScheduleAlreadyRunningError:

        def replace(input: ScheduleUpdateInput) -> ScheduleUpdate:
            # The state (paused, its note) is the operator's, not the deploy's.
            schedule.state = input.description.schedule.state
            return ScheduleUpdate(schedule=schedule)

        await handle.update(replace)
    if not schedule.state.paused:
        # A run still open (a rollout stopped the old pod mid-sweep) would SKIP this
        # one; it queues behind that run instead.
        await handle.trigger(overlap=ScheduleOverlapPolicy.BUFFER_ONE)
    return schedule.state.paused


async def ensure_schedules(client: Client, task_queue: str, interval: float) -> bool:
    """Both Schedules: the prune's fixed one, and every sweep at ``interval``. True
    when an operator has paused the sweeps' one."""
    await ensure_schedule(
        client,
        task_queue,
        PRUNE_INTERVAL,
        schedule_id=prune_schedule_id_for(task_queue),
        sweeps=PRUNE_SWEEPS,
    )
    return await ensure_schedule(client, task_queue, interval)
