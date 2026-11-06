"""The projects worker's flow-run routes (#5298, inventory J6-J15, J21-J23): thin calls
to the flows component's `FlowStore`, for `crud/projects.py` `RemoteFlowStore`.

Every write is natural (plan D5): sent again, it answers as it did the first time. A
write that announces the run (`insert_run`, `project`, `move_hop`) runs here, so its
`flow_run.changed` event still commits with it (D7).
"""

from __future__ import annotations

from typing import Any

from fastapi import status
from pydantic import BaseModel

from scadbuddy.core.problems import ApiError
from scadbuddy.crud.guard import crud_router
from scadbuddy.flows.component import FlowsDep
from scadbuddy.flows.models import Decision, Proposal, Run
from scadbuddy.flows.store import HopMovedError
from scadbuddy.workflows.flow_models import (
    FlowRecord,
    FlowWaiting,
    Hop,
    HopRecord,
    ProjectionWrite,
)

#: A decision refused because its call left the hop it was made for.
HOP_MOVED = "hop_moved"

router = crud_router("projects")


class Floor(BaseModel):
    floor: int | None


class HopMove(BaseModel):
    call_id: str
    record: HopRecord
    proposal: Proposal | None = None


class DecisionWrite(BaseModel):
    decision: Decision
    at_hop: Hop | None = None


class Recorded(BaseModel):
    recorded: bool


class Tiers(BaseModel):
    tiers: list[str] | None


class Expiry(BaseModel):
    seconds: int


def _missing(what: str) -> ApiError:
    return ApiError(status.HTTP_404_NOT_FOUND, f"No such {what}.")


@router.post("/runs", name="crud_projects_insert_run")
async def insert_run(body: FlowRecord, flows: FlowsDep) -> Run:
    return await flows.store.insert_run(body)


# Before `/runs/{run_id}`, which would take `talking-to` for a run id.
@router.get("/runs/talking-to", name="crud_projects_run_talking_to")
async def run_talking_to(session_id: str, call_id: str, flows: FlowsDep) -> Run | None:
    return await flows.store.run_talking_to(session_id, call_id)


@router.get("/runs/{run_id}", name="crud_projects_get_run")
async def get_run(run_id: str, flows: FlowsDep) -> Run:
    run = await flows.store.get_run(run_id)
    if run is None:
        raise _missing("run")
    return run


@router.post("/runs/{run_id}/project", name="crud_projects_project")
async def project(run_id: str, body: ProjectionWrite, flows: FlowsDep) -> Run | None:
    if body.run_id != run_id:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, "The write names another run.")
    return await flows.store.project(body)


@router.get("/runs/{run_id}/reset-floor", name="crud_projects_reset_floor")
async def reset_floor(run_id: str, flows: FlowsDep) -> Floor:
    return Floor(floor=await flows.store.reset_floor(run_id))


@router.post("/runs/{run_id}/hops", name="crud_projects_move_hop")
async def move_hop(run_id: str, body: HopMove, flows: FlowsDep) -> FlowWaiting | None:
    moved = await flows.store.move_hop(run_id, body.call_id, body.record, proposal=body.proposal)
    if moved is not None:
        return moved
    # Sent again after it moved the call (D5): the call's entry, waiting or resolved
    # since, carries this very record, and only one move ever leaves hop `parent`.
    entry = await flows.store.hop_entry(run_id, body.call_id)
    if entry is None or entry.hop != "person" or body.record not in entry.hops:
        return None
    mine = entry.hops.index(body.record)
    return entry.model_copy(update={"hops": entry.hops[: mine + 1], "resolved_at": None})


@router.get("/decisions/{request_id}", name="crud_projects_get_decision")
async def get_decision(request_id: str, flows: FlowsDep) -> Decision:
    decision = await flows.store.get_decision(request_id)
    if decision is None:
        raise _missing("decision")
    return decision


@router.post("/decisions", name="crud_projects_record_decision")
async def record_decision(body: DecisionWrite, flows: FlowsDep) -> Recorded:
    decision = body.decision
    try:
        return Recorded(recorded=await flows.store.record_decision(decision, at_hop=body.at_hop))
    except HopMovedError:
        # Sent again after it was recorded (D5), the call may have moved on since.
        held = await flows.store.get_decision(decision.request_id)
        if held is not None and _same(held, decision):
            return Recorded(recorded=True)
        raise ApiError(
            status.HTTP_409_CONFLICT,
            "The call is no longer at the hop the decision was made for.",
            reason=HOP_MOVED,
        ) from None


def _same(held: Decision, decision: Decision) -> bool:
    """This very decision: as `FlowStore.record_decision` tells it from another, and by
    who made it (as `flows/operations.py` `run_escalate` checks its own denial)."""
    held_by: tuple[Any, ...] = (held.outcome, held.response, held.responder)
    return held_by == (decision.outcome, decision.response, decision.responder)


@router.delete(
    "/decisions/{request_id}",
    name="crud_projects_delete_decision",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_decision(request_id: str, flows: FlowsDep) -> None:
    await flows.store.delete_decision(request_id)


@router.get("/proposals/{request_id}", name="crud_projects_get_proposal")
async def get_proposal(request_id: str, flows: FlowsDep) -> Proposal:
    proposal = await flows.store.get_proposal(request_id)
    if proposal is None:
        raise _missing("proposal")
    return proposal


@router.get("/sessions/{session}/tiers", name="crud_projects_parent_tiers")
async def parent_tiers(session: str, principal: str, flows: FlowsDep) -> Tiers:
    return Tiers(tiers=await flows.store.parent_tiers(session, principal))


@router.get("/approval-expiry", name="crud_projects_approval_expiry")
async def approval_expiry(flows: FlowsDep) -> Expiry:
    return Expiry(seconds=await flows.store.approval_expiry_s())
