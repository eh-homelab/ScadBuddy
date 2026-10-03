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

from datetime import timedelta

from temporalio.client import (
    Client,
    WithStartWorkflowOperation,
    WorkflowUpdateRPCTimeoutOrCancelledError,
)
from temporalio.common import (
    TypedSearchAttributes,
    WorkflowIDConflictPolicy,
    WorkflowIDReusePolicy,
)
from temporalio.exceptions import WorkflowAlreadyStartedError

#: Below Envoy's 15 s route timeout (§1), so no command holds a request open past it.
COMMAND_ANSWER_DEADLINE = timedelta(seconds=10)
#: What a client waits before it sends the same request again.
RETRY_AFTER_SECONDS = 2


class CommandStillAcceptingError(Exception):
    """The command's Update did not answer within the deadline. The execution goes on,
    and the same request attaches to it."""


class AlreadyClosedError(Exception):
    """The ID's last execution closed and the reuse policy refuses another: the route
    answers from our record."""


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
    )
    try:
        answer: T = await client.execute_update_with_start_workflow(
            update,
            start_workflow_operation=operation,
            result_type=result_type,
            rpc_timeout=deadline,
        )
    except WorkflowUpdateRPCTimeoutOrCancelledError as error:
        raise CommandStillAcceptingError(id) from error
    except WorkflowAlreadyStartedError as error:
        raise AlreadyClosedError(id) from error
    return answer
