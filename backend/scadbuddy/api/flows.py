"""Flows (#1057, spec 2026-10-01 §7.3, plan 2026-10-09-durable-phase-6-flows.md Task
B5): register a script, start runs of it, and read them.

A run is started by update-with-start through the harness's own client, and answered at
acceptance (decision C): its row is written moments later by the run's first activity,
and until then `GET /workflow-runs/{id}` answers `starting` from the execution.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

import psycopg
from fastapi import APIRouter, Body, Query, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from temporal_agent_harness.harness.agent_client import AgentClient, MidTurnRejectedError
from temporal_agent_harness.harness.agent_protocol import AgentConfig
from temporalio.client import Client, WorkflowExecutionStatus, WorkflowUpdateFailedError
from temporalio.service import RPCError, RPCStatusCode

from scadbuddy.api.deps import StateDep
from scadbuddy.api.operations import (
    KEY_REQUIRED_DETAIL,
    KEY_REQUIRED_PROBLEM,
    OPERATION_RESPONSES,
    IdempotencyKey,
    operation_answer,
    run_operation,
    still_accepting,
)
from scadbuddy.core.authorship import current_author
from scadbuddy.core.problems import ApiError
from scadbuddy.core.settings import check_approval_timeout
from scadbuddy.flows.component import Flows, FlowsDep
from scadbuddy.flows.forget import forget_run
from scadbuddy.flows.history import ResetPreview, outward_since
from scadbuddy.flows.models import (
    HOST_FN,
    TERMINAL,
    Definition,
    DefinitionSummary,
    Run,
    RunStatus,
)
from scadbuddy.flows.operations import FLOW_ANSWER, FLOW_DECIDE, FLOW_RESET
from scadbuddy.flows.typecheck import MAX_SCRIPT_BYTES, ScriptProblem, check_script
from scadbuddy.operations.component import OperationsDep
from scadbuddy.workflows.commands import COMMAND_ANSWER_DEADLINE
from scadbuddy.workflows.payload_codec import SubjectForgottenError
from scadbuddy.workflows.project import PROJECT_WORKFLOW, FlowStart, RunFlow

logger = logging.getLogger(__name__)

router = APIRouter(tags=["flows"])

#: A run's id is this namespace's uuid5 of its version and key (Ruling 2), so a re-sent
#: start reaches the same workflow id.
FLOW_NAMESPACE = uuid.UUID("5b6f1d0e-6a8f-4f0e-9a51-0c1f1e57f10f")
FLOWS_UNAVAILABLE = "https://scadbuddy.dev/problems/flows-unavailable"
SCRIPT_PROBLEMS = "https://scadbuddy.dev/problems/flow-script"
BROWSER_ONLY = "https://scadbuddy.dev/problems/flow-browser-only"
ENTRY_BROWSER_ONLY = "https://scadbuddy.dev/problems/flow-entry-browser-only"
_STATUS_SECONDS = 2.0

ApprovalTimeout = int | Literal["never"]


def _seconds(value: ApprovalTimeout | None) -> int | None:
    """An approval timeout as stored: None inherits, 0 is never."""
    if value is None:
        return None
    if value == "never":
        return 0
    return check_approval_timeout(value, "approval_timeout")


class DefinitionCreate(BaseModel):
    """A flow version to register."""

    #: Lower-case letters, digits and dashes; a new version of an existing name.
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    #: The script: Python over the host functions, checked before it is stored.
    script: str
    #: Seconds an outward call of its runs waits for a decision, or "never". Omitted,
    #: the global setting applies.
    approval_timeout: ApprovalTimeout | None = None

    @field_validator("approval_timeout")
    @classmethod
    def _timeout(cls, value: ApprovalTimeout | None) -> ApprovalTimeout | None:
        _seconds(value)
        return value


class RunCreate(BaseModel):
    """Options of one run."""

    #: Overrides the flow's approval timeout for this run: seconds, or "never".
    approval_timeout: ApprovalTimeout | None = None

    @field_validator("approval_timeout")
    @classmethod
    def _timeout(cls, value: ApprovalTimeout | None) -> ApprovalTimeout | None:
        _seconds(value)
        return value


class RunAnswer(BaseModel):
    """The run a start reached."""

    id: str
    status: RunStatus
    #: An earlier start with the same key reached this run: nothing new was started.
    repeated: bool = False


class PendingEntry(BaseModel):
    """A host call parked for a person. Never an approval's input (§6.6)."""

    call_id: str
    kind: Literal["approval", "answer"]
    fn: str
    prompt: str | None = None
    since: datetime | None = None


