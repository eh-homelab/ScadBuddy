"""The agent's audit log behind CRUD routes (#5299 step 10): under
``/api/v1/internal/crud/agent/``, for the ``agent`` credential only (`crud_router`). Each route is
one method of the agent's ``audit/log.ts`` `AuditLog`; the SQL is ``agent_data/audit_log.py``.
The agent calls them once ``…/areas`` lists ``audit``, and its own SQL until then (D9).

Area ``audit``:

- ``POST …/audit/record``: one row, a tool call's approver copied in the same statement;
- ``POST …/audit/list``: a page of rows, newest first, and the next page's cursor;
- ``POST …/audit/prune``: the rows older than the retention deleted, in the one transaction the
  table's trigger lets a DELETE through.

Every text is bounded at the agent's own cap (the agent caps before it sends), so a row the
agent would have written is never refused.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from scadbuddy.agent_data import audit_log
from scadbuddy.api.crud_agent_gate import _Body, _in_conn
from scadbuddy.api.deps import StateDep
from scadbuddy.crud.guard import crud_router

router = crud_router("agent")

Kind = Literal[
    "tool_call",
    "resource",
    "approval",
    "credential",
    "plugin",
    "settings",
    "token",
    "memory",
    "http",
    "question",
    "turn",
    "payload_key",
]
Outcome = Literal["ok", "error", "refused", "denied"]
Surface = Literal["harness", "mcp", "http", "system"]


Text50 = Annotated[str, Field(max_length=50)]
Text200 = Annotated[str, Field(max_length=200)]
Text500 = Annotated[str, Field(max_length=500)]
Text1000 = Annotated[str, Field(max_length=1000)]
Text1024 = Annotated[str, Field(max_length=1024)]


class Actor(_Body):
    kind: Text50
    id: Text200
    label: Text200


class Record(_Body):
    """The agent's ``AuditEntry``, its texts capped as ``insert`` caps them."""

    kind: Kind
    action: Text200
    surface: Surface
    actor: Actor
    outcome: Outcome
    client_ip: Text200 | None = None
    session_id: Text200 | None = None
    turn_id: Text200 | None = None
    tool_use_id: Text1024 | None = None
    tier: Literal["read", "write", "outward"] | None = None
    input_hash: Text1024 | None = None
    input_summary: Text1000 | None = None
    approval_id: Text200 | None = None
    request_id: Text500 | None = None
    detail: Text500 | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    cost_usd: float | None = None
    cost_priced: bool | None = None
    cost_estimated_usd: float | None = None


class Done(BaseModel):
    done: bool


@router.post("/audit/record", name="crud_agent_audit_record")
async def record(body: Record, state: StateDep) -> Done:
    fields = body.model_dump(exclude={"actor"})
    entry = audit_log.Entry(
        actor_kind=body.actor.kind, actor_id=body.actor.id, actor_label=body.actor.label, **fields
    )
    await _in_conn(state, lambda conn: audit_log.insert(conn, entry))
    return Done(done=True)


class ListFilter(_Body):
    """The agent's ``AuditFilter``."""

    kind: Kind | None = None
    outcome: Outcome | None = None
    surface: Surface | None = None
    action: Text200 | None = None
    session_id: Text200 | None = None
    principal: Text200 | None = None
    since: datetime | None = None
    until: datetime | None = None
    before: Annotated[int, Field(ge=0)] | None = None
    limit: int | None = None


class Row(BaseModel):
    id: str
    at: datetime
    kind: str
    action: str
    surface: str
    principal_kind: str
    principal_id: str
    principal_label: str
    client_ip: str | None
    session_id: str | None
    turn_id: str | None
    tool_use_id: str | None
    tier: str | None
    input_hash: str | None
    input_summary: str | None
    approval_id: str | None
    approved_by_kind: str | None
    approved_by_id: str | None
    approved_by_label: str | None
    outcome: str
    detail: str | None
    started_at: datetime | None
    finished_at: datetime | None
    duration_ms: int | None
    cost_usd: float | None
    cost_priced: bool | None
    cost_estimated_usd: float | None


class Page(BaseModel):
    rows: list[Row]
    next: str | None


@router.post("/audit/list", name="crud_agent_audit_list")
async def list_rows(body: ListFilter, state: StateDep) -> Page:
    f = audit_log.Filter(**body.model_dump())
    rows, cursor = await _in_conn(state, lambda conn: audit_log.page(conn, f))
    return Page(rows=[Row.model_validate(r) for r in rows], next=cursor)


class Prune(_Body):
    days: int


class Pruned(BaseModel):
    deleted: int


@router.post("/audit/prune", name="crud_agent_audit_prune")
async def prune(body: Prune, state: StateDep) -> Pruned:
    deleted = await _in_conn(state, lambda conn: audit_log.prune(conn, body.days))
    return Pruned(deleted=deleted)
