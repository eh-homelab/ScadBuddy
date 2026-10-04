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
    """Temporal did not answer at all within the deadline (its frontend is down or
    unreachable): nothing was started."""


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


async def _late(client: Client, id: str) -> Exception:
    """What a call that outlived its bound means: the execution exists, so it is still
    accepting (a slow Update, a busy loop); or Temporal cannot say, so it is down."""
    try:
        async with asyncio.timeout(DESCRIBE_SECONDS):
            await client.get_workflow_handle(id).describe(
                rpc_timeout=timedelta(seconds=DESCRIBE_SECONDS)
            )
    except Exception:
        return TemporalUnavailableError(id)
    return CommandStillAcceptingError(id)


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
) -> T:
    """Start ``workflow`` as ``id`` (or attach to its running execution) and return its
    ``update``'s answer."""
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
    # `rpc_timeout` bounds the Update; the outer bound is for the connect a lazy
    # client makes on its first call, which retries for minutes on its own.
    bound = asyncio.timeout(deadline.total_seconds() + CONNECT_MARGIN_SECONDS)
    try:
        async with bound:
            answer: T = await client.execute_update_with_start_workflow(
                update,
                start_workflow_operation=operation,
                result_type=result_type,
                rpc_timeout=deadline,
            )
    except TimeoutError as error:
        raise await _late(client, id) from error
    except RPCError as error:
        # The frontend refused the connection outright.
        if error.status == RPCStatusCode.UNAVAILABLE:
            raise TemporalUnavailableError(id) from error
        raise
    except RuntimeError as error:
        # How a lazy client's first connect fails (temporalio 1.33).
        if str(error).startswith("Failed client connect"):
            raise TemporalUnavailableError(id) from error
        raise
    except WorkflowUpdateRPCTimeoutOrCancelledError as error:
        # The SDK reports the outer bound's cancellation as this error too.
        if bound.expired():
            raise await _late(client, id) from error
        raise CommandStillAcceptingError(id) from error
    except WorkflowAlreadyStartedError as error:
        raise AlreadyClosedError(id) from error
    except WorkflowUpdateFailedError as error:
        cause = error.cause
        if isinstance(cause, ApplicationError) and cause.type == UPDATE_OUTLIVED:
            raise CommandClosedError(id) from error
        raise
    return answer
