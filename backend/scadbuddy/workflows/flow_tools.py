"""ScadBuddy's flow host functions (spec 2026-10-01 §7.1), as harness tools.

Every function records its step on the run's row (plan 2026-10-09 Ruling 7). No
``from __future__ import annotations``: the harness reads these signatures to type-check
scripts and to coerce their arguments.
"""

from datetime import timedelta
from typing import Any
from urllib.parse import quote

from pydantic import BaseModel
from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from temporal_agent_harness.harness import agent
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner

    from scadbuddy.workflows.flow_entries import run_callback, run_gated
    from scadbuddy.workflows.flow_models import ProjectionWrite
    from scadbuddy.workflows.flow_routes import (
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


class HumanAnswer(BaseModel):
    """A person's answer on the Workflows page."""

    answer: str


@agent.callback_tool_defn(inherently_safe=True, timeout=timedelta(seconds=MAX_WAIT_S))
async def human_answer(question: str) -> HumanAnswer:  # type: ignore[empty-body]
    """Wait for a person to answer `question` on the Workflows page."""
    ...


@agent.tool_defn(inherently_safe=True)
async def wait_for_human(
    question: str, timeout_s: int, runner: agent.Injected[AgentWorkflowRunner]
) -> HumanAnswer:
    """Ask a person `question` and wait up to `timeout_s` seconds (10 to 86400) for the
    answer. Raises TimeoutError if nobody answers in time."""
    # timeout_s has no default: the pinned harness's stubs render every parameter as
    # required (code_mode/stubs.py at 04a49d1), so a default would never be used.
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


class RouteRefusedError(Exception):
    """ScadBuddy refused the call; the message is the problem's `detail`."""


class ToolFailedError(Exception):
    """An agent tool failed; the message is the tool's own text."""


#: The agent's activity refusals (`agent/src/temporal/toolActivities.ts`) and a tool's
#: own error: final, the call is never sent again.
_TOOL_REFUSALS = ["ToolError", "NotApproved", "UnknownFlow", "NotForFlows"]
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


def _refusal(err: ActivityError) -> BaseException:
    cause = err.cause
    if isinstance(cause, ApplicationError) and cause.type in {ROUTE_REFUSED, "ApiUrlMissing"}:
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
    the output id that `print` takes."""
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


async def _gated(
    fn: str, runner: AgentWorkflowRunner, tool: Any, call_id: str, **kwargs: Any
) -> Any:
    owner: StepOwner = workflow.instance()
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
        "queue_print", runner, approved_print, call_id, call=call_id, path=path, request=request
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
        "arrange", runner, approved_arrange, call_id, call=call_id, request=request
    )
    return result


async def _call_tool(call_id: str, name: str, args: dict[str, Any], *, attempts: int) -> str:
    """The agent tool `name` as an activity on the agent's `agent-tools` queue, under the
    activity id `tool-<call id>` the agent reads the call (and its approval) from."""
    owner: StepOwner = workflow.instance()
    try:
        text: str = await workflow.execute_activity(
            name,
            args,
            activity_id=f"{TOOL_PREFIX}{call_id}",
            task_queue=owner.tools_queue,
            result_type=str,
            start_to_close_timeout=_TOOL_TIMEOUT,
            retry_policy=RetryPolicy(
                maximum_attempts=attempts, non_retryable_error_types=_TOOL_REFUSALS
            ),
        )
    except ActivityError as err:
        if isinstance(err.cause, ApplicationError):
            raise ToolFailedError(err.cause.message) from None
        raise
    return text


@agent.tool_defn()
async def approved_tool(call: str, name: str, args: dict[str, Any]) -> str:
    """An outward agent tool, run once a person approved it, and at most once."""
    await _approved(call)
    return await _call_tool(call, name, args, attempts=1)


@agent.tool_defn(inherently_safe=True)
async def tool(name: str, args: dict[str, Any], runner: agent.Injected[AgentWorkflowRunner]) -> str:
    """Call ScadBuddy's agent tool `name` with `args`, as the person or session that
    started this run; returns its text. An outward tool (one that changes something
    outside ScadBuddy) waits for a person to approve it first."""
    owner: StepOwner = workflow.instance()
    tier = owner.tool_tier(name)
    if tier is None:
        raise ValueError(f"there is no tool {name!r}")
    call_id = str(workflow.uuid4())
    if tier == "outward":
        text: str = await _gated(
            f"tool:{name}", runner, approved_tool, call_id, call=call_id, name=name, args=args
        )
        return text
    async with step(f"tool:{name}", call_id, outward=False):
        # Only a read is retried: a write that landed and then lost its answer would
        # land again.
        return await _call_tool(call_id, name, args, attempts=3 if tier == "read" else 1)


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
    approved_tool,
]
_INNER = (human_answer, approved_print, approved_arrange, approved_tool)
SCRIPT_TOOLS: list[Any] = [t for t in FLOW_TOOLS if t not in _INNER]


def run_flow_tool(runner: AgentWorkflowRunner | None = None) -> Any:
    """The Code Mode tool over the host functions a script may call. Outside a workflow
    (the type check) no runner is injected."""
    injections = {"runner": runner} if runner is not None else None
    return agent.code_mode_tool(SCRIPT_TOOLS, name="run_flow", injections=injections)
