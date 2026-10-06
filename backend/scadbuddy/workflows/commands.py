"""The one way a route starts a command (spec 2026-10-01 §4.2): update-with-start.

The route names the execution by its key (``<kind>-<key>``), and one call starts it
or attaches to the one already running (``USE_EXISTING``), then waits on its first
Update. The workflow's first activity validates and writes our record, and the Update
answers with that record or the refusal, so a row exists only if its execution does.

``reuse`` is the kind's: ``ALLOW_DUPLICATE_FAILED_ONLY`` for a command keyed by a
client ``request_id``, so a completed execution is never repeated (the route then
answers from our record, :class:`AlreadyClosedError`); ``ALLOW_DUPLICATE`` for a key
that holds itself open for a window (a print without a ``request_id``, §5.2).
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import timedelta
from typing import Any

from temporalio.api.errordetails.v1 import NamespaceNotFoundFailure
from temporalio.api.workflowservice.v1 import DescribeNamespaceRequest
from temporalio.client import (
    Client,
    WithStartWorkflowOperation,
    WorkflowUpdateFailedError,
    WorkflowUpdateRPCTimeoutOrCancelledError,
)
from temporalio.common import (
    TypedSearchAttributes,
    WorkflowIDConflictPolicy,
    WorkflowIDReusePolicy,
)
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

#: Below Envoy's 15 s route timeout (§1), so no command holds a request open past it.
COMMAND_ANSWER_DEADLINE = timedelta(seconds=10)
#: How much longer than the deadline a call may take before Temporal counts as
#: unreachable; still below Envoy's 15 s with the default deadline.
CONNECT_MARGIN_SECONDS = 2.0
#: What a client waits before it sends the same request again.
RETRY_AFTER_SECONDS = 2


class CommandStillAcceptingError(Exception):
    """The command's Update did not answer within the deadline. The execution goes on,
    and the same request attaches to it."""


class TemporalUnavailableError(Exception):
    """Temporal did not answer within the deadline, or answered ``UNAVAILABLE`` (its
    frontend is down or unreachable). A request may have reached it before it went, so
    a start may exist (review #1316 (9) 1a). Worth sending again."""


class TemporalUnreachableError(TemporalUnavailableError):
    """The lazy client's first connect failed: no request was written, so nothing was
    started."""


class TemporalBusyError(TemporalUnavailableError):
    """Temporal answered, or gRPC ended the call, with a code Temporal's own client
    retries, or the namespace is not (yet) known: it could not take the call now, and
    the same call may succeed shortly."""


class TemporalRefusedError(Exception):
    """Temporal answered and refused (a denied permission, a failed authentication, a
    request it rejects): sending it again will not help until that is fixed."""


#: The `RPCError`s worth sending again; any other is :class:`TemporalRefusedError`
#: (review #1316 1a). First the codes Temporal's own client retries,
#: `RETRYABLE_ERROR_CODES` in the sdk-core this SDK bundles (temporalio 1.33.0's
#: `temporalio/bridge/sdk-core` at temporalio/sdk-rust@85b71d7e,
#: `crates/client/src/retry.rs`): one reaching us outlived those retries.
#: `RESOURCE_EXHAUSTED` is a namespace past its rate limit or a busy server (review #1316
#: 4); `ABORTED` is gRPC's "retry at a higher level" (google.rpc.Code). Then the two gRPC
#: itself raises on a call that never answered
#: (https://grpc.github.io/grpc/core/md_doc_statuscodes.html): `DEADLINE_EXCEEDED`, and
#: `CANCELLED`, which sdk-core also retries when the transport cancelled it. The rest
#: (`NOT_FOUND` other than a namespace's, `PERMISSION_DENIED`, `UNAUTHENTICATED`,
#: `INVALID_ARGUMENT`, `FAILED_PRECONDITION`, `UNIMPLEMENTED`) are what neither retries.
#: A start the reuse policy refuses (`ALREADY_EXISTS`) never reaches here as an
#: `RPCError`: update-with-start raises it as `WorkflowAlreadyStartedError`, which
#: `start_command` answers as `AlreadyClosedError` (review #1316 (10) 2).
TRANSIENT_RPC = frozenset(
    {
        RPCStatusCode.DATA_LOSS,
        RPCStatusCode.INTERNAL,
        RPCStatusCode.UNKNOWN,
        RPCStatusCode.RESOURCE_EXHAUSTED,
        RPCStatusCode.ABORTED,
        RPCStatusCode.OUT_OF_RANGE,
        RPCStatusCode.UNAVAILABLE,
        RPCStatusCode.DEADLINE_EXCEEDED,
        RPCStatusCode.CANCELLED,
    }
)


def namespace_not_found(error: RPCError) -> bool:
    """A NOT_FOUND about the namespace (a `NamespaceNotFoundFailure` in its details, as
    #1066's), not about the execution the call named."""
    return error.status == RPCStatusCode.NOT_FOUND and any(
        detail.Is(NamespaceNotFoundFailure.DESCRIPTOR) for detail in error.grpc_status.details
    )


def temporal_failure(error: BaseException, id: str) -> Exception | None:
    """What a failed call to Temporal means, the one reading the routes and the
    lost-run reconcilers share (review #1316 (11) 2, 5; (12) 1): unreachable,
    unavailable, busy (each worth sending again) or refused. ``None`` for a failure
    that is not about Temporal at all. A caller that expects an execution NOT_FOUND
    tells it apart first: here it is a refusal."""
    if isinstance(error, TimeoutError):
        return TemporalUnavailableError(id)
    if isinstance(error, RuntimeError):
        # How a lazy client's first connect fails (temporalio 1.33).
        if str(error).startswith("Failed client connect"):
            return TemporalUnreachableError(id)
        return None
    if not isinstance(error, RPCError):
        return None
    if error.status == RPCStatusCode.UNAVAILABLE:
        # A connected client's `UNAVAILABLE` cannot tell a refused reconnect from a
        # stream reset after the start was persisted (temporalio 1.33.0,
        # `bridge/src/client.rs` `rpc_resp`), and sdk-core retries it.
        return TemporalUnavailableError(id)
    if error.status in TRANSIENT_RPC or namespace_not_found(error):
        return TemporalBusyError(id)
    return TemporalRefusedError(id)


#: The failure Temporal gives an Update whose execution completed before it answered.
UPDATE_OUTLIVED = "AcceptedUpdateCompletedWorkflow"


class CommandClosedError(Exception):
    """The execution ended (terminated or cancelled) before its Update answered: nothing
    was recorded, and a failed-only reuse policy lets the same request start again."""


class AlreadyClosedError(Exception):
    """The ID's last execution closed and the reuse policy refuses another: the route
    answers from our record."""


#: How long the route asks Temporal whether an execution exists, once the bound passed.
DESCRIBE_SECONDS = 2.0


async def late_answer(client: Client, id: str) -> Exception:
    """What a command call that outlived its bound means, for the caller to raise.
    Describes ``id`` within `DESCRIBE_SECONDS`: returns `CommandStillAcceptingError`
    when the execution exists (a slow Update, a busy loop: the same request follows
    it), and `TemporalUnavailableError` when the describe fails for any reason (no
    such execution, or Temporal does not answer). Never raises."""
    try:
        async with asyncio.timeout(DESCRIBE_SECONDS):
            await client.get_workflow_handle(id).describe(
                rpc_timeout=timedelta(seconds=DESCRIBE_SECONDS)
            )
    except Exception:
        return TemporalUnavailableError(id)
    return CommandStillAcceptingError(id)


async def namespace_retention(client: Client) -> timedelta:
    """How long the client's namespace keeps a closed execution (``DescribeNamespace``).
    A failure is raised as ``temporal_failure`` reads it (review #1316 (12) 1):
    ``TemporalUnavailableError`` (or a subclass), or ``TemporalRefusedError``."""
    try:
        async with asyncio.timeout(DESCRIBE_SECONDS + CONNECT_MARGIN_SECONDS):
            described = await client.workflow_service.describe_namespace(
                DescribeNamespaceRequest(namespace=client.namespace),
                timeout=timedelta(seconds=DESCRIBE_SECONDS),
            )
    except (TimeoutError, RPCError, RuntimeError) as error:
        if (failure := temporal_failure(error, client.namespace)) is None:
            raise
        raise failure from error
    return described.config.workflow_execution_retention_ttl.ToTimedelta()


async def start_command[T](
    client: Client,
    workflow: str,
    arg: object,
    *,
    id: str,
    task_queue: str,
    update: str,
    result_type: type[T],
    reuse: WorkflowIDReusePolicy,
    search_attributes: TypedSearchAttributes | None = None,
    memo: Mapping[str, Any] | None = None,
    deadline: timedelta = COMMAND_ANSWER_DEADLINE,
    update_id: str | None = None,
) -> T:
    """Start ``workflow`` as ``id`` (or attach to its running execution) and return its
    ``update``'s answer. ``update_id`` names the Update: Temporal answers a second one
    with the same id on the same execution with the first's outcome."""
    operation: WithStartWorkflowOperation[object, object] = WithStartWorkflowOperation(
        workflow,
        arg,
        id=id,
        task_queue=task_queue,
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        id_reuse_policy=reuse,
        search_attributes=search_attributes,
        memo=memo,
    )
    # `rpc_timeout` bounds each RPC, not the Update: the server may answer a poll
    # with no outcome just before it, and the SDK then polls again. So the outer bound
    # is what ends a slow Update, and it also covers the connect a lazy client makes
    # on its first call, which retries for minutes on its own.
    bound = asyncio.timeout(deadline.total_seconds() + CONNECT_MARGIN_SECONDS)
    try:
        async with bound:
            answer: T = await client.execute_update_with_start_workflow(
                update,
                start_workflow_operation=operation,
                id=update_id,
                result_type=result_type,
                rpc_timeout=deadline,
            )
    except TimeoutError as error:
        raise await late_answer(client, id) from error
    except (RPCError, RuntimeError) as error:
        if (failure := temporal_failure(error, id)) is None:
            raise
        raise failure from error
    except WorkflowUpdateRPCTimeoutOrCancelledError as error:
        # The SDK reports the outer bound's cancellation as this error too.
        if bound.expired():
            raise await late_answer(client, id) from error
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            # A caller's cancel (its own deadline): never swallowed, or its
            # `asyncio.timeout` cannot tell that it expired.
            raise asyncio.CancelledError from error
        raise CommandStillAcceptingError(id) from error
    except WorkflowAlreadyStartedError as error:
        raise AlreadyClosedError(id) from error
    except WorkflowUpdateFailedError as error:
        cause = error.cause
        if isinstance(cause, ApplicationError) and cause.type == UPDATE_OUTLIVED:
            raise CommandClosedError(id) from error
        raise
    return answer
