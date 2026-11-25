"""``LanCommand``: one request's commands to a printer over its LAN link (#7276, plan
2026-11-25 Task 2, R-7276-8).

The API starts it on the ``bambuddy`` queue and waits (`execute_lan_commands`): the
K-profile and AMS slot routes and the RFID override reconcile. A route waits at most
`LAN_ROUTE_WAIT_S` and then answers 202 with the run's workflow id, which
`read_lan_commands` reads back (P27). It runs ``lan_command``
(`printers/print_activities.py` `LanActivities`) once per command, in order, each tried
once, and the first failure ends the run with it. The workflow id is
``lan-command-<printer>-<kind>-<key hash>`` (P9, P63). The kind is the route's fixed
`LanCommandKind`, never the body's commands: a configure sends two or three commands by its
body, and two first sends of one key must still be one id. The key hash is `KEY_HASH_CHARS`
of the key's sha256, so a key with ``/`` or another character the command read route or the
agent's ``COMMAND_ID`` refuses still makes an id they take. One key on one route on one
printer is one execution: a key sent again, or two first sends at once, attach to the
running execution or read the closed one's outcome, so the printer gets the commands once.
A key reused on another route is another execution. The answer is always the started
run's (`start_lan_commands`), never a later request's own run.

On the versioned print worker it is AUTO_UPGRADE (`client.print_deployment`), so a change
to ``LanCommandWorkflow.run``'s commands goes behind ``workflow.patched``, and
``tests/test_lan_command_replay.py`` replays the committed histories. A new workflow type:
roll ``scadbuddy-print`` with ``Recreate`` (README, "A release that needs ``Recreate``").
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from typing import Any, Literal

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
    from temporalio.api.enums.v1 import EventType
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
        LanCommandInput,
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

NOT_LAN_LINKED = "not_lan_linked"
"""The 409 code of a printer with no LAN link (``native`` shares it)."""

_ONCE = RetryPolicy(maximum_attempts=1)

LanCommandKind = Literal[
    "kprofile-read",
    "kprofile-set",
    "kprofile-batch",
    "kprofile-delete",
    "slot-configure",
    "slot-reset",
    "slot-refresh",
    "slot-load",
    "slot-unload",
    "rfid-slot",
]
"""The route that runs a LAN command, the middle part of its workflow id (P63): each
K-profile route, each AMS slot route and the RFID override reconcile has one. Each holds
only ``[A-Za-z0-9_+-]``, which the agent's ``COMMAND_ID`` takes."""

KEY_HASH_CHARS = 32
"""The hex characters of the key's sha256 in a workflow id: 128 bits, so two keys of one
printer's command share an id only by a negligible chance."""


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


def lan_command_id(printer_id: str, kind: LanCommandKind, key: str | None) -> str:
    """``lan-command-<printer>-<kind>-<key hash>`` (P9, P63); a fresh id with no key. The
    key goes in as `KEY_HASH_CHARS` hex characters of its sha256: one key on one route
    always makes one id, and the id holds only ``[A-Za-z0-9_+-]`` past the printer's id."""
    tail = (
        uuid.uuid4().hex if not key else hashlib.sha256(key.encode()).hexdigest()[:KEY_HASH_CHARS]
    )
    return f"lan-command-{printer_id}-{kind}-{tail}"


async def execute_lan_commands(
    client: Client,
    task_queue: str,
    printer_id: str,
    run: LanCommandRun,
    *,
    kind: LanCommandKind,
    key: str | None,
) -> LanCommandAnswer:
    """`start_lan_commands`' answer alone."""
    answer, _ = await start_lan_commands(client, task_queue, printer_id, run, kind=kind, key=key)
    return answer


async def start_lan_commands(
    client: Client,
    task_queue: str,
    printer_id: str,
    run: LanCommandRun,
    *,
    kind: LanCommandKind,
    key: str | None,
) -> tuple[LanCommandAnswer, LanCommandRun]:
    """Run ``run`` on ``task_queue`` (the ``bambuddy`` queue), bounded by the sum of its
    commands' timeouts (P20), and wait for it at most `LAN_ROUTE_WAIT_S` (P27): a run
    still open then is `LanCommandPending`, the route's 202, which `read_lan_commands`
    follows. A key sent again joins the open run or reads the closed one's outcome.
    Failures are `_answer`'s.

    With the answer comes the run it belongs to, which a route builds its body from. With a
    key it is the run the execution was started with, read from its first event: two first
    sends of one key on one route at once both start, Temporal joins the second to the
    first (USE_EXISTING, one id per ``kind`` and key, P63), and the second must answer the
    first's run, not its own body's, which may name other commands."""
    workflow_id = lan_command_id(printer_id, kind, key)
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
    started = run if not key else await _started_run(client, handle)
    return await _answer(client, handle, started), started


