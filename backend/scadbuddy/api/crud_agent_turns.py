"""A durable turn's writes of its session's row behind CRUD routes (#5299 step 9, #6449): under
``/api/v1/internal/crud/agent/``, for the ``agent`` credential only (`crud_router`), area
``session-turns``. Each route is one method of the agent's ``sessions/turnRows.ts``
`TurnRows` and runs its SQL, a transaction as one transaction, the fences checked in it
(#5299 D2); the SQL is in ``agent_data/turns.py``.

- ``POST …/sessions/{id}/turn/send-claim``: a send's claim (``claimed: null`` when the row
  cannot take it); ``…/turn/give-back``: a refused send's claim given back with its status
  event; ``…/turn/interrupt``: a Stop's flag;
- ``POST …/sessions/{id}/turn/queueable``: the row's ``event_seq`` while the author may queue
  behind its turn;
- ``GET …/sessions/{id}/turn`` (``grace_ms``): who runs the row's turn and where;
- ``POST …/sessions/{id}/turn/placement``: a placement takes the turn (``turn_attempt``, which
  no other route writes) while the row runs it, and the transcript's mark;
- ``POST …/sessions/{id}/turn/spend``: what a placement spent, once per key;
- ``POST …/sessions/{id}/turn/finish``: the turn's end while the row runs it, with the queue's
  head claimed, in one transaction; ``…/turn/begin``: the queue's head claimed alone;
- ``POST …/sessions/{id}/turn/queue`` and ``GET`` it: the queue's ``session.queue`` event;
- ``GET …/turns/stale`` and ``POST …/sessions/{id}/turn/end-lost``: the running sweep's page,
  and a lost turn's end while the row is as the sweep read it.

Appended events are answered as ``logged`` (their JSON texts and seqs) for the agent's
followers. The session id is the only id in a path, a UUID checked by its pattern; a turn id
is a UUID too (``durable_turn_id``), and one that is not is refused 422 ``turn_id``, as the
agent's SQL cast throws, and is read lowercase. Every body is bounded, and a write that
waits past ``turns.LOCK_TIMEOUT_MS`` for its row's lock is refused 503 ``row_busy``, never
committed late."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Annotated, Any, Literal

import psycopg
from fastapi import Depends, Path, Query, Request, status
from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from scadbuddy.agent_data import events as event_log
from scadbuddy.agent_data import turns
from scadbuddy.api.deps import StateDep
from scadbuddy.core.problems import ApiError
from scadbuddy.crud.guard import crud_router

router = crud_router("agent")

UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
#: A turn id: a UUID (``durable_turn_id``), any case, as Postgres reads one.
TURN_ID_PATTERN = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
#: The longest owner id (``crud_agent_sessions.py`` ``NAME_MAX``).
NAME_MAX = 512
#: A placement's attempt (the workflow's run id and cycle) and a mark's key (the turn id, or the
#: turn id and a request id: ``REQUEST_ID_MAX`` 500 in ``gate/crudStore.ts``).
ATTEMPT_MAX = 500
MARK_KEY_MAX = 1100
#: A turn's tiers (``auth/principal.ts`` ``Tier``): at most every one.
TIERS_MAX = 16
TIER_MAX = 64
#: A turn's closing events and a queued turn's start: a handful each (``turnCore.ts``
#: ``tallyTurn``: a result, an error, a status). An event's text and a body are bounded as the
#: event log's (``crud_agent_events.py``): a user turn's words have no smaller bound in the agent.
TAIL_MAX = 10
EVENT_CHARS_MAX = 16 * 1024 * 1024
BODY_BYTES_MAX = 32 * 1024 * 1024
#: A spend's delta: a session's budget is at most 100 USD (``manager.ts``), and a placement's
#: estimate may run past it; the most turns one end adds (``MAX_SESSION_MAX_TURNS`` 200).
USD_MAX = 1_000_000.0
TURNS_MAX = 1_000_000
#: The sweep's grace (``RUNNING_GRACE_MS``, a test passes 0) and page (``BATCH``).
GRACE_MS_MAX = 24 * 3600 * 1000
STALE_MAX = 1000
#: A spend's key: a token of the placement's own and a sequence number, or `end`.
SPEND_KEY_MAX = ATTEMPT_MAX + 32

SessionIdPath = Annotated[str, Path(pattern=UUID_PATTERN)]
Status = Literal["running", "waiting_input", "waiting_approval", "idle", "done", "failed"]
OwnerKind = Literal["browser", "bearer", "oidc", "anonymous", "flow"]


async def _bounded(request: Request) -> None:
    if request.method != "POST":
        return
    length = request.headers.get("content-length")
    if length is None or not length.isdigit():
        raise ApiError(status.HTTP_411_LENGTH_REQUIRED, "a body names its Content-Length")
    if int(length) > BODY_BYTES_MAX:
        raise ApiError(
            status.HTTP_413_CONTENT_TOO_LARGE, f"a body is at most {BODY_BYTES_MAX} bytes"
        )


Bounded = Depends(_bounded)


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


async def _in_conn[T](state: Any, fn: Callable[[psycopg.Connection[Any]], T]) -> T:
    def run() -> T:
        with state.settings_store.pool.connection() as conn:
            return fn(conn)

    try:
        return await asyncio.to_thread(run)
    except event_log.SessionGoneError as err:
        raise ApiError(status.HTTP_404_NOT_FOUND, str(err), reason="session_gone") from None
    except psycopg.errors.LockNotAvailable:
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "the session's row stayed locked; send the write again",
            reason="row_busy",
        ) from None


def _event(text: str) -> str:
    """A JSON object with a string ``type``: the log's reads parse it as ``jsonb``."""
    try:
        event = json.loads(text)
    except ValueError:
        raise ValueError("an event is not JSON") from None
    if not isinstance(event, dict) or not isinstance(event.get("type"), str):
        raise ValueError("an event is an object with a type")
    return text


