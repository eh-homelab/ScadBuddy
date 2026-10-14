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

The reverse is accepted, not reconciled (#2221): an Update still on its way to the
history when Temporal answers the probe with no such Update lands after its row was
deleted, so the call is resolved with no row recording who resolved it. Only that audit
row is lost; the run itself is right, and a later decision for the call is refused.

The answer text never reaches the operation's history in the clear (#2221): the route
seals it under the run's own payload key (`seal_answer`), so the operation's input and
its `operations` row carry `answer_sealed`, and forgetting the run (`forget.py`) makes
them unreadable with the rest of the run's payloads.
"""

from __future__ import annotations

import asyncio
import base64
import hmac
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx
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
from scadbuddy.core.secrets import open_bytes, seal_bytes
from scadbuddy.flows.approval_hash import input_hash
from scadbuddy.flows.grants import Inside, Outside, Verdict, match_grant
from scadbuddy.flows.history import outward_since
from scadbuddy.flows.models import TERMINAL, Decision, Proposal, Run
from scadbuddy.flows.narrowing import narrowing_problem
from scadbuddy.flows.store import (
    FlowStore,
    HopMovedError,
    ResetBeforeFloorError,
    ResetSupersededError,
)
from scadbuddy.operations.kinds import KindsBuild, OperationKind
from scadbuddy.workflows.flow_grants import FlowGrants, parked_call
from scadbuddy.workflows.flow_models import Hop, HopAction, HopRecord
from scadbuddy.workflows.payload_codec import PayloadKeys, dedup_iv

if TYPE_CHECKING:
    from scadbuddy.core.components import Components, Core

FLOW_ANSWER = "flow_answer"
FLOW_DECIDE = "flow_decide"
STALE_ENTRY = "https://scadbuddy.dev/problems/stale-entry"
ALREADY_RESOLVED = "https://scadbuddy.dev/problems/already-resolved"
RUN_CLOSED = "https://scadbuddy.dev/problems/flow-run-closed"
FLOW_RESET = "flow_reset"
#: The starter session's action on a call outside the run's grant (#2482).
FLOW_ESCALATE = "flow_escalate"
NOT_PARENT = "https://scadbuddy.dev/problems/flow-escalation-not-parent"
WITH_PERSON = "https://scadbuddy.dev/problems/flow-escalation-with-person"
NO_PROPOSAL = "https://scadbuddy.dev/problems/flow-escalation-no-proposal"
PROPOSAL_CHANGED = "https://scadbuddy.dev/problems/flow-escalation-proposal-changed"
NOT_NARROWER = "https://scadbuddy.dev/problems/flow-escalation-not-a-narrowing"
PARENT_TIER = "https://scadbuddy.dev/problems/flow-escalation-parent-tier"
RESET_POINT = "https://scadbuddy.dev/problems/flow-reset-point"
RESET_CHANGED = "https://scadbuddy.dev/problems/flow-reset-changed"
RESET_UNRECORDED = "https://scadbuddy.dev/problems/flow-reset-unrecorded"
#: How long asking Temporal whether a decision's Update landed may take.
LANDED_PROBE = timedelta(seconds=10)
#: The harness tool an outward `tool(...)` call parks as (`flow_tools.approved_tool`):
#: its input names the agent tool and its arguments.
APPROVED_TOOL = "approved_tool"


def request_id(run: Run, call_id: str) -> str:
    """The decision's id (spec 2026-10-01 §6.6): the run's current execution's, so a
    card from before a Reset never matches."""
    return f"flow:{run.id}:{run.workflow_run_id}:{call_id}"


def _answer_context(subject: str) -> str:
    return f"flow-answer:{subject}"


async def seal_answer(keys: PayloadKeys, run_id: str, answer: str) -> str:
    """`answer` sealed under the run's payload key, as the request's `answer_sealed`.
    The IV is derived from the text, so a resent request seals to the same bytes and keeps
    its operation key; one IV is only ever used for one text. Raises
    `SubjectForgottenError` for a run with no key (forgotten, or never started)."""
    subject = f"flow-{run_id}"
    key = await keys.key_for(subject, False)
    plain = answer.encode()
    iv = dedup_iv(key, _answer_context(subject), plain)
    return base64.b64encode(seal_bytes(key, plain, _answer_context(subject), iv)).decode()


async def open_answer(keys: PayloadKeys, run_id: str, sealed: str) -> str:
    subject = f"flow-{run_id}"
    key = await keys.key_for(subject, False)
    return open_bytes(key, base64.b64decode(sealed), _answer_context(subject)).decode()


def _stale() -> ApiError:
    return ApiError(
        status.HTTP_409_CONFLICT,
        "That question or approval is no longer waiting: the run moved on. Reload it.",
        type_=STALE_ENTRY,
    )


def _resolved() -> ApiError:
    return ApiError(status.HTTP_409_CONFLICT, "That was answered already.", type_=ALREADY_RESOLVED)


def _not_parent() -> ApiError:
    return ApiError(
        status.HTTP_403_FORBIDDEN,
        "Only the session that started the run acts on its calls before the person does.",
        type_=NOT_PARENT,
    )


def _proposal_changed() -> ApiError:
    return ApiError(
        status.HTTP_409_CONFLICT,
        "That proposal is not the one you were shown, or it no longer fits the call. Reload it.",
        type_=PROPOSAL_CHANGED,
    )


def _unavailable_key() -> ApiError:
    return ApiError(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "Narrowing needs ScadBuddy's secret key file (SCADBUDDY_SECRET_KEY_FILE).",
    )


def _with_person() -> ApiError:
    return ApiError(
        status.HTTP_409_CONFLICT,
        "That call is with the person now: the starting session's turn at it is over.",
        type_=WITH_PERSON,
    )


def _before_floor(floor: int) -> ApiError:
    return ApiError(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        f"This run keeps the questions and approvals it can park again only for points"
        f" after event {floor + 1}; reset it to a later point.",
        type_=RESET_POINT,
    )


def _wanted(
    kind: str, request: dict[str, Any], answer: str | None = None
) -> tuple[str, dict[str, Any]]:
    """The outcome and response a request asks for, as its row records them; `answer`
    is an answer request's text, opened."""
    if kind == "answer":
        return "answered", {"answer": str(answer)}
    reason = request.get("reason")
    return (
        "approved" if bool(request["approved"]) else "denied",
        {} if reason is None else {"reason": str(reason)},
    )


def _same(existing: Decision, kind: str, request: dict[str, Any], answer: str | None) -> bool:
    """Whether a recorded row is the decision `request` asks for (a resend), not another."""
    outcome, response = _wanted(kind, request, answer)
    return existing.kind == kind and existing.outcome == outcome and existing.response == response


def flow_kinds(
    store: FlowStore,
    keys: PayloadKeys | None,
    hash_key: bytes | None,
    *,
    api: Callable[[], httpx.AsyncClient | None] | None = None,
) -> list[OperationKind]:
    """The kinds over `store`, opening answers with `keys`; the `projects` worker serves
    them. `hash_key` makes an approval's input hash (`approval_hash.py`); None records
    none, and the agent then runs no approved tool call. `api` makes a client on
    ScadBuddy's own API, which a parent's narrowing is matched through (the grant's
    facts, as `flow_grant_check` reads them); None, and every narrowing asks the person."""

    async def answer_of(request: dict[str, Any]) -> str:
        if "answer" in request:
            # Sent before answers were sealed (#2221), still running after the upgrade.
            return str(request["answer"])
        if keys is None:
            raise ApiError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "Flows need ScadBuddy's secret key file (SCADBUDDY_SECRET_KEY_FILE).",
            )
        return await open_answer(keys, str(request["run_id"]), str(request["answer_sealed"]))

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
        if existing is not None and existing.responder.startswith("grant:"):
            # Let through by the run's grant (#2481): never listed, so not a person's.
            raise _stale()
        if existing is not None:
            answer = await answer_of(request) if kind == "answer" else None
            if not _same(existing, kind, request, answer):
                raise _resolved()
        entry = next((w for w in run.waiting_on if w.call_id == call_id and w.kind == kind), None)
        if existing is None and entry is None:
            raise _stale()
        return {
            "request_id": rid,
            "workflow_id": run.workflow_id,
            "workflow_run_id": run.workflow_run_id,
            # Where the entry waits and how it got there (#2482), for its decision's record.
            "hop": (entry.hop if entry is not None else None) or "person",
            "hops": [h.model_dump(mode="json") for h in entry.hops] if entry is not None else [],
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
        outcome, response = _wanted("answer", request, await answer_of(request))
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

    async def parked_hash(checked: dict[str, Any], call_id: str) -> str | None:
        """The input hash of the call as it is parked (#2318): an outward agent tool's
        name and arguments, which the agent hashes again from the call it is asked to
        run. None for a ScadBuddy host function (the workflow runs it itself)."""
        client = AgentClient(activity.client(), str(checked["workflow_id"]))
        for parked in await client.get_pending_approvals():
            if parked.tool_id != call_id:
                continue
            if parked.tool_name != APPROVED_TOOL or hash_key is None:
                return None
            args = parked.tool_input.get("args")
            return input_hash(
                hash_key, str(parked.tool_input.get("name")), args if isinstance(args, dict) else {}
            )
        raise _stale()

    async def check_decide(request: dict[str, Any]) -> dict[str, Any]:
        checked = await check("approval", request)
        if request.get("proposal") and request.get("approved"):
            # The person approves a parent's narrowing (#2482): the server's record of
            # it, and only the very one the person was shown (its hash).
            await proposed(checked, request)
        return checked

    async def run_decide(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        approved = bool(request["approved"])
        reason = request.get("reason")
        outcome, response = _wanted("approval", request)
        decision = _decision(request, checked, kind="approval", outcome=outcome, response=response)
        decision.hops = hops_of(checked, "approve" if approved else "deny", "browser")
        if approved and request.get("proposal"):
            # Bound to the narrowed input, which the gated body runs only when it
            # hashes to this (`flow_decision`).
            held = await proposed(checked, request)
            decision.response = {**response, "args": held.args, "narrowed_by": held.proposed_by}
            decision.input_hash = held.input_hash
        elif approved and await store.get_decision(decision.request_id) is None:
            # Bound to the call as it waits now, which is what the person was shown; a
            # resend keeps the hash its first attempt recorded.
            decision.input_hash = await parked_hash(checked, decision.call_id)
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

    def hops_of(checked: dict[str, Any], action: HopAction, actor: str) -> list[HopRecord]:
        """The entry's way up, with this decision as its last step (spec §4.6)."""
        held = [HopRecord.model_validate(h) for h in checked.get("hops") or []]
        hop: Hop = "parent" if checked.get("hop") == "parent" else "person"
        return [*held, HopRecord(hop=hop, action=action, actor=actor, at=datetime.now(UTC))]

    def hash_of(fn: str, tool: str | None, args: dict[str, Any]) -> str | None:
        """As the agent hashes the call it runs, and `flow_decision` the body's (#2318)."""
        if hash_key is None:
            return None
        return input_hash(hash_key, tool if fn == "tool" and tool else fn, args)

    async def parked_of(
        checked: dict[str, Any], call_id: str
    ) -> tuple[str, str | None, dict[str, Any]]:
        """The call as the run parked it, from the harness: never from the request."""
        client = AgentClient(activity.client(), str(checked["workflow_id"]))
        for parked in await client.get_pending_approvals():
            if parked.tool_id == call_id:
                call = parked_call(parked.tool_name, parked.tool_input)
                if call is None:
                    raise ApiError(
                        status.HTTP_422_UNPROCESSABLE_CONTENT,
                        "That call cannot be narrowed: no grant names it.",
                    )
                return call
        raise _stale()

    async def proposed(checked: dict[str, Any], request: dict[str, Any]) -> Proposal:
        """The parent's proposal for the call, exactly as the person was shown it: its
        hash is the one the request names, its arguments still hash to it, and they are
        still a narrowing of the call as it is parked. Anything else is a 409."""
        held = await store.get_proposal(str(checked["request_id"]))
        if held is None:
            raise ApiError(
                status.HTTP_409_CONFLICT,
                "That call carries no proposal to approve.",
                type_=NO_PROPOSAL,
            )
        fn, tool, parked = await parked_of(checked, str(request["call_id"]))
        shown = request.get("proposal_hash")
        if (
            not isinstance(shown, str)
            or not hmac.compare_digest(shown, held.input_hash)
            or hash_of(fn, tool, held.args) != held.input_hash
            or narrowing_problem(fn, parked, held.args) is not None
        ):
            raise _proposal_changed()
        return held

    def narrowed_of(fn: str, parked: dict[str, Any], given: dict[str, Any]) -> dict[str, Any]:
        """The parked call with the parent's input (a print's or arrange's request), when
        that is a narrowing of it; a 422 otherwise (#2482)."""
        narrowed = (
            given
            if fn == "tool"
            else {**{k: v for k, v in parked.items() if k == "source"}, "request": given}
        )
        if (problem := narrowing_problem(fn, parked, narrowed)) is not None:
            raise ApiError(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"That is not a narrowing of the call: {problem}.",
                type_=NOT_NARROWER,
            )
        return narrowed

    async def check_escalate(request: dict[str, Any]) -> dict[str, Any]:
        """Spec 2026-10-11 §4.2: the caller is the run's starter session, read from the
        run's own record (`started_by`), and the call waits at hop `parent`."""
        run = await store.get_run(str(request["run_id"]))
        if run is None:
            raise ApiError(status.HTTP_404_NOT_FOUND, "No such flow run.")
        by = run.started_by
        session, principal = request.get("session"), request.get("principal")
        if (
            by.get("kind") != "agent"
            or not isinstance(by.get("session"), str)
            or not isinstance(session, str)
            or not session
            or by.get("session") != session
            or not isinstance(by.get("principal"), str)
            or not isinstance(principal, str)
            or not principal
            or by.get("principal") != principal
        ):
            raise _not_parent()
        # The session is still the starter's (no handoff since), and acts with its
        # owner's tiers as they are now (spec §4.4, §5).
        tiers = await store.parent_tiers(session, principal)
        if tiers is None:
            raise _not_parent()
        if request.get("action") == "narrow" and "outward" not in tiers:
            # A narrowing may run the call with no person: never past what the parent
            # itself may do (every gated call is outward).
            raise ApiError(
                status.HTTP_403_FORBIDDEN,
                "The starting session's owner does not hold the outward tier now, so it"
                " cannot narrow an outward call; it may deny it or pass it up.",
                type_=PARENT_TIER,
            )
        if await store.get_decision(request_id(run, str(request["call_id"]))) is not None:
            raise _resolved()
        checked = await check("approval", request)
        if checked["hop"] != "parent":
            raise _with_person()
        return checked

    async def run_escalate(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        run_id, call_id = str(request["run_id"]), str(request["call_id"])
        actor = f"session:{request['session']}"
        action: HopAction = request["action"]
        reason = request.get("reason")
        now = datetime.now(UTC)
        answer = {"run_id": run_id, "call_id": call_id}
        if action == "pass_up":
            record = HopRecord(hop="parent", action="pass_up", actor=actor, at=now)
            if await store.move_hop(run_id, call_id, record) is None:
                raise _with_person()
            return {**answer, "outcome": "passed_up"}
        if action == "deny":
            decision = Decision(
                request_id=str(checked["request_id"]),
                run_id=run_id,
                workflow_run_id=str(checked["workflow_run_id"]),
                call_id=call_id,
                kind="approval",
                outcome="denied",
                response={} if reason is None else {"reason": str(reason)},
                responder=actor,
                hops=hops_of(checked, "deny", actor),
            )
            held = await store.get_decision(decision.request_id)
            if held is not None and (held.outcome, held.responder) == ("denied", actor):
                # A retried attempt after this very denial was recorded (and maybe sent):
                # the entry may have left `parent` since; send it again, idempotently.
                await send_approval(checked, held, approved=False, reason=reason)
                return {**answer, "outcome": "denied"}
            try:
                recorded = await store.record_decision(decision, at_hop="parent")
            except HopMovedError:
                raise _with_person() from None
            if not recorded:
                raise _resolved()
            await send_approval(checked, decision, approved=False, reason=reason)
            return {**answer, "outcome": "denied"}
        return await narrow(request, checked, actor, answer)

    async def narrow(
        request: dict[str, Any], checked: dict[str, Any], actor: str, answer: dict[str, Any]
    ) -> dict[str, Any]:
        """A parent's narrowing (§4.2): the parked call's own function and tool with the
        parent's input, matched by the run's grant like any call. Inside, it runs as the
        grant approved it, with `narrowed_by`; outside, it goes up as a proposal."""
        run_id, call_id = str(request["run_id"]), str(request["call_id"])
        given = request.get("input")
        if not isinstance(given, dict):
            raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, "A narrowing names its input.")
        fn, tool, parked = await parked_of(checked, call_id)
        narrowed = narrowed_of(fn, parked, given)
        if hash_key is None:
            # No approval key: nothing can be bound, so nothing is narrowed.
            raise _unavailable_key()
        verdict: Verdict = Outside(reason="no approval key")
        client = None if api is None else api()
        try:
            if hash_key is not None:
                grants = FlowGrants(store, client, hash_key)
                verdict = await match_grant(
                    store,
                    grants.facts_of,
                    lambda f, t, a: input_hash(hash_key, t if f == "tool" and t else f, a),
                    run_id=run_id,
                    request_id=str(checked["request_id"]),
                    fn=fn,
                    tool=tool,
                    args=narrowed,
                    now=datetime.now(UTC),
                )
        finally:
            if client is not None:
                await client.aclose()
        now = datetime.now(UTC)
        if isinstance(verdict, Inside) and verdict.input_hash != hash_of(fn, tool, verdict.args):
            # The request id's earlier use (the call's own check, before its grant
            # lapsed) answered for other input: never approved under that hash.
            verdict = Outside(reason="the grant answered for the call's own input")
        if isinstance(verdict, Inside):
            decision = Decision(
                request_id=str(checked["request_id"]),
                run_id=run_id,
                workflow_run_id=str(checked["workflow_run_id"]),
                call_id=call_id,
                kind="approval",
                outcome="approved",
                response={"args": verdict.args, "narrowed_by": actor},
                responder=f"grant:{verdict.grant_id}",
                input_hash=verdict.input_hash,
                hops=hops_of(checked, "narrow", actor),
            )
            if await store.record_grant_decision(decision):
                try:
                    await send_approval(checked, decision, approved=True, reason=None)
                except BaseException:
                    await store.withdraw_grant_decision(decision.request_id)
                    raise
                return {**answer, "outcome": "approved"}
            verdict = Outside(reason="the grant lapsed")
        record = HopRecord(
            hop="parent", action="narrow", actor=actor, at=now, reason=verdict.reason
        )
        proposal = Proposal(
            args=narrowed, input_hash=str(hash_of(fn, tool, narrowed)), proposed_by=actor
        )
        if await store.move_hop(run_id, call_id, record, proposal=proposal) is None:
            raise _with_person()
        return {**answer, "outcome": "with_person"}

    async def send_approval(
        checked: dict[str, Any], decision: Decision, *, approved: bool, reason: Any
    ) -> None:
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
        event_id = int(request["event_id"])
        floor = await store.reset_floor(run.id)
        if floor is not None and event_id - 1 <= floor:
            raise _before_floor(floor)
        client = activity.client()
        try:
            described = await client.get_workflow_handle(run.workflow_id).describe()
        except RPCError as err:
            if err.status == RPCStatusCode.NOT_FOUND:
                raise ApiError(status.HTTP_404_NOT_FOUND, "The run's history is gone.") from None
            raise
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
                " workflow task before the run's last event, and for a finished run,"
                " one before it recorded its end.",
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
        except ResetBeforeFloorError as err:
            raise _before_floor(err.floor) from None
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
        OperationKind(
            name=FLOW_ESCALATE,
            check=check_escalate,
            run=run_escalate,
            run_attempts=3,
            queue="projects",
        ),
    ]


def _kinds(core: Core, components: Components) -> list[OperationKind]:
    from scadbuddy.flows.component import FLOWS
    from scadbuddy.flows.keys import approval_key
    from scadbuddy.workflows.flow_routes import api_client

    flows = components.get(FLOWS)
    return flow_kinds(
        flows.store,
        flows.keys,
        approval_key(core.settings),
        api=lambda: api_client(core.settings.api_internal_url),
    )


OPERATION_KINDS: KindsBuild = _kinds
