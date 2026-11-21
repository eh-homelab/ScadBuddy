"""The agent's change stream behind its CRUD routes (#5299 D4, step 11): under
``/api/v1/internal/crud/agent/``, for the ``agent`` credential only (`crud_router`).

Area ``changes``: ``GET …/agent/changes``, a server-sent event stream of what the agent's own
``LISTEN`` connection heard, on the six channels (``scadbuddy_events``, ``_input``,
``_pending``, ``_mcp``, ``_warm_evict``, ``_settings``), with ``Last-Event-ID`` replay of the
backend's ``events`` log (`agent_changes/hub.py` has the design). Frames:

- ``change``: ``{"c": <channel>, "p": <the NOTIFY payload>}``, with ``id`` when it is a logged
  event;
- ``ready``: ``{"resync", "reconnect"}``, with ``id`` the stream's place: sent once, after a
  reconnect's replay;
- ``reconnected``: ``{"resync"}``: changes were lost here (this process's LISTEN connection
  dropped, or the reader fell behind), and the log after the place was replayed before it;
- a ``: keepalive`` comment every 15 s.

A ``Last-Event-ID`` that is not an event id (`hub.py` ``EVENT_ID``) or ``0`` names no place:
the stream starts with ``resync``, never a guess. A credential may hold
`STREAMS_PER_CREDENTIAL` streams from one address (`stream_key`) and the process `STREAMS_MAX`;
one more is 429.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Header, Request, status
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from scadbuddy.agent_changes.component import CHANGES
from scadbuddy.agent_changes.hub import START, ChangeHub, StreamsFullError, frames
from scadbuddy.api.components import component_dep
from scadbuddy.api.deps import StateDep
from scadbuddy.auth.gate import caller_of
from scadbuddy.auth.models import Service
from scadbuddy.core.pg_events import PgNotifyEventBus
from scadbuddy.core.problems import ApiError
from scadbuddy.crud.guard import crud_router

router = crud_router("agent")

#: An event id is 32 hex digits (`core/events.py`); a header past this is no place.
LAST_EVENT_ID_MAX = 64

HubDep = Annotated[ChangeHub, component_dep(CHANGES)]


def stream_key(request: Request) -> str:
    """What the streams are capped by: the credential and the peer's address. Every agent
    process that reads the sealed credential from its view holds the same one, so a cap by the
    credential alone would refuse the fifth process for as long as the others stay; each process
    connects from its own pod's address (the API is reached directly, never through the
    ingress), so the pair counts one process's streams."""
    principal = caller_of(request.scope)
    credential = (
        principal.credential_id or principal.caller if isinstance(principal, Service) else "unknown"
    )
    peer = request.client.host if request.client else "unknown"
    return f"{credential}@{peer}"


@router.get("/changes", name="crud_agent_changes")
async def changes(
    request: Request,
    state: StateDep,
    hub: HubDep,
    last_event_id: Annotated[str | None, Header()] = None,
) -> StreamingResponse:
    log = state.events
    if not isinstance(log, PgNotifyEventBus):
        raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, "this process has no event log")
    key = stream_key(request)
    if last_event_id is not None and len(last_event_id) > LAST_EVENT_ID_MAX:
        last_event_id = ""  # no place: a resync, as any other malformed id
    # Read before the stream subscribes, so every live event comes after its place.
    start = await log.newest() or START if last_event_id is None else None
    try:
        stream = hub.open(key)
    except StreamsFullError as error:
        raise ApiError(status.HTTP_429_TOO_MANY_REQUESTS, str(error)) from None

    async def body() -> AsyncIterator[bytes]:
        try:
            async for chunk in frames(stream, log, last_event_id, start=start):
                yield chunk
        finally:
            hub.close(stream)

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no"},
        # A response never started (the client gone first) runs no generator's `finally`.
        background=BackgroundTask(hub.close, stream),
    )
