"""Starting and reading operations (#1053, spec 2026-10-01 §4.2): the one helper every
route that runs a generic command calls, and ``GET /operations/{id}``.

A route's request is keyed by its kind, subject, body and the client's
``Idempotency-Key`` (one per deliberate press). The key is required: without one a
retry after a lost answer could not be told from a second press, so it would upload,
enqueue or create again (#1143); a keyless request is refused with 428. Our record is
read first: a repeat answers from it and starts nothing. Otherwise ``Operation`` is
started (or attached to) with update-with-start. Every kind is ``done``: the answer is
the route's body once the effect ended, or 202 with the operation when it has not
within the deadline.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Header, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from temporalio.common import WorkflowIDReusePolicy

from scadbuddy.api.deps import OperationIdPath
from scadbuddy.core.authorship import current_author
from scadbuddy.core.problems import PROBLEM_MEDIA_TYPE, ApiError, Problem
from scadbuddy.operations.claims import ClaimStore, Held
from scadbuddy.operations.component import OperationCommands, OperationsDep
from scadbuddy.operations.kinds import OperationKind, operation_key
from scadbuddy.operations.store import Operation, OperationAccepted
from scadbuddy.workflows.commands import (
    RETRY_AFTER_SECONDS,
    AlreadyClosedError,
    CommandClosedError,
    CommandStillAcceptingError,
    TemporalBusyError,
    TemporalRefusedError,
    TemporalUnavailableError,
    TemporalUnreachableError,
    start_command,
)
from scadbuddy.workflows.operation_models import (
    OPERATION_WORKFLOW,
    OperationAnswer,
    OperationAuthor,
    OperationInput,
)
from scadbuddy.workflows.print_models import ACCEPTED_UPDATE
from scadbuddy.workflows.problems import OPERATION_UNEXPECTED_DETAIL

logger = logging.getLogger(__name__)

#: Problem ``type``s for a command the route could not hand to Temporal (#1052, #1053).
STILL_ACCEPTING_PROBLEM = "https://scadbuddy.dev/problems/command-still-accepting"
TEMPORAL_UNAVAILABLE_PROBLEM = "https://scadbuddy.dev/problems/temporal-unavailable"
#: Temporal answered and refused (a denied permission, a failed authentication):
#: sending the same request again will not help until it is fixed (review #1316 (2) 7).
TEMPORAL_REFUSED_PROBLEM = "https://scadbuddy.dev/problems/temporal-refused"
#: A request that already ran and whose record was pruned: it may have been done.
RECORD_GONE_PROBLEM = "https://scadbuddy.dev/problems/operation-record-gone"
#: A write sent without an ``Idempotency-Key`` (#1143): nothing was done.
KEY_REQUIRED_PROBLEM = "https://scadbuddy.dev/problems/idempotency-key-required"
KEY_REQUIRED_DETAIL = (
    "This request needs an Idempotency-Key header: one new value per deliberate press,"
    " kept when the same request is sent again. Nothing was done."
)

#: The problem extension on every ``temporal-unavailable`` and ``temporal-refused``
#: answer to a command: whether its start may have reached Temporal (review #1316 (13)
#: 1a). False only when the first connect failed, so no request was written; true, a
#: client re-sends the same request (the same ``request_id`` or ``Idempotency-Key``)
#: to follow it, and never one with a new key, which would do it a second time.
MAY_HAVE_STARTED = "may_have_started"

#: ``TemporalUnreachableError``, ``TemporalUnavailableError``, ``TemporalBusyError``
#: and ``TemporalRefusedError`` for an operation; the print-run routes word their own.
OPERATION_UNREACHABLE_DETAIL = (
    "ScadBuddy cannot reach Temporal, where operations run. Nothing was done; try again shortly."
)
_FOLLOW = (
    " Send the same request again shortly: with the same Idempotency-Key it follows the"
    " operation if it started."
)
OPERATION_DOWN_DETAIL = "ScadBuddy cannot reach Temporal, where operations run." + _FOLLOW
OPERATION_BUSY_DETAIL = "Temporal could not start this right now." + _FOLLOW
OPERATION_REFUSED_DETAIL = (
    "Temporal refused to start this; see ScadBuddy's logs. Send the same request again,"
    " with the same Idempotency-Key, to follow it if it started."
)
OPERATION_STILL_CHECKING_DETAIL = (
    "ScadBuddy is still checking this request. Send it again to follow it."
)

#: Inline: a ``model`` would be documented as ``application/json``, the route's own type.
PROBLEM_SCHEMA = Problem.model_json_schema()


def temporal_problems(
    *,
    refused: str,
    unreachable: str,
    down: str,
    busy: str,
    still_checking: str,
    unexpected: str,
    other: str,
) -> dict[int | str, dict[str, Any]]:
    """The 500, 503 and other problems a command route answers, quoting the details it
    sends (review #1316 (10) 3, (11) 1, (12) 2, (13) 2a)."""
    content = {PROBLEM_MEDIA_TYPE: {"schema": PROBLEM_SCHEMA}}
    started = (
        f"`{MAY_HAVE_STARTED}` says whether the start may have reached Temporal: true, send"
        " the same request again (never one with a new key) to follow it."
    )
    return {
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "content": content,
            "description": f"`{TEMPORAL_REFUSED_PROBLEM}`, with `{MAY_HAVE_STARTED}` true:"
            f' "{refused}" Or `about:blank`: it failed unexpectedly ("{unexpected}"), or'
            " ScadBuddy did.",
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "content": content,
            "description": f"`{TEMPORAL_UNAVAILABLE_PROBLEM}`, with `Retry-After`: Temporal"
            " did not answer or could not start it right now, one of: "
            + "; ".join(f'"{detail}"' for detail in (unreachable, down, busy))
            + f" (only the first has `{MAY_HAVE_STARTED}` false). {started} Or"
            f' `{STILL_ACCEPTING_PROBLEM}`, with `Retry-After`: "{still_checking}"',
        },
        "default": {"content": content, "description": other},
    }


