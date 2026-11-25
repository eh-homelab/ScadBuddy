"""A person's answer and approval for a flow run's parked calls, as operations on the
`projects` queue (#1057, plan 2026-10-09-durable-phase-6-flows.md decisions A and B).

Each records the decision in `workflow_run_decisions` first, then tells the harness with
its own public Update (`provide_callback_result`, `tool_approval`). The Update id is the
request id and a keyed tag of the decision (`decision_update_id`). So a retried run resends
the same Update, Temporal answers it once, and two decisions never share an Update.

The row lives as long as the Update may have landed (#2247):

- The harness refusing the Update is definite: the row is deleted. One exception is a
  decision that a worker from before #7575 (an approval or a denial: #7588) sent first,
  under the request id alone: it landed, so the row stays and the operation succeeds. The
  other is a row that stands for an approval it replaced (below). When Temporal cannot say
  whether either landed, the next rule's last sentence holds.
- Any other failure (an RPC error, a deadline, the reply lost) is settled by asking
  Temporal for the Update by its id. Completed means it landed: the row stays and the
  operation succeeds. No such Update means it never landed: the row is deleted, so a
  failed send never blocks the decision that would resolve the call. When Temporal
  cannot say, the row stays and the error is raised; the next attempt resends.
- Cancelled (the activity's deadline, a worker stopping): the row stays and the
  cancellation is raised. Nothing is asked while cancelling; the next attempt resends
  only if it did not land.

A retry, or a new request with the same decision, is let through by `check` on a
matching row before `waiting_on` is looked at: a call the landed Update resolved is no
longer waiting, and the resend then gets Temporal's answer for it instead of a 409. Any
other decision for the call is refused as answered, but for the next rule. Two approvals
are the same only with the same input: the call as it was parked, or the same proposal,
and the same binding shown (`_same`).

A person's decision takes over the row of another one of theirs that never landed (#7575,
#7588, `take_over`). All of these must hold:

- No running operation may still send the row's decision. An operation's record is
  `running` from before its run until the operation ends, so this reads no clock.
- After that, Temporal has no Update of the row's decision.
- The execution is still the run's current one.
- In one transaction, the row still has that decision, the call has no operation that was
  not looked at, and a call that asks for an approval still waits for it
  (`FlowStore.take_over_decision`). A decision recorded again locks the row
  (`record_decision`): it is seen there, or it finds the new row.

Anything else refuses the decision, and a later one may take the row over. The row of a
grant or of the starter session is never taken over. A send past all this (a paused or
cancelled attempt, an Update on its way) is another Update: the run takes one of the two and
refuses the other, which undoes its own row alone (`withdraw`). When the attempt that took
the row over is refused so, or finds the run ended, it puts the first decision back if that
one landed. A replaced approval is not put back: the new decision stands (below).

The harness resolves a call once, and an Update id is of one decision, so a late one is not
taken for another's. For an answer that is the safety against a late Update: the script gets
one answer, and the take-over's checks only keep the audit row right. An Update from before
#7575 or #7588, under the request id alone, names no decision: it is taken for the one of
the row, or for the one that the row's decision replaced. A denial is put back only for an
Update of its own.

An approval's Update says only yes or no. What the call may run is what the row says, its
outcome, its input hash and its narrowed input: a gated body reads it (`flow_decision`; not
in a run that made an approved call before #2482), and the agent's gate reads it for each of
its own tools and a plugin's. Both refuse a call with no approval on record. The body first
takes the call off the run's row (`flow_tools._approved`), which the take-over locks and
checks. So a take-over never changes a row that a call has read, and after it the replaced
decision lets nothing run. When a replaced approval lands late, the call runs what the new
approval says, or does not run when the new decision is a denial. So the row keeps the Update
ids of the approvals it replaced (`REPLACED`): any of them means that the row stands.

Residual window: a row kept because the Update's fate was unknown (or because the
operation was cancelled) says `approved`, `denied` or `answered` before the harness has
it. If no attempt ever resends it and the call is later resolved another way (the
approval timer, a Reset), the row disagrees with the run. The window is the time until
the next attempt, or until another decision takes the row over; it stays open only once
every attempt has been exhausted without Temporal ever answering. A recorded row whose
Update is known not to have landed, and whose execution was replaced, stays as it is on the
replaced execution: the attempt is refused as stale.

The reverse is accepted, not reconciled (#2221): an Update still on its way to the
history when Temporal answers the probe with no such Update lands after its row was
deleted, so the call is resolved with no row recording who resolved it. The same holds
after a take-over when the attempt that took the row over stopped before it could put the
row back, and its retry is refused. Only that audit row is lost: the script has its answer,
a later decision for the call is refused, and a call that an approval let through does not
run once the row is gone.

The answer text never reaches the operation's history in the clear (#2221): the route
seals it under the run's own payload key (`seal_answer`), so the operation's input and
its `operations` row carry `answer_sealed`, and forgetting the run (`forget.py`) makes
them unreadable with the rest of the run's payloads.

A Reset undoes the decisions after its point (#7364). An attempt of an answer or of a
person's decision that finds its row on record asks first whether its Update landed on the
execution it checked, and sends nothing if it did. Any other attempt of one sends only after
it checked that this execution is the run's current one. A Reset between that check and the
send is still crossed (milliseconds: the harness client cannot pin the Update to an
execution).

`task_answer` (#7364) is `flow_answer` for a prompt, whose call id is its task's id and
whose answer is an object the route checked against the task's schema. Once the Update
landed, its task is marked once, best effort: the run has the answer whether or not the
mark works. Neither kind's result holds the answer.
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol

import httpx
from fastapi import status
from temporal_agent_harness.harness.agent_client import (
    AgentClient,
    CallbackResultError,
    ToolApprovalError,
)
from temporal_agent_harness.harness.agent_protocol import ToolApprovalResult
from temporalio import activity
from temporalio.api.common.v1 import WorkflowExecution
from temporalio.api.enums.v1 import ResetReapplyExcludeType
from temporalio.api.workflowservice.v1 import ResetWorkflowExecutionRequest
from temporalio.client import (
    Client,
    WorkflowUpdateFailedError,
    WorkflowUpdateRPCTimeoutOrCancelledError,
)
from temporalio.service import RPCError, RPCStatusCode

from scadbuddy.core.problems import ApiError
from scadbuddy.core.secrets import open_bytes, seal_bytes
from scadbuddy.crud.client import CrudRefusedError
from scadbuddy.crud.projects import RESET_REFUSED
from scadbuddy.flows.approval_hash import input_hash
from scadbuddy.flows.grants import Inside, Outside, Verdict, match_grant
from scadbuddy.flows.history import outward_since
from scadbuddy.flows.models import TERMINAL, Decision, Proposal, Run
from scadbuddy.flows.narrowing import narrowing_problem
from scadbuddy.flows.print_binding import (
    api_drafts,
    api_lookup,
    bind_call,
    binds,
    named_draft,
)
from scadbuddy.flows.store import (
    FlowStore,
    HopMovedError,
    ResetBeforeFloorError,
    ResetSupersededError,
)
from scadbuddy.operations.kinds import KindsBuild, OperationKind
from scadbuddy.tasks.models import Task, TaskState
from scadbuddy.workflows.flow_grants import FlowGrants, parked_call
from scadbuddy.workflows.flow_models import Hop, HopAction, HopRecord
from scadbuddy.workflows.payload_codec import PayloadKeys, dedup_iv

if TYPE_CHECKING:
    from scadbuddy.core.components import Components, Core
    from scadbuddy.crud.projects import RemoteFlowStore

FLOW_ANSWER = "flow_answer"
FLOW_DECIDE = "flow_decide"
#: A person's typed answer to a prompt's task (#7364).
TASK_ANSWER = "task_answer"
STALE_ENTRY = "https://scadbuddy.dev/problems/stale-entry"
#: A task takes no answer: its `state` says why (#7364).
TASK_NOT_OPEN = "https://scadbuddy.dev/problems/tasks/not-open"
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
#: A print's registry printer links another printer than the run page showed (#4445).
PRINTER_CHANGED = "https://scadbuddy.dev/problems/flow-printer-changed"
#: An approval of a print on a registry printer the run page could not name (#4445).
PRINTER_UNBOUND = "https://scadbuddy.dev/problems/flow-printer-unbound"
#: The API refused the projects worker's read of a print's registry printer (#5466).
PRINTER_REFUSED = "https://scadbuddy.dev/problems/flow-printer-refused"
#: A publish's hosted plugin draft is not the one the run page showed (#7199).
DRAFT_CHANGED = "https://scadbuddy.dev/problems/flow-draft-changed"
#: An approval of a publish whose draft the run page could not name (#7199).
DRAFT_UNBOUND = "https://scadbuddy.dev/problems/flow-draft-unbound"
#: The API refused the projects worker's read of a publish's draft (#7199).
DRAFT_REFUSED = "https://scadbuddy.dev/problems/flow-draft-refused"
#: The statuses of a binding's read that no retry mends: the worker's service credential (#5472).
REFUSED_READS = frozenset({401, 403})
RESET_CHANGED = "https://scadbuddy.dev/problems/flow-reset-changed"
RESET_UNRECORDED = "https://scadbuddy.dev/problems/flow-reset-unrecorded"
#: In an approval's or a denial's response: the Update ids of the approvals that its row
#: replaced, any of which may still let the call through, to do what the row says (#7588).
REPLACED = "replaced"
#: The harness's refusal of a call that another Update resolved.
ALREADY = frozenset({"CallbackAlreadyResolved", "ToolApprovalAlreadyResolved"})
#: How long asking Temporal whether a decision's Update landed may take.
LANDED_PROBE = timedelta(seconds=10)
#: The harness tool an outward `tool(...)` call parks as (`flow_tools.approved_tool`):
#: its input names the agent tool and its arguments.
APPROVED_TOOL = "approved_tool"
#: The harness tool an outward turn tool call (a plugin's, `scadbuddy_http__http_request`)
#: parks as (`flow_tools.approved_turn_tool`): its input names the `<server>__<tool>` and its
#: arguments, which the agent's `turn:<name>` activity hashes as `inputHash(name, args)`
#: (agent/src/temporal/turnTools.ts `runTurnTool`) before it runs the call (#6118).
APPROVED_TURN_TOOL = "approved_turn_tool"
#: The harness tools whose approval binds the agent call they name.
AGENT_CALLS = frozenset({APPROVED_TOOL, APPROVED_TURN_TOOL})


async def reset_execution(
    client: Client, workflow_id: str, *, replaced: str, point: int, request_id: str, reason: str
) -> str:
    """Reset the execution `replaced` to event `point` under `request_id` (Temporal dedupes
    on it): the new execution's run id."""
    answer = await client.workflow_service.reset_workflow_execution(
        ResetWorkflowExecutionRequest(
            namespace=client.namespace,
            workflow_execution=WorkflowExecution(workflow_id=workflow_id, run_id=replaced),
            reason=reason,
            workflow_task_finish_event_id=point,
            request_id=request_id,
            # Every answer, approval and `close` after the point is undone with it: the
            # calls past it run again and park again for a person.
            reset_reapply_exclude_types=[
                ResetReapplyExcludeType.RESET_REAPPLY_EXCLUDE_TYPE_SIGNAL,
                ResetReapplyExcludeType.RESET_REAPPLY_EXCLUDE_TYPE_UPDATE,
            ],
        )
    )
    return answer.run_id


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


