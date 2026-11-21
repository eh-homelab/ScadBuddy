"""The agent's NOTIFYs and its last reads of a session's events behind CRUD routes (#5299,
#6501): under ``/api/v1/internal/crud/agent/``, for the ``agent`` credential only
(`crud_router`).

Area ``publish``, the NOTIFYs the agent sent over its own SQL:

- ``POST …/publish/session-event``: one ``session.*`` bus event (``sessions/busEvents.ts``
  ``SessionEventPublisher``) on ``scadbuddy_events``, which every replica's change stream
  carries; each field checked and bounded, so the payload stays far under NOTIFY's 8000 bytes;
- ``POST …/publish/warm-evict``: a warm process's eviction (``sessions/warmEvict.ts``
  ``notifyWarmEvict``) on ``scadbuddy_warm_evict``, for a session id or ``*``.

Area ``session-reads``, a session's own events (never its fork chain, as the agent's SQL read
them; `agent_data/events.py`):

- ``GET …/sessions/{id}/open-calls`` (``turn_id``): the calls with a ``tool.call`` and no
  ``tool.result`` (``tools/activityProjection.ts`` ``openCalls``);
- ``GET …/sessions/{id}/reasoning`` (``tool_use_id``, ``scan``): the text deltas a child's
  model wrote before a call (``flows/queue.ts`` ``childReasoning``).

The session id is the only id in a path, a UUID checked by its pattern. Every body is bounded.
"""

from __future__ import annotations

import asyncio
import json
from typing import Annotated, Any, Literal

from fastapi import Query
from pydantic import BaseModel, ConfigDict, Field

from scadbuddy.agent_data import events
from scadbuddy.api.crud_agent_events import (
    UUID_PATTERN,
    Bounded,
    SessionIdPath,
    TurnId,
    _in_conn,
)
from scadbuddy.api.deps import StateDep
from scadbuddy.core.events import PG_CHANNEL
from scadbuddy.crud.guard import crud_router

router = crud_router("agent")

WARM_EVICT_CHANNEL = "scadbuddy_warm_evict"
#: ``busEvents.ts`` ``SessionBusKind``.
SessionBusKind = Literal[
    "session.started", "session.message", "session.waiting", "session.done", "session.owner"
]
#: A tool_use id (``toolu_…``) is far shorter; a longer one names no logged call.
TOOL_USE_ID_MAX = 500
#: ``queue.ts`` ``REASONING_SCAN``, and the most one read may scan.
REASONING_SCAN_MAX = 10_000


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SessionEvent(_Body):
    """``busEvents.ts`` ``SessionBusEvent``, as the agent built it."""

    id: str = Field(pattern=r"^[0-9a-f]{32}$")
    at: str = Field(min_length=1, max_length=40)
    kind: SessionBusKind
    session_id: str = Field(pattern=UUID_PATTERN)
    seq: int = Field(ge=0, le=2**53)
    status: str | None = Field(default=None, pattern=r"^[a-z_]{1,32}$")
    replica: str = Field(min_length=1, max_length=200)


class Sent(BaseModel):
    sent: bool


def _notify(state: Any, channel: str, payload: str) -> None:
    with state.settings_store.pool.connection() as conn:
        conn.execute("SELECT pg_notify(%s, %s)", (channel, payload))


@router.post(
    "/publish/session-event", name="crud_agent_publish_session_event", dependencies=[Bounded]
)
async def session_event(body: SessionEvent, state: StateDep) -> Sent:
    event = body.model_dump(exclude_none=True)
    await asyncio.to_thread(_notify, state, PG_CHANNEL, json.dumps(event, separators=(",", ":")))
    return Sent(sent=True)


class WarmEvict(_Body):
    #: A session id, or ``*`` for every warm process.
    target: str = Field(pattern=r"^(\*|" + UUID_PATTERN[1:-1] + r")$")


@router.post("/publish/warm-evict", name="crud_agent_publish_warm_evict", dependencies=[Bounded])
async def warm_evict(body: WarmEvict, state: StateDep) -> Sent:
    await asyncio.to_thread(_notify, state, WARM_EVICT_CHANNEL, body.target)
    return Sent(sent=True)


class Call(BaseModel):
    id: str
    name: str


class OpenCalls(BaseModel):
    calls: list[Call]


@router.get("/sessions/{session_id}/open-calls", name="crud_agent_session_open_calls")
async def open_calls(
    session_id: SessionIdPath, state: StateDep, turn_id: TurnId | None = None
) -> OpenCalls:
    rows = await _in_conn(state, lambda c: events.open_calls(c, session_id, turn_id))
    return OpenCalls(calls=[Call.model_validate(r) for r in rows])


class Delta(BaseModel):
    message: str | None
    delta: str | None


class Reasoning(BaseModel):
    #: Whether the call's ``tool.call`` is within the log's last ``scan`` events.
    logged: bool
    deltas: list[Delta]


@router.get("/sessions/{session_id}/reasoning", name="crud_agent_session_reasoning")
async def reasoning(
    session_id: SessionIdPath,
    tool_use_id: Annotated[str, Query(min_length=1, max_length=TOOL_USE_ID_MAX)],
    state: StateDep,
    scan: Annotated[int, Query(ge=0, le=REASONING_SCAN_MAX)] = 2000,
) -> Reasoning:
    rows = await _in_conn(
        state, lambda c: events.reasoning_deltas(c, session_id, tool_use_id, scan)
    )
    if rows is None:
        return Reasoning(logged=False, deltas=[])
    return Reasoning(logged=True, deltas=[Delta.model_validate(r) for r in rows])
