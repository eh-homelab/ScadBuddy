"""Starting and reading operations (#1053, spec 2026-10-01 §4.2): the one helper every
route that runs a generic command calls, and ``GET /operations/{id}``.

A route's request is keyed by its kind, subject, body and the client's
``Idempotency-Key`` (one per deliberate press; without one, each request is its own
command, as before). Our record is read first: a repeat answers from it and starts
nothing. Otherwise ``Operation`` is started (or attached to) with update-with-start.
Every kind is ``done``: the answer is the route's body once the effect ended, or 202
with the operation when it has not within the deadline.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Header, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from temporalio.client import WorkflowExecutionStatus
from temporalio.common import WorkflowIDReusePolicy
from temporalio.service import RPCError, RPCStatusCode

from scadbuddy.api.deps import OperationIdPath
from scadbuddy.core.authorship import current_author
from scadbuddy.core.problems import ApiError
from scadbuddy.operations.claims import ClaimStore, Held
from scadbuddy.operations.component import OperationCommands, OperationsDep
from scadbuddy.operations.kinds import OperationKind, operation_key
from scadbuddy.operations.store import Operation
from scadbuddy.workflows.commands import (
    RETRY_AFTER_SECONDS,
    AlreadyClosedError,
    CommandClosedError,
    CommandStillAcceptingError,
    TemporalUnavailableError,
    start_command,
)
from scadbuddy.workflows.operation_models import (
    OPERATION_WORKFLOW,
    OperationAnswer,
    OperationAuthor,
    OperationInput,
)
from scadbuddy.workflows.print_models import ACCEPTED_UPDATE

logger = logging.getLogger(__name__)

#: Problem ``type``s for a command the route could not hand to Temporal (#1052, #1053).
STILL_ACCEPTING_PROBLEM = "https://scadbuddy.dev/problems/command-still-accepting"
TEMPORAL_UNAVAILABLE_PROBLEM = "https://scadbuddy.dev/problems/temporal-unavailable"

#: The client's key for one deliberate press (§4.2 step 1).
IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key", max_length=128)]

router = APIRouter(prefix="/operations", tags=["operations"])


def still_accepting() -> ApiError:
    return ApiError(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "ScadBuddy is still checking this request. Send it again to follow it.",
        type_=STILL_ACCEPTING_PROBLEM,
        headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
    )


def temporal_unavailable(what: str) -> ApiError:
    logger.warning("could not start %s on Temporal", what, exc_info=True)
    return ApiError(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        f"ScadBuddy cannot reach Temporal, where {what} run. Nothing was done; try again shortly.",
        type_=TEMPORAL_UNAVAILABLE_PROBLEM,
        headers={"Retry-After": "5"},
    )


def _problem(
    status_code: int, detail: str, title: str | None, type_: str, extensions: dict[str, Any]
) -> ApiError:
    """A recorded problem as the route raised it. Only its body is recorded, so a
    ``retry_after`` it carries is its Retry-After header again."""
    retry_after = extensions.get("retry_after")
    headers = {"Retry-After": str(retry_after)} if isinstance(retry_after, int) else None
    return ApiError(status_code, detail, title=title, type_=type_, headers=headers, **extensions)


def _answer(op: Operation, response: Response, *, repeated: bool) -> dict[str, Any] | Operation:
    """The route's answer for a recorded operation: its body, its problem, or a 202."""
    if op.status == "succeeded":
        return op.result or {}
    if op.status == "failed":
        assert op.error is not None  # a failed operation records its problem
        error = op.error
        raise _problem(error.status, error.detail, error.title, error.type, error.extensions)
    response.status_code = status.HTTP_202_ACCEPTED
    return op.model_copy(update={"repeated": repeated})


#: The most an operation's request may carry inline: well under Temporal's 512 KB
#: payload warning, since it is repeated in the start, check, insert and run inputs.
MAX_REQUEST_BYTES = 128 * 1024


def _author() -> OperationAuthor | None:
    """The request's agent author, for the run's commits (#252)."""
    author = current_author()
    return None if author is None else OperationAuthor(**vars(author))


@dataclass(frozen=True)
class Claimed:
    """The claims a request wrote (``operations/claims.py``), released once its answer
    is final (review 3c 1.2)."""

    store: ClaimStore
    held: list[Held]


async def _release(ops: OperationCommands, claimed: Claimed) -> None:
    """Release ``claimed``: what this request's puts created and nothing has put since,
    and no operation still running names (another request holding the same bytes)."""
    created = [held for held in claimed.held if held.created]
    if not created:
        return
    running = await ops.store.named_by_running([held.name for held in created])
    for held in created:
        if held.name not in running:
            await asyncio.to_thread(claimed.store.release, held)


async def recorded(
    ops: OperationCommands,
    *,
    kind: OperationKind,
    subject: str,
    request: BaseModel | dict[str, Any],
    idempotency_key: str | None,
) -> Operation | None:
    """The operation a keyed request already started, which ``run_operation`` answers
    from; None without a client key, since each such request is its own command."""
    if idempotency_key is None:
        return None
    return await ops.store.find(operation_key(kind.name, subject, _body(request), idempotency_key))


async def _running(ops: OperationCommands, workflow_id: str) -> bool:
    """Whether the operation's workflow is running: not when there is none, or it closed."""
    try:
        described = await ops.client.get_workflow_handle(workflow_id).describe()
    except RPCError as error:
        if error.status == RPCStatusCode.NOT_FOUND:
            return False
        raise temporal_unavailable("operations") from None
    return described.status == WorkflowExecutionStatus.RUNNING