def temporal_unavailable(what: str, detail: str, *, may_have_started: bool | None) -> ApiError:
    """A ``TemporalUnavailableError`` (or a subclass): worth sending again.
    ``may_have_started`` ``None`` leaves the extension out, for a call that starts
    nothing."""
    logger.warning("could not reach Temporal for %s", what, exc_info=True)
    extensions: dict[str, Any] = (
        {} if may_have_started is None else {MAY_HAVE_STARTED: may_have_started}
    )
    return ApiError(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        detail,
        type_=TEMPORAL_UNAVAILABLE_PROBLEM,
        headers={"Retry-After": "5"},
        **extensions,
    )


def temporal_refused(what: str, detail: str, *, may_have_started: bool | None) -> ApiError:
    """``TemporalRefusedError``, read as the reconcilers read it (review #1316 (12) 1).
    Its gRPC message stays in the log, out of the response."""
    logger.error("Temporal refused %s", what, exc_info=True)
    extensions: dict[str, Any] = (
        {} if may_have_started is None else {MAY_HAVE_STARTED: may_have_started}
    )
    return ApiError(
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail,
        type_=TEMPORAL_REFUSED_PROBLEM,
        **extensions,
    )


#: The client's key for one deliberate press (§4.2 step 1).
IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key", max_length=128)]

router = APIRouter(prefix="/operations", tags=["operations"])


def still_accepting() -> ApiError:
    return ApiError(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        OPERATION_STILL_CHECKING_DETAIL,
        type_=STILL_ACCEPTING_PROBLEM,
        headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
    )


def _unavailable_detail(error: TemporalUnavailableError) -> str:
    """As the print-run routes word it: only a failed first connect wrote nothing; any
    other may follow a persisted start (review #1316 (9) 1a). "Cannot reach" only when
    it did not answer (review #1316 (9) 1b)."""
    if isinstance(error, TemporalUnreachableError):
        return OPERATION_UNREACHABLE_DETAIL
    if isinstance(error, TemporalBusyError):
        return OPERATION_BUSY_DETAIL
    return OPERATION_DOWN_DETAIL


def _problem(
    status_code: int, detail: str, title: str | None, type_: str, extensions: dict[str, Any]
) -> ApiError:
    """A recorded problem as the route raised it. Only its body is recorded, so a
    ``retry_after`` it carries is its Retry-After header again."""
    retry_after = extensions.get("retry_after")
    headers = {"Retry-After": str(retry_after)} if isinstance(retry_after, int) else None
    return ApiError(status_code, detail, title=title, type_=type_, headers=headers, **extensions)


