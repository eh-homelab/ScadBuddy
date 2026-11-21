"""``ProfileFetch``: one run's fetch of a type's 3DFP presets (#6625, plan Decision 13), on
the profile-fetch worker's queue. The API starts it per run (`fetch_workflow_id`).

The export activity signs in, exports each target and PUTs each file as it comes; then the
import activity asks the API to import the run. A step that cannot finish (its attempts
spent, a non-retryable failure, or no worker to take it within `EXPORT_SCHEDULE_TO_START`)
posts ``failed`` with why (a run no worker serves at all is ended by the API, which
reads the queue's pollers), and the workflow returns a ``failed`` outcome. A CRUD call that
answers that the run is not running (`NOT_RUNNING`: the API ended it, Decision 25) ends the
workflow at once with None: no retry, and no ``failed``, which would be refused too. The
worker is not versioned: a change to this workflow's commands goes behind
`workflow.patched`.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Final

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError, TimeoutError, TimeoutType

with workflow.unsafe.imports_passed_through():
    from scadbuddy.profile_fetch.wire import (
        EXPORT_ACTIVITY,
        FAILED_ACTIVITY,
        FETCH_WORKFLOW,
        IMPORT_ACTIVITY,
        MAX_FAILED_REASON,
        NOT_RUNNING,
        ExportReport,
        FetchAsk,
        ImportOutcome,
    )

#: One attempt: sign in, add the filament, and export every target (Decision 13).
EXPORT_TIMEOUT: Final = timedelta(minutes=20)
#: The activity heartbeats every `core.heartbeat.HEARTBEAT_EVERY` (10 s).
EXPORT_HEARTBEAT: Final = timedelta(minutes=2)
#: Caps how long one export attempt may wait in the queue: one worker exports one run at a
#: time, so an invoice confirm's many runs queue behind each other (Decision 16). A worker
#: that is not there is found sooner by the API, from the queue's pollers (build Decision
#: 62).
EXPORT_SCHEDULE_TO_START = timedelta(hours=6)
#: The activity marks `steps.SiteError`'s kinds that are not worth another try as
#: non-retryable (`activities.RETRYABLE_KINDS`).
EXPORT_RETRY: Final = RetryPolicy(
    initial_interval=timedelta(minutes=1), backoff_coefficient=2.0, maximum_attempts=3
)
#: The API imports into Bambuddy and reads K tables: idempotent by its key, so a lost
#: answer or a Bambuddy 5xx is tried again.
IMPORT_TIMEOUT: Final = timedelta(minutes=10)
#: How long the import activity's one request waits for the API's answer (R-F2). The route
#: does all of the import's Bambuddy calls in that request: about 12 (two listings, the
#: zip, a K table per printer and nozzle, the held copy's refresh), each up to the
#: Bambuddy client's 30 s. Under `IMPORT_TIMEOUT`, so the attempt ends by its own read.
IMPORT_REQUEST_TIMEOUT: Final = timedelta(minutes=9)
IMPORT_RETRY: Final = RetryPolicy(
    initial_interval=timedelta(seconds=10),
    maximum_interval=timedelta(minutes=2),
    maximum_attempts=5,
)
#: The workflow's execution timeout (`filament.profile_fetch.FetchStarter`): Temporal ends
#: a workflow still open this long after its start. The steps' own timeouts give no bound
#: of their own: `EXPORT_SCHEDULE_TO_START` applies to each attempt, and ``failed`` retries
#: without a limit.
EXECUTION_TIMEOUT: Final = timedelta(hours=24)
#: A run still ``running`` this long after it started has no workflow that will end it
#: (Temporal ended it at `EXECUTION_TIMEOUT`, and an hour has passed for its last post):
#: the API ends it ``failed`` (`filament.profile_fetch.end_unserved`, the last resort).
LONGEST_RUN: Final = EXECUTION_TIMEOUT + timedelta(hours=1)

#: Posting ``failed`` waits out a CRUD service that does not answer.
FAILED_TIMEOUT: Final = timedelta(minutes=2)
FAILED_RETRY: Final = RetryPolicy(
    initial_interval=timedelta(seconds=5), maximum_interval=timedelta(minutes=10)
)

#: The run's ``error`` when the API ends a run because no worker polls the queue
#: (`filament.profile_fetch.end_unserved`).
NO_WORKER: Final = "no profile-fetch worker picked the fetch up"
#: The run's ``error`` when the API ends a run at the last resort `LONGEST_RUN`: a worker may
#: still poll, so the text does not say none did (#6625 R-F10).
TOO_LONG: Final = "the fetch did not end within 25 h; start it again"
#: The ``failed`` reason when the export waited out `EXPORT_SCHEDULE_TO_START`: the worker
#: may be down, or alive and busy with an invoice's other fetches (R-F6).
NOT_STARTED_IN_TIME: Final = (
    "no profile-fetch worker started the fetch within 6 h:"
    " none is running, or it is busy with other fetches"
)


class _RunEndedError(Exception):
    """The API answered that the run is not running."""


def _ended(error: ActivityError) -> bool:
    cause = error.cause
    return isinstance(cause, ApplicationError) and cause.type == NOT_RUNNING


def failure_reason(error: ActivityError, step: str) -> str:
    """Why ``step`` (``export`` or ``import``) gave up, for the run's ``error``: the
    activity's own message (`activities.site_failure` keeps it free of the login), or
    what timed out."""
    cause = error.cause
    if isinstance(cause, TimeoutError):
        if cause.type == TimeoutType.SCHEDULE_TO_START:
            return NOT_STARTED_IN_TIME
        return f"the {step} did not finish in time"
    if isinstance(cause, ApplicationError) and cause.message:
        return cause.message[:MAX_FAILED_REASON]
    return f"the {step} could not finish"


@workflow.defn(name=FETCH_WORKFLOW)
class ProfileFetch:
    @workflow.run
    async def run(self, ask: FetchAsk) -> ImportOutcome | None:
        """The import's outcome; a ``failed`` one when a step gave up; None when the API
        had already ended the run."""
        try:
            reason = await self._fetch(ask)
        except _RunEndedError:
            workflow.logger.info("run %s is no longer running; the fetch ends", ask.run_id)
            return None
        if isinstance(reason, ImportOutcome):
            return reason
        try:
            await workflow.execute_activity(
                FAILED_ACTIVITY,
                args=[ask.run_id, reason],
                start_to_close_timeout=FAILED_TIMEOUT,
                retry_policy=FAILED_RETRY,
            )
        except ActivityError as error:
            if not _ended(error):
                raise
        return ImportOutcome(status="failed", presets=0, k_values=0, missing=[], notes=[reason])

    async def _fetch(self, ask: FetchAsk) -> ImportOutcome | str:
        """The import's outcome, or why a step gave up."""
        try:
            report: ExportReport = await workflow.execute_activity(
                EXPORT_ACTIVITY,
                ask,
                result_type=ExportReport,
                start_to_close_timeout=EXPORT_TIMEOUT,
                heartbeat_timeout=EXPORT_HEARTBEAT,
                schedule_to_start_timeout=EXPORT_SCHEDULE_TO_START,
                retry_policy=EXPORT_RETRY,
            )
        except ActivityError as error:
            if _ended(error):
                raise _RunEndedError from error
            return failure_reason(error, "export")
        try:
            outcome: ImportOutcome = await workflow.execute_activity(
                IMPORT_ACTIVITY,
                args=[ask.run_id, report],
                result_type=ImportOutcome,
                start_to_close_timeout=IMPORT_TIMEOUT,
                retry_policy=IMPORT_RETRY,
            )
        except ActivityError as error:
            if _ended(error):
                raise _RunEndedError from error
            return failure_reason(error, "import")
        return outcome
