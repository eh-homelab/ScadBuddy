"""ScadBuddy's flow host functions (spec 2026-10-01 §7.1), as harness tools.

Every function records its step on the run's row (plan 2026-10-09 Ruling 7). No
``from __future__ import annotations``: the harness reads these signatures to type-check
scripts and to coerce their arguments.
"""

import asyncio
import json
import re
from collections.abc import Callable
from datetime import timedelta
from typing import Any, Literal, NamedTuple
from urllib.parse import quote

from pydantic import BaseModel, Field, ValidationError
from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError, TimeoutType
from temporalio.exceptions import TimeoutError as ActivityTimeoutError

with workflow.unsafe.imports_passed_through():
    import jsonschema
    import referencing.exceptions
    from temporal_agent_harness.harness import agent
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner

    from scadbuddy.flows.inputs import inputs_model
    from scadbuddy.workflows.flow_entries import (
        await_escalated,
        await_gated,
        run_callback,
        run_gated,
    )
    from scadbuddy.workflows.flow_models import (
        FLOW_DECISION,
        FLOW_GRANT_APPROVE,
        FLOW_GRANT_CHECK,
        FLOW_GRANT_WITHDRAW,
        GRANT_REFUSED,
        DecisionAsk,
        DecisionRead,
        GrantApproval,
        GrantApprove,
        GrantCheck,
        GrantVerdict,
        ProjectionWrite,
        StepPolicy,
    )
    from scadbuddy.workflows.flow_routes import (
        API_URL_MISSING,
        FLOW_ROUTE_FOLLOW,
        FLOW_ROUTE_SEND,
        ROUTE_REFUSED,
        RouteAnswer,
        RouteFollow,
        RouteSend,
        route_key,
    )
    from scadbuddy.workflows.flow_steps import StepOwner, project, step

MIN_WAIT_S = 10
MAX_WAIT_S = 86_400
DEFAULT_WAIT_S = 3_600


class HumanAnswer(BaseModel):
    """A person's answer on the Workflows page."""

    answer: str


@agent.callback_tool_defn(inherently_safe=True, timeout=timedelta(seconds=MAX_WAIT_S))
async def human_answer(question: str) -> HumanAnswer:  # type: ignore[empty-body]
    """Wait for a person to answer `question` on the Workflows page."""
    ...


@agent.tool_defn(inherently_safe=True)
async def wait_for_human(
    question: str,
    runner: agent.Injected[AgentWorkflowRunner],
    timeout_s: int = DEFAULT_WAIT_S,
) -> HumanAnswer:
    """Ask a person `question` and wait up to `timeout_s` seconds (10 to 86400, an hour
    if left out) for the answer. Raises TimeoutError if nobody answers in time."""
    if not MIN_WAIT_S <= timeout_s <= MAX_WAIT_S:
        raise ValueError(f"timeout_s must be {MIN_WAIT_S} to {MAX_WAIT_S} seconds")
    call_id = str(workflow.uuid4())
    async with step("wait_for_human", call_id, outward=False, waiting="answer", prompt=question):
        answer: HumanAnswer = await run_callback(
            runner.run_tool, human_answer, timeout_s, call_id=call_id, question=question
        )
        return answer


@agent.tool_defn(inherently_safe=True)
async def sleep(seconds: float) -> None:
    """Wait `seconds` seconds, durably (a Temporal timer)."""
    async with step("sleep", str(workflow.uuid4()), outward=False):
        await workflow.sleep(timedelta(seconds=seconds))


class RenderResult(BaseModel):
    """A render job once it settled: `done`, `failed` or `cancelled`."""

    job_id: str
    status: str
    error: str | None = None


class PrintOutcome(BaseModel):
    """A print run once it settled: `succeeded` or `failed`. A failed run with
    `may_have_queued` may be on Bambuddy's queue anyway."""

    run_id: str
    status: str
    may_have_queued: bool = False
    error: str | None = None
    #: Who narrowed the print before it was approved (`session:<id>`, `browser`).
    narrowed_by: str | None = None


class ArrangeResult(BaseModel):
    """An arrange job once it settled, and the output it was saved as when `done`."""

    job_id: str
    status: str
    output_id: str | None = None
    error: str | None = None
    #: Who narrowed the arrange before it was approved.
    narrowed_by: str | None = None


class Image(BaseModel):
    """An image a flow holds: a pointer to bytes kept with the run, never the bytes
    (#2477). `bytes` is its size. Pass it to `agent` or `ask_session` as
    `images`, or return it; the run page shows the run's images."""

    name: str = Field(pattern=r"^[0-9a-f]{64}\.(png|jpg|gif|webp)$")
    mediaType: Literal["image/png", "image/jpeg", "image/gif", "image/webp"]  # noqa: N815
    bytes: int = Field(gt=0)


class ToolResult(BaseModel):
    """An agent tool's answer: its text (each image named in it as `[image <name>]`) and
    the images it returned."""

    text: str
    images: list[Image]


class RouteRefusedError(Exception):
    """ScadBuddy refused the call; the message is the problem's `detail`."""


class ToolFailedError(Exception):
    """An agent tool failed; the message is the tool's own text."""


#: The agent's activity refusals (`agent/src/temporal/toolActivities.ts`) and a tool's
#: own error: final, the call is never sent again.
_TOOL_REFUSALS = ["ToolError", "NotApproved", "GrantRevoked", "UnknownFlow", "NotForFlows"]
_TOOL_TIMEOUT = timedelta(seconds=120)


def _agent_heartbeat() -> timedelta:
    """The heartbeat timeout of a call to the agent: the run's `agent_heartbeat_s`, 30 s.
    The agent's activities beat every 10 s from their start (`agent/src/temporal/worker.ts`
    `heartbeatingActivities`), as `flow_route_send` does here: an attempt Temporal recorded
    as started but never delivered (a poll that gave up mid-handoff on a slow Temporal
    database, or a replica a deploy took) is retried after this, not after the call's
    whole start-to-close. Two such `turn_tool_tier` attempts held a run 4 min (#4199)."""
    owner: StepOwner = workflow.instance()
    return timedelta(seconds=owner.agent_heartbeat_s)


