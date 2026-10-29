"""``PrintRun``: the print dialog's run as a Temporal workflow (#1052, spec 2026-10-01
§5.3), the command shape of §4.2 that every later command copies.

1. ``print_check`` makes today's cheap refusals; ``print_insert`` then writes the run,
   retried on its own so a refusal never follows a committed row. A refusal answers the
   ``accepted`` Update and *fails* the execution: no record, so a retry may start.
2. From the record on, every outcome *completes* the execution and is recorded: one
   ``try`` holds the rest, and whatever an activity raised becomes ``print_fail``.
3. A run that succeeded or may have queued stays open for its repeat window, so a
   repeat (``USE_EXISTING``) gets the same row (§5.2); a cancelled one closes at once.

``print_slice_start`` and ``print_enqueue`` each start something Bambuddy does not
dedupe, so they run once (``maximum_attempts = 1``). A slice that left nothing behind
(``SLICE_LOST``) is the one exception: the workflow starts the plate's slice again, a
few times, since a second slice costs only slicer time and the plate is still queued
once (#2081). Only pure database writes retry
without limit; ``print_record`` also touches the data volume and Bambuddy, so it gives
up and the run is recorded as failed. ``print_finish`` remembers the project's printer
best effort and, like ``print_succeed``, retries without limit, so once every plate is
queued the run never ends ``failed``.

``print_plan`` uploads the 3MF and retries (``READ_RETRY``): ``ensure_uploaded`` reuses
the copy ScadBuddy recorded, and a file an attempt uploaded but died before recording is
found in the folder by its bytes and taken (``upload_copy``, #1127), so a retry leaves
no spare library file. The workflow's commands are unchanged.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import timedelta
from typing import Any, Literal

from temporalio import workflow
from temporalio.common import RetryPolicy, SearchAttributeKey, SearchAttributeUpdate
from temporalio.exceptions import (
    ActivityError,
    ApplicationError,
    FailureError,
    RetryState,
    TimeoutType,
    WorkflowAlreadyStartedError,
    is_cancelled_exception,
)
from temporalio.exceptions import TimeoutError as TemporalTimeoutError

with workflow.unsafe.imports_passed_through():
    from scadbuddy.bambuddy.dispatch import QueueOutcome, SliceStarted
    from scadbuddy.bambuddy.print_run import PlannedRun, PlatePlan, PrintRunResult, QueuedPlate
    from scadbuddy.bambuddy.runs import PrintRun, PrintRunError
    from scadbuddy.bambuddy.subject import PrintSubject
    from scadbuddy.library.outputs import PlateSend
    from scadbuddy.printers.lan_models import LanStarted, LanStopped
    from scadbuddy.workflows.follow import FOLLOW_WORKFLOW, POKE_SIGNAL, follow_id
    from scadbuddy.workflows.print_models import (
        ACCEPTED_UPDATE,
        FAILED,
        LAN_PRINT_ACTIVITY,
        LAN_PROBE_TIMEOUT,
        LAN_SEQUENCE_WORKFLOW,
        LAN_START_WAIT,
        LAN_UNSURE,
        LAN_UPLOAD_ACTIVITY,
        LAN_WAIT_ACTIVITY,
        PRINT_LAN_UPDATE_ACTIVITY,
        PRINT_RUN_WORKFLOW,
        REFUSED,
        SLICE_GONE,
        SLICE_LOST,
        SLICE_RUN_ACTIVITY,
        SLICED_UPLOAD_ACTIVITY,
        AcceptAnswer,
        Accepted,
        Checked,
        CheckInput,
        EnqueueInput,
        FailInput,
        FinishInput,
        InsertInput,
        LanPrintInput,
        LanSequenceInput,
        LanStart,
        LanUpdateInput,
        LanUploaded,
        LanUploadInput,
        LanWaitInput,
        NativeSliced,
        PlanInput,
        PrintRunInput,
        RecordInput,
        SlicedSource,
        SlicedUploadInput,
        SliceRunInput,
        SliceStartInput,
        SucceedInput,
        lan_sequence_id,
    )
    from scadbuddy.workflows.problems import problem_of
    from scadbuddy.workflows.slicing import SLICE_RETRY, SLICE_ROUND, SLICE_TIMEOUT

#: The reads and the insert: a Bambuddy blip is retried, a refusal is not.
READ_RETRY = RetryPolicy(
    maximum_attempts=3, initial_interval=timedelta(seconds=1), backoff_coefficient=2.0
)
#: What starts something Bambuddy does not dedupe runs once (§5.3).
ONCE = RetryPolicy(maximum_attempts=1)
#: Writes that touch more than Postgres: retried a while, then the run fails (§4.2).
BOUNDED_RETRY = RetryPolicy(
    maximum_attempts=5, initial_interval=timedelta(seconds=1), backoff_coefficient=2.0
)
#: The record's own writes: a Postgres blip must not lose an outcome.
RECORD_RETRY = RetryPolicy(
    maximum_attempts=0,
    initial_interval=timedelta(seconds=1),
    maximum_interval=timedelta(seconds=30),
    backoff_coefficient=2.0,
)
ACCEPT_TIMEOUT = timedelta(seconds=60)
#: How long a client keeps re-sending a print answered ``command-still-accepting``:
#: ``frontend`` ``printRunPoll.acceptingMs`` and ``agent`` ``ACCEPTING_MS`` are this, in
#: ms, and their tests say so. Past ``print_check``'s worst case (three attempts of
#: ``ACCEPT_TIMEOUT`` and the backoff between them), so a client rarely gives up on a run
#: that will still be accepted (review #1061); one whose check ends past it is refused
#: before its record, so none is (review #1061 (2) 2).
CLIENT_ACCEPTING = timedelta(seconds=240)
#: The upload is one 3MF (``DEFAULT_UPLOAD_TIMEOUT``, 180 s) plus the plates' reads.
PLAN_TIMEOUT = timedelta(minutes=5)
SHORT = timedelta(seconds=60)
#: ``DEFAULT_SLICE_TIMEOUT`` (600 s) and a margin; polled inside the activity.
SLICE_WAIT_TIMEOUT = timedelta(seconds=660)
#: How many times a plate is sliced when its slices leave nothing behind (#2081), and
#: the wait before the next, times the attempt: a slicer pod's Recreate rollout or
#: restart takes about one to two minutes.
SLICE_ATTEMPTS = 3
SLICE_BACKOFF = timedelta(seconds=60)
#: The upload and the slice wait beat while they run, so a worker that died is noticed
#: within this rather than at the activity's whole budget.
HEARTBEAT = timedelta(seconds=30)
#: One ``slice_run`` attempt: every attempt the shared `SliceWorkflow` makes, and a
#: margin for its prepare and record. It beats, so a worker that died is retried at
#: `HEARTBEAT`; an attempt that ran out joins the same slice again.
SLICE_RUN_TIMEOUT = SLICE_RETRY.maximum_attempts * SLICE_TIMEOUT + timedelta(minutes=10)
#: Every ``slice_run`` attempt together (Task 13 review M2; the Task 14/15 ruling): one
#: round of the slice and a margin, about 4 h 25 min. Past it the run fails and says to
#: print again (`outlived_slice`), which joins the slice if it still runs: an open run
#: gains nothing by waiting longer.
SLICE_RUN_BOUND = SLICE_ROUND + timedelta(minutes=10)
#: Rejoining is idempotent, so every retryable failure retries until `SLICE_RUN_BOUND`.
#: No type is listed as non-retryable (Task 14/15 review I1): a listed type is never
#: retried whatever its own flag says, and a transient Bambuddy error is a retryable
#: ``FAILED``; every deliberate refusal sets ``non_retryable`` itself.
SLICE_RUN_RETRY = RetryPolicy(
    maximum_attempts=0,
    initial_interval=timedelta(seconds=1),
    maximum_interval=timedelta(minutes=1),
    backoff_coefficient=2.0,
)


def outlived_slice(error: ActivityError) -> ApplicationError | None:
    """The run's failure for a ``slice_run`` that ran out of `SLICE_RUN_BOUND` (Task 13
    review M2): a named 504, since printing again joins the slice if it still runs; None
    for any other failure. Out of it is the schedule-to-close timeout, or an attempt's
    own timeout whose retry would have landed past it (``retry_state`` ``TIMEOUT``,
    Task 14/15 review M1)."""
    cause = error.cause
    if not isinstance(cause, TemporalTimeoutError):
        return None
    if cause.type != TimeoutType.SCHEDULE_TO_CLOSE and error.retry_state != RetryState.TIMEOUT:
        return None
    detail = "The slice is still running after the run's wait: print again to wait for it."
    return ApplicationError(
        detail,
        PrintRunError(status=504, title="The slice is taking too long", detail=detail),
        type=FAILED,
        non_retryable=True,
    )


#: Why a cancelled LAN run started none of its later plate copies (#4541).
_LAN_CANCELLED = LanStopped(
    stage="cancelled",
    message="The print was cancelled, so the plates still to print were not started.",
)


#: Where a LAN run's later copies stopped: waiting for a print, starting the next
#: copy, or recording a plate it started.
LanStep = Literal["wait", "start", "record"]


def _lan_failed(error: BaseException, step: LanStep, plate_id: int, next_id: int) -> LanStopped:
    """Why a LAN run started none of its later plate copies (#4541): the step that failed
    on ``plate_id``, in its own words, and ``next_id`` the first plate it did not start."""
    detail = problem_of(error).detail
    if (
        step == "wait"
        and isinstance(error, ActivityError)
        and isinstance(error.cause, TemporalTimeoutError)
    ):
        detail = (
            f"The printer did not finish the print before plate {next_id} within"
            f" {LAN_WAIT_BOUND.days} days, so it and the plates after it were not started."
        )
        return LanStopped(stage="failed", message=detail)
    if step == "record":
        message = (
            f"Plate {plate_id} is printing, but it could not be recorded, so plate"
            f" {next_id} and the plates after it were not started: {detail}"
        )
    else:
        message = f"Plate {next_id} was not started, nor the plates after it: {detail}"
    return LanStopped(stage="failed", message=message)


KIND = SearchAttributeKey.for_keyword("ScadbuddyKind")
SUBJECT = SearchAttributeKey.for_keyword("ScadbuddySubject")
STATUS = SearchAttributeKey.for_keyword("ScadbuddyStatus")
MAY_HAVE_QUEUED = SearchAttributeKey.for_bool("ScadbuddyMayHaveQueued")


#: What a run cancelled before it printed answers, before or during its record: nothing
#: was queued.
CANCELLED = PrintRunError(
    status=409,
    title="Conflict",
    detail="This print was cancelled before it started. Nothing was queued; print again.",
)
#: What a run cancelled after its record and before its first ``POST /queue/`` records:
#: Bambuddy may have sliced a plate, but nothing was queued (review #1316 (9) 2a).
CANCELLED_UNQUEUED = PrintRunError(
    status=409,
    title="Conflict",
    detail="This print was cancelled before it was queued. Bambuddy may have sliced it,"
    " but nothing was queued; print again.",
)
#: What a run cancelled after it began queueing records: a plate may be queued.
CANCELLED_QUEUEING = PrintRunError(
    status=409,
    title="Conflict",
    detail="This print was cancelled while it was being queued, so it may be queued; check"
    " Bambuddy's queue before printing again.",
)
#: What a run answers when its check ends after every client stopped waiting for it.
UNWAITED = PrintRunError(
    status=409,
    title="Conflict",
    detail="Nobody was waiting for this print any more, so it was not started. Nothing was"
    " queued; print again.",
)


#: ``workflow.patched`` id for review #1316's cancel handling: from the last plate's
#: ``print_enqueue`` on, a cancel waits for the last activities, and a cancel the run
#: absorbed ends its repeat window. A history from before it (#1061) replays with
#: neither.
CANCEL_PATCH = "print-cancel-ends-window"
#: ``workflow.patched`` id for review #1316 (3): ``print_start_enqueue`` and every
#: plate's ``print_enqueue`` and ``print_record`` wait through a cancel, so what was
#: queued is recorded, and the run stops before the next ``POST /queue/``. A history from
#: before it cancels them, as ``CANCEL_PATCH`` alone did.
PLATES_PATCH = "print-cancel-records-every-plate"
#: ``workflow.patched`` id for review #1316 (13) 3a: ``print_fail`` is a record, and
#: waits through a cancel as the others do, so the run ends with the error it records
#: and its execution completes. A history from before it cancels ``print_fail`` and ends
#: the execution cancelled (``cancelled_during_fail_1061``).
FAIL_PATCH = "print-cancel-waits-for-fail"
#: ``workflow.patched`` id of the follow after a run succeeds (#1053, §4.4).
FOLLOW_PATCH = "follow-print"
#: ``workflow.patched`` id of a library file's follow (#1073): a library print is followed
#: and settled as an output's is. A library run from before it replays unfollowed.
LIBRARY_FOLLOW_PATCH = "follow-library-print"
#: ``workflow.patched`` id of slicing a plate again when its slice was lost (#2081). A
#: history from before it recorded the run failed at the first lost slice.
SLICE_RETRY_PATCH = "print-slice-again-when-lost"
#: ``workflow.patched`` id of a plate queued with no slice (#2671): a sliced library file,
#: or ScadBuddy's own earlier slice of the copy. Only such a plate asks, and no history
#: from before it has one.
SLICED_PATCH = "print-sliced-file"
#: ``workflow.patched`` id of slicing a plate again when the earlier slice it reused is
#: gone from Bambuddy as it is queued (#2955). Only a plate that reused a slice and was
#: refused for it asks; a history from before it recorded the run failed.
REUSED_GONE_PATCH = "print-slice-again-when-reused-gone"
#: ``workflow.patched`` id of a plate sliced on the native slicer (plan 2026-10-16 Task
#: 13): ``slice_run`` and ``print_sliced_upload`` on the ``library`` queue in place of
#: ``print_slice_start`` and ``print_slice_wait``. Only a run planned native asks, so
#: no Bambuddy run's history records it.
NATIVE_SLICER_PATCH = "native-slicer"
#: ``workflow.patched`` id of a plate printed over the LAN (plan 2026-10-16 Task 9):
#: ``lan_upload`` and ``lan_print`` in place of ``print_enqueue``. Only a run whose check
#: set ``lan_start`` asks, so no recorded run's history does.
NATIVE_PATCH = "native-print"
#: One upload of a sliced 3MF to the printer, beating.
LAN_UPLOAD_TIMEOUT = timedelta(minutes=10)
#: How many times a plate is uploaded and started when its start may not have been
#: taken (``LAN_UNSURE``, or ``lan_print`` timed out): each attempt uploads under a new
#: name, since the printer moves a started upload away (Task 7's measurement), and
#: ``lan_print`` finds a start an earlier attempt did take rather than send it again.
LAN_START_ATTEMPTS = 3
#: One ``lan_print``: its report read, its wait for the start, and a margin for its two
#: MQTT connects (90 s). No patch is needed: no released build ran ``lan_print``, so no
#: recorded history schedules it.
LAN_PRINT_TIMEOUT = timedelta(seconds=LAN_PROBE_TIMEOUT + LAN_START_WAIT + 45)
#: How long a run of several LAN plate copies waits for the printer to finish one before
#: it starts the next (#4541): a print of days, and the printer off a while between.
#: Past it the run starts none of the rest, and says so.
LAN_WAIT_BOUND = timedelta(days=7)
#: ``lan_wait`` only reads, so it is taken up again whenever it stops, within the bound.
LAN_WAIT_RETRY = RetryPolicy(
    maximum_attempts=0,
    initial_interval=timedelta(seconds=5),
    maximum_interval=timedelta(minutes=1),
    backoff_coefficient=2.0,
)


def _sliced_by(
    started: SliceStarted | NativeSliced,
) -> tuple[int | None, str | None, NativeSliced | None]:
    """A slice's Bambuddy job id (never a native row's: ``REUSED_GONE_PATCH`` reads a
    reused plate by its ``None``), its preset key, and the native slice if it was one."""
    if isinstance(started, NativeSliced):
        return None, started.preset_key, started
    return started.job_id, started.preset_key, None


class _PlateStarts:
    """What ``PrintRun`` and ``PrintLanSequence`` share: shielding an activity through a
    cancel, a plate's LAN start and its record, and rewriting a LAN run's result."""

    def __init__(self) -> None:
        #: Whether the run caught a cancel and carried on; it then holds no repeat
        #: window, since Temporal drops a second cancel request (review #1316 2).
        self.cancel_absorbed = False

    async def _shielded[T](
        self, handle: workflow.ActivityHandle[T], patch: str = CANCEL_PATCH
    ) -> T:
        """``handle``'s result, waited for through a cancel: the activity is not
        cancelled. A run from before ``patch`` cancels it, as it did then."""
        try:
            return await asyncio.shield(handle)
        except asyncio.CancelledError:
            if not workflow.patched(patch):
                handle.cancel()
            else:
                self.cancel_absorbed = True
            return await handle

    def _stop_if_cancelled(self) -> None:
        """A cancel a shielded activity absorbed stops the run before what comes next."""
        if self.cancel_absorbed:
            raise asyncio.CancelledError

    async def _lan_update(
        self, input: PrintRunInput, accepted: Accepted, result: PrintRunResult
    ) -> PrintRun:
        """Record ``result`` on the succeeded run, through a cancel: the plates it names
        are printing."""
        row: PrintRun = await self._shielded(
            workflow.start_activity(
                PRINT_LAN_UPDATE_ACTIVITY,
                LanUpdateInput(run_id=accepted.run.id, slug=input.slug, result=result),
                result_type=PrintRun,
                start_to_close_timeout=SHORT,
                retry_policy=RECORD_RETRY,
            )
        )
        return row

    async def _record_plate(
        self,
        accepted: Accepted,
        planned: PlannedRun,
        plate: PlatePlan,
        outcome: QueueOutcome,
        sent: list[PlateSend],
        index: int,
        last: int,
    ) -> list[PlateSend]:
        """``print_record`` of one plate as soon as it is queued or started."""
        record = workflow.start_activity(
            "print_record",
            RecordInput(
                source=accepted.source,
                library_file_id=planned.library_file_id,
                plate_id=plate.plate_id,
                outcome=outcome,
                project_id=planned.project_id,
                sent=sent,
                run_id=accepted.run.id,
            ),
            result_type=list[PlateSend],
            start_to_close_timeout=SHORT,
            retry_policy=BOUNDED_RETRY,
        )
        # Once the last plate is queued, a cancel waits for the rest and the run
        # still ends succeeded, as the insert is shielded (review #1316 1).
        recorded: list[PlateSend] = await (
            self._shielded(record) if index == last else self._shielded(record, PLATES_PATCH)
        )
        if index != last:
            self._stop_if_cancelled()
        return recorded

    async def _start_lan(
        self,
        input: PrintRunInput,
        accepted: Accepted,
        planned: PlannedRun,
        plate: PlatePlan,
        sliced: int,
        native: NativeSliced | None,
        *,
        last: bool,
        copy: int = 1,
    ) -> LanStarted:
        """Upload the sliced plate to the printer and start it (`NATIVE_PATCH`). A start
        that may not have been taken is uploaded again under a new name and started
        again, up to `LAN_START_ATTEMPTS`; ``lan_print`` reads the report first, so one
        that was taken is not sent twice. The start is shielded as ``print_enqueue`` is:
        a cancel cannot take back a ``project_file`` in flight."""
        source = (
            SlicedSource(blob_key=native.sliced_blob_key)
            if native is not None
            else SlicedSource(library_file_id=sliced)
        )
        stem = f"scadbuddy-{accepted.run.id[:8]}-p{plate.plate_id}"
        if copy > 1:
            # Each copy is a print of its own, under a name of its own (#4541).
            stem = f"{stem}-c{copy}"
        options = planned.options
        attempt = 1
        while True:
            remote = f"{stem}.3mf" if attempt == 1 else f"{stem}-{attempt}.3mf"
            uploaded = await workflow.execute_activity(
                LAN_UPLOAD_ACTIVITY,
                LanUploadInput(
                    run_id=accepted.run.id,
                    printer=planned.printer,
                    sliced=source,
                    remote_name=remote,
                    plate_id=plate.plate_id,
                ),
                result_type=LanUploaded,
                start_to_close_timeout=LAN_UPLOAD_TIMEOUT,
                heartbeat_timeout=HEARTBEAT,
                retry_policy=BOUNDED_RETRY,
            )
            start = workflow.start_activity(
                LAN_PRINT_ACTIVITY,
                LanPrintInput(
                    printer=planned.printer,
                    uploaded=uploaded,
                    plate_id=plate.plate_id,
                    subtask_name=stem,
                    plan=input.request.filament_plan,
                    # The plate's type is in its sliced G-code already; "auto" is what the
                    # measured start sent (Task 7).
                    bed_type="auto",
                    timelapse=bool(options.timelapse),
                    layer_inspect=options.layer_inspect is not False,
                    # The rack pick runs in `lan_print`, against the report (Task 8).
                    rack_algorithm=planned.rack_algorithm,
                    rack_position=planned.rack_position,
                    spools=plate.spools,
                ),
                result_type=LanStarted,
                start_to_close_timeout=LAN_PRINT_TIMEOUT,
                retry_policy=ONCE,
            )
            try:
                return await (
                    self._shielded(start) if last else self._shielded(start, PLATES_PATCH)
                )
            except ActivityError as error:
                cause = error.cause
                unsure = isinstance(cause, TemporalTimeoutError) or (
                    isinstance(cause, ApplicationError) and cause.type == LAN_UNSURE
                )
                if not unsure or attempt >= LAN_START_ATTEMPTS:
                    raise
            workflow.logger.warning(
                "a LAN start may not have been taken; uploading and starting it again",
                extra={"plate_id": plate.plate_id, "attempt": attempt},
            )
            self._stop_if_cancelled()
            attempt += 1


@workflow.defn(name=PRINT_RUN_WORKFLOW)
class PrintRunWorkflow(_PlateStarts):
    def __init__(self) -> None:
        super().__init__()
        self.row: PrintRun | None = None
        self.refusal: PrintRunError | None = None
        self.updates = 0
        self.search_attributes = False
        #: Whether ``_print`` has reached the first ``POST /queue/``.
        self.enqueue_attempted = False
        #: Whether ``print_start_enqueue`` wrote the row's column and the run then
        #: stopped before that POST: its record clears the column (review #1316 (8) 2).
        self.enqueue_unstarted = False

    @workflow.update(name=ACCEPTED_UPDATE)
    async def accepted(self) -> AcceptAnswer:
        self.updates += 1
        repeated = self.updates > 1
        await workflow.wait_condition(lambda: self.row is not None or self.refusal is not None)
        if self.refusal is not None:
            return AcceptAnswer(refusal=self.refusal, repeated=repeated)
        return AcceptAnswer(run=self.row, repeated=repeated)

    @workflow.run
    async def run(self, input: PrintRunInput) -> PrintRun:
        self.search_attributes = input.search_attributes
        self._upsert(kind="print", subject=input.subject, status="accepting")
        # The execution's start on the server's clock, not a time the route stamped on
        # its own host: pods' clocks differ (review #1061 3).
        started_at = workflow.info().workflow_start_time
        try:
            checked = await workflow.execute_activity(
                "print_check",
                CheckInput(input=input, started_at=started_at),
                result_type=Checked,
                start_to_close_timeout=ACCEPT_TIMEOUT,
                retry_policy=READ_RETRY,
            )
        except (ActivityError, asyncio.CancelledError) as error:
            # Nothing was written: the execution fails, and a retry may start again. A
            # cancel answers the Update too, so it is never outlived by its execution.
            cancelled = is_cancelled_exception(error)
            refusal = CANCELLED if cancelled else problem_of(error)
            await self._refuse(refusal)
            if cancelled:
                raise
            raise ApplicationError(refusal.detail, type=REFUSED, non_retryable=True) from None
        if workflow.now() - started_at > CLIENT_ACCEPTING:
            # The point of no return: a check that ended after every client stopped
            # re-sending this run would print it with nobody watching (review #1061 (2) 2).
            await self._refuse(UNWAITED)
            raise ApplicationError(UNWAITED.detail, type=REFUSED, non_retryable=True)
        insert = workflow.start_activity(
            "print_insert",
            InsertInput(input=input, checked=checked),
            result_type=PrintRun,
            start_to_close_timeout=SHORT,
            retry_policy=RECORD_RETRY,
        )
        cancelled = False
        # The insert does not heartbeat, so a cancel would return here while it may
        # still commit, leaving a `running` row nothing ends (review #1061 2). It
        # finishes, and its row is recorded cancelled.
        try:
            run = await asyncio.shield(insert)
        except asyncio.CancelledError:
            cancelled = self.cancel_absorbed = True
            run = await insert
        accepted = Accepted(run=run, source=checked.source, prepared=checked.prepared)
        if cancelled:
            self.row = await self._fail(input, accepted, CANCELLED)
        else:
            self.row = accepted.run
            self._upsert(status="running")
            try:
                self.row = await self._print(input, accepted)
            except (ActivityError, ApplicationError, asyncio.CancelledError) as error:
                # The record exists: whatever happened is recorded, and the execution
                # completes. A cancel is recorded as one (review #1061 (3) 1).
                if not is_cancelled_exception(error):
                    problem = problem_of(error)
                else:
                    self.cancel_absorbed = True
                    # The row exists and a plate may be sliced: never "before it
                    # started" (review #1316 (9) 2a).
                    problem = CANCELLED_QUEUEING if self.enqueue_attempted else CANCELLED_UNQUEUED
                self.row = await self._fail(input, accepted, problem)
        self._upsert(status=self.row.status, may_have_queued=self.row.may_have_queued)
        if (self.row.status == "succeeded" or self.row.may_have_queued) and not (
            self.cancel_absorbed and workflow.patched(CANCEL_PATCH)
        ):
            # Repeats of a body-only key inside the window get this row (§5.2). A cancel
            # ends the window early; a re-sent request still does not print twice: for a
            # body-only key (`ALLOW_DUPLICATE`) `accept_run`'s `runs.store.find` answers
            # this row inside its repeat window, and for a `request_id` key the
            # failed-only reuse policy refuses a second start (review #1316 (2) 2).
            with suppress(asyncio.CancelledError):
                await workflow.sleep(timedelta(seconds=input.repeat_window_s))
        await workflow.wait_condition(workflow.all_handlers_finished)
        return self.row

    async def _fail(
        self, input: PrintRunInput, accepted: Accepted, error: PrintRunError
    ) -> PrintRun:
        record = workflow.start_activity(
            "print_fail",
            FailInput(
                run_id=accepted.run.id,
                slug=input.slug,
                error=error,
                unqueued=self.enqueue_unstarted,
            ),
            result_type=PrintRun,
            start_to_close_timeout=SHORT,
            retry_policy=RECORD_RETRY,
        )
        return await self._shielded(record, FAIL_PATCH)

    async def _refuse(self, refusal: PrintRunError) -> None:
        """Answer the Update with ``refusal``; nothing was written."""
        self.refusal = refusal
        self._upsert(status="refused")
        await workflow.wait_condition(workflow.all_handlers_finished)

    async def _slice(
        self, planned: PlannedRun, plate: PlatePlan, subject: str
    ) -> tuple[SliceStarted | NativeSliced, int]:
        """Slice one plate: its job and the sliced file; on the native slicer, the
        `native_slices` row and the file uploaded from it (`NATIVE_SLICER_PATCH`). A
        slice that left nothing behind (``SLICE_LOST``: the sidecar died under it,
        Bambuddy restarted and forgot it, or could not be reached to start it) is started
        again after :data:`SLICE_BACKOFF`, up to :data:`SLICE_ATTEMPTS` in all (#2081). Starting it
        again is safe: nothing was queued, and the run queues each plate once, from the
        slice that finished. Any other failure ends the run as before."""
        if plate.plan is None:
            # Only a plate with a sliced file has no plan, and it is never sliced; a
            # non-retryable failure rather than an assert python -O strips (#2691 review).
            raise ApplicationError(
                f"plate {plate.plate_id} has no slice plan", type=REFUSED, non_retryable=True
            )
        if planned.slicer == "native" and workflow.patched(NATIVE_SLICER_PATCH):
            return await self._slice_native(planned, plate, subject)
        attempt = 1
        while True:
            try:
                started = await workflow.execute_activity(
                    "print_slice_start",
                    SliceStartInput(
                        library_file_id=planned.library_file_id,
                        plan=plate.plan,
                        plate_id=plate.plate_id,
                    ),
                    result_type=SliceStarted,
                    start_to_close_timeout=SHORT,
                    retry_policy=ONCE,
                )
                sliced = await workflow.execute_activity(
                    "print_slice_wait",
                    started.job_id,
                    result_type=int,
                    start_to_close_timeout=SLICE_WAIT_TIMEOUT,
                    heartbeat_timeout=HEARTBEAT,
                    retry_policy=READ_RETRY,
                )
            except ActivityError as error:
                lost = isinstance(error.cause, ApplicationError) and error.cause.type == SLICE_LOST
                # A history from before the retry ended the run here (`SLICE_RETRY_PATCH`).
                if not lost or attempt >= SLICE_ATTEMPTS or not workflow.patched(SLICE_RETRY_PATCH):
                    raise
                workflow.logger.warning(
                    "a slice left nothing behind; slicing the plate again",
                    extra={"plate_id": plate.plate_id, "attempt": attempt},
                )
                await workflow.sleep(SLICE_BACKOFF * attempt)
                attempt += 1
                continue
            return started, sliced

    async def _slice_native(
        self, planned: PlannedRun, plate: PlatePlan, subject: str
    ) -> tuple[NativeSliced, int]:
        """The shared `SliceWorkflow`, started or joined and waited for by ``slice_run``
        (re-attaching is idempotent), then its file uploaded beside the copy."""
        assert plate.plan is not None
        try:
            native = await workflow.execute_activity(
                SLICE_RUN_ACTIVITY,
                SliceRunInput(
                    library_file_id=planned.library_file_id,
                    plan=plate.plan,
                    plate_id=plate.plate_id,
                    subject=subject,
                ),
                result_type=NativeSliced,
                task_queue=planned.library_queue,
                schedule_to_close_timeout=SLICE_RUN_BOUND,
                start_to_close_timeout=SLICE_RUN_TIMEOUT,
                heartbeat_timeout=HEARTBEAT,
                retry_policy=SLICE_RUN_RETRY,
            )
        except ActivityError as error:
            outlived = outlived_slice(error)
            if outlived is None:
                raise
            raise outlived from None
        # An upload's budget, beating (review I1): a sliced 3MF carries its G-code.
        uploaded = await workflow.execute_activity(
            SLICED_UPLOAD_ACTIVITY,
            SlicedUploadInput(planned=planned, plate_id=plate.plate_id, sliced=native),
            result_type=int,
            task_queue=planned.library_queue,
            start_to_close_timeout=PLAN_TIMEOUT,
            heartbeat_timeout=HEARTBEAT,
            retry_policy=READ_RETRY,
        )
        return native, uploaded

    async def _print(self, input: PrintRunInput, accepted: Accepted) -> PrintRun:
        planned = await workflow.execute_activity(
            "print_plan",
            PlanInput(input=input, accepted=accepted),
            result_type=PlannedRun,
            start_to_close_timeout=PLAN_TIMEOUT,
            heartbeat_timeout=HEARTBEAT,
            retry_policy=READ_RETRY,
        )
        if (
            planned.lan_start
            and len(planned.plates) * planned.copies > 1
            and workflow.patched(NATIVE_PATCH)
        ):
            return await self._print_lan(input, accepted, planned)
        outcomes: list[QueueOutcome] = []
        queued: list[QueuedPlate] = []
        sent: list[PlateSend] = []
        last = len(planned.plates) - 1
        for index, plate in enumerate(planned.plates):
            job_id: int | None = None
            preset_key: str | None = None
            native: NativeSliced | None = None
            if plate.sliced_file_id is not None and workflow.patched(SLICED_PATCH):
                # Sliced already (#2671): queued as it is.
                sliced = plate.sliced_file_id
            else:
                started, sliced = await self._slice(planned, plate, input.subject)
                job_id, preset_key, native = _sliced_by(started)
            if not self.enqueue_attempted:
                await self._start_enqueue(accepted)
            # Keyed on the check's own mark, never `path`: a run checked by an older build
            # may plan `path="native"` without the gate (#4540 review).
            if planned.lan_start and workflow.patched(NATIVE_PATCH):
                lan = await self._start_lan(
                    input, accepted, planned, plate, sliced, native, last=index == last
                )
                outcome = QueueOutcome(
                    slice_job_id=job_id,
                    sliced_library_file_id=sliced,
                    preset_key=preset_key,
                    native_slice_id=native.slice_id if native is not None else None,
                    sliced_blob_key=native.sliced_blob_key if native is not None else None,
                    queue_item_ids=[],
                    printer_id=planned.printer_id,
                    lan=lan,
                )
                outcomes.append(outcome)
                sent = await self._record_plate(
                    accepted, planned, plate, outcome, sent, index, last
                )
                continue
            enqueue = workflow.start_activity(
                "print_enqueue",
                EnqueueInput(
                    planned=planned,
                    plate=plate,
                    sliced=sliced,
                ),
                result_type=QueuedPlate,
                start_to_close_timeout=SHORT,
                retry_policy=ONCE,
            )
            # A cancel cannot take back a POST /queue/ in flight, so it waits for it: on
            # the last plate the run goes on to succeed (review #1316 (2) 1); before it,
            # the plate is recorded and the run stops (review #1316 (3) 1).
            try:
                queued_plate = await (
                    self._shielded(enqueue)
                    if index == last
                    else self._shielded(enqueue, PLATES_PATCH)
                )
            except ActivityError as error:
                gone = isinstance(error.cause, ApplicationError) and error.cause.type == SLICE_GONE
                # Only a reused slice is gone this way (`print_enqueue` checks), and the
                # POST /queue/ that said so queued nothing: slice it now and queue that.
                if not gone or job_id is not None or not workflow.patched(REUSED_GONE_PATCH):
                    raise
                workflow.logger.warning(
                    "a reused slice is gone from Bambuddy; slicing the plate again",
                    extra={"plate_id": plate.plate_id, "sliced_library_file_id": sliced},
                )
                # The POST that said so queued nothing. On the first plate nothing is
                # queued at all until the new slice is sent, so a cancel until then says
                # so, as one before any POST does (#3495 review).
                if index == 0:
                    self.enqueue_attempted, self.enqueue_unstarted = False, True
                self._stop_if_cancelled()
                started, sliced = await self._slice(planned, plate, input.subject)
                self.enqueue_attempted, self.enqueue_unstarted = True, False
                job_id, preset_key, native = _sliced_by(started)
                again = workflow.start_activity(
                    "print_enqueue",
                    EnqueueInput(planned=planned, plate=plate, sliced=sliced),
                    result_type=QueuedPlate,
                    start_to_close_timeout=SHORT,
                    retry_policy=ONCE,
                )
                queued_plate = await (
                    self._shielded(again) if index == last else self._shielded(again, PLATES_PATCH)
                )
            outcome = QueueOutcome(
                slice_job_id=job_id,
                sliced_library_file_id=sliced,
                preset_key=preset_key,
                native_slice_id=native.slice_id if native is not None else None,
                sliced_blob_key=native.sliced_blob_key if native is not None else None,
                queue_item_ids=[queued_plate.item_id],
                printer_id=planned.printer_id,
            )
            outcomes.append(outcome)
            queued.append(queued_plate)
            sent = await self._record_plate(accepted, planned, plate, outcome, sent, index, last)
        result: PrintRunResult = await self._shielded(
            workflow.start_activity(
                "print_finish",
                FinishInput(
                    input=input,
                    run_id=accepted.run.id,
                    planned=planned,
                    outcomes=outcomes,
                    queued=queued,
                ),
                result_type=PrintRunResult,
                start_to_close_timeout=SHORT,
                # Every plate is queued: from here a settings read that fails for a while
                # delays the run and never fails it (review #1061 1).
                retry_policy=RECORD_RETRY,
            )
        )
        # Only the record is left, and it does not give up either (review #1061).
        finished: PrintRun = await self._shielded(
            workflow.start_activity(
                "print_succeed",
                SucceedInput(input=input, run_id=accepted.run.id, result=result),
                result_type=PrintRun,
                start_to_close_timeout=SHORT,
                retry_policy=RECORD_RETRY,
            )
        )
        await self._follow_run(input)
        return finished

    async def _follow_run(self, input: PrintRunInput) -> None:
        """Follow what the run printed, an output's or a library file's."""
        # Patched: a run started on a build without the follow replays without it
        # (review #1061); `PrintRun` runs in the wild since #1061 merged.
        if workflow.patched(FOLLOW_PATCH):
            source = input.source
            if source.kind == "output" and source.output_id is not None:
                await self._follow(source.output_id)
            elif (
                source.kind == "library"
                and source.file_id is not None
                and workflow.patched(LIBRARY_FOLLOW_PATCH)
            ):
                await self._follow(PrintSubject.library(source.file_id).run_subject)

    async def _start_enqueue(self, accepted: Accepted) -> None:
        """Before the first POST /queue/ (or LAN start): from here a failure may have
        queued. The flag follows the row's column, so a cancel's message agrees with it.
        A cancel it absorbed stops the run before that POST, so it is recorded as
        queueing nothing, and the column is cleared (review #1316 (8) 2)."""
        try:
            await self._shielded(
                workflow.start_activity(
                    "print_start_enqueue",
                    accepted.run.id,
                    start_to_close_timeout=SHORT,
                    retry_policy=RECORD_RETRY,
                ),
                PLATES_PATCH,
            )
        except (ActivityError, asyncio.CancelledError):
            # As #1061 did, which counted the run as queueing from here on. A cancel
            # reaches here only on its histories, whose `print_fail` it replays (review
            # #1316 (9) 2b).
            self.enqueue_attempted = True
            raise
        self.enqueue_unstarted = self.cancel_absorbed
        self._stop_if_cancelled()
        self.enqueue_attempted = True

    async def _print_lan(
        self, input: PrintRunInput, accepted: Accepted, planned: PlannedRun
    ) -> PrintRun:
        """A LAN run of several plates or copies (#4541). The printer has no queue, and a
        second ``project_file`` replaces the print it runs, so the run is the queue, as
        Bambuddy's is on its path: every plate is sliced first (a slice that fails then
        starts nothing), then each plate's copies start in turn, plate by plate as
        Bambuddy expands a ``quantity``, each once ``lan_wait`` sees the printer finish
        the one before. As Bambuddy's queue does with its defaults
        (``require_plate_clear`` and ``require_previous_success`` off), the next starts
        whether the one before finished or failed.

        The run succeeds once the first is started, so the dialog and the follow see it
        print, and its result names each start still to come (``lan_waiting``). The rest
        are ``PrintLanSequence``'s, a child left to run past this run, which closes after
        its repeat window as any run does: a repeat of the request is answered as one of
        any run is, never kept waiting days for the last copy."""
        plates: list[tuple[PlatePlan, int, int | None, str | None, NativeSliced | None]] = []
        for plate in planned.plates:
            if plate.sliced_file_id is not None and workflow.patched(SLICED_PATCH):
                plates.append((plate, plate.sliced_file_id, None, None, None))
            else:
                started, sliced = await self._slice(planned, plate, input.subject)
                plates.append((plate, sliced, *_sliced_by(started)))
        starts = [(entry, copy) for entry in plates for copy in range(1, planned.copies + 1)]
        await self._start_enqueue(accepted)
        (plate, sliced, job_id, preset_key, native), _ = starts[0]
        first_plate = plate.plate_id
        # As the last plate of a run's would: a cancel waits for the start, and the run
        # then succeeds with the rest stopped (review #1316 (2) 1).
        first = await self._start_lan(input, accepted, planned, plate, sliced, native, last=True)
        first = first.model_copy(update={"plate_id": plate.plate_id})
        outcome = QueueOutcome(
            slice_job_id=job_id,
            sliced_library_file_id=sliced,
            preset_key=preset_key,
            native_slice_id=native.slice_id if native is not None else None,
            sliced_blob_key=native.sliced_blob_key if native is not None else None,
            queue_item_ids=[],
            printer_id=planned.printer_id,
            lan=first,
        )
        sent = await self._record_plate(accepted, planned, plate, outcome, [], 0, 0)
        result: PrintRunResult = await self._shielded(
            workflow.start_activity(
                "print_finish",
                FinishInput(
                    input=input,
                    run_id=accepted.run.id,
                    planned=planned,
                    outcomes=[outcome],
                    queued=[],
                ),
                result_type=PrintRunResult,
                start_to_close_timeout=SHORT,
                retry_policy=RECORD_RETRY,
            )
        )
        result = result.model_copy(
            update={
                "lan_waiting": [entry[0].plate_id for entry, _ in starts[1:]],
                "lan_stopped": _LAN_CANCELLED if self.cancel_absorbed else None,
            }
        )
        row: PrintRun = await self._shielded(
            workflow.start_activity(
                "print_succeed",
                SucceedInput(input=input, run_id=accepted.run.id, result=result),
                result_type=PrintRun,
                start_to_close_timeout=SHORT,
                retry_policy=RECORD_RETRY,
            )
        )
        # The row a repeat of the request is answered with, from here on.
        self.row = row
        self._upsert(status=row.status)
        await self._follow_run(input)
        if self.cancel_absorbed:
            return row
        later = [
            LanStart(
                plate=plate,
                sliced=sliced,
                slice_job_id=job_id,
                preset_key=preset_key,
                native=native,
                copy_number=copy,
            )
            for (plate, sliced, job_id, preset_key, native), copy in starts[1:]
        ]
        # Left to run past this run, which closes after its repeat window as any run
        # does: its next start may be days away (`LAN_WAIT_BOUND`).
        await workflow.start_child_workflow(
            LAN_SEQUENCE_WORKFLOW,
            LanSequenceInput(
                input=input,
                accepted=accepted,
                planned=planned,
                starts=later,
                result=row.result or result,
                recorded=[first_plate],
                sent=sent,
            ),
            id=lan_sequence_id(accepted.run.id),
            parent_close_policy=workflow.ParentClosePolicy.ABANDON,
            cancellation_type=workflow.ChildWorkflowCancellationType.ABANDON,
        )
        return row

    async def _follow(self, output_id: str) -> None:
        """Follow the print it queued (§4.4): `FollowPrint`, abandoned so it outlives
        this run, or a poke to the one already following the subject (a child start has
        no id-conflict policy). A poke that finds it closed in between starts it once more.
        ``output_id`` is the run subject: an output's id, or ``library:<file id>``."""
        for _ in range(2):
            try:
                await workflow.start_child_workflow(
                    FOLLOW_WORKFLOW,
                    output_id,
                    id=follow_id(output_id),
                    parent_close_policy=workflow.ParentClosePolicy.ABANDON,
                    cancellation_type=workflow.ChildWorkflowCancellationType.ABANDON,
                )
                return
            except WorkflowAlreadyStartedError:
                pass
            try:
                await workflow.get_external_workflow_handle(follow_id(output_id)).signal(
                    POKE_SIGNAL
                )
                return
            except FailureError:
                continue
        # Only a log, so no command: no `patched` (review #1091 3). The progress route
        # starts the follow again once someone opens the output.
        workflow.logger.warning("could not follow the print", extra={"output_id": output_id})

    def _upsert(
        self,
        *,
        kind: str | None = None,
        subject: str | None = None,
        status: str | None = None,
        may_have_queued: bool | None = None,
    ) -> None:
        """§4.2's attributes: identifiers and states only, never content."""
        if not self.search_attributes:
            return
        pairs: list[SearchAttributeUpdate[Any]] = []
        if kind is not None:
            pairs.append(KIND.value_set(kind))
        if subject is not None:
            pairs.append(SUBJECT.value_set(subject))
        if status is not None:
            pairs.append(STATUS.value_set(status))
        if may_have_queued is not None:
            pairs.append(MAY_HAVE_QUEUED.value_set(may_have_queued))
        workflow.upsert_search_attributes(pairs)


@workflow.defn(name=LAN_SEQUENCE_WORKFLOW)
class LanSequenceWorkflow(_PlateStarts):
    """A LAN run's later plate copies (#4541), each started once ``lan_wait`` sees the
    printer finish the one before, then recorded on the run (``print_lan_update``). The
    run has succeeded, so nothing here fails it: a wait or a start that fails, or a
    cancel, stops the rest, and the run's result says why (``lan_stopped``)."""

    @workflow.run
    async def run(self, sequence: LanSequenceInput) -> PrintRun:
        input, accepted, planned = sequence.input, sequence.accepted, sequence.planned
        result = sequence.result
        recorded = set(sequence.recorded)
        sent = sequence.sent
        row = accepted.run
        #: What the loop was doing when it stopped, for the reason it records.
        step: LanStep = "wait"
        plate_id = 0
        try:
            for start in sequence.starts:
                # A cancel a start, a record or an update absorbed stops the next start.
                self._stop_if_cancelled()
                plate, native = start.plate, start.native
                plate_id = plate.plate_id
                step = "wait"
                ended = await workflow.execute_activity(
                    LAN_WAIT_ACTIVITY,
                    LanWaitInput(printer=planned.printer, started=result.lan[-1]),
                    result_type=LanStarted,
                    schedule_to_close_timeout=LAN_WAIT_BOUND,
                    heartbeat_timeout=HEARTBEAT,
                    retry_policy=LAN_WAIT_RETRY,
                )
                result = result.model_copy(update={"lan": [*result.lan[:-1], ended]})
                row = await self._lan_update(input, accepted, result)
                self._stop_if_cancelled()
                step = "start"
                taken = await self._start_lan(
                    input,
                    accepted,
                    planned,
                    plate,
                    start.sliced,
                    native,
                    last=True,
                    copy=start.copy_number,
                )
                taken = taken.model_copy(update={"plate_id": plate_id})
                # On the run before anything else, so whatever fails next never reads a
                # copy the printer is printing as one it never started.
                result = result.model_copy(
                    update={
                        "lan": [*result.lan, taken],
                        "lan_waiting": result.lan_waiting[1:],
                        "warnings": [
                            *result.warnings,
                            *(w for w in taken.warnings if w not in result.warnings),
                        ],
                    }
                )
                row = await self._lan_update(input, accepted, result)
                if plate_id not in recorded:
                    # A plate is recorded once, by its first copy, as Bambuddy's one
                    # queue item stands for all of a plate's copies.
                    recorded.add(plate_id)
                    step = "record"
                    outcome = QueueOutcome(
                        slice_job_id=start.slice_job_id,
                        sliced_library_file_id=start.sliced,
                        preset_key=start.preset_key,
                        native_slice_id=native.slice_id if native is not None else None,
                        sliced_blob_key=native.sliced_blob_key if native is not None else None,
                        queue_item_ids=[],
                        printer_id=planned.printer_id,
                        lan=taken,
                    )
                    sent = await self._record_plate(accepted, planned, plate, outcome, sent, 0, 0)
        except (ActivityError, ApplicationError, asyncio.CancelledError) as error:
            cancelled = is_cancelled_exception(error)
            if cancelled:
                self.cancel_absorbed = True
            if not result.lan_waiting:
                # Every copy started: nothing is left to stop.
                workflow.logger.warning(
                    "a LAN run's last step failed after its last start",
                    extra={"step": step, "plate_id": plate_id},
                )
                return row
            stopped = (
                _LAN_CANCELLED
                if cancelled
                else _lan_failed(error, step, plate_id, result.lan_waiting[0])
            )
            row = await self._lan_update(
                input, accepted, result.model_copy(update={"lan_stopped": stopped})
            )
        return row
