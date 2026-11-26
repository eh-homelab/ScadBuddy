"""The flow grants waiting for a person, behind a CRUD route (#6399): under
``/api/v1/internal/crud/agent/``, for the ``agent`` credential only (`crud_router`).

The rest of area ``notify`` (the agent's Web Push records and its notification channels,
sealed and opened with the KEK) is Go's (``tools/go/core/internal/notify``, #7667). This
route stays here: it reads the flow grants through the API's own stores.

- ``GET …/push/waiting`` the flow grants waiting for a person, by id, which the agent pushes
  beside its own pending input.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel

from scadbuddy.api.flow_triggers import GRANT_REQUESTS_MAX
from scadbuddy.crud.guard import crud_router
from scadbuddy.flow_triggers.component import TriggersDep

router = crud_router("agent")

#: The agent's ``notify/pushStore.ts`` ``SENT_KEEP_DAYS``: how long a push's claim is kept.
SENT_KEEP_DAYS = 2

#: The agent's ``notify/dispatcher.ts`` ``WAITING_PUSH_WINDOW_MS``: a request older than
#: this is not pushed, so it is not listed.
PUSH_WINDOW = timedelta(hours=24)


def _epoch_s(at: datetime) -> int:
    """Whole seconds since the epoch, floored: the page's ``Math.floor(Date.parse(at) / 1000)``."""
    return (at - _EPOCH) // timedelta(seconds=1)


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class WaitingRequest(BaseModel):
    """A flow grant waiting for a person's decision, as a push names it: the id the page's
    own notification uses as its tag (frontend `lib/needsYou.ts`), never what it asks."""

    id: str
    reason: Literal["grant"]
    #: When it started to wait: the agent pushes it only while it is young, so a swept claim
    #: (``SENT_KEEP_DAYS``) is never claimed again.
    since: datetime


class WaitingRequests(BaseModel):
    requests: list[WaitingRequest]


@router.get("/push/waiting", name="crud_agent_push_waiting")
async def push_waiting(triggers: TriggersDep) -> WaitingRequests:
    """#6399: the flow grants waiting for a person (a run's and a trigger's standing grant),
    which the agent pushes to a closed tab beside its own pending input: those of
    ``GET /api/v1/flow-grant-requests`` requested within ``PUSH_WINDOW``, by id only. A
    standing grant's id carries its ``requested_at`` (epoch seconds), as the page's does, so
    a grant an edit returned to ``requested`` is a new request with a claim of its own."""
    # Only those the agent still pushes, so older ones never crowd a new one out of the page.
    newer_than = datetime.now(UTC) - PUSH_WINDOW
    runs, _ = await triggers.flows.store.requested_grants(GRANT_REQUESTS_MAX, newer_than)
    standing = await triggers.store.requested_grant_ids(GRANT_REQUESTS_MAX, newer_than)
    return WaitingRequests(
        requests=[
            WaitingRequest(id=f"run-grant:{grant.id}", reason="grant", since=grant.created_at)
            for grant, _ in runs
        ]
        + [
            WaitingRequest(id=f"trigger-grant:{gid}:{_epoch_s(asked)}", reason="grant", since=asked)
            for gid, asked in standing
        ]
    )
