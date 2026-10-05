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
dedupe, so they run once (``maximum_attempts = 1``). Only pure database writes retry
without limit; ``print_record`` also touches the data volume and Bambuddy, so it gives
up and the run is recorded as failed. ``print_finish`` remembers the project's printer
best effort and, like ``print_succeed``, retries without limit, so once every plate is
queued the run never ends ``failed``.

``print_plan`` uploads the 3MF and retries (``READ_RETRY``): ``ensure_uploaded`` reuses
the copy ScadBuddy recorded, so a retry uploads again only when the attempt died after
Bambuddy stored the file and before the copy was recorded. That leaves a spare library
file, never a second print, and is tracked as a follow-up (review #1061).
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy, SearchAttributeKey, SearchAttributeUpdate
from temporalio.exceptions import ActivityError, ApplicationError, is_cancelled_exception

with workflow.unsafe.imports_passed_through():
    from scadbuddy.bambuddy.dispatch import QueueOutcome, SliceStarted
    from scadbuddy.bambuddy.print_run import PlannedRun, PrintRunResult, QueuedPlate
    from scadbuddy.bambuddy.runs import UNEXPECTED_DETAIL, PrintRun, PrintRunError
    from scadbuddy.library.outputs import PlateSend
    from scadbuddy.workflows.print_models import (
        ACCEPTED_UPDATE,
        FAILED,
        PRINT_RUN_WORKFLOW,
        REFUSED,
        AcceptAnswer,
        Accepted,
        Checked,
        CheckInput,
        EnqueueInput,
        FailInput,
        FinishInput,
        InsertInput,
        PlanInput,
        PrintRunInput,
        RecordInput,
        SliceStartInput,
        SucceedInput,
    )

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
#: The upload and the slice wait beat while they run, so a worker that died is noticed
#: within this rather than at the activity's whole budget.
HEARTBEAT = timedelta(seconds=30)

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


def _problem(error: BaseException) -> PrintRunError:
    """The problem an activity reported (``REFUSED``/``FAILED`` details), else the
    unexpected failure's: never the exception's own text, which may say anything."""
    cause = error.cause if isinstance(error, ActivityError) else error
    if isinstance(cause, ApplicationError) and cause.type in (REFUSED, FAILED) and cause.details:
        detail: Any = cause.details[0]
        return detail if isinstance(detail, PrintRunError) else PrintRunError.model_validate(detail)
    return PrintRunError(status=500, title="Internal Server Error", detail=UNEXPECTED_DETAIL)


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


@workflow.defn(name=PRINT_RUN_WORKFLOW)
class PrintRunWorkflow:
    def __init__(self) -> None:
        self.row: PrintRun | None = None
        self.refusal: PrintRunError | None = None
        self.updates = 0
        self.search_attributes = False
        #: Whether ``_print`` has reached the first ``POST /queue/``.
        self.enqueue_attempted = False
        #: Whether ``print_start_enqueue`` wrote the row's column and the run then
        #: stopped before that POST: its record clears the column (review #1316 (8) 2).
        self.enqueue_unstarted = False
        #: Whether the run caught a cancel and carried on; it then holds no repeat
        #: window, since Temporal drops a second cancel request (review #1316 2).
        self.cancel_absorbed = False

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
            refusal = CANCELLED if cancelled else _problem(error)
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
                    problem = _problem(error)
                else:
                    self.cancel_absorbed = True
                    problem = CANCELLED_QUEUEING if self.enqueue_attempted else CANCELLED
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

    async def _fail(
        self, input: PrintRunInput, accepted: Accepted, error: PrintRunError
    ) -> PrintRun:
        failed: PrintRun = await workflow.execute_activity(
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
        return failed

    async def _refuse(self, refusal: PrintRunError) -> None:
        """Answer the Update with ``refusal``; nothing was written."""
        self.refusal = refusal
        self._upsert(status="refused")
        await workflow.wait_condition(workflow.all_handlers_finished)

    async def _print(self, input: PrintRunInput, accepted: Accepted) -> PrintRun:
        planned = await workflow.execute_activity(
            "print_plan",
            PlanInput(input=input, accepted=accepted),
            result_type=PlannedRun,
            start_to_close_timeout=PLAN_TIMEOUT,
            heartbeat_timeout=HEARTBEAT,
            retry_policy=READ_RETRY,
        )
        outcomes: list[QueueOutcome] = []
        queued: list[QueuedPlate] = []
        sent: list[PlateSend] = []
        last = len(planned.plates) - 1
        for index, plate in enumerate(planned.plates):
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
            if not self.enqueue_attempted:
                # Before the first POST /queue/: from here a failure may have queued. The
                # flag follows the row's column, so a cancel's message agrees with it. A
                # cancel it absorbed stops the run before that POST, so it is recorded
                # as queueing nothing, and the column is cleared (review #1316 (8) 2).
                await self._shielded(
                    workflow.start_activity(
                        "print_start_enqueue",
                        accepted.run.id,
                        start_to_close_timeout=SHORT,
                        retry_policy=RECORD_RETRY,
                    ),
                    PLATES_PATCH,
                )
                self.enqueue_unstarted = self.cancel_absorbed
                self._stop_if_cancelled()
                self.enqueue_attempted = True
            enqueue = workflow.start_activity(
                "print_enqueue",
                EnqueueInput(
                    planned=planned,
                    plate=plate,
                    sliced=sliced,
                    credit=accepted.source.kind == "output",
                ),
                result_type=QueuedPlate,
                start_to_close_timeout=SHORT,
                retry_policy=ONCE,
            )
            # A cancel cannot take back a POST /queue/ in flight, so it waits for it: on
            # the last plate the run goes on to succeed (review #1316 (2) 1); before it,
            # the plate is recorded and the run stops (review #1316 (3) 1).
            queued_plate = await (
                self._shielded(enqueue) if index == last else self._shielded(enqueue, PLATES_PATCH)
            )
            outcome = QueueOutcome(
                slice_job_id=started.job_id,
                sliced_library_file_id=sliced,
                preset_key=started.preset_key,
                queue_item_ids=[queued_plate.item_id],
                printer_id=planned.printer_id,
            )
            outcomes.append(outcome)
            queued.append(queued_plate)
            record = workflow.start_activity(
                "print_record",
                RecordInput(
                    source=accepted.source,
                    library_file_id=planned.library_file_id,
                    plate_id=plate.plate_id,
                    outcome=outcome,
                    project_id=planned.project_id,
                    sent=sent,
                ),
                result_type=list[PlateSend],
                start_to_close_timeout=SHORT,
                retry_policy=BOUNDED_RETRY,
            )
            # Once the last plate is queued, a cancel waits for the rest and the run
            # still ends succeeded, as the insert is shielded (review #1316 1).
            sent = await (
                self._shielded(record) if index == last else self._shielded(record, PLATES_PATCH)
            )
            if index != last:
                self._stop_if_cancelled()
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
        return finished

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
