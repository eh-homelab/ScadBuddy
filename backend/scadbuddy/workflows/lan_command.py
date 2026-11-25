"""``LanCommand``: one request's commands to a printer over its LAN link (#7276, plan
2026-11-25 Task 2, R-7276-8).

The API starts it on the ``bambuddy`` queue and waits (`execute_lan_commands`): the
K-profile and AMS slot routes and the RFID override reconcile. A route waits at most
`LAN_ROUTE_WAIT_S` and then answers 202 with the run's workflow id, which
`read_lan_commands` reads back (P27). It runs ``lan_command``
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
    CancelledError,
    TerminatedError,
    WorkflowAlreadyStartedError,
)
from temporalio.exceptions import (
    TimeoutError as TemporalTimeoutError,
)

with workflow.unsafe.imports_passed_through():
    from fastapi import status
    from temporalio.client import (
        Client,
        WorkflowExecutionStatus,
        WorkflowFailureError,
        WorkflowHandle,
    )
    from temporalio.service import RPCError, RPCStatusCode

    from scadbuddy.bambuddy.runs import PrintRunError
    from scadbuddy.core.problems import ApiError
    from scadbuddy.printers.lan_command_models import (
        LanCommandOutcome,
        LanCommandPending,
        LanCommandReply,
        LanCommandRun,
        command_timeout,
        run_timeout,
    )
    from scadbuddy.workflows.print_models import (
        FAILED,
        LAN_CERT_CHANGED,
        LAN_COMMAND_ACTIVITY,
        LAN_COMMAND_WORKFLOW,
        LAN_NO_ANSWER,
        LAN_REFUSED,
        LAN_ROUTE_WAIT_S,
        LAN_UNSURE,
    )

_ONCE = RetryPolicy(maximum_attempts=1)


@workflow.defn(name=LAN_COMMAND_WORKFLOW)
class LanCommandWorkflow:
    @workflow.run
    async def run(self, run: LanCommandRun) -> LanCommandOutcome:
        """Each command's reply, in order; the first failure is raised as it came. A write
        timed out may have reached the printer, so it is ``confirmed: false`` and the rest
        still run (P30); a read timed out is raised."""
        replies: list[LanCommandReply] = []
        for command in run.commands:
            try:
                reply: LanCommandReply = await workflow.execute_activity(
                    LAN_COMMAND_ACTIVITY,
                    command,
                    result_type=LanCommandReply,
                    start_to_close_timeout=command_timeout(command),
                    retry_policy=_ONCE,
                )
            except ActivityError as error:
                if command.kind != "write" or not isinstance(error.cause, TemporalTimeoutError):
                    raise
                reply = LanCommandReply(confirmed=False)
            replies.append(reply)
        return LanCommandOutcome(replies=replies)


LanCommandAnswer = LanCommandOutcome | LanCommandPending


def lan_command_id(printer_id: str, run: LanCommandRun, key: str | None) -> str:
    """``lan-command-<printer>-<commands>-<key>`` (P9); a fresh id with no key."""
    kind = "+".join(command.name for command in run.commands)
    return f"lan-command-{printer_id}-{kind}-{key or uuid.uuid4().hex}"


async def execute_lan_commands(
    client: Client, task_queue: str, printer_id: str, run: LanCommandRun, *, key: str | None
) -> LanCommandAnswer:
    """Run ``run`` on ``task_queue`` (the ``bambuddy`` queue), bounded by the sum of its
    commands' timeouts (P20), and wait for it at most `LAN_ROUTE_WAIT_S` (P27): a run
    still open then is `LanCommandPending`, the route's 202, which `read_lan_commands`
    follows. A key sent again joins the open run or reads the closed one's outcome.
    Failures are `_answer`'s."""
    workflow_id = lan_command_id(printer_id, run, key)
    try:
        handle = await client.start_workflow(
            LAN_COMMAND_WORKFLOW,
            run,
            id=workflow_id,
            task_queue=task_queue,
            execution_timeout=run_timeout(run),
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            result_type=LanCommandOutcome,
        )
    except WorkflowAlreadyStartedError:
        # It closed: its outcome, never the commands again (R-7276-8).
        handle = client.get_workflow_handle(workflow_id, result_type=LanCommandOutcome)
    return await _answer(client, handle, run)


