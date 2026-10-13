"""Flows (#1057, spec 2026-10-01 §7.3, plan 2026-10-09-durable-phase-6-flows.md Task
B5): register a script, start runs of it, and read them.

A run is started by update-with-start through the harness's own client, and answered at
acceptance (decision C): its row is written moments later by the run's first activity,
and until then `GET /workflow-runs/{id}` answers `starting` from the execution.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

import psycopg
from fastapi import APIRouter, Body, Path, Query, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
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
from scadbuddy.core.gitrepo import COMMIT_ID_PATTERN, GitError, RevisionNotFoundError
from scadbuddy.core.problems import ApiError
from scadbuddy.core.settings import check_approval_timeout
from scadbuddy.flows.component import Flows, FlowsDep
from scadbuddy.flows.definitions import (
    FlowHistoryUnavailableError,
    ScriptMissingError,
    UnknownFlowError,
)
from scadbuddy.flows.forget import forget_run
from scadbuddy.flows.grants import (
    Grant,
    GrantSpec,
    GrantStatus,
    GrantUse,
    RuleUse,
    validate_spec,
)
from scadbuddy.flows.history import ResetPoints, ResetPreview, outward_since, reset_points
from scadbuddy.flows.images import BLOB_NAME, RunImage, open_image, run_images
from scadbuddy.flows.inputs import InputsError, resolve_inputs
from scadbuddy.flows.keys import approval_key
from scadbuddy.flows.manifest import raise_tiers, tiers
from scadbuddy.flows.models import (
    HOST_FN,
    TERMINAL,
    Definition,
    DefinitionSummary,
    FlowDiff,
    FlowState,
    FlowVersion,
    Run,
    RunStatus,
)
from scadbuddy.flows.operations import (
    FLOW_ANSWER,
    FLOW_DECIDE,
    FLOW_ESCALATE,
    FLOW_RESET,
    NOT_PARENT,
    seal_answer,
)
from scadbuddy.flows.store import GrantStateError
from scadbuddy.flows.typecheck import MAX_SCRIPT_BYTES, ScriptProblem, check_script
from scadbuddy.operations.component import OperationsDep
from scadbuddy.workflows.commands import COMMAND_ANSWER_DEADLINE
from scadbuddy.workflows.flow_grants import FlowGrants, parked_call
from scadbuddy.workflows.flow_models import HopRecord
from scadbuddy.workflows.flow_routes import api_client
from scadbuddy.workflows.payload_codec import SubjectForgottenError
from scadbuddy.workflows.project import PROJECT_WORKFLOW, FlowStart, RunFlow

logger = logging.getLogger(__name__)

router = APIRouter(tags=["flows"])

#: A run's id is this namespace's uuid5 of its version and key (Ruling 2), so a re-sent
#: start reaches the same workflow id.
FLOW_NAMESPACE = uuid.UUID("5b6f1d0e-6a8f-4f0e-9a51-0c1f1e57f10f")
FLOWS_UNAVAILABLE = "https://scadbuddy.dev/problems/flows-unavailable"
SCRIPT_PROBLEMS = "https://scadbuddy.dev/problems/flow-script"
SCRIPT_MISSING = "https://scadbuddy.dev/problems/flow-script-missing"
FLOW_ARCHIVED = "https://scadbuddy.dev/problems/flow-archived"
FLOW_DELETED = "https://scadbuddy.dev/problems/flow-deleted"
FLOW_INPUTS = "https://scadbuddy.dev/problems/flow-inputs"
NAME_PATTERN = r"^[a-z0-9][a-z0-9-]{0,62}$"
BROWSER_ONLY = "https://scadbuddy.dev/problems/flow-browser-only"
ENTRY_BROWSER_ONLY = "https://scadbuddy.dev/problems/flow-entry-browser-only"
GRANT_INVALID = "https://scadbuddy.dev/problems/flow-grant-invalid"
GRANT_STATE = "https://scadbuddy.dev/problems/flow-grant-state"
GRANT_STARTER_ONLY = "https://scadbuddy.dev/problems/flow-grant-starter-only"
GRANT_BROWSER_ONLY = "https://scadbuddy.dev/problems/flow-grant-browser-only"
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
    name: str = Field(pattern=NAME_PATTERN)
    #: The script: Python over the host functions, checked before it is stored.
    script: str
    #: Seconds an outward call of its runs waits for a decision, or "never". Omitted,
    #: the global setting applies.
    approval_timeout: ApprovalTimeout | None = None
    #: Its typed inputs (#2483): a JSON Schema of `type: object` whose properties are
    #: strings, integers, numbers, booleans or arrays of one of them, each with an
    #: optional default and description. Committed as `inputs.json` beside the script;
    #: the script reads them with `inputs()`, type-checked against it. Omitted, its runs
    #: take no inputs.
    inputs_schema: dict[str, Any] | None = None

    @field_validator("approval_timeout")
    @classmethod
    def _timeout(cls, value: ApprovalTimeout | None) -> ApprovalTimeout | None:
        _seconds(value)
        return value


class RunCreate(BaseModel):
    """Options of one run."""

    #: Overrides the flow's approval timeout for this run: seconds, or "never".
    approval_timeout: ApprovalTimeout | None = None
    #: What the run's outward calls may do without asking (#2481). From the browser it
    #: is approved as sent; from an agent it is only requested, until a person approves
    #: it on the run's page. At most 7 days.
    grant: GrantSpec | None = None
    #: The run's inputs (#2483), by name, checked against the flow's inputs schema; an
    #: input left out takes its default. A flow with no inputs schema takes none.
    inputs: dict[str, Any] | None = None

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
    """A host call parked for a person. Never an approval's input (§6.6), nor a
    parent's narrowing of it (#2482): only that one was proposed."""

    call_id: str
    kind: Literal["approval", "answer"]
    fn: str
    prompt: str | None = None
    since: datetime | None = None
    #: Where an approval outside the run's grant waits (#2482): `parent` (the starting
    #: session) until `hop_until`, then `person`; the person may decide at either.
    hop: Literal["parent", "person"] | None = None
    hop_until: datetime | None = None
    #: Why the grant did not admit it.
    reason: str | None = None
    #: Whether the starting session narrowed it into a proposal the person may approve.
    proposed: bool = False
    hops: list[HopRecord] = []


