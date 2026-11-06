"""The projects worker's flow-run routes (#5298, inventory J6-J23): thin calls to the
flows component's `FlowStore`, for `crud/projects.py` `RemoteFlowStore`.

Every write is natural (plan D5): sent again, it answers as it did the first time. A
write that announces the run (`insert_run`, `project`, `move_hop`) runs here, so its
`flow_run.changed` event still commits with it (D7). Logic that holds a lock runs here
too (D6): a grant's consume chooses its rule under the grant's row lock, and a Reset's
Temporal call runs under the run's row lock.
"""

from __future__ import annotations

from typing import Any

from fastapi import status
from pydantic import BaseModel
from temporalio.service import RPCError, RPCStatusCode

from scadbuddy.core.problems import ApiError
from scadbuddy.crud.guard import crud_router
from scadbuddy.flows.component import FlowsDep
from scadbuddy.flows.grants import ConsumeRequest, Grant, GrantUse
from scadbuddy.flows.models import Decision, Proposal, Run
from scadbuddy.flows.operations import reset_execution
from scadbuddy.flows.store import HopMovedError, ResetBeforeFloorError, ResetSupersededError
from scadbuddy.workflows.flow_models import (
    FlowRecord,
    FlowWaiting,
    Hop,
    HopRecord,
    ProjectionWrite,
)

#: A decision refused because its call left the hop it was made for.
HOP_MOVED = "hop_moved"
#: A Reset refused because another Reset replaced the execution first.
RESET_SUPERSEDED = "reset-superseded"
#: A Reset refused because its point is before the entries the row keeps.
RESET_BEFORE_FLOOR = "reset-before-floor"
#: A Reset Temporal refused for good; any other failure of its call is a 5xx, retried.
RESET_REFUSED = "reset-refused"
#: The gRPC statuses that no retry of the same Reset mends.
RESET_REFUSALS = frozenset(
    {
        RPCStatusCode.INVALID_ARGUMENT,
        RPCStatusCode.NOT_FOUND,
        RPCStatusCode.ALREADY_EXISTS,
        RPCStatusCode.PERMISSION_DENIED,
        RPCStatusCode.FAILED_PRECONDITION,
        RPCStatusCode.OUT_OF_RANGE,
        RPCStatusCode.UNIMPLEMENTED,
        RPCStatusCode.UNAUTHENTICATED,
    }
)

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


class Used(BaseModel):
    use: GrantUse


class NotUsed(BaseModel):
    outside: str


class ResetWrite(BaseModel):
    replaced: str
    point: int
    request_id: str
    #: Who asked, for the Reset's reason; absent from a worker before it was sent.
    responder: str | None = None


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


@router.post("/runs/{run_id}/reset", name="crud_projects_reset_run")
async def reset_run(run_id: str, body: ResetWrite, flows: FlowsDep) -> Run | None:
    """`FlowStore.reset_run`, with Temporal's Reset made here under the row's lock. Sent
    again with its `request_id`, it answers the row as that Reset left it."""
    client = flows.client
    if client is None:
        raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, "Flows are not connected.")
    run = await flows.store.get_run(run_id)
    if run is None:
        return None
    workflow_id = run.workflow_id

    async def reset() -> str:
        return await reset_execution(
            client,
            workflow_id,
            replaced=body.replaced,
            point=body.point,
            request_id=body.request_id,
            # Without a responder, the operation's id: its row names who asked.
            reason=f"flow reset by {body.responder or f'operation {body.request_id}'}",
        )

    try:
        return await flows.store.reset_run(
            run_id,
            replaced=body.replaced,
            point=body.point,
            request_id=body.request_id,
            reset=reset,
        )
    except ResetSupersededError as err:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            "Another Reset moved the run first.",
            reason=RESET_SUPERSEDED,
            run=err.run.model_dump(mode="json"),
        ) from None
    except ResetBeforeFloorError as err:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            str(err),
            reason=RESET_BEFORE_FLOOR,
            floor=err.floor,
        ) from None
    except RPCError as err:
        if err.status not in RESET_REFUSALS:
            raise  # a 5xx: the worker tries again within its budget
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"Temporal refused the Reset: {err.message}",
            reason=RESET_REFUSED,
            grpc_status=err.status.name,
        ) from None


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


@router.post("/decisions/grant", name="crud_projects_record_grant_decision")
async def record_grant_decision(body: Decision, flows: FlowsDep) -> Recorded:
    return Recorded(recorded=await flows.store.record_grant_decision(body))


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


@router.delete(
    "/decisions/{request_id}/grant",
    name="crud_projects_withdraw_grant_decision",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def withdraw_grant_decision(request_id: str, flows: FlowsDep) -> None:
    await flows.store.withdraw_grant_decision(request_id)


@router.get("/grants/{run_id}", name="crud_projects_get_grant")
async def get_grant(run_id: str, flows: FlowsDep) -> Grant | None:
    return await flows.store.get_grant(run_id)


@router.get("/grant-uses/{request_id}", name="crud_projects_grant_use")
async def grant_use(request_id: str, flows: FlowsDep) -> GrantUse | None:
    return await flows.store.grant_use(request_id)


@router.post("/grants/{run_id}/consume", name="crud_projects_consume_grant")
async def consume_grant(run_id: str, body: ConsumeRequest, flows: FlowsDep) -> Used | NotUsed:
    """The call's rule chosen and spent under the grant's row lock, here; natural by
    `request_id` (`FlowStore.consume`)."""
    taken = await flows.store.consume(run_id, body)
    return NotUsed(outside=taken) if isinstance(taken, str) else Used(use=taken)


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
