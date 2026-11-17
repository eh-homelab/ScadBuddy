"""The order items a session revised and its spend's report behind CRUD routes (#5299 step 9,
part c): under ``/api/v1/internal/crud/agent/``, for the ``agent`` credential only
(`crud_router`), area ``session-order-costs``. Each route is one method of the agent's
``sessions/orderCosts.ts`` ``OrderCostRows`` and runs its SQL as one transaction; the SQL is in
``agent_data/order_costs.py``.

- ``POST …/sessions/{id}/order-costs``: a revision's row (written once per order and item);
- ``POST …/order-costs/sweep``: the sessions the sweep reports;
- ``POST …/sessions/{id}/order-costs/report``, ``…/attempt`` and ``…/settle``: a report's start,
  a share about to be sent and an order's answer, each under the session's lease
  (``order-cost:<id>``, taken through ``…/leases/{key}``, #5299 D3), refused 409
  ``lease-lost`` once the caller's token no longer holds it.

The session id is the only id in a path, a UUID checked by its pattern. Every body is bounded.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Annotated, Any, Literal
from uuid import UUID

import psycopg
from fastapi import Depends, Path, Request, status
from pydantic import BaseModel, ConfigDict, Field

from scadbuddy.agent_data import order_costs
from scadbuddy.api.deps import StateDep
from scadbuddy.core.problems import ApiError
from scadbuddy.crud.guard import crud_router

router = crud_router("agent")

UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
#: An order's and an item's id as the order tools take them (``tools/orders.ts``).
ID_MAX = 200
#: The items of one order a report names, and the sessions one sweep reports.
ITEMS_MAX = 1000
SWEEP_MAX = 100

#: The largest body: an attempt naming every item of an order.
BODY_BYTES_MAX = 512 * 1024

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
Id = Annotated[str, Field(min_length=1, max_length=ID_MAX)]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


async def _in_conn[T](state: Any, fn: Callable[[psycopg.Connection[Any]], T]) -> T:
    def run() -> T:
        with state.settings_store.pool.connection() as conn:
            return fn(conn)

    try:
        return await asyncio.to_thread(run)
    except order_costs.LeaseLostError as err:
        raise ApiError(status.HTTP_409_CONFLICT, str(err), reason="lease-lost") from None


class Done(BaseModel):
    done: bool = True


class Revision(_Body):
    order_id: Id
    order_item_id: Id


@router.post(
    "/sessions/{session_id}/order-costs",
    name="crud_agent_order_costs_insert",
    dependencies=[Bounded],
)
async def insert(session_id: SessionIdPath, body: Revision, state: StateDep) -> Done:
    await _in_conn(
        state,
        lambda c: order_costs.insert(c, session_id, body.order_id, body.order_item_id),
    )
    return Done()


class Sweep(_Body):
    limit: int = Field(ge=1, le=SWEEP_MAX)


class Sessions(BaseModel):
    sessions: list[str]


@router.post("/order-costs/sweep", name="crud_agent_order_costs_sweep", dependencies=[Bounded])
async def sweep(body: Sweep, state: StateDep) -> Sessions:
    return Sessions(
        sessions=await _in_conn(state, lambda c: order_costs.sweep_sessions(c, body.limit))
    )


class Leased(_Body):
    token: UUID


class Pending(BaseModel):
    order_id: str
    order_item_id: str
    attempted_usd: float | None


class Report(BaseModel):
    pending: list[Pending]
    cost: float
    carried: float
    principal: str
    over: bool


class Begun(BaseModel):
    #: Null when nothing is left to report.
    report: Report | None


@router.post(
    "/sessions/{session_id}/order-costs/report",
    name="crud_agent_order_costs_report",
    dependencies=[Bounded],
)
async def begin(session_id: SessionIdPath, body: Leased, state: StateDep) -> Begun:
    found = await _in_conn(state, lambda c: order_costs.begin(c, session_id, str(body.token)))
    return Begun(report=None if found is None else Report.model_validate(found))


class Attempt(_Body):
    token: UUID
    order_id: Id
    items: list[Id] = Field(min_length=1, max_length=ITEMS_MAX)
    #: No upper bound: a session's spend can pass its budget (the SDK stops a query only once
    #: it is over), and the SQL path took any share. Finite only, as ``double precision``
    #: would keep an infinity.
    amount_usd: float = Field(ge=0, allow_inf_nan=False)


@router.post(
    "/sessions/{session_id}/order-costs/attempt",
    name="crud_agent_order_costs_attempt",
    dependencies=[Bounded],
)
async def attempt(session_id: SessionIdPath, body: Attempt, state: StateDep) -> Done:
    await _in_conn(
        state,
        lambda c: order_costs.attempt(
            c, session_id, str(body.token), body.order_id, body.items, body.amount_usd
        ),
    )
    return Done()


class Settle(_Body):
    token: UUID
    order_id: Id
    outcome: Literal["reported", "unsent", "closed"]


@router.post(
    "/sessions/{session_id}/order-costs/settle",
    name="crud_agent_order_costs_settle",
    dependencies=[Bounded],
)
async def settle(session_id: SessionIdPath, body: Settle, state: StateDep) -> Done:
    await _in_conn(
        state,
        lambda c: order_costs.settle(c, session_id, str(body.token), body.order_id, body.outcome),
    )
    return Done()
