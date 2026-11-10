"""The agent service's CRUD routes (#5330, job cost spec 2026-11-05 §4.4): under
``/api/v1/internal/crud/agent/``, for the ``agent`` credential only (`crud_router`).

``POST …/orders/{order_id}/assistant-cost``: the assistant's spend in one session on an
order, in USD, as the agent's own ``ai_sessions.cost_usd`` knows it. The backend never
reads the agent's tables (#5295); the agent sends the figure. The lifecycle converts it
at ``pricing_usd_rate`` and writes one ``assistant`` cost entry, source
``ai_session:<id>``, so a second report of the session writes nothing (a natural write:
no ``Idempotency-Key``).

The holds of the assistant's workspace files (#5299, in place of the agent's trigger on
``blob_refs``): each file whose sealed bytes are a blob of the content store
(`api/internal_workspace.py`) is held ``(ai_session_file, <workspace>/<path>)`` while
its ``ai_session_files`` row names it, so the blob sweep keeps it. The agent holds before
it writes the row and releases once the row is gone (``agent/src/workspace/store.ts``):

- ``POST …/workspace-blob-refs`` holds a key for a holder (a hold sent again refreshes its
  ``created_at``);
- ``POST …/workspace-blob-refs/release`` drops one and says whether it did (one already
  gone is a success), with ``older_than_seconds`` only a hold older than that, so a hold
  taken again since is kept;
- ``GET …/workspace-blob-refs`` lists the holds older than ``older_than_seconds`` (every
  one by default), a page at a time, for the agent's reconcile.

All natural writes. The holder kind is always ``ai_session_file`` and the key a
workspace blob's, so the agent's credential holds or releases nothing else.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import Query, Response, status
from pydantic import BaseModel, ConfigDict, Field

from scadbuddy.api.deps import SettingsStoreDep, StateDep
from scadbuddy.api.internal_workspace import WORKSPACE_BLOB_KEY_PATTERN
from scadbuddy.api.orders import _run, check_path
from scadbuddy.crud.guard import crud_router
from scadbuddy.orders.component import OrdersDep
from scadbuddy.orders.lifecycle import UsdRate
from scadbuddy.orders.models import Actor, CostEntry

router = crud_router("agent")

#: The ``blob_refs.holder_kind`` of a workspace file (`api/internal_workspace.py`).
WORKSPACE_HOLDER = "ai_session_file"
#: A holder: ``session:<id>/<path>`` or ``principal:<kind>:<id>/<path>``.
WORKSPACE_HOLDER_PATTERN = r"^(session|principal):[^/]+(/[^/]+)*/[A-Za-z0-9_][A-Za-z0-9_.-]*$"
#: The most holds one page lists.
HOLDS_PAGE_MAX = 1000
#: The largest ``older_than_seconds`` (a year): far past any grace, and well inside
#: ``timedelta``'s range, so a larger value is a 422, never an ``OverflowError``.
OLDER_THAN_MAX_SECONDS = 31_536_000


class AssistantCost(BaseModel):
    """One session's spend on an order."""

    model_config = ConfigDict(extra="forbid")

    session_id: UUID
    #: The session's owner, for the entry's ``created_by``.
    principal: str | None = Field(default=None, max_length=300)
    order_item_id: str | None = Field(default=None, max_length=100)
    cost_usd: Annotated[Decimal, Field(ge=0, le=Decimal(10**6), decimal_places=8)]


class AssistantCostAnswer(BaseModel):
    #: The session's entry; ``None`` for a spend under one minor unit, which writes nothing.
    entry: CostEntry | None
    #: Whether this call wrote it.
    written: bool


@router.post(
    "/orders/{order_id}/assistant-cost",
    status_code=status.HTTP_201_CREATED,
    name="crud_agent_assistant_cost",
)
async def assistant_cost(
    order_id: str,
    body: AssistantCost,
    orders: OrdersDep,
    settings: SettingsStoreDep,
    response: Response,
) -> AssistantCostAnswer:
    check_path(order_id)
    runtime = settings.snapshot().runtime
    rate = UsdRate(Decimal(runtime.pricing_usd_rate), runtime.orders_currency)
    session = str(body.session_id)
    by = Actor(kind="agent", principal=body.principal, session=session)
    entry, written = await _run(
        lambda: orders.lifecycle.add_assistant_cost(
            order_id,
            session_id=session,
            order_item_id=body.order_item_id,
            cost_usd=body.cost_usd,
            usd_rate=rate,
            by=by,
        )
    )
    if not written:
        response.status_code = status.HTTP_200_OK
    return AssistantCostAnswer(entry=entry, written=written)


class WorkspaceHold(BaseModel):
    """One workspace file's hold on its blob."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(pattern=WORKSPACE_BLOB_KEY_PATTERN)
    holder_id: str = Field(min_length=3, max_length=1024, pattern=WORKSPACE_HOLDER_PATTERN)


class WorkspaceRelease(WorkspaceHold):
    #: Release only a hold taken longer ago than this; a hold taken again since is kept.
    older_than_seconds: int | None = Field(default=None, ge=0, le=OLDER_THAN_MAX_SECONDS)


class WorkspaceReleased(BaseModel):
    #: Whether a hold was dropped: false for one already gone, or taken again since.
    released: bool


class WorkspaceHolds(BaseModel):
    holds: list[WorkspaceHold]
    #: The last hold of a full page, to list from with ``after_holder_id`` and
    #: ``after_key``; ``None`` on the last page.
    next: WorkspaceHold | None


@router.post(
    "/workspace-blob-refs",
    status_code=status.HTTP_204_NO_CONTENT,
    name="crud_agent_hold_workspace_blob",
)
async def hold_workspace_blob(body: WorkspaceHold, state: StateDep) -> None:
    await asyncio.to_thread(state.refs.hold, body.key, WORKSPACE_HOLDER, body.holder_id)


@router.post("/workspace-blob-refs/release", name="crud_agent_release_workspace_blob")
async def release_workspace_blob(body: WorkspaceRelease, state: StateDep) -> WorkspaceReleased:
    older_than = (
        None if body.older_than_seconds is None else timedelta(seconds=body.older_than_seconds)
    )
    released = await asyncio.to_thread(
        lambda: state.refs.release(
            body.key, WORKSPACE_HOLDER, body.holder_id, older_than=older_than
        )
    )
    return WorkspaceReleased(released=released)


@router.get("/workspace-blob-refs", name="crud_agent_list_workspace_blob_refs")
async def list_workspace_blob_refs(
    state: StateDep,
    older_than_seconds: Annotated[int, Query(ge=0, le=OLDER_THAN_MAX_SECONDS)] = 0,
    after_holder_id: Annotated[str | None, Query(max_length=1024)] = None,
    after_key: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=HOLDS_PAGE_MAX)] = 500,
) -> WorkspaceHolds:
    after = (after_holder_id, after_key or "") if after_holder_id is not None else None
    rows = await asyncio.to_thread(
        lambda: state.refs.held_by_kind(
            WORKSPACE_HOLDER,
            older_than=timedelta(seconds=older_than_seconds),
            after=after,
            limit=limit,
        )
    )
    holds = [WorkspaceHold.model_construct(key=key, holder_id=holder) for holder, key in rows]
    return WorkspaceHolds(holds=holds, next=holds[-1] if len(holds) == limit else None)