#: A `turn_tool_tier` whose last attempt missed its beats is asked once more with no
#: heartbeat timeout, as before #4199 (#4262). An agent replica from before #4200 never
#: beats, and a tier call takes ~35 s there (it connects the turn's servers): during an
#: agent rollout, or after a rollback past #4200, all three attempts could land on such
#: replicas (`agent-tools` is unversioned, so any poller takes any attempt) and fail the
#: run's tier check. The fallback needs only the last attempt to have missed its beats:
#: that is the failure the call ends with. Runs recorded before raised that heartbeat
#: timeout to the script.
#: The cost: a tier whose attempts are all lost (recorded as started but never
#: delivered, or taken by a replica that died) now reaches the script after
#: 3 x `agent_heartbeat_s` of missed beats plus up to 3 x 120 s of start-to-close: at
#: the default 30 s heartbeat, 450 s (about 7.5 min, plus the retries' backoff), where
#: #4199 alone gave up after the first 90 s. A tier whose last attempt ends on its
#: start-to-close instead takes no fallback.
FLOW_BEATLESS_TIER_PATCH = "flow-beatless-tier-4262"


#: Sends re-send with their key while ScadBuddy has not answered, past a print's worst
#: accept (three 60 s checks, `agent/src/tools/command.ts` `ACCEPTING_MS`).
_SEND = timedelta(seconds=300)
_SEND_RETRY = RetryPolicy(maximum_attempts=3, non_retryable_error_types=[ROUTE_REFUSED])
_FOLLOW_RETRY = RetryPolicy(non_retryable_error_types=[ROUTE_REFUSED])
_HEARTBEAT = timedelta(seconds=30)
_JOB_SETTLED = ["done", "failed", "cancelled"]
_RUN_SETTLED = ["succeeded", "failed"]
OUTWARD_PREFIX = "outward-"
#: An agent tool's activity id prefix, which the agent strips to find the call id.
TOOL_PREFIX = "tool-"
#: A turn tool (agent/src/temporal/turnTools.ts): its tier activity, its call's activity
#: type prefix, and the separator in its `<server>__<tool>` name.
TURN_TOOL_TIER = "turn_tool_tier"
TURN_TOOL_PREFIX = "turn:"
TURN_TOOL_SEPARATOR = "__"
#: tool(...) reaches the turn tools (#2320); runs recorded before refuse such a name.
FLOW_TURN_TOOLS_PATCH = "flow-turn-tools-2320"
#: A turn tool's call on one agent replica's own queue (#2937): an approved call asks
#: the agent for that queue again once approved, not before it parked, and a call there
#: waits `host_queue_start_s` for a worker, then fails as that replica gone. Runs
#: recorded before kept the queue from before the park and waited without limit.
FLOW_HOST_QUEUE_PATCH = "flow-host-queue-2937"
#: The same, per call (#4152), plus one more try on the replica live when a call's wait
#: for a worker runs out. The SDK keeps a patch id's first answer for the whole run, so a
#: run that made a turn tool call before #2937 answered False to the run-wide id while
#: replaying it, and every later call, even one made after a reset onto newer code, kept
#: the gone replica's queue and waited for ever. An id per call is first asked where its
#: call is made: True for a call made on this code, False for one replayed from before.
FLOW_PINNED_CALL_PATCH = "flow-pinned-call-4152"


def _pinned_call_patched(call_id: str) -> bool:
    return workflow.patched(f"{FLOW_PINNED_CALL_PATCH}:{call_id}")


#: The agent's own tools' tiers as the agent resolves them when the call is made
#: (agent/src/temporal/flowTools.ts, #2704): runs recorded before take the run's start's.
FLOW_TOOL_TIER = "flow_tool_tier"
FLOW_LIVE_TIERS_PATCH = "flow-live-tiers-2704"
#: Lowest first (agent/src/harness/permissions.ts RISK_TIERS).
RISK_TIERS = ("read", "write", "outward")
_TIER_TIMEOUT = timedelta(seconds=30)


def _refusal(err: ActivityError) -> BaseException:
    cause = err.cause
    if isinstance(cause, ApplicationError) and cause.type in {ROUTE_REFUSED, API_URL_MISSING}:
        return RouteRefusedError(cause.message)
    return err


def _segment(value: Any, what: str) -> str:
    """`value` as one path segment of an internal route. A script names slugs and ids, so
    one must never reach another route: `/` and `\\` are refused rather than quoted
    (the server decodes `%2F` before routing), as are `.`, `..` and the empty string,
    and everything else (`?`, `#`, `%`) is percent-quoted."""
    text = str(value)
    if text in ("", ".", "..") or "/" in text or "\\" in text:
        raise ValueError(f"{what} {text!r} is not a valid {what}")
    return quote(text, safe="")


def outward_activity_id(fn: str, call_id: str) -> str:
    """An outward send's activity id, the one thing a Reset preview reads of it: its
    input is sealed (plan Ruling 14)."""
    return f"{OUTWARD_PREFIX}{fn}-{call_id}"


async def _send(
    call_id: str,
    path: str,
    body: dict[str, Any],
    *,
    suffix: str = "",
    outward: str | None = None,
) -> Any:
    """Send one command; `outward` names the gated host function whose effect it is.
    Returns the route's body."""
    return (await _post(call_id, path, body, suffix=suffix, outward=outward)).body


async def _post(
    call_id: str,
    path: str,
    body: dict[str, Any],
    *,
    suffix: str = "",
    outward: str | None = None,
) -> RouteAnswer:
    """`_send`, with the route's status as well as its body."""
    owner: StepOwner = workflow.instance()
    key = route_key(owner.run_id, workflow.info().run_id, call_id + suffix)
    try:
        answer: RouteAnswer = await workflow.execute_activity(
            FLOW_ROUTE_SEND,
            RouteSend(path=path, body=body, key=key),
            activity_id=None if outward is None else outward_activity_id(outward, call_id),
            result_type=RouteAnswer,
            start_to_close_timeout=_SEND,
            heartbeat_timeout=_HEARTBEAT,
            retry_policy=_SEND_RETRY,
        )
    except ActivityError as err:
        raise _refusal(err) from None
    return answer


async def _follow(path: str, settled: list[str], timeout: timedelta) -> dict[str, Any]:
    try:
        body: dict[str, Any] = await workflow.execute_activity(
            FLOW_ROUTE_FOLLOW,
            RouteFollow(path=path, settled=settled),
            result_type=dict,
            start_to_close_timeout=timeout,
            heartbeat_timeout=_HEARTBEAT,
            retry_policy=_FOLLOW_RETRY,
        )
    except ActivityError as err:
        raise _refusal(err) from None
    return body