def _closes(texts: list[str]) -> list[str]:
    """A turn's closing events: the last one its ``session.status``, which the row takes."""
    last = json.loads(texts[-1])
    if last.get("type") != "session.status" or last.get("status") not in ("idle", "failed"):
        raise ValueError("a turn's closing events end with its session.status, idle or failed")
    return texts


def _queue(text: str) -> str:
    if json.loads(text)["type"] != "session.queue":
        raise ValueError("the event is a session.queue")
    return text


EventText = Annotated[str, Field(min_length=2, max_length=EVENT_CHARS_MAX), AfterValidator(_event)]
#: A turn's closing events, the last one its status.
Closing = Annotated[
    list[EventText], Field(min_length=1, max_length=TAIL_MAX), AfterValidator(_closes)
]
#: Lowercase, as Postgres writes a ``uuid``, so the agent's comparisons hold.
TurnId = Annotated[str, Field(pattern=TURN_ID_PATTERN), AfterValidator(str.lower)]


class Author(_Body):
    kind: OwnerKind
    id: str = Field(min_length=1, max_length=NAME_MAX)


class Logged(BaseModel):
    #: The events' JSON texts as they were logged, and their seqs.
    events: list[str]
    seqs: list[int]


def _wire(logged: turns.Logged | None) -> Logged | None:
    return None if logged is None else Logged(**logged.wire())


Tiers = Annotated[
    list[Annotated[str, Field(min_length=1, max_length=TIER_MAX)]], Field(max_length=TIERS_MAX)
]


class SendClaim(_Body):
    author: Author
    turn_id: TurnId
    tiers: Tiers | None = None
    bare: bool


class Claimed(BaseModel):
    prior_status: Status
    runtime: Literal["durable", "unified"] | None


class SendClaimed(BaseModel):
    claimed: Claimed | None


@router.post(
    "/sessions/{session_id}/turn/send-claim",
    name="crud_agent_turns_send_claim",
    dependencies=[Bounded],
)
async def send_claim(session_id: SessionIdPath, body: SendClaim, state: StateDep) -> SendClaimed:
    author = (body.author.kind, body.author.id)
    tiers = (
        None
        if body.tiers is None
        else {"by": {"kind": author[0], "id": author[1]}, "tiers": body.tiers}
    )
    row = await _in_conn(
        state,
        lambda c: turns.send_claim(c, session_id, author, tiers, body.turn_id, body.bare),
    )
    return SendClaimed(claimed=None if row is None else Claimed.model_validate(row))


