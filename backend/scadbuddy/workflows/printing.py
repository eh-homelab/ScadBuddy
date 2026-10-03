"""``PrintRun``: the print dialog's run as a Temporal workflow (#1052, spec 2026-10-01
§5.3), the command shape of §4.2 that every later command copies.

1. ``print_check`` makes today's cheap refusals; ``print_insert`` then writes the run,
   retried on its own so a refusal never follows a committed row. A refusal answers the
   ``accepted`` Update and *fails* the execution: no record, so a retry may start.
2. From the record on, every outcome *completes* the execution and is recorded: one
   ``try`` holds the rest, and whatever an activity raised becomes ``print_fail``.
3. A run that succeeded or may have queued stays open for its repeat window, so a
   repeat (``USE_EXISTING``) gets the same row (§5.2).

``print_slice_start`` and ``print_enqueue`` each start something Bambuddy does not
dedupe, so they run once (``maximum_attempts = 1``). Only pure database writes retry
without limit; ``print_record`` and ``print_finish`` also touch the data volume and
Bambuddy, so they give up and the run is recorded as failed. ``print_succeed`` is the
record alone, so once every plate is queued the run never ends ``failed``.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy, SearchAttributeKey, SearchAttributeUpdate
from temporalio.exceptions import ActivityError, ApplicationError

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


def _problem(error: BaseException) -> PrintRunError:
    """The problem an activity reported (``REFUSED``/``FAILED`` details), else the
    unexpected failure's: never the exception's own text, which may say anything."""
    cause = error.cause if isinstance(error, ActivityError) else error
    if isinstance(cause, ApplicationError) and cause.type in (REFUSED, FAILED) and cause.details:
        detail: Any = cause.details[0]
        return detail if isinstance(detail, PrintRunError) else PrintRunError.model_validate(detail)
    return PrintRunError(status=500, title="Internal Server Error", detail=UNEXPECTED_DETAIL)


@workflow.defn(name=PRINT_RUN_WORKFLOW)
class PrintRunWorkflow:
    def __init__(self) -> None:
        self.row: PrintRun | None = None
        self.refusal: PrintRunError | None = None
        self.updates = 0
        self.search_attributes = False

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
        try:
            checked = await workflow.execute_activity(
                "print_check",
                input,
                result_type=Checked,
                start_to_close_timeout=ACCEPT_TIMEOUT,
                retry_policy=READ_RETRY,
            )
        except ActivityError as error:
            # Nothing was written: the execution fails, and a retry may start again.
            self.refusal = _problem(error)
            self._upsert(status="refused")
            await workflow.wait_condition(workflow.all_handlers_finished)
            raise ApplicationError(self.refusal.detail, type=REFUSED, non_retryable=True) from None
        run = await workflow.execute_activity(
            "print_insert",
            InsertInput(input=input, checked=checked),
            result_type=PrintRun,
            start_to_close_timeout=SHORT,
            retry_policy=RECORD_RETRY,
        )
        accepted = Accepted(run=run, source=checked.source, prepared=checked.prepared)
        self.row = accepted.run
        self._upsert(status="running")
        try:
            self.row = await self._print(input, accepted)
        except (ActivityError, ApplicationError, asyncio.CancelledError) as error:
            # The record exists: whatever happened is recorded, and the execution completes.
            self.row = await workflow.execute_activity(
                "print_fail",
                FailInput(run_id=accepted.run.id, slug=input.slug, error=_problem(error)),
                result_type=PrintRun,
                start_to_close_timeout=SHORT,
                retry_policy=RECORD_RETRY,
            )
        self._upsert(status=self.row.status, may_have_queued=self.row.may_have_queued)
        if self.row.status == "succeeded" or self.row.may_have_queued:
            # Repeats of a body-only key inside the window get this row (§5.2).
            await workflow.sleep(timedelta(seconds=input.repeat_window_s))
        await workflow.wait_condition(workflow.all_handlers_finished)
        return self.row

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
        enqueue_attempted = False
        for plate in planned.plates:
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
            if not enqueue_attempted:
                # Before the first POST /queue/: from here a failure may have queued.
                enqueue_attempted = True
                await workflow.execute_activity(
                    "print_start_enqueue",
                    accepted.run.id,
                    start_to_close_timeout=SHORT,
                    retry_policy=RECORD_RETRY,
                )
            queued_plate = await workflow.execute_activity(
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
            outcome = QueueOutcome(
                slice_job_id=started.job_id,
                sliced_library_file_id=sliced,
                preset_key=started.preset_key,
                queue_item_ids=[queued_plate.item_id],
                printer_id=planned.printer_id,
            )
            outcomes.append(outcome)
            queued.append(queued_plate)
            sent = await workflow.execute_activity(
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
        result = await workflow.execute_activity(
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
            retry_policy=BOUNDED_RETRY,
        )
        # Every plate is queued: from here only the record is left, and it does not
        # give up (review #1061).
        finished: PrintRun = await workflow.execute_activity(
            "print_succeed",
            SucceedInput(input=input, run_id=accepted.run.id, result=result),
            result_type=PrintRun,
            start_to_close_timeout=SHORT,
            retry_policy=RECORD_RETRY,
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