async def read_lan_commands(
    client: Client, printer_id: str, command_id: str
) -> tuple[LanCommandAnswer, LanCommandRun | None]:
    """The answer of the run ``command_id`` of ``printer_id`` (P27, the command read route
    of Task 6): `LanCommandPending` while it runs, else what `execute_lan_commands` would
    have answered, with the run it was started with (``None`` while it runs), read once
    for both the answer and the route's view. A run of another printer, or none, is 404
    ``command_not_found``."""
    if not command_id.startswith(f"lan-command-{printer_id}-"):
        raise command_not_found()
    handle = client.get_workflow_handle(command_id, result_type=LanCommandOutcome)
    try:
        described = await handle.describe()
    except RPCError as error:
        if error.status == RPCStatusCode.NOT_FOUND:
            raise command_not_found() from None
        raise
    if described.status == WorkflowExecutionStatus.RUNNING:
        return LanCommandPending(workflow_id=command_id), None
    run = await _started_run(client, handle)
    return await _answer(client, handle, run), run


async def lan_command_exists(
    client: Client, printer_id: str, kind: LanCommandKind, key: str | None
) -> bool:
    """Whether ``key`` already started the route ``kind``'s commands for ``printer_id``
    (P38): a resend then joins that run or reads its outcome, and is never sent again."""
    if not key:
        return False
    handle = client.get_workflow_handle(lan_command_id(printer_id, kind, key))
    try:
        await handle.describe()
    except RPCError as error:
        if error.status == RPCStatusCode.NOT_FOUND:
            return False
        raise
    return True


async def lan_command_keyed(
    client: Client, printer_id: str, kind: LanCommandKind, key: str | None
) -> LanCommandRun | None:
    """The run ``key`` already started for ``printer_id`` on the route ``kind``, as it was
    started; ``None`` when there is none (P41, P63). A route answers a resend from it, never
    from the resend's own body."""
    if not key:
        return None
    handle = client.get_workflow_handle(
        lan_command_id(printer_id, kind, key), result_type=LanCommandOutcome
    )
    try:
        await handle.describe()
    except RPCError as error:
        if error.status == RPCStatusCode.NOT_FOUND:
            return None
        raise
    return await _started_run(client, handle)


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
            raise await _with_earlier(client, handle, _problem(failure)) from failure
        commands = (run or await _started_run(client, handle)).commands
        if any(command.kind == "read" for command in commands):
            raise _no_answer() from failure
        return LanCommandOutcome(replies=[LanCommandReply(confirmed=False) for _ in commands])


async def _with_earlier(
    client: Client, handle: WorkflowHandle[Any, LanCommandOutcome], problem: ApiError
) -> ApiError:
    """``problem`` with the commands that ran before the failed one (P61): a configure is
    two or three writes, so a later one's failure leaves an earlier one applied, and the
    problem must not read as though nothing changed. Each earlier command the printer
    confirmed is named, and each write sent but not acknowledged (it may have applied,
    P30). A read that timed out is not named: it failed the run itself (N3)."""
    names: dict[int, str] = {}
    writes: set[int] = set()
    confirmed: list[str] = []
    unacknowledged: list[str] = []
    async for event in handle.fetch_history_events():
        if event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
            scheduled = event.activity_task_scheduled_event_attributes
            [command] = await client.data_converter.decode(
                list(scheduled.input.payloads), [LanCommandInput]
            )
            names[event.event_id] = command.name
            if command.kind == "write":
                writes.add(event.event_id)
        elif event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_COMPLETED:
            done = event.activity_task_completed_event_attributes
            [reply] = await client.data_converter.decode(
                list(done.result.payloads), [LanCommandReply]
            )
            name = names.get(done.scheduled_event_id, "a command")
            (confirmed if reply.confirmed else unacknowledged).append(name)
        elif event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_TIMED_OUT:
            # Only a write timed out lets the run go on (`LanCommandWorkflow.run`); a read
            # timed out is the failure itself.
            timed_out = event.activity_task_timed_out_event_attributes
            if timed_out.scheduled_event_id in writes:
                unacknowledged.append(names[timed_out.scheduled_event_id])
    if not confirmed and not unacknowledged:
        return problem
    said = []
    if confirmed:
        said.append(f"Before it, the printer confirmed {', '.join(confirmed)}.")
    if unacknowledged:
        said.append(f"Sent before it, unacknowledged: {', '.join(unacknowledged)}.")
    return ApiError(
        problem.status,
        " ".join([problem.detail, *said]),
        title=problem.title,
        type_=problem.type,
        headers=problem.headers,
        **problem.extensions,
    )


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


def command_not_found() -> ApiError:
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
            return ApiError(problem.status, problem.detail, code=NOT_LAN_LINKED)
        if problem.status == status.HTTP_503_SERVICE_UNAVAILABLE:
            return ApiError(problem.status, problem.detail, code="lan_unavailable")
    raise failure