class Queueable(_Body):
    author: Author


class Seq(BaseModel):
    seq: int | None


@router.post(
    "/sessions/{session_id}/turn/queueable",
    name="crud_agent_turns_queueable",
    dependencies=[Bounded],
)
async def queueable(session_id: SessionIdPath, body: Queueable, state: StateDep) -> Seq:
    author = (body.author.kind, body.author.id)
    return Seq(seq=await _in_conn(state, lambda c: turns.queueable(c, session_id, author)))


class TurnState(BaseModel):
    status: Status
    durable_turn_id: str | None
    turn_attempt: str | None
    interrupt_requested: bool
    owner_kind: str
    runtime: str | None
    event_seq: str
    cost_usd: float
    #: No write for ``grace_ms``.
    settled: bool


class StateRead(BaseModel):
    turn: TurnState | None


@router.get("/sessions/{session_id}/turn", name="crud_agent_turns_state")
async def turn_state(
    session_id: SessionIdPath,
    state: StateDep,
    grace_ms: Annotated[int, Query(ge=0, le=GRACE_MS_MAX)] = 0,
) -> StateRead:
    row = await _in_conn(state, lambda c: turns.state(c, session_id, grace_ms))
    return StateRead(turn=None if row is None else TurnState.model_validate(row))


class GiveBack(_Body):
    prior: Status


class MaybeLogged(BaseModel):
    logged: Logged | None


@router.post(
    "/sessions/{session_id}/turn/give-back",
    name="crud_agent_turns_give_back",
    dependencies=[Bounded],
)
async def give_back(session_id: SessionIdPath, body: GiveBack, state: StateDep) -> MaybeLogged:
    logged = await _in_conn(state, lambda c: turns.give_back(c, session_id, body.prior))
    return MaybeLogged(logged=_wire(logged))


class Changed(BaseModel):
    changed: bool


@router.post(
    "/sessions/{session_id}/turn/interrupt",
    name="crud_agent_turns_interrupt",
    dependencies=[Bounded],
)
async def interrupt(session_id: SessionIdPath, state: StateDep) -> Changed:
    return Changed(changed=await _in_conn(state, lambda c: turns.interrupt(c, session_id)))


class Placement(_Body):
    turn_id: TurnId
    attempt: str = Field(min_length=1, max_length=ATTEMPT_MAX)
    mark_key: str = Field(min_length=1, max_length=MARK_KEY_MAX)


class Mark(BaseModel):
    #: The transcript's mark, or null when the row does not run this turn.
    mark: int | None


@router.post(
    "/sessions/{session_id}/turn/placement",
    name="crud_agent_turns_placement",
    dependencies=[Bounded],
)
async def placement(session_id: SessionIdPath, body: Placement, state: StateDep) -> Mark:
    mark = await _in_conn(
        state,
        lambda c: turns.placement(c, session_id, body.turn_id, body.attempt, body.mark_key),
    )
    return Mark(mark=mark)


class Spend(_Body):
    #: The write's own key: a resend of it under the same key adds nothing.
    key: str = Field(min_length=1, max_length=SPEND_KEY_MAX)
    cost_usd: float = Field(ge=-USD_MAX, le=USD_MAX, allow_inf_nan=False)
    unpriced_usd: float = Field(ge=-USD_MAX, le=USD_MAX, allow_inf_nan=False)
    turns: int = Field(ge=-TURNS_MAX, le=TURNS_MAX)


class Written(BaseModel):
    written: bool


@router.post(
    "/sessions/{session_id}/turn/spend", name="crud_agent_turns_spend", dependencies=[Bounded]
)
async def spend(session_id: SessionIdPath, body: Spend, state: StateDep) -> Written:
    written = await _in_conn(
        state,
        lambda c: turns.spend(
            c, session_id, body.cost_usd, body.unpriced_usd, body.turns, body.key
        ),
    )
    return Written(written=written)