def _body(request: BaseModel | dict[str, Any]) -> dict[str, Any]:
    return request.model_dump(mode="json") if isinstance(request, BaseModel) else request


async def run_operation(
    ops: OperationCommands,
    response: Response,
    *,
    kind: OperationKind,
    subject: str,
    request: BaseModel | dict[str, Any],
    idempotency_key: str | None,
    claimed: Claimed | None = None,
    before_start: Callable[[], Awaitable[None]] | None = None,
) -> dict[str, Any] | Operation:
    """Run ``kind`` as an operation; its result body, or 202 with the ``Operation``.
    A refusal or a recorded failure is raised as the problem the route answers with.
    ``claimed`` is dropped once the answer is final: not on a 202 or a 503, after which
    the operation may still run. ``before_start`` is a route's own refusal, made only
    when no record answers and the same request is not still running: a repeat is its
    first answer whatever has changed since (§4.2)."""
    try:
        result = await _run_operation(
            ops,
            response,
            kind=kind,
            subject=subject,
            request=request,
            idempotency_key=idempotency_key,
            before_start=before_start,
        )
    except ApiError as error:
        if claimed is not None and error.status != status.HTTP_503_SERVICE_UNAVAILABLE:
            await _release(ops, claimed)
        raise
    if claimed is not None and not (isinstance(result, Operation) and result.status == "running"):
        await _release(ops, claimed)
    return result


async def _run_operation(
    ops: OperationCommands,
    response: Response,
    *,
    kind: OperationKind,
    subject: str,
    request: BaseModel | dict[str, Any],
    idempotency_key: str | None,
    before_start: Callable[[], Awaitable[None]] | None,
) -> dict[str, Any] | Operation:
    body = _body(request)
    size = len(json.dumps(body, separators=(",", ":")).encode())
    if size > MAX_REQUEST_BYTES:
        # The request rides in every input of the operation's history; past this it
        # nears Temporal's payload limit, which would answer as a 503 every retry
        # repeats (review 3c I2). Large bytes travel by claim instead.
        raise ApiError(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"This request is {size} bytes; at most {MAX_REQUEST_BYTES} are accepted here.",
        )
    key = operation_key(kind.name, subject, body, idempotency_key or uuid.uuid4().hex)
    recorded = await ops.store.find(key)
    if recorded is not None:
        return _answer(recorded, response, repeated=True)
    workflow_id = f"op-{kind.name}-{key}"
    # A re-send can arrive before the first one's record is written: it follows that
    # operation, whatever the route's refusal would say now (review 1130 2).
    if before_start is not None and not (
        idempotency_key is not None and await _running(ops, workflow_id)
    ):
        await before_start()
    arg = OperationInput(
        kind=kind.name,
        subject=subject,
        key=key,
        request=body,
        run_attempts=kind.run_attempts,
        run_timeout_s=kind.run_timeout.total_seconds() if kind.run_timeout else None,
        search_attributes=ops.search_attributes,
        author=_author(),
        idempotency_key=idempotency_key,
    )
    try:
        answer = await start_command(
            ops.client,
            OPERATION_WORKFLOW,
            arg,
            id=workflow_id,
            task_queue=ops.queues[kind.queue],
            update=ACCEPTED_UPDATE,
            result_type=OperationAnswer,
            reuse=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
        )
    except AlreadyClosedError:
        recorded = await ops.store.find(key)
        if recorded is None:
            # Its record was pruned while Temporal still keeps the closed execution
            # (review #1063 8): it may have been done, so this never invites a repeat.
            raise ApiError(
                status.HTTP_409_CONFLICT,
                "This request already ran, and its record has since been deleted, so it "
                f"may have been done. Check {kind.where} before sending it again.",
            ) from None
        return _answer(recorded, response, repeated=True)
    except CommandStillAcceptingError:
        # A `done` kind past the deadline: the record may exist by now (§4.2 step 4).
        recorded = await ops.store.find(key)
        if recorded is None:
            raise still_accepting() from None
        return _answer(recorded, response, repeated=False)
    except CommandClosedError:
        # Ended before it answered. A re-send reads whatever it recorded (the reconciler
        # ends a row it left running); with no record, the same request starts again.
        raise still_accepting() from None
    except (RPCError, TemporalUnavailableError):
        raise temporal_unavailable("operations") from None
    if answer.refusal is not None:
        refusal = answer.refusal
        raise _problem(
            refusal.status, refusal.detail, refusal.title, refusal.type, refusal.extensions
        )
    assert answer.operation is not None  # the Update answers one or the other
    return _answer(answer.operation, response, repeated=answer.repeated)


def operation_answer[M: BaseModel](
    result: dict[str, Any] | Operation, model: type[M]
) -> M | JSONResponse:
    """The route's own body, or 202 with the operation to follow."""
    if isinstance(result, Operation):
        return JSONResponse(result.model_dump(mode="json"), status_code=status.HTTP_202_ACCEPTED)
    return model.model_validate(result)


#: What a route that runs an operation documents beside its own answer.
OPERATION_RESPONSES: dict[int | str, dict[str, Any]] = {
    202: {"model": Operation, "description": "Still running: follow GET /operations/{id}"},
    413: {
        "description": f"The request, less what goes by claim, is past {MAX_REQUEST_BYTES} "
        "bytes; nothing was started"
    },
}


@router.get("/{operation_id}", responses={404: {"description": "No such operation"}})
async def get_operation(operation_id: OperationIdPath, ops: OperationsDep) -> Operation:
    """One operation (§4.2 "Our record"): follow a 202 here until it is not ``running``."""
    op = await ops.store.get(operation_id)
    if op is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"there is no operation {operation_id}")
    return op
