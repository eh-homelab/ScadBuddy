"""``Operation``: a generic command in the shape of spec 2026-10-01 §4.2 (#1053).

1. ``op.<kind>.check`` makes the route's refusals and writes nothing. A refusal answers
   the ``accepted`` Update and *fails* the execution, so a retry may start again.
2. ``op_insert`` writes the record, retried on its own so a refusal never follows it.
   A cancel waits it out and ends the record, which is never left ``running``.
3. ``op.<kind>.run`` is the effect, with the kind's attempts; then ``op_finish``. From
   the record on, every outcome completes the execution and is recorded.

Every kind here is ``done`` (§4.2 step 4): the Update answers once the effect ended.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy, SearchAttributeKey, SearchAttributeUpdate
from temporalio.exceptions import ActivityError, ApplicationError, is_cancelled_exception

with workflow.unsafe.imports_passed_through():
    from scadbuddy.bambuddy.runs import PrintRunError
    from scadbuddy.operations.store import Operation
    from scadbuddy.workflows.operation_models import (
        FINISH_ACTIVITY,
        INSERT_ACTIVITY,
        OPERATION_WORKFLOW,
        FinishOp,
        InsertOp,
        OperationAnswer,
        OperationInput,
        RunOp,
        check_activity,
        run_activity,
    )
    from scadbuddy.workflows.print_models import ACCEPTED_UPDATE, REFUSED
    from scadbuddy.workflows.problems import (
        OPERATION_CANCELLED,
        OPERATION_CANCELLED_RUNNING,
        OPERATION_UNEXPECTED_DETAIL,
        OPERATION_UNEXPECTED_RUNNING_DETAIL,
        problem_of,
    )

#: §4.2 step 4: the check answers well inside the route's deadline; retries go on.
CHECK_TIMEOUT = timedelta(seconds=8)
#: One 3MF upload (``DEFAULT_UPLOAD_TIMEOUT``, 180 s) and a margin. The browser's and the
#: agent's ``operationFollowMs`` are reckoned from it: change them together.
RUN_TIMEOUT = timedelta(minutes=5)
#: The run heartbeats (`operation_activities.py`), so a run on a worker that died is
#: retired after this, not after ``RUN_TIMEOUT`` (review #1063 second review 2).
RUN_HEARTBEAT = timedelta(seconds=30)
SHORT = timedelta(seconds=60)
READ_RETRY = RetryPolicy(
    maximum_attempts=3, initial_interval=timedelta(seconds=1), backoff_coefficient=2.0
)
RECORD_RETRY = RetryPolicy(
    maximum_attempts=0,
    initial_interval=timedelta(seconds=1),
    maximum_interval=timedelta(seconds=30),
    backoff_coefficient=2.0,
)

KIND = SearchAttributeKey.for_keyword("ScadbuddyKind")
SUBJECT = SearchAttributeKey.for_keyword("ScadbuddySubject")
STATUS = SearchAttributeKey.for_keyword("ScadbuddyStatus")


@workflow.defn(name=OPERATION_WORKFLOW)
class OperationWorkflow:
    def __init__(self) -> None:
        self.done: Operation | None = None
        self.refusal: PrintRunError | None = None
        self.updates = 0
        self.search_attributes = False

    @workflow.update(name=ACCEPTED_UPDATE)
    async def accepted(self) -> OperationAnswer:
        self.updates += 1
        repeated = self.updates > 1
        await workflow.wait_condition(lambda: self.done is not None or self.refusal is not None)
        return OperationAnswer(operation=self.done, refusal=self.refusal, repeated=repeated)

    @workflow.run
    async def run(self, input: OperationInput) -> Operation:
        self.search_attributes = input.search_attributes
        self._upsert(
            KIND.value_set(f"operation.{input.kind}"),
            SUBJECT.value_set(input.subject),
            STATUS.value_set("accepting"),
        )
        try:
            checked: dict[str, Any] = await workflow.execute_activity(
                check_activity(input.kind),
                input.request,
                result_type=dict,
                start_to_close_timeout=CHECK_TIMEOUT,
                retry_policy=READ_RETRY,
            )
        except (ActivityError, asyncio.CancelledError) as error:
            # Nothing was written: the execution fails, and a retry may start again. A
            # cancel answers the Update too, so it is never outlived by its execution.
            self.refusal = (
                OPERATION_CANCELLED
                if is_cancelled_exception(error)
                else problem_of(error, unexpected=OPERATION_UNEXPECTED_DETAIL)
            )
            self._upsert(STATUS.value_set("refused"))
            await workflow.wait_condition(workflow.all_handlers_finished)
            if is_cancelled_exception(error):
                raise
            raise ApplicationError(self.refusal.detail, type=REFUSED, non_retryable=True) from None
        inserting = workflow.start_activity(
            INSERT_ACTIVITY,
            InsertOp(input=input),
            result_type=Operation,
            start_to_close_timeout=SHORT,
            retry_policy=RECORD_RETRY,
        )
        try:
            op: Operation = await asyncio.shield(inserting)
            cancelled = False
        except asyncio.CancelledError:
            # The insert may commit after the cancel (review #1063 1): it is waited out,
            # never abandoned, so its row is ended below rather than left running.
            op = await inserting
            cancelled = True
        self._upsert(STATUS.value_set("running"))
        finish = (
            # Nothing ran: the cancel came before the effect.
            FinishOp(operation_id=op.id, error=OPERATION_CANCELLED)
            if cancelled
            else await self._effect(input, op.id, checked)
        )
        self.done = await workflow.execute_activity(
            FINISH_ACTIVITY,
            finish,
            result_type=Operation,
            start_to_close_timeout=SHORT,
            retry_policy=RECORD_RETRY,
        )
        self._upsert(STATUS.value_set(self.done.status))
        await workflow.wait_condition(workflow.all_handlers_finished)
        return self.done

    async def _effect(
        self, input: OperationInput, operation_id: str, checked: dict[str, Any]
    ) -> FinishOp:
        """The kind's run; whatever happened is recorded, and the execution completes."""
        try:
            result: dict[str, Any] = await workflow.execute_activity(
                run_activity(input.kind),
                RunOp(request=input.request, checked=checked),
                result_type=dict,
                start_to_close_timeout=RUN_TIMEOUT,
                heartbeat_timeout=RUN_HEARTBEAT,
                retry_policy=RetryPolicy(
                    maximum_attempts=input.run_attempts,
                    initial_interval=timedelta(seconds=1),
                    backoff_coefficient=2.0,
                ),
            )
        except (ActivityError, ApplicationError, asyncio.CancelledError) as error:
            if is_cancelled_exception(error):
                # The effect may have reached Bambuddy before the cancel (review #1063 2).
                return FinishOp(operation_id=operation_id, error=OPERATION_CANCELLED_RUNNING)
            # A crash or timeout may come after Bambuddy took the write (review #1063
            # second review 1), so the unexpected failure says it may have been done.
            return FinishOp(
                operation_id=operation_id,
                error=problem_of(error, unexpected=OPERATION_UNEXPECTED_RUNNING_DETAIL),
            )
        return FinishOp(operation_id=operation_id, result=result)

    def _upsert(self, *pairs: SearchAttributeUpdate[Any]) -> None:
        """§4.2's attributes: identifiers and states only, never content."""
        if self.search_attributes:
            workflow.upsert_search_attributes(list(pairs))