class RunView(BaseModel):
    """A run, with what it waits on now."""

    id: str
    status: RunStatus
    definition_id: str
    version: int
    name: str
    run: Run | None = None
    #: The parked calls, from the running workflow when `live`, else from the row.
    pending: list[PendingEntry] = []
    #: Whether `pending` was read from the running workflow.
    live: bool = False


def _started_by() -> dict[str, Any]:
    """Who started it (Ruling 6): the agent a request is authored as, else the browser."""
    author = current_author()
    if author is None:
        return {"kind": "browser"}
    return {"kind": "agent", "principal": author.principal, "session": author.session}


def _unavailable() -> ApiError:
    return ApiError(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "Flows need ScadBuddy's secret key file (SCADBUDDY_SECRET_KEY_FILE) and the"
        " assistant's database tables.",
        type_=FLOWS_UNAVAILABLE,
    )


def _client(flows: Flows) -> Client:
    if flows.client is None:
        raise _unavailable()
    return flows.client


def _script_problems(problems: list[ScriptProblem], detail: str) -> ApiError:
    return ApiError(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail,
        type_=SCRIPT_PROBLEMS,
        problems=[p.model_dump() for p in problems],
    )


@router.post(
    "/workflows",
    status_code=status.HTTP_201_CREATED,
    response_model=Definition,
    summary="Register a flow (a new version of its name)",
)
async def register_flow(body: DefinitionCreate, flows: FlowsDep) -> Definition:
    if len(body.script.encode()) > MAX_SCRIPT_BYTES:
        raise ApiError(status.HTTP_413_CONTENT_TOO_LARGE, "The script is over 64 KiB.")
    if problems := await check_script(body.script):
        raise _script_problems(problems, "The script does not type-check.")
    return await flows.store.create_definition(
        body.name, body.script, _started_by(), approval_timeout_s=_seconds(body.approval_timeout)
    )


@router.get("/workflows", response_model=list[DefinitionSummary], summary="List flows")
async def list_flows(flows: FlowsDep) -> list[DefinitionSummary]:
    return await flows.store.list_definitions()


@router.get("/workflows/{definition_id}", response_model=Definition, summary="Get a flow")
async def get_flow(definition_id: str, flows: FlowsDep) -> Definition:
    definition = await flows.store.get_definition(definition_id)
    if definition is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "No such flow.")
    return definition


async def _execution(client: Client, run_id: str) -> WorkflowExecutionStatus | None:
    """The run's execution's status; None when there is none."""
    try:
        described = await client.get_workflow_handle(f"flow-{run_id}").describe(
            rpc_timeout=timedelta(seconds=_STATUS_SECONDS)
        )
    except RPCError as err:
        if err.status == RPCStatusCode.NOT_FOUND:
            return None
        raise
    return described.status


