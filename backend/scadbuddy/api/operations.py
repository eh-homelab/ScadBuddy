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
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Header, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from temporalio.client import WorkflowExecutionStatus
from temporalio.common import WorkflowIDReusePolicy
from temporalio.service import RPCError, RPCStatusCode

from scadbuddy.api.accepting import (
    AcceptingAnswer,
    CommandAccepting,
    accepting,
    respond_async_request,
)
from scadbuddy.api.deps import OperationIdPath
from scadbuddy.core.authorship import current_author
from scadbuddy.core.problems import PROBLEM_MEDIA_TYPE, ApiError, Problem
from scadbuddy.operations.claims import ClaimStore, Held
from scadbuddy.operations.component import OperationCommands, OperationsDep
from scadbuddy.operations.kinds import OperationKind, operation_key
from scadbuddy.operations.store import Operation, OperationAccepted
from scadbuddy.render.inputs import nul_at
from scadbuddy.workflows.commands import (
    FAST_ACCEPT,
    RETRY_AFTER_SECONDS,
    AlreadyClosedError,
    CommandClosedError,
    CommandStillAcceptingError,
    TemporalBusyError,
    TemporalRefusedError,
    TemporalUnavailableError,
    TemporalUnreachableError,
    follow_update,
    start_command,
    temporal_failure,
)
from scadbuddy.workflows.operation_models import (
    OPERATION_WORKFLOW,
    PRELUDE_REQUESTED,
    OperationAnswer,
    OperationAuthor,
    OperationInput,
    PreludeStep,
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
#: Each is read by a person, so none names Temporal or a header: the problem's type and
#: ``may_have_started`` carry that for a client (:func:`temporal_problems`), and a retry
#: from the UI keeps its key, so it follows a start rather than doing it twice.
OPERATION_UNREACHABLE_DETAIL = (
    "ScadBuddy's background service is not answering. Nothing was done; try again shortly."
)
RETRY_FOLLOWS = (
    " Try again: a retry picks this request up if it did start, rather than doing it twice."
)
OPERATION_DOWN_DETAIL = "ScadBuddy's background service did not answer." + RETRY_FOLLOWS
OPERATION_BUSY_DETAIL = "ScadBuddy's background service is busy right now." + RETRY_FOLLOWS
OPERATION_REFUSED_DETAIL = (
    "ScadBuddy could not start this; its server log says why." + RETRY_FOLLOWS
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


#: The problems after which the operation may still run, so its claims are kept, as
#: they are after any whose ``MAY_HAVE_STARTED`` is true (a refused start, #1316).
_MAY_STILL_RUN = frozenset({STILL_ACCEPTING_PROBLEM, TEMPORAL_UNAVAILABLE_PROBLEM})


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


#: Where a 202's operation is followed as activity (spec 2026-10-09 §6).
ACTIVITY_LOCATION = "Activity-Location"


def activity_location(operation_id: str) -> str:
    return f"/api/v1/activity/operation:{operation_id}"


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
    response.headers[ACTIVITY_LOCATION] = activity_location(op.id)
    return OperationAccepted(**op.model_dump(), repeated=repeated)


#: The most an operation's request may carry inline: well under Temporal's 512 KB
#: payload warning, since it is repeated in the start, check, insert and run inputs.
MAX_REQUEST_BYTES = 128 * 1024


def _author() -> OperationAuthor | None:
    """The request's agent author, for the run's commits (#252) and its activity row."""
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
    return await ops.store.find(
        operation_key(kind.name, subject, _body(request), idempotency_key, kind.unkeyed)
    )


async def _running(ops: OperationCommands, workflow_id: str) -> bool:
    """Whether the operation's workflow is running: not when there is none, or it closed."""
    try:
        described = await ops.client.get_workflow_handle(workflow_id).describe()
    except RPCError as error:
        if error.status == RPCStatusCode.NOT_FOUND:
            return False
        # Read as a start's failure is (review #1316 (12) 1); this describe starts nothing.
        failure = temporal_failure(error, workflow_id)
        if isinstance(failure, TemporalUnavailableError):
            raise temporal_unavailable(
                "an operation", _unavailable_detail(failure), may_have_started=None
            ) from None
        raise temporal_refused(
            "to describe an operation", OPERATION_REFUSED_DETAIL, may_have_started=None
        ) from None
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
) -> dict[str, Any] | OperationAccepted:
    """Run ``kind`` as an operation; its result body, or 202 with the operation.
    A refusal or a recorded failure is raised as the problem the route answers with.
    ``claimed`` is dropped once the answer is final: not on a 202, on a 503 that says
    the operation may still run (a recorded 503 is final, review #1194 1.1), or on a
    refused start that may have reached Temporal.
    ``before_start`` is a route's own refusal, made only when no record answers and the
    same request is not still running: a repeat is its first answer whatever has changed
    since (§4.2). Its refusal, 503 included, releases ``claimed`` too: nothing started
    (review 3e final I1)."""
    refused = False

    async def refuse() -> None:
        nonlocal refused
        assert before_start is not None
        try:
            await before_start()
        except ApiError:
            refused = True
            raise

    try:
        result = await _run_operation(
            ops,
            response,
            kind=kind,
            subject=subject,
            request=request,
            idempotency_key=idempotency_key,
            before_start=refuse if before_start is not None else None,
        )
    except ApiError as error:
        may_still_run = (
            error.type in _MAY_STILL_RUN or error.extensions.get(MAY_HAVE_STARTED) is True
        )
        if claimed is not None and (refused or not may_still_run):
            await _release(ops, claimed)
        raise
    if claimed is not None and not (
        isinstance(result, OperationAccepted) and result.status == "running"
    ):
        await _release(ops, claimed)
    return result


def _prelude(
    ops: OperationCommands, kind: OperationKind, body: dict[str, Any]
) -> PreludeStep | None:
    """The step on another queue this request needs first, if any (#1060)."""
    if kind.prelude is None or not kind.needs_prelude(body):
        return None
    first = ops.kinds[kind.prelude]
    return PreludeStep(
        kind=first.name,
        task_queue=ops.queues[first.queue],
        run_attempts=first.run_attempts,
        where=first.where,
    )


async def _run_operation(
    ops: OperationCommands,
    response: Response,
    *,
    kind: OperationKind,
    subject: str,
    request: BaseModel | dict[str, Any],
    idempotency_key: str | None,
    before_start: Callable[[], Awaitable[None]] | None,
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
    nul = nul_at(body, "request")
    if nul is not None:
        # The record's jsonb holds none: its insert would fail, as a 503 every retry
        # repeats (#965).
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, f"{nul} contains a NUL byte")
    key = operation_key(kind.name, subject, body, idempotency_key, kind.unkeyed)
    recorded = await ops.store.find(key)
    if recorded is not None:
        return _answer(recorded, response, repeated=True)
    workflow_id = f"op-{kind.name}-{key}"
    # #2794: a client that follows a 202 itself waits only briefly for the Update, named
    # by its key so the follow finds it.
    # Only for a key that can sit in the follow URL's path (review #2836): `url_for` refuses
    # a `/`, which would be a 500 after the operation had started. A key it cannot carry keeps the
    # 503 re-send. Claims a 202 leaves held are not released on the follow path (it has no
    # request's claims): the claims sweep (`sweep_claims`, main.py) releases them, as any
    # claim whose request never came back.
    http = respond_async_request() if _URL_SAFE_KEY.fullmatch(idempotency_key) else None
    # Only when asked: a test's kwdefault deadline then still governs the rest.
    fast: dict[str, Any] = {"deadline": FAST_ACCEPT, "update_id": idempotency_key} if http else {}
    # A re-send can arrive before the first one's record is written: it follows that
    # operation, whatever the route's refusal would say now (review 1130 2).
    if before_start is not None and not await _running(ops, workflow_id):
        await before_start()
    prelude = _prelude(ops, kind, body)
    arg = OperationInput(
        kind=kind.name,
        subject=subject,
        key=key,
        request=body if prelude is None else {**body, PRELUDE_REQUESTED: prelude.kind},
        run_attempts=kind.run_attempts,
        run_timeout_s=kind.run_timeout.total_seconds() if kind.run_timeout else None,
        search_attributes=ops.search_attributes,
        author=_author(),
        prelude=prelude,
        idempotency_key=idempotency_key,
        answer_running=kind.answers_running,
        where=kind.where,
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
            **fast,
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
        if recorded is not None:
            return _answer(recorded, response, repeated=False)
        if http is not None:
            raise AcceptingAnswer(accepting(http, workflow_id, idempotency_key)) from None
        raise still_accepting() from None
    except CommandClosedError:
        # Ended before it answered. A re-send reads whatever it recorded (the reconciler
        # ends a row it left running); with no record, the same request starts again.
        raise still_accepting() from None
    except (TemporalRefusedError, TemporalUnavailableError) as error:
        raise _temporal_problem(error) from None
    return _outcome(answer, response)


def _temporal_problem(error: TemporalRefusedError | TemporalUnavailableError) -> ApiError:
    if isinstance(error, TemporalRefusedError):
        return temporal_refused(
            "to start an operation", OPERATION_REFUSED_DETAIL, may_have_started=True
        )
    return temporal_unavailable(
        "an operation",
        _unavailable_detail(error),
        may_have_started=not isinstance(error, TemporalUnreachableError),
    )


def _outcome(answer: OperationAnswer, response: Response) -> dict[str, Any] | OperationAccepted:
    """The route's answer for its `accepted` Update's: the operation's, or its refusal."""
    if answer.refusal is not None:
        refusal = answer.refusal
        raise _problem(
            refusal.status, refusal.detail, refusal.title, refusal.type, refusal.extensions
        )
    assert answer.operation is not None  # the Update answers one or the other
    return _answer(answer.operation, response, repeated=answer.repeated)


#: An `Idempotency-Key` the 202's follow URL can carry in its path.
#: Not `.` or `..`: a client or proxy collapses those out of a path as dot segments
#: (review #2836).
_URL_SAFE_KEY = re.compile(r"(?!\.{1,2}$)[A-Za-z0-9_.~-]+")

#: An operation's execution id (`_run_operation`): its kind, then its 64-hex key.
_OPERATION_ID = re.compile(r"op-.+-(?P<key>[0-9a-f]{64})")


async def follow_operation(
    ops: OperationCommands, response: Response, workflow_id: str, update_id: str
) -> dict[str, Any] | OperationAccepted:
    """The answer an operation route gave 202 `accepting` for (spec 2026-10-13 §5.3,
    #2794): its `accepted` Update's, as `_run_operation` answers it. A `done` kind's
    Update can outlast the wait while its record says it is running: that is its 202 to
    follow at ``GET /operations/{id}``, as the route answers it. Still nothing,
    `CommandStillAcceptingError`; an Update not known, `CommandResendError`."""
    matched = _OPERATION_ID.fullmatch(workflow_id)
    if matched is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no command {workflow_id!r} to follow")
    try:
        answer = await follow_update(
            ops.client, workflow_id, update_id, result_type=OperationAnswer
        )
    except CommandStillAcceptingError:
        recorded = await ops.store.find(matched["key"])
        if recorded is None:
            raise
        return _answer(recorded, response, repeated=False)
    except (TemporalRefusedError, TemporalUnavailableError) as error:
        raise _temporal_problem(error) from None
    return _outcome(answer, response)


def operation_answer[M: BaseModel](
    result: dict[str, Any] | OperationAccepted, model: type[M]
) -> M | JSONResponse:
    """The route's own body, or 202 with the operation to follow."""
    if isinstance(result, OperationAccepted):
        return JSONResponse(
            result.model_dump(mode="json"),
            status_code=status.HTTP_202_ACCEPTED,
            headers={ACTIVITY_LOCATION: activity_location(result.id)},
        )
    return model.model_validate(result)


#: What a route that runs an operation documents beside its own answer.
OPERATION_RESPONSES: dict[int | str, dict[str, Any]] = {
    202: {
        "model": OperationAccepted | CommandAccepting,
        "description": (
            "`OperationAccepted`: still running, follow GET /operations/{id}. With `Prefer:"
            " respond-async`, a start not yet accepted is `CommandAccepting` instead"
            " (`status: accepting`): follow its `follow_url` (POST) (review #2836)"
        ),
    },
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