async def _render(call_id: str, slug: str, params: dict[str, Any]) -> RenderResult:
    path = f"/api/v1/models/{_segment(slug, 'slug')}/render"
    accepted = await _send(call_id, path, {"params": params})
    job_path = f"/api/v1/jobs/{_segment(accepted['job_id'], 'job id')}"
    job = await _follow(job_path, _JOB_SETTLED, timedelta(hours=2))
    return RenderResult(job_id=job["id"], status=job["status"], error=job.get("error"))


async def _save(call_id: str, slug: str, job_id: str, name: str | None, *, suffix: str = "") -> str:
    """`POST /models/{slug}/outputs`: its 201 output, or its 202 operation followed."""
    body = {"job_id": job_id, **({"name": name} if name is not None else {})}
    path = f"/api/v1/models/{_segment(slug, 'slug')}/outputs"
    sent = await _post(call_id, path, body, suffix=suffix)
    answer = sent.body
    if sent.status == 202:
        operation = await _follow(
            f"/api/v1/operations/{_segment(answer['id'], 'operation id')}",
            _RUN_SETTLED,
            timedelta(minutes=30),
        )
        if operation["status"] == "failed":
            raise RouteRefusedError((operation.get("error") or {}).get("detail") or "not saved")
        answer = operation["result"] or {}
    return str(answer["id"])


@agent.tool_defn(inherently_safe=True)
async def render(slug: str, params: dict[str, Any]) -> RenderResult:
    """Render template `slug` with `params` (an identical render in progress is
    joined) and wait until it settles: `done`, `failed` or `cancelled`."""
    call_id = str(workflow.uuid4())
    async with step("render", call_id, outward=False):
        return await _render(call_id, slug, params)


@agent.tool_defn(inherently_safe=True)
async def save_output(slug: str, job_id: str, name: str) -> str:
    """Save render job `job_id` of template `slug` as an output named `name`; returns
    the output id that `queue_print` takes."""
    call_id = str(workflow.uuid4())
    async with step("save_output", call_id, outward=False):
        return await _save(call_id, slug, job_id, name)


async def _approved(call_id: str) -> None:
    """The gated body runs: the call is approved, so it waits on nothing now."""
    owner: StepOwner = workflow.instance()
    owner.mark_started(call_id)
    await project(
        ProjectionWrite(
            run_id=owner.run_id, workflow_run_id=workflow.info().run_id, waiting_remove=call_id
        )
    )


#: An approved body reads its decision, and runs a narrowed one's input (#2482): a run
#: recorded before it replays without.
FLOW_NARROWED_PATCH = "flow-narrowed-decision"


class NarrowedInputError(Exception):
    """The call was approved narrowed, but the narrowed input is not the one approved:
    nothing runs."""


async def _decided(call_id: str, fn: str, tool: str | None = None) -> DecisionRead:
    """The call's decision as the body runs it (spec 2026-10-11 §4.2): a narrowed one's
    input, bound to the decision's hash. A narrowed decision that does not hold runs
    nothing, never the input the call was parked with."""
    if not workflow.patched(FLOW_NARROWED_PATCH):
        return DecisionRead()
    read: DecisionRead = await workflow.execute_activity(
        FLOW_DECISION,
        DecisionAsk(call_id=call_id, fn=fn, tool=tool),
        result_type=DecisionRead,
        start_to_close_timeout=timedelta(seconds=10),
        retry_policy=RetryPolicy(maximum_attempts=5),
    )
    if read.narrowed and read.args is None:
        raise NarrowedInputError("the narrowed input is not the one that was approved")
    return read


def _print_path(source: dict[str, Any]) -> str:
    output_id, file_id = source.get("output_id"), source.get("file_id")
    if (output_id is None) == (file_id is None):
        raise ValueError("source names one of output_id or file_id")
    if file_id is not None:
        return f"/api/v1/print/library/{int(file_id)}/run"
    return f"/api/v1/print/outputs/{_segment(output_id, 'output id')}/run"


@agent.tool_defn()
async def approved_print(call: str, path: str, request: dict[str, Any]) -> PrintOutcome:
    """The print itself, run once a person approved it."""
    call_id = call
    await _approved(call_id)
    read = await _decided(call_id, "queue_print")
    if read.args is not None:
        # Narrowed: what the decision approved, source and request alike.
        path, request = _print_path(read.args["source"]), read.args["request"]
    owner: StepOwner = workflow.instance()
    body = {**request, "request_id": route_key(owner.run_id, workflow.info().run_id, call_id)}
    accepted = await _send(call_id, path, body, outward="queue_print")
    run = accepted
    if run["status"] not in _RUN_SETTLED:
        run_path = f"/api/v1/print/runs/{_segment(run['id'], 'run id')}"
        run = await _follow(run_path, _RUN_SETTLED, timedelta(hours=24))
    return PrintOutcome(
        run_id=run["id"],
        status=run["status"],
        may_have_queued=bool(run.get("may_have_queued")),
        error=(run.get("error") or {}).get("detail"),
        narrowed_by=read.narrowed_by,
    )


@agent.tool_defn()
async def approved_arrange(call: str, request: dict[str, Any]) -> ArrangeResult:
    """The arrange itself, run once a person approved it, then saved as an output."""
    call_id = call
    await _approved(call_id)
    read = await _decided(call_id, "arrange")
    if read.args is not None:
        request = read.args["request"]
    accepted = await _send(call_id, "/api/v1/outputs/arrange", request, outward="arrange")
    job_path = f"/api/v1/jobs/{_segment(accepted['id'], 'job id')}"
    job = await _follow(job_path, _JOB_SETTLED, timedelta(hours=2))
    result = ArrangeResult(
        job_id=job["id"],
        status=job["status"],
        error=job.get("error"),
        narrowed_by=read.narrowed_by,
    )
    if job["status"] == "done":
        name = request.get("name")
        result.output_id = await _save(
            call_id, job["slug"], job["id"], None if name is None else str(name), suffix=":save"
        )
    return result