@router.post(
    "/workflows/{definition_id}/runs",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=RunAnswer,
    summary="Start a run of a flow",
    responses={200: {"model": RunAnswer, "description": "A repeat: the run this key started."}},
)
async def start_flow_run(
    definition_id: str,
    response: Response,
    flows: FlowsDep,
    state: StateDep,
    key: IdempotencyKey = None,
    body: Annotated[RunCreate | None, Body()] = None,
) -> RunAnswer:
    if key is None:
        raise ApiError(
            status.HTTP_428_PRECONDITION_REQUIRED, KEY_REQUIRED_DETAIL, type_=KEY_REQUIRED_PROBLEM
        )
    definition = await flows.store.get_definition(definition_id)
    if definition is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "No such flow.")
    run_id = str(uuid.uuid5(FLOW_NAMESPACE, f"{definition.id}:{definition.version}:{key}"))
    # A row answers every repeat, however its run ended (Ruling 3).
    if (existing := await flows.store.get_run(run_id)) is not None:
        response.status_code = status.HTTP_200_OK
        return RunAnswer(id=existing.id, status=existing.status, repeated=True)
    client = _client(flows)
    if problems := await check_script(definition.script):
        raise _script_problems(problems, "The flow no longer type-checks.")
    override = _seconds((body or RunCreate()).approval_timeout)
    timeout = next(
        value
        for value in (
            override,
            definition.approval_timeout_s,
            state.settings.flow_approval_timeout_seconds,
        )
        if value is not None
    )
    try:
        async with asyncio.timeout(COMMAND_ANSWER_DEADLINE.total_seconds()):
            # Started already, its row not yet written: the same run, nothing new.
            if (execution := await _execution(client, run_id)) is not None:
                response.status_code = status.HTTP_200_OK
                closed = execution != WorkflowExecutionStatus.RUNNING
                return RunAnswer(
                    id=run_id, status="terminated" if closed else "starting", repeated=True
                )
            await AgentClient(client, f"flow-{run_id}").start_and_submit_message(
                "execute",
                RunFlow(script=definition.script).model_dump(),
                workflow_name=PROJECT_WORKFLOW,
                task_queue=flows.queue,
                start_config=AgentConfig(),
                start_data=FlowStart(
                    run_id=run_id,
                    definition_id=definition.id,
                    version=definition.version,
                    name=definition.name,
                    started_by=_started_by(),
                    approval_timeout_s=timeout,
                    search_attributes=flows.search_attributes,
                ),
                update_id=key,
            )
    except TimeoutError:
        raise still_accepting() from None
    except (MidTurnRejectedError, WorkflowUpdateFailedError) as err:
        # The run's workflow refused this script: one started under another update id.
        raise ApiError(status.HTTP_409_CONFLICT, "This run could not be started.") from err
    except (psycopg.errors.UndefinedTable, SubjectForgottenError) as err:
        logger.warning("flows are unavailable: %s", type(err).__name__)
        raise _unavailable() from None
    except RPCError as err:
        logger.warning("could not reach Temporal to start a flow run", exc_info=True)
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "ScadBuddy cannot reach Temporal, where flows run. Send it again.",
            headers={"Retry-After": "5"},
        ) from err
    return RunAnswer(id=run_id, status="starting")


