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
        CREATE_ACTIVITY,
        CREATE_WORKFLOW,
        EXPORT_ACTIVITY,
        FAILED_ACTIVITY,
        FETCH_WORKFLOW,
        IMPORT_ACTIVITY,
        MAX_FAILED_REASON,
        NOT_RUNNING,
        SEARCH_ACTIVITY,
        SEARCH_WORKFLOW,
        CreateAnswer,
        CreateAsk,
        ExportReport,
        FetchAsk,
        ImportOutcome,
        SearchAnswer,
        SearchAsk,
        TdfpOutcome,
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


# -- #7025: search 3DFP, and add a filament it lacks ----------------------------------------

#: A search is one page; a create one form. Both wait in the queue behind an export the
#: worker runs (one activity at a time, Decision 16), for at most these.
SEARCH_SCHEDULE_TO_START: Final = timedelta(minutes=1)
SEARCH_TIMEOUT: Final = timedelta(minutes=3)
#: One attempt (R-7025-10): a search is a read, so the caller sends it again; a second
#: attempt would double how long the route waits.
SEARCH_RETRY: Final = RetryPolicy(maximum_attempts=1)
CREATE_SCHEDULE_TO_START: Final = timedelta(minutes=2)
CREATE_TIMEOUT: Final = timedelta(minutes=5)
#: Never retried: a create that failed after "Save Filament" may have made the filament
#: (R-7025-7).
CREATE_RETRY: Final = RetryPolicy(maximum_attempts=1)
TDFP_HEARTBEAT: Final = timedelta(minutes=2)
#: A search's or create's workflow ends this long after its start whatever its steps do.
TDFP_EXECUTION_TIMEOUT: Final = timedelta(minutes=15)
#: What the route adds to `step_wait` for Temporal's own turns.
WAIT_MARGIN: Final = timedelta(seconds=30)
#: The kind of an outcome whose step no worker took in time.
NOT_STARTED: Final = "not_started"
#: The site's failure kinds of a create that the driver raises only before it clicks "Save
#: Filament" (`site.ThreeDfpSite.create`: a failure after the click is ``not_created``, and
#: so is a lost answer of the browser service, `remote.RemoteSite.create`). A create that
#: failed so made nothing: its workflow fails, so the same request key may run it again
#: (R-7025-11). Any other failure, and a Temporal timeout of the step, may come after the
#: click: its outcome is kept under the key, and its reason says the filament may exist.
BEFORE_SAVE: Final = frozenset(
    {
        NOT_STARTED,
        "option_missing",
        "no_defaults",
        "login_failed",
        "bot_check",
        "timeout",
        "browser",
    }
)
MAY_EXIST: Final = "the filament may exist on 3dfilamentprofiles.com: search before another create"


def step_wait(schedule_to_start: timedelta, timeout: timedelta, retry: RetryPolicy) -> float:
    """The seconds a caller waits for a step's outcome: every attempt's wait in the queue and
    run, the back-off between attempts, and `WAIT_MARGIN` (R-7025-10)."""
    attempts = max(retry.maximum_attempts, 1)
    backoff = sum(
        (
            min(
                retry.initial_interval * retry.backoff_coefficient**n,
                retry.maximum_interval or timedelta.max,
            )
            for n in range(attempts - 1)
        ),
        timedelta(),
    )
    return (attempts * (schedule_to_start + timeout) + backoff + WAIT_MARGIN).total_seconds()


def tdfp_failure(error: ActivityError, step: str) -> tuple[str, str]:
    """The kind and the reason of a search's or create's failed step: the activity's own
    (`activities.site_failure`), or what timed out. A Temporal timeout of the step other
    than its wait in the queue is ``step_timeout``: it may cut a create after its save."""
    cause = error.cause
    if isinstance(cause, TimeoutError):
        if cause.type == TimeoutType.SCHEDULE_TO_START:
            return NOT_STARTED, (
                f"no profile-fetch worker took the {step} in time: none is running, or it is"
                " busy with a preset fetch"
            )
        return "step_timeout", f"the {step} did not finish in time"
    if isinstance(cause, ApplicationError):
        return cause.type or "browser", (cause.message or f"the {step} failed")[:MAX_FAILED_REASON]
    return "step_failed", f"the {step} failed"


@workflow.defn(name=SEARCH_WORKFLOW)
class TdfpSearch:
    """One search of 3DFP (`activities.TdfpActivities.search`); the API waits for it."""

    @workflow.run
    async def run(self, ask: SearchAsk) -> TdfpOutcome:
        try:
            answer: SearchAnswer = await workflow.execute_activity(
                SEARCH_ACTIVITY,
                ask,
                result_type=SearchAnswer,
                schedule_to_start_timeout=SEARCH_SCHEDULE_TO_START,
                start_to_close_timeout=SEARCH_TIMEOUT,
                heartbeat_timeout=TDFP_HEARTBEAT,
                retry_policy=SEARCH_RETRY,
            )
        except ActivityError as error:
            kind, reason = tdfp_failure(error, "search")
            return TdfpOutcome(kind=kind, reason=reason)
        return TdfpOutcome(search=answer)


@workflow.defn(name=CREATE_WORKFLOW)
class TdfpCreate:
    """One create of a filament on 3DFP (`activities.TdfpActivities.create`), tried once;
    one workflow per request key, type and ask (`filament.tdfp.create_workflow_id`). A
    failure before "Save Filament" fails the workflow (`BEFORE_SAVE`), so the key may run
    again; any other outcome completes it and is kept under the key."""

    @workflow.run
    async def run(self, ask: CreateAsk) -> TdfpOutcome:
        try:
            answer: CreateAnswer = await workflow.execute_activity(
                CREATE_ACTIVITY,
                ask,
                result_type=CreateAnswer,
                schedule_to_start_timeout=CREATE_SCHEDULE_TO_START,
                start_to_close_timeout=CREATE_TIMEOUT,
                heartbeat_timeout=TDFP_HEARTBEAT,
                retry_policy=CREATE_RETRY,
            )
        except ActivityError as error:
            kind, reason = tdfp_failure(error, "create")
            if kind in BEFORE_SAVE:
                raise ApplicationError(reason, type=kind, non_retryable=True) from None
            if kind != "not_created":
                reason = f"{reason}; {MAY_EXIST}"[:MAX_FAILED_REASON]
            return TdfpOutcome(kind=kind, reason=reason)
        return TdfpOutcome(create=answer)