#: The run grant's check at the gate (#2481): a run recorded before it replays without.
FLOW_GRANT_PATCH = "flow-grant-check"
#: A call outside the grant waits at the starter session first, its timer covering both
#: hops (#2482, spec §4.6).
FLOW_ESCALATION_PATCH = "flow-escalation-timer"
_GRANT_CHECK = timedelta(seconds=10)
_GRANT_CHECK_RETRY = RetryPolicy(maximum_attempts=5)
#: Retried while the call is not parked yet; past that, or on a refusal, a person is asked.
_GRANT_APPROVE_RETRY = RetryPolicy(maximum_attempts=10, non_retryable_error_types=[GRANT_REFUSED])


class GrantCall(NamedTuple):
    """A gated call as a run grant sees it: the host function (`queue_print`, `arrange`
    or `tool`), the agent tool for `tool`, its arguments, and how the arguments the
    check pinned become the gated body's."""

    fn: str
    tool: str | None
    args: dict[str, Any]
    pinned: Callable[[dict[str, Any]], dict[str, Any]]


async def _grant_verdict(call_id: str, grant: GrantCall) -> GrantVerdict | None:
    """The run grant's verdict on the call; None when the check could not be made, which
    asks the person (fail toward asking)."""
    owner: StepOwner = workflow.instance()
    try:
        verdict: GrantVerdict = await workflow.execute_activity(
            FLOW_GRANT_CHECK,
            GrantCheck(
                run_id=owner.run_id,
                request_id=f"flow:{owner.run_id}:{workflow.info().run_id}:{call_id}",
                fn=grant.fn,
                tool=grant.tool,
                args=grant.args,
            ),
            result_type=GrantVerdict,
            start_to_close_timeout=_GRANT_CHECK,
            retry_policy=_GRANT_CHECK_RETRY,
        )
    except ActivityError:
        return None
    return verdict


async def _gated(
    fn: str,
    runner: AgentWorkflowRunner,
    tool: Any,
    call_id: str,
    *,
    grant: GrantCall | None = None,
    **kwargs: Any,
) -> Any:
    owner: StepOwner = workflow.instance()
    verdict = None
    if grant is not None and workflow.patched(FLOW_GRANT_PATCH):
        verdict = await _grant_verdict(call_id, grant)
    if grant is not None and verdict is not None and verdict.verdict == "inside":
        pinned = grant.pinned(verdict.args or {})
        # Inside the grant (spec 2026-10-11 §3.3): nothing is listed and nobody asked.
        # The call parks in the harness as any gated call does, and the grant's approval
        # lets it through, bound to the call as it is parked, with its defaults pinned.
        # The approval re-derives the verdict from the store and the parked call; when it
        # will not let it through (the grant lapsed meanwhile), a person is asked.
        async with step(fn, call_id, outward=True, grant_id=verdict.grant_id) as handle:
            call = asyncio.ensure_future(runner.run_tool(call_id, tool, **{**kwargs, **pinned}))
            answer: GrantApproval
            try:
                answer = await workflow.execute_activity(  # a GrantApproval
                    FLOW_GRANT_APPROVE,
                    GrantApprove(call_id=call_id),
                    result_type=str,
                    start_to_close_timeout=_GRANT_CHECK,
                    retry_policy=_GRANT_APPROVE_RETRY,
                )
            except ActivityError:
                # The approval may have left its row behind (retries spent, a worker
                # lost): removed, so the person it is listed for can decide it.
                await workflow.execute_activity(
                    FLOW_GRANT_WITHDRAW,
                    GrantApprove(call_id=call_id),
                    start_to_close_timeout=_GRANT_CHECK,
                )
                answer = "ask"
            if answer == "approved":
                return await call
            if answer == "resolved":
                handle.step = handle.step.model_copy(update={"grant_id": None})
                return await call
            await handle.ask("approval")
            return await await_gated(
                call,
                owner.approval_timeout_s,
                lambda: owner.started(call_id),
                call_id=call_id,
            )
    # Outside the grant (#2482, spec 2026-10-11 §4.1): to the starter session first when
    # the run has one and its grant asks it, for `hop_s`, then to the person.
    hop_s = 0
    if (
        verdict is not None
        and verdict.verdict == "outside"
        and verdict.parent_hop
        and verdict.hop_s > 0
        and workflow.patched(FLOW_ESCALATION_PATCH)
    ):
        hop_s = verdict.hop_s
    async with step(
        fn,
        call_id,
        outward=True,
        waiting="approval",
        hop="parent" if hop_s else "person",
        hop_until=workflow.now() + timedelta(seconds=hop_s) if hop_s else None,
        reason=verdict.reason if verdict is not None else None,
    ):
        if hop_s:
            call = asyncio.ensure_future(runner.run_tool(call_id, tool, **kwargs))
            return await await_escalated(
                call,
                hop_s,
                owner.approval_timeout_s,
                lambda: owner.started(call_id),
                call_id=call_id,
            )
        return await run_gated(
            runner.run_tool,
            tool,
            owner.approval_timeout_s,
            lambda: owner.started(call_id),
            call_id=call_id,
            **kwargs,
        )


@agent.tool_defn(inherently_safe=True)
async def queue_print(
    source: dict[str, Any], request: dict[str, Any], runner: agent.Injected[AgentWorkflowRunner]
) -> PrintOutcome:
    """Once a person approves it, slice and queue `source` ({"output_id": ...} or
    {"file_id": ...}) with `request`, the print dialog's body (`filament_plan`,
    `choices`, ...), and wait until the run settles."""
    path = _print_path(source)
    call_id = str(workflow.uuid4())
    outcome: PrintOutcome = await _gated(
        "queue_print",
        runner,
        approved_print,
        call_id,
        grant=GrantCall(
            "queue_print",
            None,
            {"source": source, "request": request},
            lambda args: {"request": args["request"]},
        ),
        call=call_id,
        path=path,
        request=request,
    )
    return outcome


@agent.tool_defn(inherently_safe=True)
async def arrange(
    request: dict[str, Any], runner: agent.Injected[AgentWorkflowRunner]
) -> ArrangeResult:
    """Once a person approves it, lay out `request`'s objects onto plates (the body of
    POST /outputs/arrange: `objects`, `goal`, ...) and save the result as an output."""
    call_id = str(workflow.uuid4())
    result: ArrangeResult = await _gated(
        "arrange",
        runner,
        approved_arrange,
        call_id,
        grant=GrantCall(
            "arrange", None, {"request": request}, lambda args: {"request": args["request"]}
        ),
        call=call_id,
        request=request,
    )
    return result


