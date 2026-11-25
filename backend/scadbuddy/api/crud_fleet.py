"""The fleet MCP server's CRUD routes (agent fleet chat, spec 2026-11-25-agent-fleet-messaging
§4.3): under ``/api/v1/internal/crud/fleet/``, for the ``fleet`` credential only
(`crud_router`). Areas ``changes`` and ``fleet``; the ``fleet`` area's routes are core's (Go).

- ``GET …/changes``: the change stream, only events whose kind starts with ``fleet.``
  (`keep_fleet`; ``fleet.message`` is an `OpaqueEvent`, whose frame data is its payload as
  stored), with the agent stream's frames and ``Last-Event-ID`` replay
  (`api/crud_agent_changes.py`).
"""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import Header, Request
from fastapi.responses import StreamingResponse

from scadbuddy.agent_changes.hub import Change
from scadbuddy.api.crud_agent_changes import FLEET_KIND_PREFIX, HubDep, change_stream
from scadbuddy.api.deps import StateDep
from scadbuddy.core.events import PG_CHANNEL
from scadbuddy.crud.guard import crud_router

#: The kinds this caller's stream carries start with this.
#: The streams the fleet caller may hold together: its own budget, never the agent's.
FLEET_STREAMS_MAX = 8

router = crud_router("fleet")


def keep_fleet(change: Change) -> bool:
    """Only the event log's ``fleet.`` kinds: no other channel, no other kind."""
    if change.channel != PG_CHANNEL:
        return False
    try:
        kind = json.loads(change.payload).get("kind")
    except (ValueError, AttributeError):
        return False
    return isinstance(kind, str) and kind.startswith(FLEET_KIND_PREFIX)


@router.get("/changes", name="crud_fleet_changes")
async def changes(
    request: Request,
    state: StateDep,
    hub: HubDep,
    last_event_id: Annotated[str | None, Header()] = None,
) -> StreamingResponse:
    return await change_stream(
        request, state, hub, last_event_id, keep=keep_fleet, group=("fleet", FLEET_STREAMS_MAX)
    )
