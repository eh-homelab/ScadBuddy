"""One media import per order attempt (#7233, spec 2026-11-05 §4.1, §4.6).

``AttemptMedia`` runs on the ``library`` queue (the API's worker), one execution per
attempt, id ``attempt-media:<fulfilment id>``. Three triggers start it
(`orders/attempt_media.py` `TemporalMediaStarter`): a hand attempt that names an archive,
a heard print outcome, and a read of the attempt's archives. A start joins a run that is
still going, and starts again once a run ended.

Each step is one activity, ``attempt_media_step`` (`orders/archives.py`
`mirror_attempt`): it copies the archives' facts, imports the finish photo and the
timelapse the attempt does not have yet, and answers whether a medium is still missing.
Bambuddy writes the finish photo seconds after the print completes and the timelapse
minutes after, so a run started with ``retries`` steps at its start, at +2 min and at
+15 min, and ends after the first step that answers nothing missing. A run without
``retries`` (the read's) steps once.

On the versioned ``library`` worker it is AUTO_UPGRADE (`VersionedAttemptMedia`): a run
waits 15 minutes, so a deploy can move it to a new build. A change to the commands
``run`` issues goes behind ``workflow.patched``.
"""

from __future__ import annotations

from datetime import timedelta

from pydantic import BaseModel
from temporalio import workflow
from temporalio.common import RetryPolicy, VersioningBehavior
from temporalio.exceptions import ActivityError

ATTEMPT_MEDIA_WORKFLOW = "AttemptMedia"
STEP_ACTIVITY = "attempt_media_step"
#: The workflow id's prefix: one import per attempt.
WORKFLOW_PREFIX = "attempt-media:"
#: When each step runs, from the run's start, when the run has ``retries``.
STEP_AFTER = (timedelta(0), timedelta(minutes=2), timedelta(minutes=15))
#: A step can download a timelapse of up to the upload limit (1 GiB by default).
STEP_TIMEOUT = timedelta(minutes=30)
#: The step heartbeats while it downloads; a worker lost mid-step is noticed in this time.
STEP_HEARTBEAT = timedelta(minutes=2)
#: The first try and three retries. A step logs its own failures and answers; a retry
#: covers only a lost worker or a timeout.
STEP_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=10),
    backoff_coefficient=2.0,
    maximum_attempts=4,
)


class AttemptMediaInput(BaseModel):
    fulfilment_id: str
    #: Step again at +2 and +15 min while a medium is missing; else step once.
    retries: bool


class AttemptMediaStep(BaseModel):
    """What one step found: whether the attempt still lacks a medium that its archives
    can give (a finish photo or a timelapse of an archive), or has no archive read yet."""

    missing: bool


def workflow_id(fulfilment_id: str) -> str:
    return f"{WORKFLOW_PREFIX}{fulfilment_id}"


@workflow.defn(name=ATTEMPT_MEDIA_WORKFLOW)
class AttemptMedia:
    @workflow.run
    async def run(self, start: AttemptMediaInput) -> str:
        """``done`` when nothing is missing, else ``missing`` after the last step."""
        began = workflow.now()
        steps = STEP_AFTER if start.retries else STEP_AFTER[:1]
        for after in steps:
            wait = began + after - workflow.now()
            if wait > timedelta(0):
                await workflow.sleep(wait)
            try:
                found: AttemptMediaStep = await workflow.execute_activity(
                    STEP_ACTIVITY,
                    start.fulfilment_id,
                    result_type=AttemptMediaStep,
                    start_to_close_timeout=STEP_TIMEOUT,
                    heartbeat_timeout=STEP_HEARTBEAT,
                    retry_policy=STEP_RETRY,
                )
            except ActivityError:
                # Out of tries: the next step, or the next trigger, tries again.
                continue
            if not found.missing:
                return "done"
        return "missing"


@workflow.defn(name=ATTEMPT_MEDIA_WORKFLOW, versioning_behavior=VersioningBehavior.AUTO_UPGRADE)
class VersionedAttemptMedia(AttemptMedia):
    """``AttemptMedia`` on the versioned ``library`` worker (#4090). AUTO_UPGRADE: a run
    waits 15 minutes, so a deploy moves it to the new build. Its own class because an
    unversioned worker cannot complete a workflow that declares a behavior."""

    @workflow.run
    async def run(self, start: AttemptMediaInput) -> str:
        return await super().run(start)
