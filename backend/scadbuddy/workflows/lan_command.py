"""``LanCommand``: one request's commands to a printer over its LAN link (#7276, plan
2026-11-25 Task 2, R-7276-8).

The API starts it on the ``bambuddy`` queue and waits (`execute_lan_commands`): the
K-profile and AMS slot routes and the RFID override reconcile. It runs ``lan_command``
(`printers/print_activities.py` `LanActivities`) once per command, in order, each tried
once, and the first failure ends the run with it. The workflow id is
``lan-command-<printer>-<commands>-<key>`` (P9): a key sent again attaches to the running
execution or reads the closed one's outcome, so the printer gets the command once, and a
key reused for other commands is another execution.

On the versioned print worker it is AUTO_UPGRADE (`client.print_deployment`), so a change
to ``LanCommandWorkflow.run``'s commands goes behind ``workflow.patched``, and
``tests/test_lan_command_replay.py`` replays the committed histories. A new workflow type:
roll ``scadbuddy-print`` with ``Recreate`` (README, "A release that needs ``Recreate``").
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy, WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import (
    ActivityError,
    ApplicationError,
    WorkflowAlreadyStartedError,
)
from temporalio.exceptions import (
    TimeoutError as TemporalTimeoutError,
)

with workflow.unsafe.imports_passed_through():
    from fastapi import status
    from temporalio.client import Client, WorkflowFailureError

    from scadbuddy.bambuddy.runs import PrintRunError
    from scadbuddy.core.problems import ApiError
    from scadbuddy.printers.lan_command_models import (
        LanCommandOutcome,
        LanCommandReply,
        LanCommandRun,
        command_timeout,
        run_timeout,
    )
    from scadbuddy.workflows.print_models import (
        FAILED,
        LAN_COMMAND_ACTIVITY,
        LAN_COMMAND_WORKFLOW,
        LAN_NO_ANSWER,
        LAN_REFUSED,
        LAN_UNSURE,
    )

_ONCE = RetryPolicy(maximum_attempts=1)


@workflow.defn(name=LAN_COMMAND_WORKFLOW)
class LanCommandWorkflow:
    @workflow.run
    async def run(self, run: LanCommandRun) -> LanCommandOutcome:
        """Each command's reply, in order; the first failure is raised as it came."""
        replies: list[LanCommandReply] = []
        for command in run.commands:
            reply: LanCommandReply = await workflow.execute_activity(
                LAN_COMMAND_ACTIVITY,
                command,
                result_type=LanCommandReply,
                start_to_close_timeout=command_timeout(command),
                retry_policy=_ONCE,
            )
            replies.append(reply)
        return LanCommandOutcome(replies=replies)


def lan_command_id(printer_id: str, run: LanCommandRun, key: str | None) -> str:
    """``lan-command-<printer>-<commands>-<key>`` (P9); a fresh id with no key."""
    kind = "+".join(command.name for command in run.commands)
    return f"lan-command-{printer_id}-{kind}-{key or uuid.uuid4().hex}"


async def execute_lan_commands(
    client: Client, task_queue: str, printer_id: str, run: LanCommandRun, *, key: str | None
) -> LanCommandOutcome:
    """Run ``run`` on ``task_queue`` (the ``bambuddy`` queue) and wait for it, at most the
    sum of its commands' timeouts (P20). A failure is the route's problem: a refusal 409
    ``printer_refused``, no answer (or the wait ran out) 502 ``printer_no_answer``, a
    printer that cannot be reached 502 ``printer_unreachable``, and the printer row's own
    refusals with their status and the command's detail (P5): 409 ``not_lan_linked``, 503
    for a process that cannot open the access code."""
    workflow_id = lan_command_id(printer_id, run, key)
    wait = run_timeout(run)
    try:
        handle = await client.start_workflow(
            LAN_COMMAND_WORKFLOW,
            run,
            id=workflow_id,
            task_queue=task_queue,
            execution_timeout=wait,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            result_type=LanCommandOutcome,
        )
    except WorkflowAlreadyStartedError:
        # It closed: its outcome, never the commands again (R-7276-8).
        handle = client.get_workflow_handle(workflow_id, result_type=LanCommandOutcome)
    try:
        async with asyncio.timeout(wait.total_seconds()):
            outcome: LanCommandOutcome = await handle.result()
            return outcome
    except TimeoutError as error:
        raise _no_answer() from error
    except WorkflowFailureError as failure:
        raise _problem(failure) from failure


def _no_answer(detail: str = "The printer did not answer over the LAN.") -> ApiError:
    return ApiError(status.HTTP_502_BAD_GATEWAY, detail, code="printer_no_answer")


def _problem(failure: WorkflowFailureError) -> ApiError:
    cause: BaseException | None = failure.cause
    if isinstance(cause, ActivityError):
        cause = cause.cause
    if isinstance(cause, TemporalTimeoutError):
        return _no_answer()
    if not isinstance(cause, ApplicationError):
        raise failure
    if cause.type == LAN_REFUSED:
        return ApiError(status.HTTP_409_CONFLICT, cause.message, code="printer_refused")
    if cause.type == LAN_NO_ANSWER:
        return _no_answer(cause.message)
    if cause.type == LAN_UNSURE:
        return ApiError(status.HTTP_502_BAD_GATEWAY, cause.message, code="printer_unreachable")
    if cause.type == FAILED and cause.details:
        detail: Any = cause.details[0]
        problem = (
            detail if isinstance(detail, PrintRunError) else PrintRunError.model_validate(detail)
        )
        if problem.status == status.HTTP_409_CONFLICT:
            return ApiError(problem.status, problem.detail, code="not_lan_linked")
        if problem.status == status.HTTP_503_SERVICE_UNAVAILABLE:
            return ApiError(problem.status, problem.detail)
    raise failure
