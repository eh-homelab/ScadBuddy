"""The gate's pending input and responses behind CRUD routes, beyond its writers (#5299 step
10): under ``/api/v1/internal/crud/agent/``, for the ``agent`` credential only (`crud_router`).
Each route is one read or write of the agent's ``gate/projection.ts`` `PendingProjection`,
``gate/sweep.ts``, ``sessions/turnHost.ts`` or ``temporal/toolActivities.ts``
`PgApprovalRecords`; the SQL is ``agent_data/pending_input.py``. The agent calls them once
``…/areas`` lists ``pending-input``, and its own SQL until then (D9).

Area ``pending-input``:

- ``POST …/pending-input/entries``: the open entries a filter names (at most 500);
- ``POST …/pending-input/in-run`` and ``…/decided``: which of the ids given are a flow run's
  entries, or have a response row;
- ``GET …/pending-input/tab-wait?session_id=…``: whether the session waits for its tab;
- ``GET …/pending-input/recorded?request_id=…``: the response row (``recorded: null`` while
  open);
- ``GET …/pending-input/park-trace?request_id=…`` and ``POST …/decision-trace``: an open entry's
  trace, and the decision's span written on it;
- ``POST …/pending-input/trace``: a durable turn's parked call's span on its entry, only while
  the turn's placement (``attempt``, ``turn_id``) holds the session, checked ``FOR SHARE`` in
  the write's transaction (the turn fence);
- ``GET …/pending-input/session?session_id=…``: who owns and who started a session;
- ``GET …/pending-input/sweep/runs``, ``POST …/sweep/checked`` and ``…/orphans/cancel``: the
  orphan sweep, each entry it cancels one transaction with its events, the session's ``idle``
  and an approval's audit row, the batches it appended answered as ``logged``;
- ``GET …/pending-input/approval?request_id=…&input_hash=…``: the responder of the request's
  approval given for this very call (#2088), or null.

A request id travels in the body or the query, never the path. A malformed session or turn id
given to ``…/session`` or ``…/trace`` names nothing (``null`` or ``false``, never a 500); the
routes that take a session id as ``UUID_PATTERN`` (``tab-wait``, ``entries``' filter and
``orphans/cancel``) refuse a malformed one with 422, and the agent sends none of them one.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import HTTPException, Query
from pydantic import BaseModel, Field

from scadbuddy.agent_data import pending_input
from scadbuddy.api.crud_agent_gate import (
    NAME_MAX,
    REQUEST_ID_MAX,
    UUID_PATTERN,
    Logged,
    Name,
    RequestId,
    SessionId,
    Text,
    _Body,
    _in_conn,
    _logged,
)
from scadbuddy.api.deps import StateDep
from scadbuddy.crud.guard import crud_router

router = crud_router("agent")

#: The most request ids one ``in-run`` or ``decided`` read takes.
IDS_MAX = 1000
#: The longest W3C traceparent (55 characters) with room for a future version's.
TRACEPARENT_MAX = 256
#: The longest sweep age, in seconds (a day).
AGE_MAX_S = 86_400

RequestIdQuery = Annotated[str, Query(min_length=1, max_length=REQUEST_ID_MAX)]
SessionIdQuery = Annotated[str, Query(pattern=UUID_PATTERN)]
Traceparent = Annotated[str, Field(min_length=1, max_length=TRACEPARENT_MAX)]


class Owner(_Body):
    kind: Name
    id: Name


class Filter(_Body):
    """The agent's ``ProjectionFilter``."""

    session_id: SessionId | None = None
    owned_by: Owner | None = None
    all_approvals: bool = False
    own_answers: bool = False


class Entry(BaseModel):
    """A row of ``ai_pending_input`` as ``PendingProjection.entries`` reads it."""

    request_id: str
    kind: str
    session_id: str
    tool: str
    summary: str
    input_hash: str | None
    prompt: str
    requested_by: dict[str, Any] | None
    responders: list[str]
    attention: dict[str, Any] | None
    flow_run_id: str | None
    hop: str | None
    outside_reason: str | None
    created_at: datetime
    expires_at: datetime
    expiring: bool