def canonical(value: Any) -> Any:
    """The value as jsonb compares it: 1e23 and 10**23 are one number, as 1.0 and 1 are."""
    if isinstance(value, dict):
        return {key: canonical(held) for key, held in value.items()}
    if isinstance(value, list):
        return [canonical(held) for held in value]
    if isinstance(value, float) and value.is_integer():
        return int(Decimal(repr(value)))
    return value


async def decision_update_id(keys: PayloadKeys, row: Decision) -> str:
    """The Update id of a decision's row (#7575, #7588): one for each decision on the call.
    The tag is made with the run's payload key, so the id in the history tells nothing of
    the decision."""
    key = await keys.key_for(f"flow-{row.run_id}", False)
    held: Any = row.response
    if row.kind == "approval":
        decided = {k: v for k, v in row.response.items() if k != REPLACED}
        held = [row.outcome, decided, row.responder, row.input_hash]
    text = json.dumps(canonical(held), sort_keys=True, separators=(",", ":"))
    tag = dedup_iv(key, f"flow-answer-update:{row.request_id}", text.encode())
    return f"{row.request_id}:{tag.hex()}"


def _stale() -> ApiError:
    return ApiError(
        status.HTTP_409_CONFLICT,
        "That question or approval is no longer waiting: the run moved on. Reload it.",
        type_=STALE_ENTRY,
    )


