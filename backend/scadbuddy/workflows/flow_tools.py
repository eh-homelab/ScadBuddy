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

from pydantic import BaseModel, Field
from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    import jsonschema
    import referencing.exceptions
    from temporal_agent_harness.harness import agent
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner

    from scadbuddy.workflows.flow_entries import await_gated, run_callback, run_gated
    from scadbuddy.workflows.flow_models import (
        FLOW_GRANT_APPROVE,
        FLOW_GRANT_CHECK,
        GRANT_REFUSED,
        GrantApproval,
        GrantApprove,
        GrantCheck,
        GrantVerdict,
        ProjectionWrite,
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


class ArrangeResult(BaseModel):
    """An arrange job once it settled, and the output it was saved as when `done`."""

    job_id: str
    status: str
    output_id: str | None = None
    error: str | None = None


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


@agent.tool_defn()
async def approved_print(call: str, path: str, request: dict[str, Any]) -> PrintOutcome:
    """The print itself, run once a person approved it."""
    call_id = call
    await _approved(call_id)
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
    )


@agent.tool_defn()
async def approved_arrange(call: str, request: dict[str, Any]) -> ArrangeResult:
    """The arrange itself, run once a person approved it, then saved as an output."""
    call_id = call
    await _approved(call_id)
    accepted = await _send(call_id, "/api/v1/outputs/arrange", request, outward="arrange")
    job_path = f"/api/v1/jobs/{_segment(accepted['id'], 'job id')}"
    job = await _follow(job_path, _JOB_SETTLED, timedelta(hours=2))
    result = ArrangeResult(job_id=job["id"], status=job["status"], error=job.get("error"))
    if job["status"] == "done":
        name = request.get("name")
        result.output_id = await _save(
            call_id, job["slug"], job["id"], None if name is None else str(name), suffix=":save"
        )
    return result


#: The run grant's check at the gate (#2481): a run recorded before it replays without.
FLOW_GRANT_PATCH = "flow-grant-check"
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
    async with step(fn, call_id, outward=True, waiting="approval"):
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
    output_id, file_id = source.get("output_id"), source.get("file_id")
    if (output_id is None) == (file_id is None):
        raise ValueError("source names one of output_id or file_id")
    if file_id is not None:
        path = f"/api/v1/print/library/{int(file_id)}/run"
    else:
        path = f"/api/v1/print/outputs/{_segment(output_id, 'output id')}/run"
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
) -> ToolResult:
    """The agent tool `name` as an activity on the agent's `agent-tools` queue, under the
    activity id `tool-<call id>` the agent reads the call (and its approval) from. A turn
    tool's call is the activity `turn:<name>`, on the queue `turn_tool_tier` named."""
    owner: StepOwner = workflow.instance()
    try:
        # Untyped: a string, or `{text, images}` (#2477); a history from before either replays.
        answer: Any = await workflow.execute_activity(
            activity or name,
            args,
            activity_id=f"{TOOL_PREFIX}{call_id}",
            task_queue=queue or owner.tools_queue,
            start_to_close_timeout=_TOOL_TIMEOUT,
            retry_policy=RetryPolicy(
                maximum_attempts=attempts, non_retryable_error_types=_TOOL_REFUSALS
            ),
        )
    except ActivityError as err:
        if isinstance(err.cause, ApplicationError):
            raise ToolFailedError(err.cause.message) from None
        raise
    return _tool_result(answer)


@agent.tool_defn()
async def approved_tool(call: str, name: str, args: dict[str, Any]) -> ToolResult:
    """An outward agent tool, run once a person approved it, and at most once."""
    await _approved(call)
    return await _call_tool(call, name, args, attempts=1)


@agent.tool_defn()
async def approved_turn_tool(call: str, name: str, args: dict[str, Any], queue: str) -> ToolResult:
    """An outward plugin or http_request call, run once a person approved it, at most once."""
    await _approved(call)
    return await _call_tool(
        call, name, args, attempts=1, activity=f"{TURN_TOOL_PREFIX}{name}", queue=queue
    )