class GrantUseView(BaseModel):
    """One call the run's grant let through without asking."""

    request_id: str
    fn: str
    tool: str | None = None
    rule_index: int
    prints: int = 0
    filament_g: float = 0
    #: The child session the call ran in, for a session's flow turn.
    session_id: str | None = None
    created_at: datetime | None = None


class GrantView(BaseModel):
    """A run's grant (#2481): its spec, state and what it let through."""

    id: str
    status: GrantStatus
    spec: GrantSpec
    #: Per rule index: what it let through so far, against its maxima.
    used: dict[str, RuleUse] = {}
    #: Who started the run: the grant's proposer when it was requested by an agent.
    requested_by: dict[str, Any]
    approved_by: str | None = None
    approved_at: datetime | None = None
    revoked_by: str | None = None
    revoked_at: datetime | None = None
    expires_at: datetime | None = None
    created_at: datetime
    #: Approved, not revoked and not expired: calls inside it run without asking.
    in_force: bool
    uses: list[GrantUseView] = []
    #: On approval: the parked calls it let through.
    released: list[str] = []


def _grant_view(grant: Grant, uses: list[GrantUse]) -> GrantView:
    return GrantView(
        **grant.model_dump(exclude={"run_id"}),
        in_force=grant.in_force(datetime.now(UTC)),
        uses=[GrantUseView.model_validate(u.model_dump()) for u in uses],
    )


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
    #: The images the run's tool calls returned (#2477), as pointers; each is served by
    #: `GET /workflow-runs/{id}/images/{name}`.
    images: list[RunImage] = []
    #: The run's grant (#2481); None for a run started without one.
    grant: GrantView | None = None


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
    if problems := await check_script(body.script, body.inputs_schema):
        raise _script_problems(problems, "The script does not type-check.")
    with _versioned():
        return await flows.definitions.register(
            body.name,
            body.script,
            _started_by(),
            approval_timeout_s=_seconds(body.approval_timeout),
            inputs_schema=body.inputs_schema,
        )