class Next(_Body):
    """The queue's head: whose it is, how it runs, and its ``user.turn`` and status events."""

    turn_id: TurnId
    author: Author
    tiers: Tiers | None = None
    bare: bool
    started: list[EventText] = Field(min_length=1, max_length=TAIL_MAX)


def _next(body: Next) -> dict[str, Any]:
    return {
        "turn_id": body.turn_id,
        "author": {"kind": body.author.kind, "id": body.author.id},
        "tiers": body.tiers,
        "bare": body.bare,
        "started": body.started,
    }


class Begun(BaseModel):
    outcome: Literal["started", "busy", "dropped"]
    logged: Logged | None


class Finish(_Body):
    turn_id: TurnId
    tail: Closing
    next: Next | None = None


class Finished(BaseModel):
    #: The turn's end as written, or null when the row no longer runs it.
    ended: Logged | None
    #: The queue's head, when one was given.
    next: Begun | None


@router.post(
    "/sessions/{session_id}/turn/finish", name="crud_agent_turns_finish", dependencies=[Bounded]
)
async def finish(session_id: SessionIdPath, body: Finish, state: StateDep) -> Finished:
    tail = [json.loads(text) for text in body.tail]
    nxt = None if body.next is None else _next(body.next)
    ended, started = await _in_conn(
        state, lambda c: turns.finish(c, session_id, body.turn_id, tail, nxt)
    )
    return Finished(
        ended=_wire(ended),
        next=None if started is None else Begun(outcome=started[0], logged=_wire(started[1])),
    )


class Begin(_Body):
    next: Next


@router.post(
    "/sessions/{session_id}/turn/begin", name="crud_agent_turns_begin", dependencies=[Bounded]
)
async def begin(session_id: SessionIdPath, body: Begin, state: StateDep) -> Begun:
    nxt = _next(body.next)
    outcome, logged = await _in_conn(state, lambda c: turns.begin(c, session_id, nxt))
    return Begun(outcome=outcome, logged=_wire(logged))


class QueueWrite(_Body):
    #: The ``session.queue`` event's JSON text.
    event: Annotated[EventText, AfterValidator(_queue)]


@router.post(
    "/sessions/{session_id}/turn/queue", name="crud_agent_turns_queue", dependencies=[Bounded]
)
async def queue_changed(
    session_id: SessionIdPath, body: QueueWrite, state: StateDep
) -> MaybeLogged:
    logged = await _in_conn(state, lambda c: turns.queue_changed(c, session_id, body.event))
    return MaybeLogged(logged=_wire(logged))


class LastQueue(BaseModel):
    #: The log's last ``session.queue`` event's JSON text, or null.
    event: str | None


@router.get("/sessions/{session_id}/turn/queue", name="crud_agent_turns_last_queue")
async def last_queue(session_id: SessionIdPath, state: StateDep) -> LastQueue:
    return LastQueue(event=await _in_conn(state, lambda c: turns.last_queue(c, session_id)))


class StaleRow(BaseModel):
    id: str
    event_seq: str


class Stale(BaseModel):
    rows: list[StaleRow]


@router.get("/turns/stale", name="crud_agent_turns_stale")
async def stale(
    state: StateDep,
    after: Annotated[str, Query(max_length=36)] = "",
    grace_ms: Annotated[int, Query(ge=0, le=GRACE_MS_MAX)] = 0,
    limit: Annotated[int, Query(ge=1, le=STALE_MAX)] = 10,
) -> Stale:
    rows = await _in_conn(state, lambda c: turns.stale(c, after, grace_ms, limit))
    return Stale(rows=[StaleRow.model_validate(r) for r in rows])


class EndLost(_Body):
    #: The ``event_seq`` the sweep read: the row is ended only while it still is.
    event_seq: int = Field(ge=0)
    tail: Closing


@router.post(
    "/sessions/{session_id}/turn/end-lost",
    name="crud_agent_turns_end_lost",
    dependencies=[Bounded],
)
async def end_lost(session_id: SessionIdPath, body: EndLost, state: StateDep) -> MaybeLogged:
    logged = await _in_conn(
        state, lambda c: turns.end_lost(c, session_id, body.event_seq, body.tail)
    )
    return MaybeLogged(logged=_wire(logged))
