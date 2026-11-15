"""A session's event log behind CRUD routes (#5299 step 9, part b): under
``/api/v1/internal/crud/agent/``, for the ``agent`` credential only (`crud_router`), area
``session-events``. Each route is one method of the agent's event log
(``agent/src/sessions/eventLog.ts`` `EventRows`) and runs its SQL, a transaction as one
transaction (#5299 D2); the SQL is in ``agent_data/events.py``. An event travels as its JSON
text, as it is stored.

- ``POST …/sessions/{id}/events``: an append, its seqs; with ``fence`` only while the row names
  that turn placement (``seqs: null`` otherwise, ``turnHost.ts`` ``holds(tx, 'update')``); 404
  ``session_gone`` for a session with no row;
- ``GET …/sessions/{id}/events`` (``after``, ``limit``): the log after a seq, a fork's through its
  chain in one snapshot (D4: the 1 s follower polls read this);
- ``GET …/sessions/{id}/events/last-seq``, ``…/events/user-turn`` (``turn_id``) and
  ``…/events/turn`` (``turn_id``, ``after``): the reads;
- ``POST …/sessions/{id}/fork-log``: a new fork's own start, the parent's log taken through the
  cut and its status, in one transaction under the parent's payload-key lock, shared; 404
  ``parent_gone`` once the parent was forgotten since the fork's insert.

The session id is the only id in a path, a UUID checked by its pattern. Every body is bounded.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Annotated, Any

import psycopg
from fastapi import Depends, Path, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from scadbuddy.agent_data import events
from scadbuddy.api.deps import StateDep
from scadbuddy.core.problems import ApiError
from scadbuddy.crud.guard import crud_router

router = crud_router("agent")

UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
#: One append's events: a turn hands over a batch per SDK message; the agent splits above this.
EVENTS_MAX = 1000
#: One event's JSON text, and one append's body (a tool result's text is the largest event; its
#: images are blobs, ``sessions/blobs.ts``). The agent splits a batch to stay under the body's.
EVENT_CHARS_MAX = 16 * 1024 * 1024
BODY_BYTES_MAX = 32 * 1024 * 1024
#: One read's rows at most (``eventLog.ts`` ``PAGE``).
READ_MAX = 500
TURN_ID_MAX = 500

SessionIdPath = Annotated[str, Path(pattern=UUID_PATTERN)]


async def _bounded(request: Request) -> None:
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
    except events.SessionGoneError as err:
        raise ApiError(status.HTTP_404_NOT_FOUND, str(err), reason="session_gone") from None
    except events.ParentForgottenError as err:
        raise ApiError(status.HTTP_404_NOT_FOUND, str(err), reason="parent_gone") from None


EventText = Annotated[str, Field(min_length=2, max_length=EVENT_CHARS_MAX)]


def _checked(texts: list[str]) -> list[str]:
    """Each text a JSON object with a string ``type``: the log's reads parse it as ``jsonb``."""
    for text in texts:
        try:
            event = json.loads(text)
        except ValueError:
            raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, "an event is not JSON") from None
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise ApiError(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "an event is an object with a type"
            )
    return texts


class Fence(_Body):
    attempt: str = Field(min_length=1, max_length=500)
    turn_id: str = Field(min_length=1, max_length=TURN_ID_MAX)


class Append(_Body):
    events: list[EventText] = Field(min_length=1, max_length=EVENTS_MAX)
    fence: Fence | None = None


class Seqs(BaseModel):
    #: The events' seqs, oldest first; null when the fence refused them.
    seqs: list[int] | None


@router.post(
    "/sessions/{session_id}/events", name="crud_agent_events_append", dependencies=[Bounded]
)
async def append(session_id: SessionIdPath, body: Append, state: StateDep) -> Seqs:
    texts = _checked(body.events)
    fence = None if body.fence is None else events.TurnFence(body.fence.attempt, body.fence.turn_id)
    return Seqs(seqs=await _in_conn(state, lambda c: events.append(c, session_id, texts, fence)))


class Logged(BaseModel):
    seq: int
    #: The event's JSON text as it was logged.
    event: str
    #: Taken through the fork chain: the agent addresses it to this session.
    inherited: bool


class Read(BaseModel):
    events: list[Logged]


@router.get("/sessions/{session_id}/events", name="crud_agent_events_read")
async def read(
    session_id: SessionIdPath,
    state: StateDep,
    after: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=READ_MAX)] = READ_MAX,
) -> Read:
    rows = await _in_conn(state, lambda c: events.read(c, session_id, after, limit))
    return Read(events=[Logged.model_validate(r) for r in rows])


class LastSeq(BaseModel):
    seq: int


@router.get("/sessions/{session_id}/events/last-seq", name="crud_agent_events_last_seq")
async def last_seq(session_id: SessionIdPath, state: StateDep) -> LastSeq:
    return LastSeq(seq=await _in_conn(state, lambda c: events.last_seq(c, session_id)))


TurnId = Annotated[str, Query(min_length=1, max_length=TURN_ID_MAX)]


class UserTurn(BaseModel):
    text: str | None


@router.get("/sessions/{session_id}/events/user-turn", name="crud_agent_events_user_turn")
async def user_turn(session_id: SessionIdPath, turn_id: TurnId, state: StateDep) -> UserTurn:
    text = await _in_conn(state, lambda c: events.user_turn_text(c, session_id, turn_id))
    return UserTurn(text=text)


class TurnEvents(BaseModel):
    #: Whether the session still stands.
    here: bool
    #: The seq read through, for the next read; null while the turn's start is not logged.
    after: int | None
    events: list[str]


@router.get("/sessions/{session_id}/events/turn", name="crud_agent_events_turn")
async def turn(
    session_id: SessionIdPath,
    turn_id: TurnId,
    state: StateDep,
    after: Annotated[int | None, Query(ge=0)] = None,
) -> TurnEvents:
    here, through, texts = await _in_conn(
        state, lambda c: events.turn_events(c, session_id, turn_id, after)
    )
    return TurnEvents(here=here, after=through, events=texts)


class ForkLog(_Body):
    parent_id: str = Field(pattern=UUID_PATTERN)
    cut: int = Field(ge=0)
    started: list[EventText] = Field(min_length=1, max_length=10)
    then: list[EventText] = Field(max_length=10)


@router.post(
    "/sessions/{session_id}/fork-log", name="crud_agent_events_fork_log", dependencies=[Bounded]
)
async def fork_log(session_id: SessionIdPath, body: ForkLog, state: StateDep) -> Seqs:
    started, then = _checked(body.started), _checked(body.then)
    seqs = await _in_conn(
        state,
        lambda c: events.fork_log(c, session_id, body.parent_id, body.cut, started, then),
    )
    return Seqs(seqs=seqs)
