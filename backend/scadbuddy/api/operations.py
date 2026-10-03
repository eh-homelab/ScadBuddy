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

import logging
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Header, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from temporalio.common import WorkflowIDReusePolicy
from temporalio.service import RPCError

from scadbuddy.api.deps import OperationCommands, OperationsDep
from scadbuddy.core.problems import ApiError
from scadbuddy.operations.kinds import OperationKind, operation_key
from scadbuddy.operations.store import Operation
from scadbuddy.workflows.commands import (
    RETRY_AFTER_SECONDS,
    AlreadyClosedError,
    CommandStillAcceptingError,
    TemporalUnavailableError,
    start_command,
)
from scadbuddy.workflows.operation_models import (
    OPERATION_WORKFLOW,
    OperationAnswer,
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


def _answer(op: Operation, response: Response, *, repeated: bool) -> dict[str, Any] | Operation:
    """The route's answer for a recorded operation: its body, its problem, or a 202."""
    if op.status == "succeeded":
        return op.result or {}
    if op.status == "failed":
        assert op.error is not None  # a failed operation records its problem
        error = op.error
        raise ApiError(
            error.status, error.detail, title=error.title, type_=error.type, **error.extensions
        )
    response.status_code = status.HTTP_202_ACCEPTED
    return op.model_copy(update={"repeated": repeated})


async def run_operation(
    ops: OperationCommands,
    response: Response,
    *,
    kind: OperationKind,
    subject: str,
    request: BaseModel | dict[str, Any],
    idempotency_key: str | None,
) -> dict[str, Any] | Operation:
    """Run ``kind`` as an operation; its result body, or 202 with the ``Operation``.
    A refusal or a recorded failure is raised as the problem the route answers with."""
    body = request.model_dump(mode="json") if isinstance(request, BaseModel) else request
    key = operation_key(kind.name, subject, body, idempotency_key or uuid.uuid4().hex)
    recorded = await ops.store.find(key)
    if recorded is not None:
        return _answer(recorded, response, repeated=True)
    arg = OperationInput(
        kind=kind.name,
        subject=subject,
        key=key,
        request=body,
        run_attempts=kind.run_attempts,
        run_timeout_s=kind.run_timeout.total_seconds() if kind.run_timeout else None,
        search_attributes=ops.search_attributes,
    )
    try:
        answer = await start_command(
            ops.client,
            OPERATION_WORKFLOW,
            arg,
            id=f"op-{kind.name}-{key}",
            task_queue=ops.queues[kind.queue],
            update=ACCEPTED_UPDATE,
            result_type=OperationAnswer,
            reuse=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
        )
    except AlreadyClosedError:
        recorded = await ops.store.find(key)
        if recorded is None:
            raise ApiError(
                status.HTTP_409_CONFLICT,
                "This request's operation has ended and left no record. Try again.",
            ) from None
        return _answer(recorded, response, repeated=True)
    except CommandStillAcceptingError:
        # A `done` kind past the deadline: the record may exist by now (§4.2 step 4).
        recorded = await ops.store.find(key)
        if recorded is None:
            raise still_accepting() from None
        return _answer(recorded, response, repeated=False)
    except (RPCError, TemporalUnavailableError):
        raise temporal_unavailable("operations") from None
    if answer.refusal is not None:
        refusal = answer.refusal
        raise ApiError(
            refusal.status,
            refusal.detail,
            title=refusal.title,
            type_=refusal.type,
            **refusal.extensions,
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
    202: {"model": Operation, "description": "Still running: follow GET /operations/{id}"}
}


@router.get("/{operation_id}", responses={404: {"description": "No such operation"}})
async def get_operation(operation_id: str, ops: OperationsDep) -> Operation:
    """One operation (§4.2 "Our record"): follow a 202 here until it is not ``running``."""
    op = await ops.store.get(operation_id)
    if op is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"there is no operation {operation_id}")
    return op