@router.get("/workflows", response_model=list[DefinitionSummary], summary="List flows")
async def list_flows(
    flows: FlowsDep,
    archived: Annotated[
        bool, Query(description="Archived and deleted flows too, each with when and by whom.")
    ] = False,
) -> list[DefinitionSummary]:
    """Each flow at its newest version, by name. Archived and deleted flows (#2485) only
    with `archived`."""
    return await flows.definitions.flows(hidden=archived)


@router.get("/workflows/{definition_id}", response_model=Definition, summary="Get a flow")
async def get_flow(definition_id: str, flows: FlowsDep) -> Definition:
    with _versioned():
        definition = await flows.definitions.get(definition_id)
    if definition is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "No such flow.")
    return definition


@contextmanager
def _versioned() -> Iterator[None]:
    """A flow's versions are git (#2503): its errors as the API answers them."""
    try:
        yield
    except UnknownFlowError:
        raise ApiError(status.HTTP_404_NOT_FOUND, "No such flow.") from None
    except RevisionNotFoundError:
        raise ApiError(status.HTTP_404_NOT_FOUND, "No such version of this flow.") from None
    except ScriptMissingError:
        raise ApiError(
            status.HTTP_409_CONFLICT, "This version's script is missing.", type_=SCRIPT_MISSING
        ) from None
    except FlowHistoryUnavailableError:
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Flow history is unavailable: the flows directory is not a git repository.",
            type_=FLOWS_UNAVAILABLE,
        ) from None
    except GitError as error:
        raise ApiError(status.HTTP_500_INTERNAL_SERVER_ERROR, str(error)) from None


NamePath = Annotated[str, Path(pattern=NAME_PATTERN)]
CommitPath = Annotated[str, Path(pattern=COMMIT_ID_PATTERN)]


