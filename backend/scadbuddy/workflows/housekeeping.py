"""Housekeeping on a Temporal Schedule (#1054, spec 2026-10-01 §4.4).

The API process's periodic loops (the asset sweeper and the render prune) are one
Schedule, ``scadbuddy-housekeeping-<queue>``, that starts ``Housekeeping`` on the ``library``
queue every ``SCADBUDDY_ASSET_SWEEP_INTERVAL`` seconds. The queue is served in the API
process, which holds the data volume the sweeps read. Each sweep is best effort, as the
loop's were: one that fails is logged and the rest still run, and the next tick tries
again.
"""

from __future__ import annotations

import logging
from datetime import timedelta

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

logger = logging.getLogger(__name__)

HOUSEKEEPING_WORKFLOW = "Housekeeping"
HOUSEKEEPING_SCHEDULE = "scadbuddy-housekeeping"


def schedule_id_for(task_queue: str) -> str:
    """One Schedule per queue: a test's app (a queue of its own) never moves another's."""
    return f"{HOUSEKEEPING_SCHEDULE}-{task_queue}"


#: Today's order: settled jobs first (they hold blob refs), then what they freed.
SWEEPS = (
    "housekeeping_prune_jobs",
    "housekeeping_sweep_assets",
    "housekeeping_sweep_blobs",
    "housekeeping_sweep_staging",
)
#: An asset sweep converges with the store over Bambuddy, at length.
SWEEP_TIMEOUT = timedelta(minutes=30)


@workflow.defn(name=HOUSEKEEPING_WORKFLOW)
class Housekeeping:
    @workflow.run
    async def run(self) -> list[str]:
        failed: list[str] = []
        for sweep in SWEEPS:
            try:
                await workflow.execute_activity(
                    sweep,
                    start_to_close_timeout=SWEEP_TIMEOUT,
                    # The next tick is the retry, as it was for the loop.
                    retry_policy=RetryPolicy(maximum_attempts=1),
                )
            except ActivityError:
                workflow.logger.warning("housekeeping sweep %s failed", sweep)
                failed.append(sweep)
        return failed


def _schedule(task_queue: str, interval: float) -> Schedule:
    return Schedule(
        action=ScheduleActionStartWorkflow(
            HOUSEKEEPING_WORKFLOW, id=f"housekeeping-{task_queue}", task_queue=task_queue
        ),
        spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=timedelta(seconds=interval))]),
        policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP),
    )


async def ensure_schedule(
    client: Client,
    task_queue: str,
    interval: float,
    *,
    schedule_id: str | None = None,
) -> None:
    """The Schedule at ``interval`` seconds (0: none), then one run now: the boot's
    converging sweep."""
    schedule_id = schedule_id or schedule_id_for(task_queue)
    handle = client.get_schedule_handle(schedule_id)
    if interval <= 0:
        try:
            await handle.delete()
        except RPCError as error:
            if error.status != RPCStatusCode.NOT_FOUND:
                raise
        return
    schedule = _schedule(task_queue, interval)
    try:
        await client.create_schedule(schedule_id, schedule)
    except ScheduleAlreadyRunningError:

        def replace(_: ScheduleUpdateInput) -> ScheduleUpdate:
            return ScheduleUpdate(schedule=schedule)

        await handle.update(replace)
    await handle.trigger()