def _resolved() -> ApiError:
    return ApiError(status.HTTP_409_CONFLICT, "That was answered already.", type_=ALREADY_RESOLVED)


def task_not_open(state: TaskState) -> ApiError:
    return ApiError(
        status.HTTP_409_CONFLICT, f"The task is {state}.", type_=TASK_NOT_OPEN, state=state
    )


def _task_taken() -> ApiError:
    """Another answer holds the task's call. The route adds the row's own `state`, which is
    `open` until the mark of the answer that won."""
    return ApiError(
        status.HTTP_409_CONFLICT,
        "That was answered already. Read the task again.",
        type_=TASK_NOT_OPEN,
    )


class AnswerMarks(Protocol):
    """Where a task is marked answered: `RemoteTasks`, or the API's own `TasksComponent`."""

    async def mark_answered(
        self, task_id: str, answer: dict[str, Any], by: str | None, workflow_run_id: str
    ) -> Task: ...


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


def _printer_changed() -> ApiError:
    return ApiError(
        status.HTTP_409_CONFLICT,
        "The printer this call prints on is not the one you were shown. Reload it.",
        type_=PRINTER_CHANGED,
    )


def _printer_unbound() -> ApiError:
    return ApiError(
        status.HTTP_409_CONFLICT,
        "The run page could not say which printer this call prints on, so it cannot be"
        " approved yet. Reload it, and approve once it names the printer.",
        type_=PRINTER_UNBOUND,
    )