@router.get(
    "/workflows/names/{name}/versions",
    response_model=list[FlowVersion],
    summary="A flow's history: its versions, newest first",
)
async def list_flow_versions(
    name: NamePath,
    flows: FlowsDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[FlowVersion]:
    """Every version of the flow, one commit each in `data/flows`, with who made it and
    when. Each carries its version id, which `GET /workflows/{id}` and a run's start
    take."""
    with _versioned():
        return await flows.definitions.versions(name, limit=limit)


@router.get(
    "/workflows/names/{name}/versions/{commit}/source",
    response_class=Response,
    responses={200: {"content": {"text/plain": {}}}},
    summary="A flow's script at one of its versions",
)
async def get_flow_version_source(name: NamePath, commit: CommitPath, flows: FlowsDep) -> Response:
    with _versioned():
        script = await flows.definitions.source(name, commit)
    return Response(content=script, media_type="text/plain; charset=utf-8")


@router.get(
    "/workflows/names/{name}/versions/{commit}/diff",
    response_model=FlowDiff,
    summary="Diff a flow's script between two versions (the one before by default)",
)
async def get_flow_version_diff(
    name: NamePath,
    commit: CommitPath,
    flows: FlowsDep,
    base: Annotated[str | None, Query(pattern=COMMIT_ID_PATTERN)] = None,
) -> FlowDiff:
    with _versioned():
        return await flows.definitions.diff(name, commit, base)


@router.post(
    "/workflows/names/{name}/versions/{commit}/restore",
    status_code=status.HTTP_201_CREATED,
    response_model=Definition,
    summary="Restore a flow's script at one of its versions, as a new version",
)
async def restore_flow_version(name: NamePath, commit: CommitPath, flows: FlowsDep) -> Definition:
    """A new commit with the script as it was at `commit`, and a new version row with
    that version's approval timeout: never a rewrite. Type-checked first, as a
    registration is."""
    with _versioned():
        script = await flows.definitions.source(name, commit)
        inputs_schema = await flows.definitions.inputs_schema(name, commit)
    if problems := await check_script(script, inputs_schema):
        raise _script_problems(problems, "That version no longer type-checks.")
    with _versioned():
        return await flows.definitions.restore(name, commit, _started_by())


@router.post(
    "/workflows/names/{name}/archive",
    response_model=FlowState,
    summary="Archive a flow (every version): hidden from the list, and never started",
)
async def archive_flow(name: NamePath, flows: FlowsDep) -> FlowState:
    """Reversible (`unarchive`). Runs already started keep running; finished runs keep
    their script. One archived already keeps who archived it and when."""
    with _versioned():
        return await flows.definitions.archive(name, _started_by())


@router.post(
    "/workflows/names/{name}/unarchive",
    response_model=FlowState,
    summary="Unarchive a flow: listed again, and its versions start again",
)
async def unarchive_flow(name: NamePath, flows: FlowsDep) -> FlowState:
    with _versioned():
        return await flows.definitions.unarchive(name)


@router.delete(
    "/workflows/names/{name}",
    response_model=FlowState,
    summary="Delete a flow: a commit removing its script; its history keeps every version",
)
async def delete_flow(name: NamePath, flows: FlowsDep) -> FlowState:
    """One commit in `data/flows` removing `<name>/` (#2485). Its versions stay in the
    history and their records stay: runs in progress keep running the version they
    started with, finished runs still show the script they ran, and nothing new starts.
    Restoring one of its versions, or registering the name again, brings it back as its
    next version. A flow deleted already is answered as it is."""
    with _versioned():
        return await flows.definitions.delete(name, _started_by())


def _refuse_hidden(definition: Definition) -> None:
    """An archived or deleted flow never starts (#2485)."""
    if definition.deleted_at is not None:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            "This flow was deleted. Restore one of its versions to run it.",
            type_=FLOW_DELETED,
        )
    if definition.archived_at is not None:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            "This flow is archived. Unarchive it to run it.",
            type_=FLOW_ARCHIVED,
        )


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
    with _versioned():
        definition = await flows.definitions.get(definition_id)
    if definition is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "No such flow.")
    run_id = str(uuid.uuid5(FLOW_NAMESPACE, f"{definition.id}:{definition.version}:{key}"))
    # A row answers every repeat, however its run ended (Ruling 3).
    if (existing := await flows.store.get_run(run_id)) is not None:
        response.status_code = status.HTTP_200_OK
        return RunAnswer(id=existing.id, status=existing.status, repeated=True)
    _refuse_hidden(definition)
    client = _client(flows)
    if problems := await check_script(definition.script, definition.inputs_schema):
        raise _script_problems(problems, "The flow no longer type-checks.")
    options = body or RunCreate()
    try:
        inputs = resolve_inputs(definition.inputs_schema, options.inputs)
    except InputsError as err:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "The run's inputs do not fit the flow's inputs schema: " + "; ".join(err.problems),
            type_=FLOW_INPUTS,
            problems=err.problems,
        ) from None
    override = _seconds(options.approval_timeout)
    timeout = next(
        value
        for value in (
            override,
            definition.approval_timeout_s,
            state.settings.flow_approval_timeout_seconds,
        )
        if value is not None
    )
    # As the agent will enforce them: Settings may have raised a tool to outward.
    tool_tiers = raise_tiers(tiers(), await flows.store.tool_overrides())
    try:
        async with asyncio.timeout(COMMAND_ANSWER_DEADLINE.total_seconds()):
            # Started already, its row not yet written: the same run, nothing new.
            if (execution := await _execution(client, run_id)) is not None:
                response.status_code = status.HTTP_200_OK
                closed = execution != WorkflowExecutionStatus.RUNNING
                return RunAnswer(
                    id=run_id, status="terminated" if closed else "starting", repeated=True
                )
            # Checked only once no repeat answered: a repeat answers its run, whatever
            # has become of the grant it was sent with since.
            if options.grant is not None:
                _check_grant(options.grant, tool_tiers)
            # No execution, so a grant held for this run is a start's that never started
            # it (it raised after the insert): never this request's, so it goes.
            await flows.store.delete_grant(run_id)
            if options.grant is not None:
                # Before the run starts, so its first outward call finds it. A person's
                # (the browser's) is approved; an agent's only proposes one (§3.2).
                await flows.store.insert_grant(
                    run_id,
                    options.grant,
                    status="approved" if current_author() is None else "requested",
                    requested_by=_started_by(),
                )
            await AgentClient(client, f"flow-{run_id}").start_and_submit_message(
                "execute",
                RunFlow(
                    script=definition.script,
                    inputs_schema=definition.inputs_schema,
                    inputs=inputs if definition.inputs_schema is not None else None,
                ).model_dump(exclude_none=True),
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
                    tools_queue=flows.settings.temporal_task_queue_agent_tools,
                    tool_tiers=tool_tiers,
                    search_attributes=flows.search_attributes,
                    commit=definition.commit or "",
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
            "ScadBuddy's background service did not answer. Try again shortly.",
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
            PendingEntry.model_validate(seen.model_dump())
            if seen is not None
            else PendingEntry(
                call_id=approval.tool_id,
                kind="approval",
                fn=HOST_FN.get(approval.tool_name, approval.tool_name),
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
        if (grant := await flows.store.get_grant(run_id)) is not None:
            view.grant = _grant_view(grant, [])
        return view
    view = RunView(
        id=run.id,
        status=run.status,
        definition_id=run.definition_id,
        version=run.version,
        name=run.name,
        run=run,
        images=await run_images(flows.connect, run.id) if flows.connect is not None else [],
    )
    if (grant := await flows.store.get_grant(run.id)) is not None:
        view.grant = _grant_view(grant, await flows.store.grant_uses(run.id))
    if run.status in TERMINAL:
        return view
    if flows.client is None:
        view.pending = [PendingEntry.model_validate(w.model_dump()) for w in run.waiting_on]
        return view
    view.pending, view.live = await _pending(flows.client, run)
    return view


#: An image's bytes never change under its name (the name is their sha256).
_IMAGE_HEADERS = {
    "Cache-Control": "private, max-age=31536000, immutable",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'",
}


@router.get(
    "/workflow-runs/{run_id}/images/{name}",
    response_class=Response,
    responses={
        200: {"content": {"image/png": {}, "image/jpeg": {}, "image/gif": {}, "image/webp": {}}}
    },
    summary="Get one of a flow run's images",
)
async def get_flow_run_image(run_id: str, name: str, flows: FlowsDep) -> Response:
    """An image one of the run's tool calls returned (#2477), by the name its pointer
    carries. Gone with the run once it is forgotten."""
    try:
        uuid.UUID(run_id)
    except ValueError:
        raise ApiError(status.HTTP_404_NOT_FOUND, "No such image.") from None
    if not BLOB_NAME.match(name) or flows.connect is None or flows.keys is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "No such image.")
    try:
        found = await open_image(flows.connect, flows.keys, str(uuid.UUID(run_id)), name)
    except SubjectForgottenError:
        found = None
    if found is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "No such image.")
    media_type, data = found
    return Response(content=data, media_type=media_type, headers=_IMAGE_HEADERS)


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
    #: Approve the starting session's narrowing of the call as it proposed it (#2482),
    #: rather than the call as it was parked.
    proposal: bool = False
    #: With `proposal`: the `proposal_hash` of the proposal the person was shown
    #: (`GET .../proposals/{call_id}`). Any other, or none, is refused: the person
    #: approves what they saw, never a proposal replaced since.
    proposal_hash: str | None = Field(default=None, max_length=128)


