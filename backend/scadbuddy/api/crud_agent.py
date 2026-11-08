"""The agent service's CRUD routes (#5330, job cost spec 2026-11-05 §4.4): under
``/api/v1/internal/crud/agent/``, for the ``agent`` credential only (`crud_router`).

``POST …/orders/{order_id}/assistant-cost``: the assistant's spend in one session on an
order, in USD, as the agent's own ``ai_sessions.cost_usd`` knows it. The backend never
reads the agent's tables (#5295); the agent sends the figure. The lifecycle converts it
at ``pricing_usd_rate`` and writes one ``assistant`` cost entry, source
``ai_session:<id>``, so a second report of the session writes nothing (a natural write:
no ``Idempotency-Key``).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import Response, status
from pydantic import BaseModel, ConfigDict, Field

from scadbuddy.api.deps import SettingsStoreDep
from scadbuddy.api.orders import _run, check_path
from scadbuddy.crud.guard import crud_router
from scadbuddy.orders.component import OrdersDep
from scadbuddy.orders.lifecycle import UsdRate
from scadbuddy.orders.models import Actor, CostEntry

router = crud_router("agent")


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