@router.get("/workflow-runs", response_model=list[Run], summary="List flow runs")
async def list_flow_runs(
    flows: FlowsDep,
    definition_id: str | None = None,
    session: Annotated[str | None, Query(description="The agent session that started it.")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[Run]:
    return await flows.store.list_runs(definition_id=definition_id, session=session, limit=limit)


async def _starting(client: Client, run_id: str) -> RunView | None:
    """A run whose row is not written yet, from its open execution's input. None
    without one: a closed execution with no row did nothing, or was deleted."""
    if await _execution(client, run_id) != WorkflowExecutionStatus.RUNNING:
        return None
    handle = client.get_workflow_handle(f"flow-{run_id}")
    try:
        async for event in handle.fetch_history_events():
            attrs = event.workflow_execution_started_event_attributes
            _, start = await client.data_converter.decode(
                attrs.input.payloads, [AgentConfig, FlowStart]
            )
            return RunView(
                id=run_id,
                status="starting",
                definition_id=start.definition_id,
                version=start.version,
                name=start.name,
                live=True,
            )
    except (RPCError, SubjectForgottenError) as err:
        if isinstance(err, RPCError) and err.status != RPCStatusCode.NOT_FOUND:
            raise
    return None


async def _pending(client: Client, run: Run) -> tuple[list[PendingEntry], bool]:
    """The run's parked calls: from the workflow's `agent_status`, joined with the row's
    `waiting_on` for the prompt and the time; from the row alone if it does not answer."""
    waiting = {w.call_id: w for w in run.waiting_on}
    try:
        async with asyncio.timeout(_STATUS_SECONDS):
            status_ = await AgentClient(client, run.workflow_id).get_status()
    except (TimeoutError, RPCError):
        return [PendingEntry.model_validate(w.model_dump()) for w in run.waiting_on], False
    entries: list[PendingEntry] = []
    for approval in status_.pending_approvals:
        seen = waiting.get(approval.tool_id)
        entries.append(
            PendingEntry(
                call_id=approval.tool_id,
                kind="approval",
                fn=seen.fn if seen else HOST_FN.get(approval.tool_name, approval.tool_name),
                since=seen.since if seen else None,
            )
        )
    for callback in status_.pending_callbacks:
        seen = waiting.get(callback.tool_id)
        entries.append(
            PendingEntry(
                call_id=callback.tool_id,
                kind="answer",
                fn=seen.fn if seen else HOST_FN.get(callback.tool_name, callback.tool_name),
                prompt=seen.prompt if seen else None,
                since=seen.since if seen else None,
            )
        )
    return entries, True


@router.get("/workflow-runs/{run_id}", response_model=RunView, summary="Get a flow run")
async def get_flow_run(run_id: str, flows: FlowsDep) -> RunView:
    run = await flows.store.get_run(run_id)
    if run is None:
        if flows.client is None or (view := await _starting(flows.client, run_id)) is None:
            raise ApiError(status.HTTP_404_NOT_FOUND, "No such flow run.")
        return view
    view = RunView(
        id=run.id,
        status=run.status,
        definition_id=run.definition_id,
        version=run.version,
        name=run.name,
        run=run,
    )
    if run.status in TERMINAL:
        return view
    if flows.client is None:
        view.pending = [PendingEntry.model_validate(w.model_dump()) for w in run.waiting_on]
        return view
    view.pending, view.live = await _pending(flows.client, run)
    return view


@router.delete(
    "/workflow-runs/{run_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a flow run and forget its payloads",
)
async def delete_flow_run(run_id: str, flows: FlowsDep) -> Response:
    """Its key is deleted first, so every copy of its history is unreadable from then on;
    then its workflow (terminated if open) and its row. A person's decision only."""
    if current_author() is not None:
        raise ApiError(
            status.HTTP_403_FORBIDDEN, "Only a person can delete a flow run.", type_=BROWSER_ONLY
        )
    try:
        uuid.UUID(run_id)
    except ValueError:
        raise ApiError(status.HTTP_404_NOT_FOUND, "No such flow run.") from None
    if flows.client is None or flows.connect is None:
        raise _unavailable()
    if await flows.store.get_run(run_id) is None and await _execution(flows.client, run_id) is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "No such flow run.")
    await forget_run(run_id, connect=flows.connect, client=flows.client, store=flows.store)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


class AnswerBody(BaseModel):
    """A person's answer to a run's `wait_for_human` question."""

    #: The parked call, from the run's `pending`.
    call_id: str = Field(min_length=1, max_length=64)
    answer: str = Field(max_length=16_384)


class DecideBody(BaseModel):
    """A person's decision on a run's outward call."""

    call_id: str = Field(min_length=1, max_length=64)
    approved: bool
    #: Why, for a denial: the script sees it.
    reason: str | None = Field(default=None, max_length=1024)


class Decided(BaseModel):
    """What a decision did."""

    run_id: str
    call_id: str
    outcome: Literal["approved", "denied", "answered"]


def _browser_only() -> None:
    # Spec §6.6, plan 6 Ruling 6: a flow's entries are answered on the Workflows page,
    # never by an agent, whatever grant it holds.
    if current_author() is not None:
        raise ApiError(
            status.HTTP_403_FORBIDDEN,
            "Only a person answers a flow's questions and approvals.",
            type_=ENTRY_BROWSER_ONLY,
        )


@router.post(
    "/workflow-runs/{run_id}/answer",
    response_model=Decided,
    responses=OPERATION_RESPONSES,
    summary="Answer a run's question",
)
async def answer_flow_run(
    run_id: str,
    body: AnswerBody,
    response: Response,
    ops: OperationsDep,
    idempotency_key: IdempotencyKey = None,
) -> Decided | JSONResponse:
    _browser_only()
    result = await run_operation(
        ops,
        response,
        kind=ops.kinds[FLOW_ANSWER],
        subject=f"flow:{run_id}",
        request={"run_id": run_id, **body.model_dump()},
        idempotency_key=idempotency_key,
    )
    return operation_answer(result, Decided)


@router.post(
    "/workflow-runs/{run_id}/decide",
    response_model=Decided,
    responses=OPERATION_RESPONSES,
    summary="Approve or deny a run's outward call",
)
async def decide_flow_run(
    run_id: str,
    body: DecideBody,
    response: Response,
    ops: OperationsDep,
    idempotency_key: IdempotencyKey = None,
) -> Decided | JSONResponse:
    _browser_only()
    result = await run_operation(
        ops,
        response,
        kind=ops.kinds[FLOW_DECIDE],
        subject=f"flow:{run_id}",
        request={"run_id": run_id, **body.model_dump()},
        idempotency_key=idempotency_key,
    )
    return operation_answer(result, Decided)


class ResetBody(BaseModel):
    """Where to reset a run to, and the preview the caller saw."""

    #: A completed workflow task of the run's history, after its script started.
    event_id: int = Field(ge=1)
    #: The preview's `as_of_event_id`: a Reset is refused (409, with the new preview)
    #: when outward calls were added after it.
    as_of_event_id: int = Field(ge=0)


class Reset(BaseModel):
    """A Reset done: the run continues on a new execution."""

    run_id: str
    workflow_run_id: str
    event_id: int


@router.get(
    "/workflow-runs/{run_id}/reset-preview",
    response_model=ResetPreview,
    summary="What a Reset of a run would run again",
)
async def preview_flow_reset(
    run_id: str,
    flows: FlowsDep,
    event_id: Annotated[int, Query(ge=1, description="A completed workflow task's event id.")],
) -> ResetPreview:
    """The outward calls (a print, an arrange) a Reset to `event_id` would send again,
    as new effects (spec 2026-10-01 §7.4). Every host call after the point runs again;
    the others are local. Pinned to the history's last event, `as_of_event_id`, which
    the Reset sends back."""
    run = await flows.store.get_run(run_id)
    if run is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "No such flow run.")
    if flows.client is None:
        raise _unavailable()
    return await outward_since(flows.client, run.workflow_id, event_id)


@router.post(
    "/workflow-runs/{run_id}/reset",
    response_model=Reset,
    responses=OPERATION_RESPONSES,
    summary="Reset a run to an earlier point",
)
async def reset_flow_run(
    run_id: str,
    body: ResetBody,
    response: Response,
    ops: OperationsDep,
    idempotency_key: IdempotencyKey = None,
) -> Reset | JSONResponse:
    """Temporal's Reset (spec 2026-10-01 §7.4): the run continues from `event_id` on
    the current code with the same script, and every host call after it runs again.
    The answers and approvals after it are undone, so those calls park again. 409
    `flow-reset-changed` with the new `preview` when outward calls landed after
    `as_of_event_id`; 422 `flow-reset-point` for an event that is not a completed
    workflow task after the script started."""
    author = current_author()
    result = await run_operation(
        ops,
        response,
        kind=ops.kinds[FLOW_RESET],
        subject=f"flow:{run_id}",
        request={
            "run_id": run_id,
            **body.model_dump(),
            "responder": "browser" if author is None else f"agent {author.principal}",
        },
        idempotency_key=idempotency_key,
    )
    return operation_answer(result, Reset)