class ProposalView(BaseModel):
    """A parent's proposal beside the call it narrows, for the person to compare
    (#2482). Only the person reads it; approving it names `proposal_hash`."""

    call_id: str
    fn: str
    tool: str | None = None
    #: The call's arguments as it is parked, and as the starting session narrowed them.
    original: dict[str, Any]
    proposed: dict[str, Any]
    proposal_hash: str
    proposed_by: str


class EscalateBody(BaseModel):
    """The starting session's action on a call of its run outside the run's grant
    (#2482, spec 2026-10-11 §4.2). Never an approval: only a person or the grant
    approves."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["deny", "narrow", "pass_up"]
    #: For `narrow`: the call's input for the same tool (a print's or arrange's
    #: `request`, an agent tool's arguments). It runs only if the grant admits it;
    #: otherwise it goes to the person as a proposal.
    input: dict[str, Any] | None = None
    #: Why, for a denial: the script sees it.
    reason: str | None = Field(default=None, max_length=1024)


#: A narrowed input's largest size, as any pending-input response (10-01 §6.6).
NARROWED_MAX_BYTES = 16 * 1024


class Escalated(BaseModel):
    """What the starting session's action did."""

    run_id: str
    call_id: str
    outcome: Literal["denied", "approved", "passed_up", "with_person"]