async def _turn_tool(name: str, args: dict[str, Any], runner: AgentWorkflowRunner) -> ToolResult:
    """A tool a durable turn has beside ScadBuddy's own (a remote plugin's,
    `scadbuddy_http__http_request`), named `<server>__<tool>` (agent/src/temporal/turnTools.ts).
    The agent tiers it from its input as the run's starter, and refuses one no enabled
    plugin or setting offers before anything is called."""
    owner: StepOwner = workflow.instance()
    try:
        tiered: dict[str, Any] = await workflow.execute_activity(
            TURN_TOOL_TIER,
            {"tool": name, "input": args},
            task_queue=owner.tools_queue,
            result_type=dict,
            start_to_close_timeout=_TOOL_TIMEOUT,
            retry_policy=RetryPolicy(maximum_attempts=3, non_retryable_error_types=_TOOL_REFUSALS),
        )
    except ActivityError as err:
        if isinstance(err.cause, ApplicationError) and err.cause.type == "ToolError":
            raise ValueError(f"there is no tool {name!r}: {err.cause.message}") from None
        if isinstance(err.cause, ApplicationError):
            raise ToolFailedError(err.cause.message) from None
        raise
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


async def _run_tool(name: str, args: dict[str, Any], runner: AgentWorkflowRunner) -> ToolResult:
    owner: StepOwner = workflow.instance()
    tier = owner.tool_tier(name)
    if tier is None:
        # Before #2320 an unknown name was refused here with no command: a run recorded
        # then replays without the patch marker and is refused again.
        if TURN_TOOL_SEPARATOR in name and workflow.patched(FLOW_TURN_TOOLS_PATCH):
            return await _turn_tool(name, args, runner)
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


async def _start_session(
    prompt: str, skills: list[str], result_schema: dict[str, Any] | None, images: list[Image]
) -> Any:
    if result_schema is not None:
        _check_schema(result_schema)
    call_id = str(workflow.uuid4())
    async with step("agent", call_id, outward=False) as at:
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
) -> Any:
    """Start a durable agent session with `prompt`, as the person who started this run,
    and wait for its answer. `skills` names ScadBuddy skills for it to use (may be
    empty). With `result_schema` (a JSON Schema, or None) the answer is the JSON value
    it answered with, checked against the schema; without, its text. `images` (this
    run's, from `tool_result`) are sent with the prompt: the session sees them as images
    its user sent, never as text. The session's own outward calls wait for approval in
    its chat."""
    return await _start_session(prompt, skills, result_schema, images or [])


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


async def _ask(session_id: str, message: str, images: list[Image]) -> str:
    call_id = str(workflow.uuid4())
    async with step("ask_session", call_id, outward=False) as at:
        await _check_session(session_id)
        await at.note_session(session_id)
        # Never retried: a lost answer would send the message twice.
        body: dict[str, Any] = {"session_id": session_id, "text": message} | _refs(images)
        sent = _untrusted((await _call_tool(call_id, "sessions_send", body, attempts=1)).text)
        if "turn_id" not in sent:
            raise ToolFailedError("the agent started no turn")
        return await _wait_turn("ask_session", call_id, session_id, str(sent["turn_id"]))


@agent.tool_defn(inherently_safe=True)
async def ask_session(session_id: str, message: str, images: list[Image] | None = None) -> str:
    """Send `message` to the durable agent session `session_id` (one the person who
    started this run owns) and wait for its answer's text. `images` (this run's, from
    `tool_result`) are sent with the message: the session sees them as images its user
    sent, never as text."""
    return await _ask(session_id, message, images or [])


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


def run_flow_tool(runner: AgentWorkflowRunner | None = None) -> Any:
    """The Code Mode tool over the host functions a script may call. Outside a workflow
    (the type check) no runner is injected."""
    injections = {"runner": runner} if runner is not None else None
    return agent.code_mode_tool(SCRIPT_TOOLS, name="run_flow", injections=injections)
