"""A person's answer and approval for a flow run's parked calls, as operations on the
`projects` queue (#1057, plan 2026-10-09-durable-phase-6-flows.md decisions A and B).

Each records the decision in `workflow_run_decisions` first, then tells the harness with
its own public Update (`provide_callback_result`, `tool_approval`) under the request id
as the Update id, so a retried run resends the same Update and Temporal answers it once.

The row lives as long as the Update may have landed (#2247):

- The harness refusing the Update is definite: the row is deleted.
- Any other failure (an RPC error, a deadline, the reply lost) is settled by asking
  Temporal for the Update by its id. Completed means it landed: the row stays and the
  operation succeeds. No such Update means it never landed: the row is deleted, so a
  failed send never blocks the answer that would resolve the call. When Temporal
  cannot say, the row stays and the error is raised; the next attempt resends.
- Cancelled (the activity's deadline, a worker stopping): the row stays and the
  cancellation is raised. Nothing is asked while cancelling; the next attempt resends.

A retry, or a new request with the same decision, is let through by `check` on a
matching row before `waiting_on` is looked at: a call the landed Update resolved is no
longer waiting, and the resend then gets Temporal's answer for it instead of a 409. Any
other decision for the call is refused as answered.

Residual window: a row kept because the Update's fate was unknown (or because the
operation was cancelled) says `approved`, `denied` or `answered` before the harness has
it. If no attempt ever resends it and the call is later resolved another way (the
approval timer, a Reset), the row disagrees with the run. The window is the time until
the next attempt; it stays open only once every attempt has been exhausted without
Temporal ever answering.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from fastapi import status
from temporal_agent_harness.harness.agent_client import (
    AgentClient,
    CallbackResultError,
    ToolApprovalError,
)
from temporalio import activity
from temporalio.api.common.v1 import WorkflowExecution
from temporalio.api.enums.v1 import ResetReapplyExcludeType
from temporalio.api.workflowservice.v1 import ResetWorkflowExecutionRequest
from temporalio.client import WorkflowUpdateFailedError
from temporalio.service import RPCError, RPCStatusCode

from scadbuddy.core.problems import ApiError
from scadbuddy.flows.history import outward_since
from scadbuddy.flows.models import TERMINAL, Decision, Run
from scadbuddy.flows.store import FlowStore, ResetSupersededError
from scadbuddy.operations.kinds import KindsBuild, OperationKind

if TYPE_CHECKING:
    from scadbuddy.core.components import Components, Core

FLOW_ANSWER = "flow_answer"
FLOW_DECIDE = "flow_decide"
STALE_ENTRY = "https://scadbuddy.dev/problems/stale-entry"
ALREADY_RESOLVED = "https://scadbuddy.dev/problems/already-resolved"
RUN_CLOSED = "https://scadbuddy.dev/problems/flow-run-closed"
FLOW_RESET = "flow_reset"
RESET_POINT = "https://scadbuddy.dev/problems/flow-reset-point"
RESET_CHANGED = "https://scadbuddy.dev/problems/flow-reset-changed"
RESET_UNRECORDED = "https://scadbuddy.dev/problems/flow-reset-unrecorded"
#: How long asking Temporal whether a decision's Update landed may take.
LANDED_PROBE = timedelta(seconds=10)


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


def _wanted(kind: str, request: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """The outcome and response a request asks for, as its row records them."""
    if kind == "answer":
        return "answered", {"answer": str(request["answer"])}
    reason = request.get("reason")
    return (
        "approved" if bool(request["approved"]) else "denied",
        {} if reason is None else {"reason": str(reason)},
    )


def _same(existing: Decision, kind: str, request: dict[str, Any]) -> bool:
    """Whether a recorded row is the decision `request` asks for (a resend), not another."""
    outcome, response = _wanted(kind, request)
    return existing.kind == kind and existing.outcome == outcome and existing.response == response


def flow_kinds(store: FlowStore) -> list[OperationKind]:
    """Both kinds over `store`; the `projects` worker serves them."""

    async def check(kind: str, request: dict[str, Any]) -> dict[str, Any]:
        run = await store.get_run(str(request["run_id"]))
        if run is None:
            raise ApiError(status.HTTP_404_NOT_FOUND, "No such flow run.")
        if run.status in TERMINAL:
            raise ApiError(status.HTTP_409_CONFLICT, "The run has ended.", type_=RUN_CLOSED)
        call_id = str(request["call_id"])
        try:
            described = await activity.client().get_workflow_handle(run.workflow_id).describe()
        except RPCError as err:
            if err.status == RPCStatusCode.NOT_FOUND:
                raise _stale() from None
            raise
        if described.run_id != run.workflow_run_id:
            raise _stale()
        rid = request_id(run, call_id)
        # Before `waiting_on`: a call whose Update landed is no longer waiting, and its
        # resend must reach Temporal's answer, not a 409 (#2247).
        existing = await store.get_decision(rid)
        if existing is not None:
            if not _same(existing, kind, request):
                raise _resolved()
        elif not any(w.call_id == call_id and w.kind == kind for w in run.waiting_on):
            raise _stale()
        return {
            "request_id": rid,
            "workflow_id": run.workflow_id,
            "workflow_run_id": run.workflow_run_id,
        }

    async def record(request: dict[str, Any], checked: dict[str, Any], decision: Decision) -> None:
        if not await store.record_decision(decision):
            raise _resolved()

    async def forget(rid: str) -> None:
        """Undo a recorded decision whose Update did not land; never masks the failure."""
        try:
            await store.delete_decision(rid)
        except Exception:
            activity.logger.exception("could not delete the unsent flow decision %s", rid)

    async def landed(checked: dict[str, Any], rid: str) -> bool | None:
        """Whether the Update `rid` completed on the run: True, False (Temporal has no
        such Update), or None (it could not say, or the Update failed in its handler)."""
        run_id = str(checked["workflow_run_id"])
        workflow = activity.client().get_workflow_handle(str(checked["workflow_id"]), run_id=run_id)
        handle = workflow.get_update_handle(rid, workflow_run_id=run_id)
        try:
            await handle.result(rpc_timeout=LANDED_PROBE)
        except RPCError as err:
            if err.status == RPCStatusCode.NOT_FOUND:
                return False
            activity.logger.warning("could not tell whether flow decision %s landed: %s", rid, err)
            return None
        except WorkflowUpdateFailedError:
            return None
        return True

    async def settle(checked: dict[str, Any], rid: str, err: BaseException) -> bool:
        """After a send that failed other than by a refusal: True when the Update landed
        anyway (the decision stands); otherwise False, having deleted the row only when
        Temporal says the Update never existed."""
        if isinstance(err, asyncio.CancelledError):
            return False
        verdict = await landed(checked, rid)
        if verdict is False:
            await forget(rid)
        return verdict is True

    def _decision(request: dict[str, Any], checked: dict[str, Any], **fields: Any) -> Decision:
        return Decision(
            request_id=str(checked["request_id"]),
            run_id=str(request["run_id"]),
            workflow_run_id=str(checked["workflow_run_id"]),
            call_id=str(request["call_id"]),
            responder="browser",
            **fields,
        )

    async def check_answer(request: dict[str, Any]) -> dict[str, Any]:
        return await check("answer", request)

    async def run_answer(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        outcome, response = _wanted("answer", request)
        decision = _decision(request, checked, kind="answer", outcome=outcome, response=response)
        await record(request, checked, decision)
        client = AgentClient(activity.client(), str(checked["workflow_id"]))
        try:
            await client.provide_callback_result(
                decision.call_id, result=response, update_id=decision.request_id
            )
        except CallbackResultError as err:
            await forget(decision.request_id)
            if err.error_type == "MalformedCallbackResult":
                raise ApiError(
                    status.HTTP_422_UNPROCESSABLE_CONTENT, "The run cannot take that answer."
                ) from None
            if err.error_type == "CallbackAlreadyResolved":
                raise _resolved() from None
            raise _stale() from None
        except BaseException as err:
            if not await settle(checked, decision.request_id, err):
                raise
        return {"run_id": decision.run_id, "call_id": decision.call_id, "outcome": "answered"}

    async def check_decide(request: dict[str, Any]) -> dict[str, Any]:
        return await check("approval", request)

    async def run_decide(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        approved = bool(request["approved"])
        reason = request.get("reason")
        outcome, response = _wanted("approval", request)
        decision = _decision(request, checked, kind="approval", outcome=outcome, response=response)
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
            await forget(decision.request_id)
            if err.error_type == "ToolApprovalAlreadyResolved":
                raise _resolved() from None
            raise _stale() from None
        except BaseException as err:
            if not await settle(checked, decision.request_id, err):
                raise
        return {"run_id": decision.run_id, "call_id": decision.call_id, "outcome": decision.outcome}

    async def check_reset(request: dict[str, Any]) -> dict[str, Any]:
        run = await store.get_run(str(request["run_id"]))
        if run is None:
            raise ApiError(status.HTTP_404_NOT_FOUND, "No such flow run.")
        if any(w.history_length is None for w in run.waiting_on):
            # Parked before 6e recorded where: a Reset cannot tell whether such a call
            # comes back, and guessing would leave a card for a call nothing waits on.
            raise ApiError(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "This run parked a call before ScadBuddy recorded where in its history,"
                " so it cannot be reset safely. Answer the call, or start a new run.",
                type_=RESET_UNRECORDED,
            )
        client = activity.client()
        try:
            described = await client.get_workflow_handle(run.workflow_id).describe()
        except RPCError as err:
            if err.status == RPCStatusCode.NOT_FOUND:
                raise ApiError(status.HTTP_404_NOT_FOUND, "The run's history is gone.") from None
            raise
        event_id = int(request["event_id"])
        preview = await outward_since(client, run, event_id, run_id=described.run_id)
        if described.run_id != str(request["workflow_run_id"]):
            # Another Reset replaced the execution the preview read: past its point, the
            # same event ids name other events, so the point itself means something else.
            raise ApiError(
                status.HTTP_409_CONFLICT,
                "Another Reset moved the run since that preview; check the new one.",
                type_=RESET_CHANGED,
                preview=preview.model_dump(mode="json"),
            )
        if not preview.valid:
            raise ApiError(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"Event {event_id} is not a point a run resets to: name a completed"
                " workflow task before the run's last event.",
                type_=RESET_POINT,
            )
        if any(c.scheduled_event_id > int(request["as_of_event_id"]) for c in preview.calls):
            raise ApiError(
                status.HTTP_409_CONFLICT,
                "The run made more outward calls since that preview; check the new one.",
                type_=RESET_CHANGED,
                preview=preview.model_dump(mode="json"),
            )
        return {"workflow_id": run.workflow_id, "replaced": described.run_id}

    async def run_reset(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        client = activity.client()
        workflow_id = str(checked["workflow_id"])
        replaced = str(checked["replaced"])
        event_id = int(request["event_id"])

        async def reset() -> str:
            answer = await client.workflow_service.reset_workflow_execution(
                ResetWorkflowExecutionRequest(
                    namespace=client.namespace,
                    workflow_execution=WorkflowExecution(workflow_id=workflow_id, run_id=replaced),
                    reason=f"flow reset by {request.get('responder') or 'browser'}",
                    workflow_task_finish_event_id=event_id,
                    # The operation's own id: a retried run resets once, and the row
                    # records it to tell that retry from another Reset.
                    request_id=str(activity.info().workflow_id),
                    # Every answer, approval and `close` after the point is undone with it:
                    # the calls past it run again and park again for a person.
                    reset_reapply_exclude_types=[
                        ResetReapplyExcludeType.RESET_REAPPLY_EXCLUDE_TYPE_SIGNAL,
                        ResetReapplyExcludeType.RESET_REAPPLY_EXCLUDE_TYPE_UPDATE,
                    ],
                )
            )
            return answer.run_id

        try:
            run = await store.reset_run(
                str(request["run_id"]),
                replaced=replaced,
                point=event_id,
                request_id=str(activity.info().workflow_id),
                reset=reset,
            )
        except ResetSupersededError:
            raise ApiError(
                status.HTTP_409_CONFLICT,
                "Another Reset moved the run first; check its history again.",
                type_=RESET_CHANGED,
            ) from None
        if run is None:
            raise ApiError(status.HTTP_404_NOT_FOUND, "No such flow run.")
        return {"run_id": run.id, "workflow_run_id": run.workflow_run_id, "event_id": event_id}

    # A transport failure is retried: the same Update id makes the resend safe, and a
    # decision already recorded by the first attempt is the same decision.
    return [
        OperationKind(
            name=FLOW_ANSWER, check=check_answer, run=run_answer, run_attempts=3, queue="projects"
        ),
        OperationKind(
            name=FLOW_DECIDE, check=check_decide, run=run_decide, run_attempts=3, queue="projects"
        ),
        OperationKind(
            name=FLOW_RESET, check=check_reset, run=run_reset, run_attempts=3, queue="projects"
        ),
    ]


def _kinds(core: Core, components: Components) -> list[OperationKind]:
    from scadbuddy.flows.component import FLOWS

    return flow_kinds(components.get(FLOWS).store)


OPERATION_KINDS: KindsBuild = _kinds