class Decided(BaseModel):
    """What a decision did."""

    run_id: str
    call_id: str
    outcome: Literal["approved", "denied", "answered"]


def _browser_only(
    detail: str = "Only a person answers a flow's questions and approvals.",
    type_: str = ENTRY_BROWSER_ONLY,
) -> None:
    # Spec §6.6, plan 6 Ruling 6: a flow's entries are answered on the Workflows page,
    # never by an agent, whatever grant it holds.
    if current_author() is not None:
        raise ApiError(status.HTTP_403_FORBIDDEN, detail, type_=type_)


def _grant_browser_only() -> None:
    # Spec 2026-10-11 §3.2: an agent proposes a grant; only a person approves or declines it.
    _browser_only("Only a person approves or declines a run's grant.", GRANT_BROWSER_ONLY)


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
    flows: FlowsDep,
    idempotency_key: IdempotencyKey = None,
) -> Decided | JSONResponse:
    _browser_only()
    if flows.keys is None:
        raise _unavailable()
    try:
        # Sealed under the run's key: the text never reaches the operation's history or
        # its record in the clear (#2221).
        sealed = await seal_answer(flows.keys, run_id, body.answer)
    except SubjectForgottenError:
        raise ApiError(status.HTTP_404_NOT_FOUND, "No such flow run.") from None
    except psycopg.errors.UndefinedTable:
        raise _unavailable() from None
    result = await run_operation(
        ops,
        response,
        kind=ops.kinds[FLOW_ANSWER],
        subject=f"flow:{run_id}",
        request={"run_id": run_id, "call_id": body.call_id, "answer_sealed": sealed},
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


@router.get(
    "/workflow-runs/{run_id}/proposals/{call_id}",
    response_model=ProposalView,
    summary="Compare a parent's proposal with the call it narrows",
)
async def get_flow_run_proposal(
    run_id: str, call_id: Annotated[str, Path(min_length=1, max_length=64)], flows: FlowsDep
) -> ProposalView:
    """Browser only: the proposal is the person's to judge, so no agent reads it. The
    original is read from the run's parked call, never from anything stored with the
    proposal."""
    _browser_only("Only a person reads a proposal it may approve.")
    if flows.client is None:
        raise _unavailable()
    run = await flows.store.get_run(run_id)
    if run is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "No such proposal.")
    held = await flows.store.get_proposal(f"flow:{run.id}:{run.workflow_run_id}:{call_id}")
    if held is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "No such proposal.")
    for parked in await AgentClient(flows.client, run.workflow_id).get_pending_approvals():
        if parked.tool_id == call_id and (call := parked_call(parked.tool_name, parked.tool_input)):
            fn, tool, original = call
            return ProposalView(
                call_id=call_id,
                fn=fn,
                tool=tool,
                original=original,
                proposed=held.args,
                proposal_hash=held.input_hash,
                proposed_by=held.proposed_by,
            )
    raise ApiError(status.HTTP_404_NOT_FOUND, "That call is no longer waiting.")


