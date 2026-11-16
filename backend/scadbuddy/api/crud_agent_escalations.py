"""A flow run's calls at the starting session's hop behind CRUD routes (#5299 step 10): under
``/api/v1/internal/crud/agent/``, for the ``agent`` credential only (`crud_router`). Each route is
one statement or transaction of the agent's ``flows/escalations.ts`` `Escalations`, or
``flows/queue.ts`` `FlowQueue.forRun`'s read of a run's child entries; the SQL is
``agent_data/escalations.py``. The agent calls them once ``…/areas`` lists ``escalations``, and
its own SQL until then (D9).

Area ``escalations``:

- ``GET …/escalations/entry?request_id=…``: a run's child entry, with its hop and parent;
- ``POST …/escalations/pass-up``, ``…/claim-deny`` and ``…/release-deny``: the parent's answer,
  each written only while the entry is still at hop ``parent`` for the session it names;
- ``POST …/escalations/move-due``: the hop's timeout, and a deny's claim past its lease;
- ``POST …/escalations/waiting``: the calls a delivery pass sends;
- ``POST …/escalations/claim``, ``…/unclaim`` and ``…/skip``: a delivery's claim, handed back
  when its turn does not start, or the calls a session cannot be asked about;
- ``POST …/escalations/deliveries/page``, ``…/deliveries/sent`` and ``…/deliveries/forget``: the
  record of a run's own calls sent;
- ``GET …/escalations/run-entries?flow_run_id=…``: a run's child entries for its queue.

Ids travel in the body or the query, never the path; a malformed session id names nothing.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import Query
from pydantic import BaseModel, Field

from scadbuddy.agent_data import escalations
from scadbuddy.api.crud_agent_gate import (
    NAME_MAX,
    REQUEST_ID_MAX,
    Name,
    RequestId,
    Text,
    _Body,
    _in_conn,
)
from scadbuddy.api.deps import StateDep
from scadbuddy.crud.guard import crud_router

router = crud_router("agent")

#: The most request ids one call takes.
IDS_MAX = 1000
#: The longest grace a delivery pass asks for, in seconds (a day).
GRACE_MAX_S = 86_400

RequestIdQuery = Annotated[str, Query(min_length=1, max_length=REQUEST_ID_MAX)]
Ids = Annotated[list[RequestId], Field(max_length=IDS_MAX)]


class Entry(BaseModel):
    session_id: str
    hop: Literal["parent", "person"] | None
    due: bool
    claimed: bool
    parent_session: str | None
    parent_principal: str | None


class EntryAnswer(BaseModel):
    entry: Entry | None


@router.get("/escalations/entry", name="crud_agent_escalations_entry")
async def get_entry(state: StateDep, request_id: RequestIdQuery) -> EntryAnswer:
    row = await _in_conn(state, lambda conn: escalations.entry(conn, request_id))
    return EntryAnswer(entry=None if row is None else Entry.model_validate(row))


class PassUp(_Body):
    request_id: RequestId
    session: Name
    reason: Text | None = None


class Moved(BaseModel):
    moved: bool


@router.post("/escalations/pass-up", name="crud_agent_escalations_pass_up")
async def pass_up(body: PassUp, state: StateDep) -> Moved:
    moved = await _in_conn(
        state, lambda conn: escalations.pass_up(conn, body.request_id, body.session, body.reason)
    )
    return Moved(moved=moved)


class ClaimDeny(_Body):
    request_id: RequestId
    session: Name


class Claimed(BaseModel):
    #: The deadline before the claim, which a release puts back; null when nothing was claimed.
    hop_until: datetime | None


@router.post("/escalations/claim-deny", name="crud_agent_escalations_claim_deny")
async def claim_deny(body: ClaimDeny, state: StateDep) -> Claimed:
    held = await _in_conn(
        state, lambda conn: escalations.claim_deny(conn, body.request_id, body.session)
    )
    return Claimed(hop_until=held)


class ReleaseDeny(_Body):
    request_id: RequestId
    session: Name
    hop_until: datetime


class Done(BaseModel):
    done: bool


@router.post("/escalations/release-deny", name="crud_agent_escalations_release_deny")
async def release_deny(body: ReleaseDeny, state: StateDep) -> Done:
    released = await _in_conn(
        state,
        lambda conn: escalations.release_deny(conn, body.request_id, body.session, body.hop_until),
    )
    return Done(done=released)


class Count(BaseModel):
    moved: int


@router.post("/escalations/move-due", name="crud_agent_escalations_move_due")
async def move_due(state: StateDep) -> Count:
    return Count(moved=await _in_conn(state, escalations.move_due))


class WaitingBody(_Body):
    grace_s: Annotated[float, Field(ge=0, le=GRACE_MAX_S)]


class Waiting(BaseModel):
    request_id: str
    flow_run_id: str
    tool: str
    summary: str
    outside_reason: str | None
    hop_until: datetime
    past_grace: bool
    session_id: str
    parent_session: str
    parent_principal: str


class WaitingAnswer(BaseModel):
    calls: list[Waiting]


@router.post("/escalations/waiting", name="crud_agent_escalations_waiting")
async def waiting(body: WaitingBody, state: StateDep) -> WaitingAnswer:
    rows = await _in_conn(state, lambda conn: escalations.waiting(conn, body.grace_s))
    return WaitingAnswer(calls=[Waiting.model_validate(r) for r in rows])


class Calls(_Body):
    session: Name
    child: Ids = Field(default_factory=list)
    own: Ids = Field(default_factory=list)


class Won(BaseModel):
    claimed: list[str]


@router.post("/escalations/claim", name="crud_agent_escalations_claim")
async def claim(body: Calls, state: StateDep) -> Won:
    won = await _in_conn(
        state, lambda conn: escalations.claim(conn, body.session, list(body.child), list(body.own))
    )
    return Won(claimed=won)


@router.post("/escalations/unclaim", name="crud_agent_escalations_unclaim")
async def unclaim(body: Calls, state: StateDep) -> Done:
    def run(conn: Any) -> bool:
        escalations.unclaim(conn, body.session, list(body.child), list(body.own))
        return True

    return Done(done=await _in_conn(state, run))


@router.post("/escalations/skip", name="crud_agent_escalations_skip")
async def skip(body: Calls, state: StateDep) -> Done:
    def run(conn: Any) -> bool:
        escalations.skip(conn, body.session, list(body.child), list(body.own))
        return True

    return Done(done=await _in_conn(state, run))


class Page(_Body):
    after: Annotated[str, Field(max_length=REQUEST_ID_MAX)]
    limit: Annotated[int, Field(ge=1, le=IDS_MAX)]


class IdList(BaseModel):
    ids: list[str]


@router.post("/escalations/deliveries/page", name="crud_agent_escalations_deliveries_page")
async def deliveries_page(body: Page, state: StateDep) -> IdList:
    ids = await _in_conn(
        state, lambda conn: escalations.deliveries_page(conn, body.after, body.limit)
    )
    return IdList(ids=ids)


class IdsBody(_Body):
    ids: Ids


@router.post("/escalations/deliveries/sent", name="crud_agent_escalations_deliveries_sent")
async def deliveries_sent(body: IdsBody, state: StateDep) -> IdList:
    return IdList(ids=await _in_conn(state, lambda conn: escalations.sent(conn, list(body.ids))))


@router.post("/escalations/deliveries/forget", name="crud_agent_escalations_deliveries_forget")
async def deliveries_forget(body: IdsBody, state: StateDep) -> Done:
    def run(conn: Any) -> bool:
        escalations.forget_deliveries(conn, list(body.ids))
        return True

    return Done(done=await _in_conn(state, run))


class RunEntry(BaseModel):
    request_id: str
    kind: Literal["approval", "answer"]
    session_id: str
    tool: str
    summary: str
    input_hash: str | None
    hop: Literal["parent", "person"] | None
    #: An ISO time, or ``infinity`` while a deny's claim holds the entry.
    hop_until: str | None
    outside_reason: str | None
    hops: list[dict[str, Any]] | None
    created_at: datetime
    expires_at: datetime
    expiring: bool


class RunEntries(BaseModel):
    entries: list[RunEntry]


@router.get("/escalations/run-entries", name="crud_agent_escalations_run_entries")
async def run_entries(
    state: StateDep, flow_run_id: Annotated[str, Query(min_length=1, max_length=NAME_MAX)]
) -> RunEntries:
    rows = await _in_conn(state, lambda conn: escalations.run_entries(conn, flow_run_id))
    return RunEntries(entries=[RunEntry.model_validate(r) for r in rows])
