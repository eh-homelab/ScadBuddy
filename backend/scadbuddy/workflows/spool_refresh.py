"""The spool providers' refresh on a Temporal Schedule (#3818 tier A, plan 2026-10-18
D1, Task 6b).

``SpoolRefresh`` runs on the ``library`` queue, served in the API process, every
``spool_overlay_refresh_seconds`` (the Schedule ``scadbuddy-spool-providers-<queue>``,
overlap SKIP, and once at each boot; 0 deletes it). A run is one activity
(`spools/activities.py` ``spool_providers_refresh``): each configured provider's
records into Postgres, then the printers' trays. It is never started, signalled or
awaited by the print dialog, which reads only what it wrote: the hot path never waits
on Bambuddy. A refresh that fails is not retried within its run; the next tick is the
retry, as it is for the sweeps.

On the versioned ``library`` worker (#4090) it is AUTO_UPGRADE (`VersionedSpoolRefresh`),
so a run left open across a deploy replays on the new code: a change to the commands
``SpoolRefresh.run`` issues goes behind ``workflow.patched``
(``tests/test_spool_refresh_replay.py``).
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.client import Client, ScheduleActionStartWorkflow
from temporalio.common import RetryPolicy, VersioningBehavior

from scadbuddy.workflows.housekeeping import ensure_workflow_schedule

SPOOL_REFRESH_WORKFLOW = "SpoolRefresh"
SPOOL_SCHEDULE = "scadbuddy-spool-providers"
REFRESH_ACTIVITY = "spool_providers_refresh"
#: Each provider's read is bounded at a minute (`activities.PROVIDER_TIMEOUT`); a few
#: providers, then the writes.
REFRESH_TIMEOUT = timedelta(minutes=5)
#: A run that can never finish ends, and the Schedule's overlap SKIP stops holding
#: later ticks back.
RUN_TIMEOUT = timedelta(minutes=15)


def spool_schedule_id_for(task_queue: str) -> str:
    """One Schedule per queue: a test's app (a queue of its own) never moves another's."""
    return f"{SPOOL_SCHEDULE}-{task_queue}"


@workflow.defn(name=SPOOL_REFRESH_WORKFLOW)
class SpoolRefresh:
    @workflow.run
    async def run(self) -> dict[str, str]:
        """What came of each provider: ``refreshed``, ``failed`` or ``skipped``."""
        outcome: dict[str, str] = await workflow.execute_activity(
            REFRESH_ACTIVITY,
            result_type=dict[str, str],
            start_to_close_timeout=REFRESH_TIMEOUT,
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
        return outcome


@workflow.defn(name=SPOOL_REFRESH_WORKFLOW, versioning_behavior=VersioningBehavior.AUTO_UPGRADE)
class VersionedSpoolRefresh(SpoolRefresh):
    """``SpoolRefresh`` on the versioned ``library`` worker (#4090). AUTO_UPGRADE: a run
    is minutes long, but one a rollout's old replica held must finish on the new one.
    Its own class because an unversioned worker cannot complete a workflow that
    declares a behavior."""

    @workflow.run
    async def run(self) -> dict[str, str]:
        return await super().run()


async def ensure_spool_schedule(client: Client, task_queue: str, interval: float) -> None:
    """The refresh's Schedule every ``interval`` seconds, then one run now; none at 0.
    A Schedule an operator paused stays paused, and is not run."""
    schedule_id = spool_schedule_id_for(task_queue)
    action = ScheduleActionStartWorkflow(
        SPOOL_REFRESH_WORKFLOW,
        id=schedule_id,
        task_queue=task_queue,
        execution_timeout=RUN_TIMEOUT,
    )
    await ensure_workflow_schedule(client, schedule_id, interval, action)