def _answer(
    op: Operation, response: Response, *, repeated: bool
) -> dict[str, Any] | OperationAccepted:
    """The route's answer for a recorded operation: its body, its problem, or a 202."""
    if op.status == "succeeded":
        return op.result or {}
    if op.status == "failed":
        assert op.error is not None  # a failed operation records its problem
        error = op.error
        raise _problem(error.status, error.detail, error.title, error.type, error.extensions)
    response.status_code = status.HTTP_202_ACCEPTED
    return OperationAccepted(**op.model_dump(), repeated=repeated)


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
) -> dict[str, Any] | OperationAccepted:
    """Run ``kind`` as an operation; its result body, or 202 with the operation.
    A refusal or a recorded failure is raised as the problem the route answers with.
    ``claimed`` is dropped once the answer is final: not on a 202 or a 503, after which
    the operation may still run."""
    try:
        result = await _run_operation(
            ops,
            response,
            kind=kind,
            subject=subject,
            request=request,
            idempotency_key=idempotency_key,
        )
    except ApiError as error:
        if claimed is not None and error.status != status.HTTP_503_SERVICE_UNAVAILABLE:
            await _release(ops, claimed)
        raise
    if claimed is not None and not (
        isinstance(result, OperationAccepted) and result.status == "running"
    ):
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
) -> dict[str, Any] | OperationAccepted:
    if not idempotency_key:
        raise ApiError(
            status.HTTP_428_PRECONDITION_REQUIRED, KEY_REQUIRED_DETAIL, type_=KEY_REQUIRED_PROBLEM
        )
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
    key = operation_key(kind.name, subject, body, idempotency_key)
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
        author=_author(),
        idempotency_key=idempotency_key,
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
            # Its record was pruned while Temporal still keeps the closed execution
            # (review #1063 8): it may have been done, so this never invites a repeat.
            raise ApiError(
                status.HTTP_409_CONFLICT,
                "This request already ran, and its record has since been deleted, so it "
                f"may have been done. Check {kind.where} before sending it again.",
                type_=RECORD_GONE_PROBLEM,
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
    except TemporalRefusedError:
        raise temporal_refused(
            "to start an operation", OPERATION_REFUSED_DETAIL, may_have_started=True
        ) from None
    except TemporalUnavailableError as error:
        raise temporal_unavailable(
            "an operation",
            _unavailable_detail(error),
            may_have_started=not isinstance(error, TemporalUnreachableError),
        ) from None
    if answer.refusal is not None:
        refusal = answer.refusal
        raise _problem(
            refusal.status, refusal.detail, refusal.title, refusal.type, refusal.extensions
        )
    assert answer.operation is not None  # the Update answers one or the other
    return _answer(answer.operation, response, repeated=answer.repeated)


def operation_answer[M: BaseModel](
    result: dict[str, Any] | OperationAccepted, model: type[M]
) -> M | JSONResponse:
    """The route's own body, or 202 with the operation to follow."""
    if isinstance(result, OperationAccepted):
        return JSONResponse(result.model_dump(mode="json"), status_code=status.HTTP_202_ACCEPTED)
    return model.model_validate(result)


#: What a route that runs an operation documents beside its own answer.
OPERATION_RESPONSES: dict[int | str, dict[str, Any]] = {
    202: {"model": OperationAccepted, "description": "Still running: follow GET /operations/{id}"},
    413: {
        "description": f"The request, less what goes by claim, is past {MAX_REQUEST_BYTES} "
        "bytes; nothing was started"
    },
    428: {
        "content": {PROBLEM_MEDIA_TYPE: {"schema": PROBLEM_SCHEMA}},
        "description": f'`{KEY_REQUIRED_PROBLEM}`: "{KEY_REQUIRED_DETAIL}"',
    },
    **temporal_problems(
        refused=OPERATION_REFUSED_DETAIL,
        unreachable=OPERATION_UNREACHABLE_DETAIL,
        down=OPERATION_DOWN_DETAIL,
        busy=OPERATION_BUSY_DETAIL,
        still_checking=OPERATION_STILL_CHECKING_DETAIL,
        unexpected=OPERATION_UNEXPECTED_DETAIL,
        other="Any other problem: the operation's check or effect refused or failed, with its"
        f" own status and type, or `{RECORD_GONE_PROBLEM}` (409): it already ran and its"
        " record has expired, so it may have been done.",
    ),
}


@router.get("/{operation_id}", responses={404: {"description": "No such operation"}})
async def get_operation(operation_id: OperationIdPath, ops: OperationsDep) -> Operation:
    """One operation (§4.2 "Our record"): follow a 202 here until it is not ``running``."""
    op = await ops.store.get(operation_id)
    if op is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"there is no operation {operation_id}")
    return op