@router.post(
    "/workflow-runs/{run_id}/escalations/{call_id}",
    response_model=Escalated,
    responses=OPERATION_RESPONSES,
    summary="Deny, narrow or pass up a run's call as its starting session",
)
async def escalate_flow_run(
    run_id: str,
    call_id: Annotated[str, Path(min_length=1, max_length=64)],
    body: EscalateBody,
    response: Response,
    ops: OperationsDep,
    idempotency_key: IdempotencyKey = None,
) -> Escalated | JSONResponse:
    """The run's starting session, while the call waits at hop `parent` (spec 2026-10-11
    §4.4). The session is the request's author, which the agent sets from the session
    the call runs in; the run's own record (`started_by`) must name it, and the call is
    read from the run's parked entry. A forged author can at most deny, narrow into the
    grant or pass up (§4.2). The browser decides through `/decide`."""
    author = current_author()
    if author is None or not author.session or not author.principal:
        raise ApiError(
            status.HTTP_403_FORBIDDEN,
            "Only the session that started the run acts on its calls before the person does.",
            type_=NOT_PARENT,
        )
    if body.action == "narrow":
        if body.input is None:
            raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, "A narrowing names its input.")
        if len(json.dumps(body.input, separators=(",", ":")).encode()) > NARROWED_MAX_BYTES:
            raise ApiError(status.HTTP_413_CONTENT_TOO_LARGE, "A narrowed input is at most 16 KiB.")
    result = await run_operation(
        ops,
        response,
        kind=ops.kinds[FLOW_ESCALATE],
        subject=f"flow:{run_id}",
        request={
            "run_id": run_id,
            "call_id": call_id,
            **body.model_dump(),
            "session": author.session,
            "principal": author.principal,
        },
        idempotency_key=idempotency_key,
    )
    return operation_answer(result, Escalated)


def _check_grant(spec: GrantSpec, tool_tiers: dict[str, str]) -> None:
    if problems := validate_spec(spec, tool_tiers=tool_tiers, now=datetime.now(UTC)):
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "The grant is not valid.",
            type_=GRANT_INVALID,
            problems=[{"message": p} for p in problems],
        )


def _no_grant() -> ApiError:
    return ApiError(status.HTTP_404_NOT_FOUND, "This run has no grant.")


def _grant_state(err: GrantStateError) -> ApiError:
    grant = err.grant
    state = "revoked" if grant.revoked_at is not None else grant.status
    return ApiError(
        status.HTTP_409_CONFLICT,
        f"The run's grant is {state}: a grant is decided once, and revoking it is final.",
        type_=GRANT_STATE,
    )


async def _grant_answer(flows: Flows, grant: Grant | None) -> GrantView:
    if grant is None:
        raise _no_grant()
    return _grant_view(grant, await flows.store.grant_uses(grant.run_id))


class ApproveGrant(BaseModel):
    """A person's approval of a run's requested grant."""

    #: The grant as approved, when the person edited the request (any bound, either
    #: way: the request was only a proposal). Omitted: as requested.
    spec: GrantSpec | None = None
    #: Whether calls outside it go to the starter session first (#2482): overrides the
    #: spec's `ask_starter_session`.
    ask_starter_session: bool | None = None


@router.post(
    "/workflow-runs/{run_id}/grant/approve",
    response_model=GrantView,
    summary="Approve a run's requested grant",
)
async def approve_flow_grant(
    run_id: str, flows: FlowsDep, body: Annotated[ApproveGrant | None, Body()] = None
) -> GrantView:
    """Browser only (spec 2026-10-11 §3.2): no agent approves a grant, its own or
    another's. From now until it expires or is revoked, the run's outward calls inside it
    run without asking."""
    _grant_browser_only()
    grant = await flows.store.get_grant(run_id)
    if grant is None:
        raise _no_grant()
    options = body or ApproveGrant()
    spec = options.spec or grant.spec
    if options.ask_starter_session is not None:
        spec = spec.model_copy(update={"ask_starter_session": options.ask_starter_session})
    # Only a grant still requested is checked: any other answers 409 from the store, which
    # says what is wrong with it better than its spec's problems would.
    if grant.status == "requested" and grant.revoked_at is None:
        _check_grant(spec, raise_tiers(tiers(), await flows.store.tool_overrides()))
    try:
        approved = await flows.store.approve_grant(run_id, spec, approved_by="browser")
    except GrantStateError as err:
        raise _grant_state(err) from None
    view = await _grant_answer(flows, approved)
    view.released = await _release(flows, run_id)
    return view


