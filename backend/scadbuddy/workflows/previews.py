"""The default-render preview backfill on a Temporal Schedule (#1054, spec 2026-10-01 §4.4).

What the boot's pass over every model (`PreviewScheduler.request_all`) did is
``PreviewBackfill`` on the ``library`` queue, served in the API process, which holds
the data volume the listing reads. The Schedule ``scadbuddy-previews-<queue>`` starts
it every `BACKFILL_INTERVAL` and once at each boot; with previews off there is no
Schedule. A run lists the models whose preview is missing or stale (``previews_due``)
and refreshes them one at a time (``preview_refresh``: `PreviewScheduler.refresh`,
whose render is a ``RenderPreview`` on the render queue), pausing after each render as
the scheduler did. After the first pass a run renders nothing: a current preview is
skipped. The per-change requests stay the scheduler's own, in-process.

The ``library`` worker is unversioned, so a run left open across a deploy replays on
the new code: ``PreviewBackfill.run`` must stay replay-compatible. A change to its
command sequence goes behind ``workflow.patched``. A run that fails on replay anyway
ends at `BACKFILL_TIMEOUT`.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.client import Client, ScheduleActionStartWorkflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

from scadbuddy.workflows.housekeeping import HEARTBEAT_TIMEOUT, ensure_workflow_schedule

PREVIEW_BACKFILL_WORKFLOW = "PreviewBackfill"
PREVIEW_SCHEDULE = "scadbuddy-previews"
DUE_ACTIVITY = "previews_due"
REFRESH_ACTIVITY = "preview_refresh"
#: Every hour, and at each boot: after the first pass a tick is one listing, and it
#: retries what a render that could not be run (not one that failed) left behind.
BACKFILL_INTERVAL = 3600.0
#: The scheduler's pause after each render, so a backlog never runs back to back.
PAUSE = timedelta(seconds=1)
#: Refreshes per run before the rest go on in a new run, so a first boot over hundreds
#: of models does not grow one history without bound.
BATCH = 100
#: A listing of the catalogue and a plan per model: reads only.
DUE_TIMEOUT = timedelta(minutes=5)
#: A refresh waits for the preview lock, then a render bounded by 3 x `render_timeout`
#: (plus the snapshot's transfer on the Bambuddy store). It heartbeats while it waits.
REFRESH_TIMEOUT = timedelta(hours=1)
#: The whole continue-as-new chain: a run that can never finish ends, and the
#: Schedule's overlap SKIP stops holding later ticks back. A longer backlog resumes at
#: the next tick, since what was rendered is skipped.
BACKFILL_TIMEOUT = timedelta(hours=12)


def preview_schedule_id_for(task_queue: str) -> str:
    """One Schedule per queue: a test's app (a queue of its own) never moves another's."""
    return f"{PREVIEW_SCHEDULE}-{task_queue}"


@workflow.defn(name=PREVIEW_BACKFILL_WORKFLOW)
class PreviewBackfill:
    @workflow.run
    async def run(self, slugs: list[str] | None = None) -> list[str]:
        """Refresh the preview of each model due one (``slugs``, or the listing's when
        None); the slugs whose refresh failed in this run."""
        if slugs is None:
            slugs = await workflow.execute_activity(
                DUE_ACTIVITY,
                result_type=list[str],
                start_to_close_timeout=DUE_TIMEOUT,
                # The next tick is the retry, as it is for the sweeps.
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
        failed: list[str] = []
        for index, slug in enumerate(slugs):
            if index >= BATCH or workflow.info().is_continue_as_new_suggested():
                workflow.continue_as_new(args=[slugs[index:]])
            try:
                rendered = await workflow.execute_activity(
                    REFRESH_ACTIVITY,
                    slug,
                    result_type=bool,
                    start_to_close_timeout=REFRESH_TIMEOUT,
                    heartbeat_timeout=HEARTBEAT_TIMEOUT,
                    retry_policy=RetryPolicy(maximum_attempts=1),
                )
            except ActivityError:
                workflow.logger.warning("could not refresh the preview of %s", slug)
                failed.append(slug)
                continue
            if rendered:
                await workflow.sleep(PAUSE)
        return failed


async def ensure_preview_schedule(client: Client, task_queue: str, enabled: bool) -> None:
    """The backfill's Schedule, then one run now (the boot's pass); none with previews
    off. A Schedule an operator paused stays paused, and is not run."""
    schedule_id = preview_schedule_id_for(task_queue)
    action = ScheduleActionStartWorkflow(
        PREVIEW_BACKFILL_WORKFLOW,
        id=schedule_id,
        task_queue=task_queue,
        execution_timeout=BACKFILL_TIMEOUT,
    )
    await ensure_workflow_schedule(
        client, schedule_id, BACKFILL_INTERVAL if enabled else 0.0, action
    )