class Entries(BaseModel):
    entries: list[Entry]


@router.post("/pending-input/entries", name="crud_agent_pending_entries")
async def entries(body: Filter, state: StateDep) -> Entries:
    owner = None if body.owned_by is None else (body.owned_by.kind, body.owned_by.id)
    rows = await _in_conn(
        state,
        lambda conn: pending_input.entries(
            conn,
            session_id=body.session_id,
            owner=owner,
            all_approvals=body.all_approvals,
            own_answers=body.own_answers,
        ),
    )
    return Entries(entries=[Entry.model_validate(r) for r in rows])


class Ids(_Body):
    request_ids: Annotated[list[RequestId], Field(max_length=IDS_MAX)]


class Found(BaseModel):
    request_ids: list[str]


@router.post("/pending-input/in-run", name="crud_agent_pending_in_run")
async def in_run(body: Ids, state: StateDep) -> Found:
    ids = body.request_ids
    found = [] if not ids else await _in_conn(state, lambda c: pending_input.in_run(c, ids))
    return Found(request_ids=found)


@router.post("/pending-input/decided", name="crud_agent_pending_decided")
async def decided(body: Ids, state: StateDep) -> Found:
    ids = body.request_ids
    found = [] if not ids else await _in_conn(state, lambda c: pending_input.decided(c, ids))
    return Found(request_ids=found)


class TabWait(BaseModel):
    waits: bool


@router.get("/pending-input/tab-wait", name="crud_agent_pending_tab_wait")
async def tab_wait(state: StateDep, session_id: SessionIdQuery) -> TabWait:
    waits = await _in_conn(state, lambda conn: pending_input.waits_for_tab(conn, session_id))
    return TabWait(waits=waits)


class Recorded(BaseModel):
    kind: Literal["approval", "answer"]
    outcome: str
    response: Any
    response_hash: str | None
    responder: dict[str, Any]
    reason: str | None
    decision_traceparent: str | None


class RecordedAnswer(BaseModel):
    recorded: Recorded | None


@router.get("/pending-input/recorded", name="crud_agent_pending_recorded")
async def recorded(state: StateDep, request_id: RequestIdQuery) -> RecordedAnswer:
    row = await _in_conn(state, lambda conn: pending_input.recorded(conn, request_id))
    return RecordedAnswer(recorded=None if row is None else Recorded.model_validate(row))


class ParkTrace(BaseModel):
    tool: str
    created_at: datetime
    traceparent: str | None


class ParkTraceAnswer(BaseModel):
    trace: ParkTrace | None


@router.get("/pending-input/park-trace", name="crud_agent_pending_park_trace")
async def park_trace(state: StateDep, request_id: RequestIdQuery) -> ParkTraceAnswer:
    row = await _in_conn(state, lambda conn: pending_input.park_trace(conn, request_id))
    return ParkTraceAnswer(trace=None if row is None else ParkTrace.model_validate(row))


class DecisionTrace(_Body):
    request_id: RequestId
    traceparent: Traceparent


class Done(BaseModel):
    done: bool


@router.post("/pending-input/decision-trace", name="crud_agent_pending_decision_trace")
async def decision_trace(body: DecisionTrace, state: StateDep) -> Done:
    def run(conn: Any) -> bool:
        with conn.transaction():
            pending_input.mark_decision(conn, body.request_id, body.traceparent)
        return True

    return Done(done=await _in_conn(state, run))


class ParkMark(_Body):
    """A durable turn's parked call's span, fenced by the turn's placement."""

    request_id: RequestId
    traceparent: Traceparent
    session_id: Annotated[str, Field(min_length=1, max_length=NAME_MAX)]
    attempt: Annotated[str, Field(min_length=1, max_length=NAME_MAX)]
    turn_id: Annotated[str, Field(min_length=1, max_length=NAME_MAX)]