async def _release(flows: Flows, run_id: str) -> list[str]:
    """The run's parked calls the newly approved grant admits, let through (the user's
    ruling: an agent-started run's outward calls wait until its grant, or each call, is
    approved). One that cannot be let through stays for a person to decide."""
    run = await flows.store.get_run(run_id)
    if flows.client is None or run is None or run.status in TERMINAL:
        return []
    api = api_client(flows.settings.api_internal_url)
    try:
        grants = FlowGrants(flows.store, api, approval_key(flows.settings))
        return await grants.release(flows.client, run)
    except (RPCError, psycopg.Error):
        logger.warning("could not release a run's parked calls into its grant", exc_info=True)
        return []
    finally:
        if api is not None:
            await api.aclose()


@router.post(
    "/workflow-runs/{run_id}/grant/decline",
    response_model=GrantView,
    summary="Decline a run's requested grant",
)
async def decline_flow_grant(run_id: str, flows: FlowsDep) -> GrantView:
    """Browser only. Final: the run's outward calls ask for the rest of its life."""
    _grant_browser_only()
    try:
        declined = await flows.store.decline_grant(run_id)
    except GrantStateError as err:
        raise _grant_state(err) from None
    return await _grant_answer(flows, declined)


@router.post(
    "/workflow-runs/{run_id}/grant/revoke",
    response_model=GrantView,
    summary="Revoke a run's grant",
)
async def revoke_flow_grant(run_id: str, flows: FlowsDep) -> GrantView:
    """The browser, or the agent principal that started the run: revoking only takes
    authority away (spec 2026-10-11 §3.5). Final, and the run goes on: its next outward
    call waits for a decision."""
    grant = await flows.store.get_grant(run_id)
    if grant is None:
        raise _no_grant()
    author = current_author()
    if author is None:
        revoked_by = "browser"
    else:
        starter = grant.requested_by
        principal = starter.get("principal")
        # Named on both sides: no principal never matches another missing one.
        if (
            starter.get("kind") != "agent"
            or not isinstance(principal, str)
            or not principal
            or not author.principal
            or principal != author.principal
        ):
            raise ApiError(
                status.HTTP_403_FORBIDDEN,
                "Only a person or the run's starter revokes its grant.",
                type_=GRANT_STARTER_ONLY,
            )
        revoked_by = f"agent {author.principal}"
    try:
        revoked = await flows.store.revoke_grant(run_id, revoked_by=revoked_by)
    except GrantStateError as err:
        raise _grant_state(err) from None
    return await _grant_answer(flows, revoked)


class ResetBody(BaseModel):
    """Where to reset a run to, and the preview the caller saw."""

    #: A completed workflow task of the run's history, after its script started.
    event_id: int = Field(ge=1)
    #: The preview's `as_of_event_id`: a Reset is refused (409, with the new preview)
    #: when outward calls were added after it.
    as_of_event_id: int = Field(ge=0)
    #: The preview's `workflow_run_id`: a Reset is refused (409, with the new preview)
    #: when another Reset replaced that execution since.
    workflow_run_id: str = Field(min_length=1, max_length=64)


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
    return await outward_since(flows.client, run, event_id)


@router.get(
    "/workflow-runs/{run_id}/reset-points",
    response_model=ResetPoints,
    summary="The points a Reset of a run takes",
)
async def flow_reset_points(run_id: str, flows: FlowsDep) -> ResetPoints:
    """Every completed workflow task of the run's current execution a Reset takes, each
    with the first step it runs again (#2350): what the run page offers, so nobody
    probes `reset-preview` id by id. The preview of the chosen point says what it sends."""
    run = await flows.store.get_run(run_id)
    if run is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "No such flow run.")
    if flows.client is None:
        raise _unavailable()
    return await reset_points(flows.client, run, floor=await flows.store.reset_floor(run.id))


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
    `as_of_event_id`, or (with no preview) when another Reset moved the run first; 422
    `flow-reset-point` for an event that is not a completed workflow task after the
    script started, and 422 `flow-reset-unrecorded` for a run with a call parked
    before ScadBuddy recorded where in the history."""
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