async def read_lan_commands(client: Client, printer_id: str, command_id: str) -> LanCommandAnswer:
    """The answer of the run ``command_id`` of ``printer_id`` (P27, the command read route
    of Task 6): `LanCommandPending` while it runs, else what `execute_lan_commands` would
    have answered. A run of another printer, or none, is 404 ``command_not_found``."""
    if not command_id.startswith(f"lan-command-{printer_id}-"):
        raise _not_found()
    handle = client.get_workflow_handle(command_id, result_type=LanCommandOutcome)
    try:
        described = await handle.describe()
    except RPCError as error:
        if error.status == RPCStatusCode.NOT_FOUND:
            raise _not_found() from None
        raise
    if described.status == WorkflowExecutionStatus.RUNNING:
        return LanCommandPending(workflow_id=command_id)
    return await _answer(client, handle, None)


async def _answer(
    client: Client,
    handle: WorkflowHandle[Any, LanCommandOutcome],
    run: LanCommandRun | None,
) -> LanCommandAnswer:
    """The run's outcome within `LAN_ROUTE_WAIT_S`, else `LanCommandPending`. A failure is
    the route's problem (`_problem`). A run that timed out whole (its ``execution_timeout``),
    or was cancelled or terminated, answers ``confirmed: false`` for every command when
    all are writes, which may have reached the printer, else 502 ``printer_no_answer``
    (P30)."""
    try:
        async with asyncio.timeout(LAN_ROUTE_WAIT_S):
            outcome: LanCommandOutcome = await handle.result()
            return outcome
    except TimeoutError:
        return LanCommandPending(workflow_id=handle.id)
    except WorkflowFailureError as failure:
        cause = failure.cause
        # A cancel reaches the running activity first, so the run fails with it.
        if isinstance(cause, ActivityError) and isinstance(cause.cause, CancelledError):
            cause = cause.cause
        if not isinstance(cause, (TemporalTimeoutError, CancelledError, TerminatedError)):
            raise _problem(failure) from failure
        commands = (run or await _started_run(client, handle)).commands
        if any(command.kind == "read" for command in commands):
            raise _no_answer() from failure
        return LanCommandOutcome(replies=[LanCommandReply(confirmed=False) for _ in commands])


async def _started_run(
    client: Client, handle: WorkflowHandle[Any, LanCommandOutcome]
) -> LanCommandRun:
    """The run a closed execution was started with: its first event's input."""
    async for event in handle.fetch_history_events():
        payloads = event.workflow_execution_started_event_attributes.input.payloads
        [run] = await client.data_converter.decode(payloads, [LanCommandRun])
        if isinstance(run, LanCommandRun):
            return run
        break
    raise RuntimeError("a LanCommand execution has no input")


def _not_found() -> ApiError:
    return ApiError(
        status.HTTP_404_NOT_FOUND, "No such command for this printer.", code="command_not_found"
    )


def _no_answer(detail: str = "The printer did not answer over the LAN.") -> ApiError:
    return ApiError(status.HTTP_502_BAD_GATEWAY, detail, code="printer_no_answer")


def _problem(failure: WorkflowFailureError) -> ApiError:
    """A failed run as the route's problem: a refusal 409 ``printer_refused``, no answer
    to a read 502 ``printer_no_answer``, a printer that could not be reached before the
    publish 502 ``printer_unreachable``, another certificate 409
    ``printer_cert_changed``, and the printer row's own refusals with their detail (P5):
    409 ``not_lan_linked``, and 503 ``lan_unavailable`` for a process that cannot open
    the access code (P30). Anything else is raised as it came."""
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
    if cause.type == LAN_CERT_CHANGED:
        return ApiError(status.HTTP_409_CONFLICT, cause.message, code="printer_cert_changed")
    if cause.type == FAILED and cause.details:
        detail: Any = cause.details[0]
        problem = (
            detail if isinstance(detail, PrintRunError) else PrintRunError.model_validate(detail)
        )
        if problem.status == status.HTTP_409_CONFLICT:
            return ApiError(problem.status, problem.detail, code="not_lan_linked")
        if problem.status == status.HTTP_503_SERVICE_UNAVAILABLE:
            return ApiError(problem.status, problem.detail, code="lan_unavailable")
    raise failure