class Marked(BaseModel):
    #: False when the placement no longer holds the session or the entry is not its own.
    marked: bool


@router.post("/pending-input/trace", name="crud_agent_pending_trace")
async def park_mark(body: ParkMark, state: StateDep) -> Marked:
    marked = await _in_conn(
        state,
        lambda conn: pending_input.mark_park(
            conn,
            body.request_id,
            body.traceparent,
            session_id=body.session_id,
            attempt=body.attempt,
            turn_id=body.turn_id,
        ),
    )
    return Marked(marked=marked)


class GateSession(BaseModel):
    owner_kind: str
    owner_id: str
    owner_label: str
    creator_kind: str
    creator_id: str


class SessionAnswer(BaseModel):
    session: GateSession | None


@router.get("/pending-input/session", name="crud_agent_pending_session")
async def session(
    state: StateDep,
    session_id: Annotated[str, Query(min_length=1, max_length=NAME_MAX)],
) -> SessionAnswer:
    row = await _in_conn(state, lambda conn: pending_input.session(conn, session_id))
    return SessionAnswer(session=None if row is None else GateSession.model_validate(row))


class Run(BaseModel):
    workflow_id: str
    workflow_run_id: str


class Runs(BaseModel):
    runs: list[Run]


@router.get("/pending-input/sweep/runs", name="crud_agent_pending_sweep_runs")
async def sweep_runs(
    state: StateDep,
    min_age_s: Annotated[int, Query(ge=0, le=AGE_MAX_S)],
    recheck_s: Annotated[int, Query(ge=0, le=AGE_MAX_S)],
) -> Runs:
    rows = await _in_conn(state, lambda conn: pending_input.sweep_runs(conn, min_age_s, recheck_s))
    return Runs(runs=[Run.model_validate(r) for r in rows])


class RunRef(_Body):
    workflow_id: Name
    run_id: Name


@router.post("/pending-input/sweep/checked", name="crud_agent_pending_sweep_checked")
async def sweep_checked(body: RunRef, state: StateDep) -> Done:
    def run(conn: Any) -> bool:
        with conn.transaction():
            pending_input.run_checked(conn, body.workflow_id, body.run_id)
        return True

    return Done(done=await _in_conn(state, run))


class Orphans(_Body):
    """One ended run's entries, or a session's: exactly one of ``run`` and ``session_id``."""

    run: RunRef | None = None
    session_id: SessionId | None = None
    reason: Text = pending_input.ORPHAN_REASON


class Cancelled(BaseModel):
    removed: int
    logged: list[Logged]


@router.post("/pending-input/orphans/cancel", name="crud_agent_pending_orphans_cancel")
async def orphans_cancel(body: Orphans, state: StateDep) -> Cancelled:
    if (body.run is None) == (body.session_id is None):
        raise HTTPException(422, "name exactly one of run and session_id")
    run = None if body.run is None else (body.run.workflow_id, body.run.run_id)
    removed, logged = await _in_conn(
        state,
        lambda conn: pending_input.cancel_orphans(
            conn, run=run, session_id=body.session_id, reason=body.reason
        ),
    )
    return Cancelled(removed=removed, logged=_logged(logged))


class ApprovalAnswer(BaseModel):
    #: The approval's responder, or null when no approval was given for this call.
    responder: dict[str, Any] | None


@router.get("/pending-input/approval", name="crud_agent_pending_approval")
async def approval(
    state: StateDep,
    request_id: RequestIdQuery,
    input_hash: Annotated[str, Query(min_length=1, max_length=NAME_MAX)],
) -> ApprovalAnswer:
    responder = await _in_conn(
        state, lambda conn: pending_input.approval(conn, request_id, input_hash)
    )
    return ApprovalAnswer(responder=responder)
