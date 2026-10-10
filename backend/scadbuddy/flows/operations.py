"""A person's answer and approval for a flow run's parked calls, as operations on the
`projects` queue (#1057, plan 2026-10-09-durable-phase-6-flows.md decisions A and B).

Each records the decision in `workflow_run_decisions` first, then tells the harness with
its own public Update (`provide_callback_result`, `tool_approval`) under the request id
as the Update id, so a retried run resends the same Update. A decision the harness
refuses is deleted again: no row says `approved` for a call that never ran.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import status
from temporal_agent_harness.harness.agent_client import (
    AgentClient,
    CallbackResultError,
    ToolApprovalError,
)
from temporalio import activity
from temporalio.service import RPCError, RPCStatusCode

from scadbuddy.core.problems import ApiError
from scadbuddy.flows.models import TERMINAL, Decision, Run
from scadbuddy.flows.store import FlowStore
from scadbuddy.operations.kinds import KindsBuild, OperationKind

if TYPE_CHECKING:
    from scadbuddy.core.components import Components, Core

FLOW_ANSWER = "flow_answer"
FLOW_DECIDE = "flow_decide"
STALE_ENTRY = "https://scadbuddy.dev/problems/stale-entry"
ALREADY_RESOLVED = "https://scadbuddy.dev/problems/already-resolved"
RUN_CLOSED = "https://scadbuddy.dev/problems/flow-run-closed"


def request_id(run: Run, call_id: str) -> str:
    """The decision's id (spec 2026-10-01 §6.6): the run's current execution's, so a
    card from before a Reset never matches."""
    return f"flow:{run.id}:{run.workflow_run_id}:{call_id}"


def _stale() -> ApiError:
    return ApiError(
        status.HTTP_409_CONFLICT,
        "That question or approval is no longer waiting: the run moved on. Reload it.",
        type_=STALE_ENTRY,
    )


def _resolved() -> ApiError:
    return ApiError(status.HTTP_409_CONFLICT, "That was answered already.", type_=ALREADY_RESOLVED)


def flow_kinds(store: FlowStore) -> list[OperationKind]:
    """Both kinds over `store`; the `projects` worker serves them."""

    async def check(kind: str, request: dict[str, Any]) -> dict[str, Any]:
        run = await store.get_run(str(request["run_id"]))
        if run is None:
            raise ApiError(status.HTTP_404_NOT_FOUND, "No such flow run.")
        if run.status in TERMINAL:
            raise ApiError(status.HTTP_409_CONFLICT, "The run has ended.", type_=RUN_CLOSED)
        call_id = str(request["call_id"])
        if not any(w.call_id == call_id and w.kind == kind for w in run.waiting_on):
            raise _stale()
        try:
            described = await activity.client().get_workflow_handle(run.workflow_id).describe()
        except RPCError as err:
            if err.status == RPCStatusCode.NOT_FOUND:
                raise _stale() from None
            raise
        if described.run_id != run.workflow_run_id:
            raise _stale()
        rid = request_id(run, call_id)
        if await store.get_decision(rid) is not None:
            raise _resolved()
        return {"request_id": rid, "workflow_id": run.workflow_id}

    async def record(request: dict[str, Any], checked: dict[str, Any], decision: Decision) -> None:
        if not await store.record_decision(decision):
            raise _resolved()

    def _decision(request: dict[str, Any], checked: dict[str, Any], **fields: Any) -> Decision:
        rid = str(checked["request_id"])
        workflow_run_id = rid.split(":")[2]
        return Decision(
            request_id=rid,
            run_id=str(request["run_id"]),
            workflow_run_id=workflow_run_id,
            call_id=str(request["call_id"]),
            responder="browser",
            **fields,
        )

    async def check_answer(request: dict[str, Any]) -> dict[str, Any]:
        return await check("answer", request)

    async def run_answer(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        response = {"answer": str(request["answer"])}
        decision = _decision(request, checked, kind="answer", outcome="answered", response=response)
        await record(request, checked, decision)
        client = AgentClient(activity.client(), str(checked["workflow_id"]))
        try:
            await client.provide_callback_result(
                decision.call_id, result=response, update_id=decision.request_id
            )
        except CallbackResultError as err:
            await store.delete_decision(decision.request_id)
            if err.error_type == "MalformedCallbackResult":
                raise ApiError(
                    status.HTTP_422_UNPROCESSABLE_CONTENT, "The run cannot take that answer."
                ) from None
            if err.error_type == "CallbackAlreadyResolved":
                raise _resolved() from None
            raise _stale() from None
        return {"run_id": decision.run_id, "call_id": decision.call_id, "outcome": "answered"}

    async def check_decide(request: dict[str, Any]) -> dict[str, Any]:
        return await check("approval", request)

    async def run_decide(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        approved = bool(request["approved"])
        reason = request.get("reason")
        decision = _decision(
            request,
            checked,
            kind="approval",
            outcome="approved" if approved else "denied",
            response={} if reason is None else {"reason": str(reason)},
        )
        await record(request, checked, decision)
        client = AgentClient(activity.client(), str(checked["workflow_id"]))
        try:
            await client.approve_tool(
                decision.call_id,
                approved=approved,
                reason=None if reason is None else str(reason),
                update_id=decision.request_id,
            )
        except ToolApprovalError as err:
            await store.delete_decision(decision.request_id)
            if err.error_type == "ToolApprovalAlreadyResolved":
                raise _resolved() from None
            raise _stale() from None
        return {"run_id": decision.run_id, "call_id": decision.call_id, "outcome": decision.outcome}

    # A transport failure is retried: the same Update id makes the resend safe, and a
    # decision already recorded by the first attempt is the same decision.
    return [
        OperationKind(
            name=FLOW_ANSWER, check=check_answer, run=run_answer, run_attempts=3, queue="projects"
        ),
        OperationKind(
            name=FLOW_DECIDE, check=check_decide, run=run_decide, run_attempts=3, queue="projects"
        ),
    ]


def _kinds(core: Core, components: Components) -> list[OperationKind]:
    from scadbuddy.flows.component import FLOWS

    return flow_kinds(components.get(FLOWS).store)


OPERATION_KINDS: KindsBuild = _kinds