def _tool_result(answer: Any) -> ToolResult:
    """The agent's answer: its text, or, when the call returned images, `{text, images}`
    with each image a pointer to the bytes it kept under the run
    (`agent/src/temporal/toolActivities.ts` `flowResult`)."""
    if isinstance(answer, str):
        return ToolResult(text=answer, images=[])
    return ToolResult.model_validate(answer)


async def _call_tool(
    call_id: str,
    name: str,
    args: dict[str, Any],
    *,
    attempts: int,
    activity: str | None = None,
    queue: str | None = None,
    approved: bool = False,
) -> ToolResult:
    """The agent tool `name` as an activity on the agent's `agent-tools` queue, under the
    activity id `tool-<call id>` the agent reads the call (and its approval) from. A turn
    tool's call is the activity `turn:<name>`, on the queue `turn_tool_tier` named."""
    owner: StepOwner = workflow.instance()
    task_queue = queue or owner.tools_queue
    pinned = task_queue != owner.tools_queue
    per_call = pinned and _pinned_call_patched(call_id)
    # A replica's own queue has no worker once that replica is gone (a deploy replaces
    # it), and an activity, its retries included, never moves queue (#2937).
    # An approved call waits longer: a live replica with every slot busy frees one, and
    # failing it would ask the person to approve it a second time (#3420).
    start_s = owner.approved_queue_start_s if approved else owner.host_queue_start_s
    to_start = (
        timedelta(seconds=start_s)
        if pinned and (per_call or workflow.patched(FLOW_HOST_QUEUE_PATCH))
        else None
    )
    # A pinned call nothing picked up is asked for once more on the replica the agent
    # names then (#4152): one tiered just before a deploy took its replica.
    tries = 2 if per_call else 1
    while True:
        tries -= 1
        try:
            # Untyped: a string, or `{text, images}` (#2477); a history from before either
            # replays.
            answer: Any = await workflow.execute_activity(
                activity or name,
                args,
                activity_id=f"{TOOL_PREFIX}{call_id}",
                task_queue=task_queue,
                schedule_to_start_timeout=to_start,
                start_to_close_timeout=_TOOL_TIMEOUT,
                heartbeat_timeout=_agent_heartbeat(),
                retry_policy=RetryPolicy(
                    maximum_attempts=attempts, non_retryable_error_types=_TOOL_REFUSALS
                ),
            )
            return _tool_result(answer)
        except ActivityError as err:
            if isinstance(err.cause, ApplicationError):
                raise ToolFailedError(err.cause.message) from None
            if not (
                isinstance(err.cause, ActivityTimeoutError)
                and err.cause.type == TimeoutType.SCHEDULE_TO_START
            ):
                raise
        # Gone in a deploy, or alive with every slot busy: either way nothing ran, so
        # sending it to another replica keeps it at most once.
        if tries > 0:
            try:
                live = await _turn_queue(name, args)
            except (ValueError, ToolFailedError):
                live = task_queue
            if live != task_queue:
                task_queue = live
                continue
        again = (
            "Call it again; the new call needs approving again." if approved else "Call it again."
        )
        raise ToolFailedError(
            f"{name} was not run: no worker on the agent replica that holds its state "
            f"picked it up in {start_s}s (the replica may be gone or busy). {again}"
        ) from None


@agent.tool_defn()
async def approved_tool(call: str, name: str, args: dict[str, Any]) -> ToolResult:
    """An outward agent tool, run once a person approved it, and at most once."""
    await _approved(call)
    read = await _decided(call, "tool", name)
    result = await _call_tool(call, name, read.args if read.args is not None else args, attempts=1)
    if read.narrowed_by is not None:
        # As a narrowed child call's result says it (spec 2026-10-11 §4.2).
        result.text = f"Ran narrowed by {read.narrowed_by}.\n{result.text}"
    return result


@agent.tool_defn()
async def approved_turn_tool(call: str, name: str, args: dict[str, Any], queue: str) -> ToolResult:
    """An outward plugin or http_request call, run once a person approved it, at most once."""
    await _approved(call)
    # Per call first (#4152): the run-wide id alone stays False in a run that made a
    # pinned call before #2937, whatever code this call is made on.
    if _pinned_call_patched(call) or workflow.patched(FLOW_HOST_QUEUE_PATCH):
        # The replica that tiered it may have gone while it waited (#2937): ask again. Only
        # the queue is taken: a tier raised since is the `turn:` activity's to refuse,
        # which checks the call again when it runs. A tool no longer offered at all (its
        # plugin or setting turned off while it waited) is refused here (#3420).
        try:
            queue = await _turn_queue(name, args)
        except ValueError as err:
            raise ToolFailedError(
                f"{name} was not run: it is no longer offered since it was approved ({err})."
            ) from None
        except ToolFailedError as err:
            # The approval is spent either way: say the next call is asked again (#3003).
            raise ToolFailedError(
                f"{name} was not run: {err} The new call needs approving again."
            ) from None
    return await _call_tool(
        call,
        name,
        args,
        attempts=1,
        activity=f"{TURN_TOOL_PREFIX}{name}",
        queue=queue,
        approved=True,
    )


