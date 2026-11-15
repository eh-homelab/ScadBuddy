"""The agent's classic approvals behind CRUD routes (#5299 step 10): under
``/api/v1/internal/crud/agent/``, for the ``agent`` credential only (`crud_router`). Each route is
one statement or transaction of the agent's ``approvals/service.ts`` `ApprovalService`; the SQL
is ``agent_data/approvals.py``. The agent calls them once ``…/areas`` lists ``approvals``, and its
own SQL until then (D9).

Area ``approvals``:

- ``GET …/approvals/row?id=…``, ``GET …/approvals/session?session_id=…``: an approval, and the
  session access its checks take (``null`` for none, a malformed id included);
- ``POST …/approvals/visible-pending``, ``…/list``, ``…/prepared``, ``…/due``: the lists;
- ``POST …/approvals/create`` and ``…/create-prepared``: a pending approval, with its session
  events, or an MCP prepare within its bounds under one advisory lock;
- ``POST …/approvals/settle``: a decision, written once, with its span's context, its session
  events and its ``approval`` audit row in one transaction (``approval: null`` when another
  decision won or it had expired);
- ``POST …/approvals/consume``: a sessionless approval used once, by its asker, for its input.

An approval id travels in the body or the query, never the path.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import HTTPException, Query
from pydantic import BaseModel, Field

from scadbuddy.agent_data import approvals
from scadbuddy.api.crud_agent_gate import (
    NAME_MAX,
    UUID_PATTERN,
    Logged,
    Name,
    Principal,
    Text,
    _Body,
    _in_conn,
    _logged,
)
from scadbuddy.api.crud_agent_pending import Traceparent
from scadbuddy.api.deps import StateDep
from scadbuddy.crud.guard import crud_router

router = crud_router("agent")

ApprovalId = Annotated[str, Field(pattern=UUID_PATTERN)]
IdQuery = Annotated[str, Query(min_length=1, max_length=NAME_MAX)]
#: The most ids one ``due`` read leaves out (the agent's ``EXPIRE_PAGE`` times ``EXPIRE_PAGES``).
TRIED_MAX = 5000
#: The most traceparents one prepare carries for its evictions.
EVICT_MAX = 1000


class Row(BaseModel):
    """An ``ai_approvals`` row as ``COLUMNS`` reads it."""

    id: str
    session_id: str | None
    turn_id: str | None
    tool_use_id: str
    tool: str
    input_summary: str
    input_hash: str
    tier: str
    requested_by_kind: str
    requested_by_id: str
    requested_by_label: str
    requested_tiers: list[str] | None
    created_at: datetime
    expires_at: datetime
    decision: str | None
    decided_by_kind: str | None
    decided_by_id: str | None
    decided_by_label: str | None
    decided_at: datetime | None
    reason: str | None
    usable_until: datetime | None
    resume_turn_id: str | None
    consumed_at: datetime | None
    revoked_at: datetime | None
    traceparent: str | None
    decision_traceparent: str | None
    due: bool


def _rows(rows: list[dict[str, Any]]) -> list[Row]:
    return [Row.model_validate(r) for r in rows]


class One(BaseModel):
    approval: Row | None


@router.get("/approvals/row", name="crud_agent_approvals_row")
async def get_row(state: StateDep, id: IdQuery) -> One:
    found = await _in_conn(state, lambda conn: approvals.row(conn, id))
    return One(approval=None if found is None else Row.model_validate(found))


class Access(BaseModel):
    owner_kind: str
    owner_id: str
    owner_label: str
    creator_kind: str
    creator_id: str
    offer_kind: str | None
    offer_id: str | None


class AccessAnswer(BaseModel):
    session: Access | None


@router.get("/approvals/session", name="crud_agent_approvals_session")
async def session_access(state: StateDep, session_id: IdQuery) -> AccessAnswer:
    found = await _in_conn(state, lambda conn: approvals.session_access(conn, session_id))
    return AccessAnswer(session=None if found is None else Access.model_validate(found))


class Rows(BaseModel):
    approvals: list[Row]


class Who(_Body):
    kind: Name
    id: Name


@router.post("/approvals/visible-pending", name="crud_agent_approvals_visible_pending")
async def visible_pending(body: Who, state: StateDep) -> Rows:
    rows = await _in_conn(state, lambda c: approvals.visible_pending(c, body.kind, body.id))
    return Rows(approvals=_rows(rows))


@router.post("/approvals/prepared", name="crud_agent_approvals_prepared")
async def prepared(body: Who, state: StateDep) -> Rows:
    rows = await _in_conn(state, lambda c: approvals.prepared(c, body.kind, body.id))
    return Rows(approvals=_rows(rows))


class ListBody(_Body):
    session_id: Name | None = None
    pending: bool = False


@router.post("/approvals/list", name="crud_agent_approvals_list")
async def list_rows(body: ListBody, state: StateDep) -> Rows:
    rows = await _in_conn(state, lambda c: approvals.list_rows(c, body.session_id, body.pending))
    return Rows(approvals=_rows(rows))


class Due(_Body):
    tried: Annotated[list[Name], Field(default_factory=list, max_length=TRIED_MAX)]
    limit: Annotated[int, Field(ge=1, le=approvals.LIST_MAX)]


class DueIds(BaseModel):
    ids: list[str]


@router.post("/approvals/due", name="crud_agent_approvals_due")
async def due(body: Due, state: StateDep) -> DueIds:
    ids = await _in_conn(state, lambda c: approvals.due(c, body.tried, body.limit))
    return DueIds(ids=ids)


class New(_Body):
    """A pending approval's row; its summary and hash are the agent's."""

    id: ApprovalId
    session_id: Annotated[str, Field(pattern=UUID_PATTERN)] | None
    turn_id: Annotated[str, Field(pattern=UUID_PATTERN)] | None
    tool_use_id: Name
    tool: Name
    input_summary: Text
    input_hash: Name
    tier: Literal["read", "write", "outward"]
    requested_by: Principal
    requested_tiers: Annotated[list[Name], Field(max_length=8)] | None = None
    expiry_s: int
    traceparent: Traceparent | None = None


def _new(body: New) -> approvals.NewApproval:
    return approvals.NewApproval(
        id=body.id,
        session_id=body.session_id,
        turn_id=body.turn_id,
        tool_use_id=body.tool_use_id,
        tool=body.tool,
        input_summary=body.input_summary,
        input_hash=body.input_hash,
        tier=body.tier,
        requested_by=body.requested_by.model_dump(),
        requested_tiers=body.requested_tiers,
        expiry_s=body.expiry_s,
        traceparent=body.traceparent,
    )


class Created(BaseModel):
    approval: Row
    logged: list[Logged]


@router.post("/approvals/create", name="crud_agent_approvals_create")
async def create(body: New, state: StateDep) -> Created:
    made, logged = await _in_conn(state, lambda c: approvals.create(c, _new(body)))
    return Created(approval=Row.model_validate(made), logged=_logged(logged))


class Prepare(_Body):
    approval: New
    per_principal: Annotated[int, Field(ge=1)]
    total: Annotated[int, Field(ge=1)]
    evict_reason: Text
    evict_traceparents: Annotated[
        list[Traceparent], Field(default_factory=list, max_length=EVICT_MAX)
    ]


class Prepared(BaseModel):
    approval: Row | None
    evicted: list[Row]


@router.post("/approvals/create-prepared", name="crud_agent_approvals_create_prepared")
async def create_prepared(body: Prepare, state: StateDep) -> Prepared:
    made, evicted = await _in_conn(
        state,
        lambda c: approvals.create_prepared(
            c,
            _new(body.approval),
            per_principal=body.per_principal,
            total=body.total,
            evict_reason=body.evict_reason,
            evict_traceparents=list(body.evict_traceparents),
        ),
    )
    return Prepared(
        approval=None if made is None else Row.model_validate(made), evicted=_rows(evicted)
    )


class SettleBody(_Body):
    id: Name
    decision: approvals.Decision
    by: Principal | None = None
    reason: Text | None = None
    expiry_s: int = 0
    traceparent: Traceparent | None = None
    surface: Literal["harness", "mcp", "http", "system"] | None = None
    client_ip: Annotated[str, Field(max_length=200)] | None = None


class Settled(BaseModel):
    approval: Row | None
    logged: list[Logged]


@router.post("/approvals/settle", name="crud_agent_approvals_settle")
async def settle(body: SettleBody, state: StateDep) -> Settled:
    if body.decision in ("approved", "denied") and body.by is None:
        # A person's decision names who made it; only the system expires or cancels.
        raise HTTPException(422, "an approval or a denial names who decided it")
    args = approvals.Settle(
        approval_id=body.id,
        decision=body.decision,
        by=None if body.by is None else body.by.model_dump(),
        reason=body.reason,
        expiry_s=body.expiry_s,
        traceparent=body.traceparent,
        surface=body.surface,
        client_ip=body.client_ip,
    )
    settled, logged = await _in_conn(state, lambda c: approvals.settle(c, args))
    return Settled(
        approval=None if settled is None else Row.model_validate(settled), logged=_logged(logged)
    )


class Consume(_Body):
    id: Name
    by: Who
    input_hash: Name


@router.post("/approvals/consume", name="crud_agent_approvals_consume")
async def consume(body: Consume, state: StateDep) -> One:
    by = {"kind": body.by.kind, "id": body.by.id}
    used = await _in_conn(state, lambda c: approvals.consume(c, body.id, by, body.input_hash))
    return One(approval=None if used is None else Row.model_validate(used))