def _draft_changed() -> ApiError:
    return ApiError(
        status.HTTP_409_CONFLICT,
        "The draft this call publishes is not the one you were shown. Reload it.",
        type_=DRAFT_CHANGED,
    )


def _draft_unbound() -> ApiError:
    return ApiError(
        status.HTTP_409_CONFLICT,
        "The run page could not say which draft this call publishes, so it cannot be"
        " approved yet. Reload it, and approve once it names the draft.",
        type_=DRAFT_UNBOUND,
    )


def _read_refused(code: int, what: str, type_: str) -> ApiError:
    """The API refused the projects worker's read of what a call names: its service
    credential, a fault an operator fixes. Trying again would be refused the same way."""
    return ApiError(
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        f"ScadBuddy's API refused to read {what} (HTTP {code}),"
        " so nothing was approved. Check the projects worker's service credential.",
        type_=type_,
    )


def _read_failed(what: str) -> ApiError:
    return ApiError(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        f"Could not read {what}, so nothing was approved. Try again.",
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


def _call_id(request: dict[str, Any]) -> str:
    """The parked call a request is for: a task's id is its prompt's call id."""
    return str(request["task_id"] if "task_id" in request else request["call_id"])


def _wanted(kind: str, request: dict[str, Any], answer: Any = None) -> tuple[str, dict[str, Any]]:
    """The outcome and response a request asks for, as its row records them; `answer`
    is an answer request's text (a task's: its object), opened."""
    if kind == "answer":
        return "answered", {"answer": answer}
    reason = request.get("reason")
    return (
        "approved" if bool(request["approved"]) else "denied",
        {} if reason is None else {"reason": str(reason)},
    )


def _same(existing: Decision, kind: str, request: dict[str, Any], answer: Any) -> bool:
    """Whether a recorded row is the decision `request` asks for (a resend), not another.
    An approval's is of one input: the very proposal, or the call as it was parked, with the
    binding that the request was shown, if any."""
    outcome, response = _wanted(kind, request, answer)
    narrowed = "narrowed_by" in existing.response
    held = {
        k: v for k, v in existing.response.items() if k not in ("args", "narrowed_by", REPLACED)
    }
    if (existing.kind, existing.outcome, held) != (kind, outcome, response):
        return False
    if outcome != "approved":
        return True
    if request.get("proposal"):
        return narrowed and request.get("proposal_hash") == existing.input_hash
    return not narrowed and request.get("bound_hash") in (None, existing.input_hash)


def flow_kinds(
    store: FlowStore | RemoteFlowStore,
    keys: PayloadKeys | None,
    hash_key: bytes | None,
    *,
    api: Callable[[], httpx.AsyncClient | None] | None = None,
    tasks: AnswerMarks | None = None,
) -> list[OperationKind]:
    """The kinds over `store`, opening answers with `keys`; the `projects` worker serves
    them. `hash_key` makes an approval's input hash (`approval_hash.py`); None records
    none, and the agent then runs no approved tool call. `api` makes a client on
    ScadBuddy's own API, which a parent's narrowing is matched through (the grant's
    facts, as `flow_grant_check` reads them); None, and every narrowing asks the person.
    A person's approval of a `print_output` call that names a registry printer reads
    that printer through it too (#4445), and one of a `hosted_plugin_publish` call that
    sends no `draft` reads the draft's digest (#7199); None, and that approval is refused.
    `tasks` marks the task a `task_answer` answered."""

    def run_keys() -> PayloadKeys:
        if keys is None:
            raise ApiError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "Flows need ScadBuddy's secret key file (SCADBUDDY_SECRET_KEY_FILE).",
            )
        return keys

    async def answer_of(request: dict[str, Any]) -> Any:
        """An answer's text, or a task answer's object."""
        if "answer" in request:
            # Sent before answers were sealed (#2221), still running after the upgrade.
            return str(request["answer"])
        text = await open_answer(run_keys(), str(request["run_id"]), str(request["answer_sealed"]))
        return json.loads(text) if "task_id" in request else text

    async def at_execution(workflow_id: str, run_id: str) -> None:
        """Refuse unless `run_id` is the workflow's current execution: a Reset replaces it."""
        try:
            described = await activity.client().get_workflow_handle(workflow_id).describe()
        except RPCError as err:
            if err.status == RPCStatusCode.NOT_FOUND:
                raise _stale() from None
            raise
        if described.run_id != run_id:
            raise _stale()

    async def check(kind: str, request: dict[str, Any]) -> dict[str, Any]:
        run = await store.get_run(str(request["run_id"]))
        if run is None:
            raise ApiError(status.HTTP_404_NOT_FOUND, "No such flow run.")
        if run.status in TERMINAL:
            raise ApiError(status.HTTP_409_CONFLICT, "The run has ended.", type_=RUN_CLOSED)
        call_id = _call_id(request)
        await at_execution(run.workflow_id, run.workflow_run_id)
        rid = request_id(run, call_id)
        # Before `waiting_on`: a call whose Update landed is no longer waiting, and its
        # resend must reach Temporal's answer, not a 409 (#2247).
        existing = await store.get_decision(rid)
        if existing is not None and existing.responder.startswith("grant:"):
            # Let through by the run's grant (#2481): never listed, so not a person's.
            raise _stale()
        entry = next((w for w in run.waiting_on if w.call_id == call_id and w.kind == kind), None)
        # A task's is compared by `record_decision`, so `run_task_answer` sees one that lost.
        if existing is not None and "task_id" not in request:
            answer = await answer_of(request) if kind == "answer" else None
            # A decision on a call that still waits may take a person's row over (#7575).
            may_take = existing.responder == "browser" and entry is not None
            if not may_take and not _same(existing, kind, request, answer):
                raise _resolved()
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

    async def withdraw(decision: Decision) -> None:
        """Undo a recorded decision whose Update did not land: its own row, never one that
        took it over. Never masks the failure."""
        try:
            await store.withdraw_decision(decision)
        except Exception:
            activity.logger.exception(
                "could not delete the unsent flow decision %s", decision.request_id
            )

    async def landed(checked: dict[str, Any], update_id: str) -> bool | None:
        """Whether the Update completed on the run: True, False (Temporal has no
        such Update), or None (it could not say, or the Update failed in its handler)."""
        run_id = str(checked["workflow_run_id"])
        workflow = activity.client().get_workflow_handle(str(checked["workflow_id"]), run_id=run_id)
        handle = workflow.get_update_handle(update_id, workflow_run_id=run_id)
        try:
            await handle.result(rpc_timeout=LANDED_PROBE)
        except RPCError as err:
            if err.status == RPCStatusCode.NOT_FOUND:
                return False
            activity.logger.warning(
                "could not tell whether flow decision %s landed: %s", update_id, err
            )
            return None
        except WorkflowUpdateRPCTimeoutOrCancelledError as err:
            # The SDK raises a deadline and a cancel of this activity as this one class.
            if isinstance(err.__cause__, asyncio.CancelledError):
                raise err.__cause__ from None
            return None
        except WorkflowUpdateFailedError:
            return None
        return True

    async def decision_landed(checked: dict[str, Any], row: Decision) -> bool | None:
        """`landed` for the decision a row records, or for an approval that it replaced."""
        # Before #7575 (an approval's: #7588) every Update id was the request id alone.
        return await any_landed(checked, row, [*row.response.get(REPLACED, []), row.request_id])

    async def any_landed(
        checked: dict[str, Any], row: Decision, others: Iterable[str]
    ) -> bool | None:
        """`landed` for the row's own Update id or one of `others`."""
        for update_id in (await decision_update_id(run_keys(), row), *others):
            verdict = await landed(checked, update_id)
            if verdict is not False:
                return verdict
        return False

    async def settle(
        ask: Callable[[], Awaitable[bool | None]],
        err: BaseException,
        undo: Callable[[], Awaitable[None]],
    ) -> bool:
        """After a send that failed other than by a refusal: True when the Update landed
        anyway (the decision stands); otherwise False, having deleted the row (`undo`) only
        when Temporal says the Update never existed."""
        if isinstance(err, asyncio.CancelledError):
            return False
        verdict = await ask()
        if verdict is False:
            await undo()
        return verdict is True

    def _decision(request: dict[str, Any], checked: dict[str, Any], **fields: Any) -> Decision:
        return Decision(
            request_id=str(checked["request_id"]),
            run_id=str(request["run_id"]),
            workflow_run_id=str(checked["workflow_run_id"]),
            call_id=_call_id(request),
            responder="browser",
            **fields,
        )

    async def check_answer(request: dict[str, Any]) -> dict[str, Any]:
        return await check("answer", request)

    async def may_send(request: dict[str, Any], held: Decision) -> bool:
        """Whether a running operation's request may still send the decision `held`."""
        if held.kind == "approval":
            return "approved" in request and _same(held, "approval", request, None)
        if not {"answer", "answer_sealed"} & request.keys():
            return False
        return bool(canonical({"answer": await answer_of(request)}) == canonical(held.response))

    async def take_over(checked: dict[str, Any], decision: Decision) -> Decision | None:
        """Put `decision` in the call's row in place of another one that never landed and
        that nothing may still send (the module's rule). The one it replaced; None when it
        took nothing."""
        held = await store.get_decision(decision.request_id)
        if held is None or (held.kind, held.responder) != (decision.kind, "browser"):
            return None
        operations = await store.call_operations(decision.run_id, decision.call_id)
        # Before Temporal is asked: an operation that has ended sends nothing after that.
        for operation in operations:
            if operation.request is not None and await may_send(operation.request, held):
                return None
        if await decision_landed(checked, held) is not False:
            return None
        # Again: asking Temporal may take a while.
        await at_execution(str(checked["workflow_id"]), decision.workflow_run_id)
        seen = [operation.id for operation in operations]
        if decision.kind == "approval":
            before = list(held.response.get(REPLACED, []))
            if held.outcome == "approved":
                before.append(await decision_update_id(run_keys(), held))
            decision.response = {k: v for k, v in decision.response.items() if k != REPLACED}
            if before:
                decision.response[REPLACED] = before
        if not await store.take_over_decision(decision, held=held, seen=seen):
            return None
        return held

    async def put_back(checked: dict[str, Any], decision: Decision, held: Decision) -> bool:
        """Undo a take-over whose replaced decision landed after all; never masks the
        failure. Not an approval: the call reads the row, and may have read it."""
        if held.outcome == "approved":
            return False
        try:
            # An Update under the request id alone may be an approval.
            bare = [] if held.kind == "approval" else [held.request_id]
            own = await any_landed(checked, held, bare)
            # `seen=None`: no operation matters, as `held` landed. `[]` would let any stop it.
            return own is True and await store.take_over_decision(held, held=decision, seen=None)
        except Exception:
            activity.logger.exception("could not put back the decision %s", held.request_id)
            return False

    async def land(
        checked: dict[str, Any],
        decision: Decision,
        taken: Callable[[], ApiError],
        send: Callable[[str], Awaitable[Any]],
    ) -> None:
        """Record `decision`, then `tell` the harness. Raises `taken()` when another
        decision holds the call."""
        update_id = await decision_update_id(run_keys(), decision)
        row = await store.get_decision(decision.request_id)
        # Another decision's row says nothing of this one.
        sent = (
            row is not None
            and await decision_update_id(run_keys(), row) == update_id
            and await decision_landed(checked, row)
        )
        if not sent:
            await at_execution(str(checked["workflow_id"]), decision.workflow_run_id)
        replaced = None
        if not await store.record_decision(decision):
            replaced = await take_over(checked, decision)
            if replaced is None:
                raise taken()
        if not sent:
            await tell(checked, decision, taken, send, replaced)

    async def tell(
        checked: dict[str, Any],
        decision: Decision,
        taken: Callable[[], ApiError],
        send: Callable[[str], Awaitable[Any]],
        replaced: Decision | None = None,
    ) -> None:
        """`send` the recorded `decision` under its Update id: returns once that landed, or
        the row stands all the same. `replaced` is the one that it took the row over from."""
        update_id = await decision_update_id(run_keys(), decision)

        async def arrived() -> bool | None:
            # After a take-over, an Update under the request id alone is the replaced one's:
            # no matter when that is an approval, as the call does what the row says.
            if replaced is None or decision.response.get(REPLACED):
                return await decision_landed(checked, decision)
            return await landed(checked, update_id)

        async def undo() -> None:
            if replaced is None or not await put_back(checked, decision, replaced):
                await withdraw(decision)

        try:
            await send(update_id)
        except (CallbackResultError, ToolApprovalError) as err:
            resolved = err.error_type in ALREADY
            if resolved:
                verdict = await arrived()
                if verdict is None:
                    raise
                if verdict:
                    return
            await undo()
            if err.error_type == "MalformedCallbackResult":
                raise ApiError(
                    status.HTTP_422_UNPROCESSABLE_CONTENT, "The run cannot take that answer."
                ) from None
            raise (taken() if resolved else _stale()) from None
        except BaseException as err:
            if not await settle(arrived, err, undo):
                raise

    async def give(
        request: dict[str, Any], checked: dict[str, Any], taken: Callable[[], ApiError]
    ) -> Decision:
        """Record the answer and tell the harness; the decision, once its Update landed.
        Raises `taken()` when another answer holds the call."""
        outcome, response = _wanted("answer", request, await answer_of(request))
        decision = _decision(request, checked, kind="answer", outcome=outcome, response=response)
        client = AgentClient(activity.client(), str(checked["workflow_id"]))
        await land(
            checked,
            decision,
            taken,
            lambda update_id: client.provide_callback_result(
                decision.call_id, result=response, update_id=update_id
            ),
        )
        return decision

    async def run_answer(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        decision = await give(request, checked, _resolved)
        return {"run_id": decision.run_id, "call_id": decision.call_id, "outcome": "answered"}

    async def run_task_answer(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        """The result is in the operation's record and its history, so it holds no answer:
        the route reads the task. An answer that lost (`_task_taken`) marks nothing."""
        assert tasks is not None
        decision = await give(request, checked, _task_taken)
        task_id = decision.call_id
        try:
            await tasks.mark_answered(
                task_id,
                decision.response["answer"],
                request.get("by"),
                decision.workflow_run_id,
            )
        except Exception as err:
            # By its type alone: its text may quote the answer.
            activity.logger.warning("could not mark the task %s: %s", task_id, type(err).__name__)
        return {"run_id": decision.run_id, "task_id": task_id, "outcome": "answered"}

    async def parked_hash(checked: dict[str, Any], call_id: str, shown: Any) -> str | None:
        """The input hash of the call as it is parked (#2318): an outward agent tool's
        name and arguments, or a turn tool's (#6118), which the agent hashes again from
        the call it is asked to run. None for a ScadBuddy host function (the workflow
        runs it itself). A print on a registry printer is bound to the pair its row links
        now (#4445), and a publish of a hosted plugin's draft to the draft's digest now
        (#7199). Either must be the `bound_hash` the run page showed (`shown`): else a 409,
        which says whether the page showed another binding or none."""
        client = AgentClient(activity.client(), str(checked["workflow_id"]))
        for parked in await client.get_pending_approvals():
            if parked.tool_id != call_id:
                continue
            if parked.tool_name not in AGENT_CALLS or hash_key is None:
                return None
            name = str(parked.tool_input.get("name"))
            args = parked.tool_input.get("args")
            args = args if isinstance(args, dict) else {}
            if parked.tool_name == APPROVED_TOOL and binds(name, args):
                # Bound to what the call names as it is now (#4445, #7199), which the
                # agent's gate checks again when the call runs.
                draft = named_draft(name, args) is not None
                what = (
                    "the hosted plugin draft this call publishes"
                    if draft
                    else "the printer this call prints on"
                )
                registry = None if api is None else api()
                if registry is None:
                    raise ApiError(
                        status.HTTP_503_SERVICE_UNAVAILABLE,
                        f"Approving a call that names {what} needs SCADBUDDY_API_INTERNAL_URL.",
                    )
                try:
                    args = (
                        await bind_call(
                            name, args, api_lookup(registry), drafts=api_drafts(registry)
                        )
                    ).args
                except httpx.HTTPError as err:
                    activity.logger.warning(f"could not read {what}", exc_info=True)
                    if (
                        isinstance(err, httpx.HTTPStatusError)
                        and err.response.status_code in REFUSED_READS
                    ):
                        # The worker's credential refused: another try is refused the same way.
                        refused = DRAFT_REFUSED if draft else PRINTER_REFUSED
                        raise _read_refused(err.response.status_code, what, refused) from None
                    # Any other failure (408, 429, a 5xx, no answer, ...) may pass on another
                    # try. Nothing is recorded: the person approves again once it reads.
                    raise _read_failed(what) from None
                finally:
                    await registry.aclose()
                bound = input_hash(hash_key, name, args)
                if not isinstance(shown, str):
                    # The page showed no binding (its read of the run or of what the call
                    # names failed): never approved without one.
                    raise _draft_unbound() if draft else _printer_unbound()
                if not hmac.compare_digest(shown, bound):
                    raise _draft_changed() if draft else _printer_changed()
                return bound
            return input_hash(hash_key, name, args)
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
        row = await store.get_decision(str(checked["request_id"]))
        if row is not None and row.responder.startswith("grant:"):
            # Recorded since the check, which refuses it so.
            raise _stale()
        if row is not None and _same(row, "approval", request, None):
            # A resend keeps what its first attempt recorded, the hash with it.
            decision = row
        else:
            outcome, response = _wanted("approval", request)
            decision = _decision(
                request, checked, kind="approval", outcome=outcome, response=response
            )
            decision.hops = hops_of(checked, "approve" if approved else "deny", "browser")
            if approved and request.get("proposal"):
                # Bound to the narrowed input, which the gated body runs only when it
                # hashes to this (`flow_decision`).
                held = await proposed(checked, request)
                decision.response = {**response, "args": held.args, "narrowed_by": held.proposed_by}
                decision.input_hash = held.input_hash
            elif approved:
                # Bound to the call as it waits now, which is what the person was shown.
                decision.input_hash = await parked_hash(
                    checked, decision.call_id, request.get("bound_hash")
                )
        await land(checked, decision, _resolved, approval(checked, decision))
        return {"run_id": decision.run_id, "call_id": decision.call_id, "outcome": decision.outcome}

    def approval(
        checked: dict[str, Any], decision: Decision
    ) -> Callable[[str], Awaitable[ToolApprovalResult]]:
        """How to send an approval or a denial, as its row has it."""
        client = AgentClient(activity.client(), str(checked["workflow_id"]))
        return lambda update_id: client.approve_tool(
            decision.call_id,
            approved=decision.outcome == "approved",
            reason=decision.response.get("reason"),
            update_id=update_id,
        )

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
                await tell(checked, held, _resolved, approval(checked, held))
                return {**answer, "outcome": "denied"}
            try:
                recorded = await store.record_decision(decision, at_hop="parent")
            except HopMovedError:
                raise _with_person() from None
            if not recorded:
                raise _resolved()
            await tell(checked, decision, _resolved, approval(checked, decision))
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
                    await tell(checked, decision, _resolved, approval(checked, decision))
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

        # The operation's own id: a retried run resets once, and the row records it to
        # tell that retry from another Reset.
        reset_id = str(activity.info().workflow_id)
        responder = str(request.get("responder") or "browser")

        async def reset() -> str:
            return await reset_execution(
                client,
                workflow_id,
                replaced=replaced,
                point=event_id,
                request_id=reset_id,
                reason=f"flow reset by {responder}",
            )

        try:
            # On `RemoteFlowStore` the API resets under the row's lock, not `reset` (#5298).
            run = await store.reset_run(
                str(request["run_id"]),
                replaced=replaced,
                point=event_id,
                request_id=reset_id,
                reset=reset,
                responder=responder,
            )
        except ResetBeforeFloorError as err:
            raise _before_floor(err.floor) from None
        except ResetSupersededError:
            raise ApiError(
                status.HTTP_409_CONFLICT,
                "Another Reset moved the run first; check its history again.",
                type_=RESET_CHANGED,
            ) from None
        except CrudRefusedError as err:
            # The API's own refusal of a Reset Temporal refused for good: no retry mends it.
            if err.status != 422 or err.problem.get("reason") != RESET_REFUSED:
                raise
            raise ApiError(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"Temporal refused to reset the run to event {event_id}; check its history again.",
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
            name=TASK_ANSWER,
            check=check_answer,
            run=run_task_answer,
            run_attempts=3,
            queue="projects",
            # A resend is the same request, signed in as before or not.
            unkeyed=frozenset({"by"}),
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
    from scadbuddy.tasks.component import TASKS
    from scadbuddy.workflows.flow_routes import api_client

    flows = components.get(FLOWS)
    return flow_kinds(
        flows.store,
        flows.keys,
        approval_key(core.settings),
        api=lambda: api_client(core.settings.api_internal_url, flows.api_auth),
        tasks=components.get(TASKS),
    )


OPERATION_KINDS: KindsBuild = _kinds