async def _turn_tier(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """The agent's `turn_tool_tier` answer for a call: its tier, and the queue of the
    replica a pinned server's call runs on. One no enabled plugin or setting offers is
    refused."""
    owner: StepOwner = workflow.instance()
    heartbeat: timedelta | None = _agent_heartbeat()
    try:
        while True:
            try:
                tiered: dict[str, Any] = await workflow.execute_activity(
                    TURN_TOOL_TIER,
                    {"tool": name, "input": args},
                    task_queue=owner.tools_queue,
                    result_type=dict,
                    start_to_close_timeout=_TOOL_TIMEOUT,
                    heartbeat_timeout=heartbeat,
                    retry_policy=RetryPolicy(
                        maximum_attempts=3, non_retryable_error_types=_TOOL_REFUSALS
                    ),
                )
                return tiered
            except ActivityError as err:
                # Read-only, so asking again is safe (FLOW_BEATLESS_TIER_PATCH). Only the
                # last attempt's failure is known: earlier ones may have failed otherwise.
                if (
                    heartbeat is not None
                    and isinstance(err.cause, ActivityTimeoutError)
                    and err.cause.type == TimeoutType.HEARTBEAT
                    and workflow.patched(FLOW_BEATLESS_TIER_PATCH)
                ):
                    heartbeat = None
                    continue
                raise
    except ActivityError as err:
        if isinstance(err.cause, ApplicationError) and err.cause.type == "ToolError":
            raise ValueError(f"there is no tool {name!r}: {err.cause.message}") from None
        if isinstance(err.cause, ApplicationError):
            raise ToolFailedError(err.cause.message) from None
        raise


async def _turn_queue(name: str, args: dict[str, Any]) -> str:
    """The queue a turn tool's call runs on: a server whose state lives on one agent
    replica (http_request's saved bodies) names that replica's."""
    owner: StepOwner = workflow.instance()
    return str((await _turn_tier(name, args)).get("task_queue") or owner.tools_queue)


async def _turn_tool(name: str, args: dict[str, Any], runner: AgentWorkflowRunner) -> ToolResult:
    """A tool a durable turn has beside ScadBuddy's own (a remote plugin's,
    `scadbuddy_http__http_request`), named `<server>__<tool>` (agent/src/temporal/turnTools.ts).
    The agent tiers it from its input as the run's starter, and refuses one no enabled
    plugin or setting offers before anything is called."""
    owner: StepOwner = workflow.instance()
    tiered = await _turn_tier(name, args)
    tier = str(tiered.get("tier"))
    # A server whose state lives on one agent replica (http_request's saved bodies) names it.
    queue = str(tiered.get("task_queue") or owner.tools_queue)
    call_id = str(workflow.uuid4())
    if tier == "outward":
        result: ToolResult = await _gated(
            f"tool:{name}",
            runner,
            approved_turn_tool,
            call_id,
            call=call_id,
            name=name,
            args=args,
            queue=queue,
        )
        return result
    async with step(f"tool:{name}", call_id, outward=False):
        return await _call_tool(
            call_id,
            name,
            args,
            attempts=3 if tier == "read" else 1,
            activity=f"{TURN_TOOL_PREFIX}{name}",
            queue=queue,
        )


async def _live_tier(name: str, own: str | None) -> str:
    """The tool's tier as the agent resolves it now (`effectiveTool`: its definition,
    Settings' raised tiers, disabled tools), as its own turns do, and as its activity will
    check the call again when it runs. The answer only raises: a tool the run started
    with at a higher tier keeps it. One the agent does not have, or will not say, is
    refused; nothing falls back to the tiers the run started with."""
    owner: StepOwner = workflow.instance()
    try:
        tiered: dict[str, Any] = await workflow.execute_activity(
            FLOW_TOOL_TIER,
            {"tool": name},
            task_queue=owner.tools_queue,
            result_type=dict,
            start_to_close_timeout=_TIER_TIMEOUT,
            retry_policy=RetryPolicy(maximum_attempts=3, non_retryable_error_types=_TOOL_REFUSALS),
        )
    except ActivityError as err:
        if isinstance(err.cause, ApplicationError) and err.cause.type in (
            "ToolError",
            "NotForFlows",
        ):
            raise ValueError(f"there is no tool {name!r}: {err.cause.message}") from None
        if isinstance(err.cause, ApplicationError):
            raise ToolFailedError(err.cause.message) from None
        raise
    tier = tiered.get("tier")
    if tier not in RISK_TIERS:
        raise ToolFailedError(f"the agent gave no tier for {name}")
    if tiered.get("disabled") is True:
        raise ValueError(f"{name} is disabled in Settings → Plugins")
    if own in RISK_TIERS and RISK_TIERS.index(own) > RISK_TIERS.index(tier):
        return own
    return str(tier)


async def _run_tool(name: str, args: dict[str, Any], runner: AgentWorkflowRunner) -> ToolResult:
    owner: StepOwner = workflow.instance()
    tier = owner.tool_tier(name)
    # Before #2320 an unknown name was refused here with no command: a run recorded then
    # replays without the patch marker and is refused again.
    if tier is None and TURN_TOOL_SEPARATOR in name and workflow.patched(FLOW_TURN_TOOLS_PATCH):
        return await _turn_tool(name, args, runner)
    if workflow.patched(FLOW_LIVE_TIERS_PATCH):
        tier = await _live_tier(name, tier)
    elif tier is None:
        raise ValueError(f"there is no tool {name!r}")
    call_id = str(workflow.uuid4())
    if tier == "outward":
        result: ToolResult = await _gated(
            f"tool:{name}",
            runner,
            approved_tool,
            call_id,
            grant=GrantCall("tool", name, args, lambda pinned: {"args": pinned}),
            call=call_id,
            name=name,
            args=args,
        )
        return result
    async with step(f"tool:{name}", call_id, outward=False):
        # Only a read is retried: a write that landed and then lost its answer would
        # land again.
        return await _call_tool(call_id, name, args, attempts=3 if tier == "read" else 1)


@agent.tool_defn(inherently_safe=True)
async def tool(name: str, args: dict[str, Any], runner: agent.Injected[AgentWorkflowRunner]) -> str:
    """Call ScadBuddy's agent tool `name` with `args`, as the person or session that
    started this run; returns its text, each image it returned named as `[image <name>]`
    (`tool_result` gives the images themselves). `name` may also be a plugin's tool or
    `scadbuddy_http__http_request`, named `<server>__<tool>` as the assistant sees them.
    An outward tool (one that changes something outside ScadBuddy) waits for a person
    to approve it first."""
    return (await _run_tool(name, args, runner)).text


@agent.tool_defn(inherently_safe=True)
async def tool_result(
    name: str, args: dict[str, Any], runner: agent.Injected[AgentWorkflowRunner]
) -> ToolResult:
    """`tool`, answering `{"text": ..., "images": [...]}`: the images the tool returned,
    each an `Image` pointer kept with this run. An outward tool waits for approval."""
    return await _run_tool(name, args, runner)


#: The agent's refusals of a session (`agent/src/temporal/flowSessions.ts`) and a turn
#: that ended in an error: final.
_TURN_REFUSALS = ["SessionRefused", "TurnFailed", "NotForFlows", "UnknownFlow"]
#: A turn may wait on its own approvals and questions; the wait heartbeats throughout.
_TURN_TIMEOUT = timedelta(days=7)
#: A turn's wait's activity id prefix: `turn-<fn>-<call id>`, which a Reset preview reads.
TURN_PREFIX = "turn-"
_FENCED = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


def turn_activity_id(fn: str, call_id: str) -> str:
    return f"{TURN_PREFIX}{fn}-{call_id}"


def _untrusted(text: str) -> dict[str, Any]:
    """A session tool's answer, out of the untrusted-data envelope the agent wraps it in
    (`agent/src/safety/untrusted.ts`)."""
    try:
        value: Any = json.loads(text)
        if isinstance(value, dict) and "untrusted_data" in value:
            value = value["untrusted_data"].get("content")
            if isinstance(value, str):
                value = json.loads(value)
    except ValueError:
        value = None
    if not isinstance(value, dict):
        raise ToolFailedError("the agent's answer names no session")
    return value


async def _check_session(session_id: str) -> None:
    """That this run may talk to `session_id`: a durable session its starter owns."""
    owner: StepOwner = workflow.instance()
    try:
        await workflow.execute_activity(
            "flow_session_check",
            {"session_id": session_id},
            task_queue=owner.tools_queue,
            result_type=dict,
            start_to_close_timeout=_TOOL_TIMEOUT,
            heartbeat_timeout=_agent_heartbeat(),
            retry_policy=RetryPolicy(non_retryable_error_types=_TURN_REFUSALS),
        )
    except ActivityError as err:
        if isinstance(err.cause, ApplicationError):
            raise ToolFailedError(err.cause.message) from None
        raise


async def _wait_turn(fn: str, call_id: str, session_id: str, turn_id: str) -> str:
    """The turn's last message, once it ended."""
    owner: StepOwner = workflow.instance()
    try:
        answer: dict[str, Any] = await workflow.execute_activity(
            "flow_session_turn",
            {"session_id": session_id, "turn_id": turn_id},
            activity_id=turn_activity_id(fn, call_id),
            task_queue=owner.tools_queue,
            result_type=dict,
            start_to_close_timeout=_TURN_TIMEOUT,
            heartbeat_timeout=_HEARTBEAT,
            retry_policy=RetryPolicy(non_retryable_error_types=_TURN_REFUSALS),
        )
    except ActivityError as err:
        if isinstance(err.cause, ApplicationError):
            raise ToolFailedError(err.cause.message) from None
        raise
    return str(answer["text"])


def _check_schema(schema: dict[str, Any]) -> None:
    try:
        jsonschema.validators.validator_for(schema).check_schema(schema)
    except jsonschema.SchemaError as err:
        raise ValueError(f"result_schema is not a JSON Schema: {err.message}") from None


def _answer(text: str, schema: dict[str, Any] | None) -> Any:
    """The text, or with `schema` the JSON value it holds (a fenced block's included)."""
    if schema is None:
        return text
    fenced = _FENCED.match(text.strip())
    try:
        value = json.loads(fenced.group(1) if fenced else text)
    except ValueError:
        raise ValueError("the session's answer is not JSON") from None
    try:
        jsonschema.validators.validator_for(schema)(schema).validate(value)
    except jsonschema.ValidationError as err:
        raise ValueError(
            f"the session's answer does not match result_schema: {err.message}"
        ) from None
    except referencing.exceptions.Unresolvable as err:
        # A `$ref` it cannot resolve (remote refs are never fetched): `check_schema`
        # passes it, and only validating reaches the ref (#2278).
        raise ValueError(f"result_schema has a $ref that cannot be resolved: {err}") from None
    return value


def _prompt(prompt: str, skills: list[str], schema: dict[str, Any] | None) -> str:
    text = prompt
    if skills:
        text += "\n\nUse these ScadBuddy skills: " + ", ".join(skills) + "."
    if schema is not None:
        text += (
            "\n\nWhen you are done, answer with only a JSON value that matches this JSON "
            "Schema, and nothing else:\n" + json.dumps(schema)
        )
    return text


def _refs(images: list[Image]) -> dict[str, Any]:
    """Images as the `flow_image` references `sessions_start`/`sessions_send` take: the
    agent resolves each from this run's own images (`agent/src/tools/imageRefs.ts`)."""
    if not images:
        return {}
    return {"images": [{"kind": "flow_image", "name": image.name} for image in images]}


def _policy(grant: dict[str, Any] | None, on_outside: str) -> StepPolicy:
    """The step's record of the script's narrowing and `on_outside` (spec §4.3); a bad
    one is the script's error, before anything is sent."""
    try:
        return StepPolicy.model_validate({"grant": grant, "on_outside": on_outside})
    except ValidationError as err:
        raise ValueError(f"grant or on_outside: {err.errors()[0]['msg']}") from None


async def _start_session(
    prompt: str,
    skills: list[str],
    result_schema: dict[str, Any] | None,
    images: list[Image],
    policy: StepPolicy | None = None,
) -> Any:
    if result_schema is not None:
        _check_schema(result_schema)
    call_id = str(workflow.uuid4())
    async with step("agent", call_id, outward=False, policy=policy) as at:
        # Never retried: a lost answer would start a second session (Ruling 4's key is
        # not one a session start takes).
        body: dict[str, Any] = {"prompt": _prompt(prompt, skills, result_schema), "mode": "durable"}
        started = _untrusted(
            (await _call_tool(call_id, "sessions_start", body | _refs(images), attempts=1)).text
        )
        try:
            session_id, turn_id = str(started["session"]["id"]), str(started["turn_id"])
        except (KeyError, TypeError):
            raise ToolFailedError("the agent started no turn") from None
        await at.note_session(session_id)
        return _answer(await _wait_turn("agent", call_id, session_id, turn_id), result_schema)


async def _agent_session(
    prompt: str,
    skills: list[str],
    result_schema: dict[str, Any] | None,
    images: list[Image] | None = None,
    grant: dict[str, Any] | None = None,
    on_outside: str = "escalate",
) -> Any:
    """Start a durable agent session with `prompt`, as the person who started this run,
    and wait for its answer. `skills` names ScadBuddy skills for it to use (may be
    empty). With `result_schema` (a JSON Schema, or None) the answer is the JSON value
    it answered with, checked against the schema; without, its text. `images` (this
    run's, from `tool_result`) are sent with the prompt: the session sees them as images
    its user sent, never as text. The session's own outward calls wait for approval in
    its chat, unless the run's grant extends to its sessions and admits them. `grant`
    ({"rules": [...]}, the run grant's rule shape) narrows that for this session: a
    call must be admitted by both, so it can never widen it; None is the run grant as
    it is. `on_outside` ("escalate" or "deny") says what a call outside it does."""
    policy = _policy(grant, on_outside)
    return await _start_session(prompt, skills, result_schema, images or [], policy)


# `agent` is the harness's module here; the host function takes the name the script calls.
_agent_session.__name__ = _agent_session.__qualname__ = "agent"
agent_session = agent.tool_defn(inherently_safe=True)(_agent_session)


@agent.tool_defn(inherently_safe=True)
async def agent_with_images(
    prompt: str, skills: list[str], result_schema: dict[str, Any] | None, images: list[Image]
) -> Any:
    """Deprecated: call `agent(prompt, skills, result_schema, images)`, which takes
    `images` now (#2636). Kept so scripts registered before keep running."""
    return await _start_session(prompt, skills, result_schema, images)


async def _ask(
    session_id: str, message: str, images: list[Image], policy: StepPolicy | None = None
) -> str:
    call_id = str(workflow.uuid4())
    async with step("ask_session", call_id, outward=False, policy=policy) as at:
        await _check_session(session_id)
        await at.note_session(session_id)
        # Never retried: a lost answer would send the message twice.
        body: dict[str, Any] = {"session_id": session_id, "text": message} | _refs(images)
        sent = _untrusted((await _call_tool(call_id, "sessions_send", body, attempts=1)).text)
        if "turn_id" not in sent:
            raise ToolFailedError("the agent started no turn")
        return await _wait_turn("ask_session", call_id, session_id, str(sent["turn_id"]))


@agent.tool_defn(inherently_safe=True)
async def ask_session(
    session_id: str,
    message: str,
    images: list[Image] | None = None,
    grant: dict[str, Any] | None = None,
    on_outside: str = "escalate",
) -> str:
    """Send `message` to the durable agent session `session_id` (one the person who
    started this run owns) and wait for its answer's text. `images` (this run's, from
    `tool_result`) are sent with the message: the session sees them as images its user
    sent, never as text. `grant` and `on_outside` are as `agent`'s, for this turn."""
    policy = _policy(grant, on_outside)
    return await _ask(session_id, message, images or [], policy)


@agent.tool_defn(inherently_safe=True)
async def ask_session_with_images(session_id: str, message: str, images: list[Image]) -> str:
    """Deprecated: call `ask_session(session_id, message, images)`, which takes `images`
    now (#2636). Kept so scripts registered before keep running."""
    return await _ask(session_id, message, images)


#: Registered with the harness plugin. `human_answer` is the callback behind
#: `wait_for_human`, and the `approved_*` gated bodies, are never offered to a script.
FLOW_TOOLS: list[Any] = [
    sleep,
    wait_for_human,
    human_answer,
    render,
    save_output,
    queue_print,
    arrange,
    approved_print,
    approved_arrange,
    tool,
    tool_result,
    approved_tool,
    approved_turn_tool,
    agent_session,
    agent_with_images,
    ask_session,
    ask_session_with_images,
]
_INNER = (human_answer, approved_print, approved_arrange, approved_tool, approved_turn_tool)
SCRIPT_TOOLS: list[Any] = [t for t in FLOW_TOOLS if t not in _INNER]


def inputs_tool(schema: dict[str, Any], values: dict[str, Any] | None = None) -> Any:
    """`inputs()` for a flow with an inputs schema (#2483): the run's values, typed as
    the schema's `FlowInputs`. It reads the run's own message, so it records no step."""
    model = inputs_model(schema)

    async def _inputs() -> Any:
        """This run's inputs, as the flow's inputs.json declares them, defaults filled
        in. An input that is neither required nor has a default is None when the run
        was not given it."""
        return model.model_validate(values or {})

    _inputs.__name__ = _inputs.__qualname__ = "inputs"
    _inputs.__annotations__ = {"return": model}
    return agent.tool_defn(inherently_safe=True)(_inputs)


class ModelChanged(BaseModel):
    """A template's new revision was committed."""

    kind: Literal["model.changed"]
    slug: str
    commit: str


class RenderFinished(BaseModel):
    """A render finished."""

    kind: Literal["render.finished"]
    slug: str
    job_id: str


def trigger_tool(event: dict[str, Any] | None = None) -> Any:
    """`trigger()` (#2666): the event a trigger fired this run on, as the fire recorded
    it, or None. It reads the run's own message, so it records no step. Data only: who
    the run acts as, and its grant, are the trigger's record (spec 2026-10-11 §12.4)."""

    async def trigger() -> ModelChanged | RenderFinished | None:
        """The event that started this run: a template changed (`commit`) or a render
        finished (`job_id`), with its template's `slug`. None for a run started by hand
        or on a schedule."""
        if event is None:
            return None
        if event.get("kind") == "model.changed":
            return ModelChanged.model_validate(event)
        return RenderFinished.model_validate(event)

    return agent.tool_defn(inherently_safe=True)(trigger)


#: Every host function a script of a flow without inputs may call, as the type check and
#: the editor see them.
HOST_FUNCTIONS: list[Any] = [*SCRIPT_TOOLS, trigger_tool()]
#: The name of every host function a script may call on this build, `inputs()` included
#: (#4146): what the API stamps on a run it starts (`FlowStart.host_functions`), and what
#: the ``projects`` worker says it serves (`registered.RegisteredActivities`).
HOST_FUNCTION_NAMES: frozenset[str] = frozenset([*(fn.__name__ for fn in HOST_FUNCTIONS), "inputs"])


def run_flow_tool(
    runner: AgentWorkflowRunner | None = None,
    inputs_schema: dict[str, Any] | None = None,
    inputs: dict[str, Any] | None = None,
    event: dict[str, Any] | None = None,
) -> Any:
    """The Code Mode tool over the host functions a script may call, with `trigger()`,
    and `inputs()` when the flow declares inputs. Outside a workflow (the type check) no
    runner is injected."""
    injections = {"runner": runner} if runner is not None else None
    tools = [*SCRIPT_TOOLS, trigger_tool(event)]
    if inputs_schema is not None:
        tools.append(inputs_tool(inputs_schema, inputs))
    return agent.code_mode_tool(tools, name="run_flow", injections=injections)
