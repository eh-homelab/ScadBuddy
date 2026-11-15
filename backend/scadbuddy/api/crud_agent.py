"""The agent service's CRUD routes (#5330, job cost spec 2026-11-05 §4.4): under
``/api/v1/internal/crud/agent/``, for the ``agent`` credential only (`crud_router`).

``POST …/orders/{order_id}/assistant-cost``: the assistant's spend in one session on an
order, in USD, as the agent's own ``ai_sessions.cost_usd`` knows it. The backend never
reads the agent's tables (#5295); the agent sends the figure. The lifecycle converts it
at ``pricing_usd_rate`` and writes one ``assistant`` cost entry, source
``ai_session:<id>``, so a second report of the session writes nothing (a natural write:
no ``Idempotency-Key``).

The holds of the assistant's workspace files (#5299, in place of the agent's trigger on
``blob_refs``): each file whose sealed bytes are a blob of the content store
(`api/internal_workspace.py`) is held ``(ai_session_file, <workspace>/<path>)`` while
its ``ai_session_files`` row names it, so the blob sweep keeps it. The agent holds before
it writes the row and releases once the row is gone (``agent/src/workspace/store.ts``):

- ``POST …/workspace-blob-refs`` holds a key for a holder (a hold sent again refreshes its
  ``created_at``);
- ``POST …/workspace-blob-refs/release`` drops one and says whether it did (one already
  gone is a success), with ``older_than_seconds`` only a hold older than that, so a hold
  taken again since is kept;
- ``GET …/workspace-blob-refs`` lists the holds older than ``older_than_seconds`` (every
  one by default), a page at a time, for the agent's reconcile.

All natural writes. The holder kind is always ``ai_session_file`` and the key a
workspace blob's, so the agent's credential holds or releases nothing else.

The agent's reads of the backend's tables (#5299 D7), each the one question it asked over
SQL before, read only:

- area ``events``: ``GET …/events/head``, the event log's last ``seq`` and the database
  clock, and ``GET …/events``, the logged events after a ``seq``, newest first, for the
  agent's LISTEN gap replay (``agent/src/events/pgListener.ts``);
- area ``flows`` (`flows/agent_reads.py`): ``GET …/flow-runs/{id}/starter`` (who started a
  run, under the row's share lock), ``GET …/flow-decisions`` (whether a call's approval
  holds), ``GET …/flow-grants/in-force``, ``GET …/flow-runs/{id}/queue``,
  ``GET …/flow-runs/{id}/steps``, ``GET …/flow-runs/waiting`` (the open runs' parked
  calls), ``GET …/flow-runs/parent-hop-approvals`` (the calls waiting at a run's starter
  session) and ``POST …/flow-runs/parent-hop/waiting`` (which of those calls still wait
  there; a POST for its list of ids).

A request id or a grant id is a query parameter, never a path segment: it is free text.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import Path, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from scadbuddy.api.deps import SettingsStoreDep, StateDep
from scadbuddy.api.internal_workspace import WORKSPACE_BLOB_KEY_PATTERN
from scadbuddy.api.orders import _run, check_path
from scadbuddy.core.problems import ApiError
from scadbuddy.crud.guard import crud_router
from scadbuddy.flows import agent_reads
from scadbuddy.orders.component import OrdersDep
from scadbuddy.orders.lifecycle import UsdRate
from scadbuddy.orders.models import Actor, CostEntry

router = crud_router("agent")
logger = logging.getLogger(__name__)

#: The ``blob_refs.holder_kind`` of a workspace file (`api/internal_workspace.py`).
WORKSPACE_HOLDER = "ai_session_file"
#: A holder: ``session:<id>/<path>`` or ``principal:<kind>:<id>/<path>``.
WORKSPACE_HOLDER_PATTERN = r"^(session|principal):[^/]+(/[^/]+)*/[A-Za-z0-9_][A-Za-z0-9_.-]*$"
#: The most holds one page lists.
HOLDS_PAGE_MAX = 1000
#: The largest ``older_than_seconds`` (a year): far past any grace, and well inside
#: ``timedelta``'s range, so a larger value is a 422, never an ``OverflowError``.
OLDER_THAN_MAX_SECONDS = 31_536_000


class AssistantCost(BaseModel):
    """One session's spend on an order."""

    model_config = ConfigDict(extra="forbid")

    session_id: UUID
    #: The session's owner, for the entry's ``created_by``.
    principal: str | None = Field(default=None, max_length=300)
    order_item_id: str | None = Field(default=None, max_length=100)
    cost_usd: Annotated[Decimal, Field(ge=0, le=Decimal(10**6), decimal_places=8)]


class AssistantCostAnswer(BaseModel):
    #: The session's entry; ``None`` for a spend under one minor unit, which writes nothing.
    entry: CostEntry | None
    #: Whether this call wrote it.
    written: bool


@router.post(
    "/orders/{order_id}/assistant-cost",
    status_code=status.HTTP_201_CREATED,
    name="crud_agent_assistant_cost",
)
async def assistant_cost(
    order_id: str,
    body: AssistantCost,
    orders: OrdersDep,
    settings: SettingsStoreDep,
    response: Response,
) -> AssistantCostAnswer:
    check_path(order_id)
    runtime = settings.snapshot().runtime
    rate = UsdRate(Decimal(runtime.pricing_usd_rate), runtime.orders_currency)
    session = str(body.session_id)
    by = Actor(kind="agent", principal=body.principal, session=session)
    entry, written = await _run(
        lambda: orders.lifecycle.add_assistant_cost(
            order_id,
            session_id=session,
            order_item_id=body.order_item_id,
            cost_usd=body.cost_usd,
            usd_rate=rate,
            by=by,
        )
    )
    if not written:
        response.status_code = status.HTTP_200_OK
    return AssistantCostAnswer(entry=entry, written=written)


class WorkspaceHold(BaseModel):
    """One workspace file's hold on its blob."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(pattern=WORKSPACE_BLOB_KEY_PATTERN)
    holder_id: str = Field(min_length=3, max_length=1024, pattern=WORKSPACE_HOLDER_PATTERN)


class WorkspaceRelease(WorkspaceHold):
    #: Release only a hold taken longer ago than this; a hold taken again since is kept.
    older_than_seconds: int | None = Field(default=None, ge=0, le=OLDER_THAN_MAX_SECONDS)


class WorkspaceReleased(BaseModel):
    #: Whether a hold was dropped: false for one already gone, or taken again since.
    released: bool


class WorkspaceHolds(BaseModel):
    holds: list[WorkspaceHold]
    #: The last hold of a full page, to list from with ``after_holder_id`` and
    #: ``after_key``; ``None`` on the last page.
    next: WorkspaceHold | None


@router.post(
    "/workspace-blob-refs",
    status_code=status.HTTP_204_NO_CONTENT,
    name="crud_agent_hold_workspace_blob",
)
async def hold_workspace_blob(body: WorkspaceHold, state: StateDep) -> None:
    await asyncio.to_thread(state.refs.hold, body.key, WORKSPACE_HOLDER, body.holder_id)


@router.post("/workspace-blob-refs/release", name="crud_agent_release_workspace_blob")
async def release_workspace_blob(body: WorkspaceRelease, state: StateDep) -> WorkspaceReleased:
    older_than = (
        None if body.older_than_seconds is None else timedelta(seconds=body.older_than_seconds)
    )
    released = await asyncio.to_thread(
        lambda: state.refs.release(
            body.key, WORKSPACE_HOLDER, body.holder_id, older_than=older_than
        )
    )
    return WorkspaceReleased(released=released)


@router.get("/workspace-blob-refs", name="crud_agent_list_workspace_blob_refs")
async def list_workspace_blob_refs(
    state: StateDep,
    older_than_seconds: Annotated[int, Query(ge=0, le=OLDER_THAN_MAX_SECONDS)] = 0,
    after_holder_id: Annotated[str | None, Query(max_length=1024)] = None,
    after_key: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=HOLDS_PAGE_MAX)] = 500,
) -> WorkspaceHolds:
    after = (after_holder_id, after_key or "") if after_holder_id is not None else None
    rows = await asyncio.to_thread(
        lambda: state.refs.held_by_kind(
            WORKSPACE_HOLDER,
            older_than=timedelta(seconds=older_than_seconds),
            after=after,
            limit=limit,
        )
    )
    holds = [WorkspaceHold.model_construct(key=key, holder_id=holder) for holder, key in rows]
    return WorkspaceHolds(holds=holds, next=holds[-1] if len(holds) == limit else None)


# -- the agent's reads of the backend's tables (#5299 D7) ---------------------------------

#: The most events one gap read returns: the agent asks for its replay limit plus one.
EVENTS_PAGE_MAX = 5001
#: Postgres's largest ``bigint``, the event log's ``seq``.
SEQ_MAX = 2**63 - 1
#: A run id (a uuid4 today), an execution id, a grant id.
ID_MAX = 200
#: A request id (``flow:<run>:<execution>:<call>``).
REQUEST_ID_MAX = 1024
#: An input hash (the approvals' HMAC, hex).
INPUT_HASH_MAX = 256
#: The most request ids one ``parent-hop/waiting`` asks about.
STILL_WAITING_MAX = 500
#: The most runs one page of ``parent-hop-approvals`` lists.
PARENT_HOP_PAGE_MAX = 200

RunId = Annotated[str, Path(min_length=1, max_length=ID_MAX)]


class _Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EventLogHead(_Answer):
    #: The last ``seq`` of the log, 0 when it is empty; a string, as a ``bigint`` is.
    seq: str
    #: The database clock when it was read.
    at: datetime


class LoggedEvent(_Answer):
    seq: str
    event_id: str
    payload: dict[str, Any]
    logged_at: datetime


class EventLogGap(_Answer):
    #: Newest first.
    events: list[LoggedEvent]
    #: The database clock when they were read.
    read_at: datetime


def _event_log_head(pool: agent_reads.Pool) -> EventLogHead:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT coalesce(max(seq), 0) AS seq, now() AS at FROM events"
        ).fetchone()
    assert row is not None
    return EventLogHead(seq=str(row["seq"]), at=row["at"])


def _event_log_gap(pool: agent_reads.Pool, after: int, limit: int) -> EventLogGap:
    # One transaction: `now()` is its start, before the rows are read, as the agent's SQL
    # read of the gap had it (`pgListener.ts` GUARD).
    with pool.connection() as conn, conn.transaction():
        rows = conn.execute(
            "SELECT seq, event_id, payload, logged_at FROM events"
            " WHERE seq > %s ORDER BY seq DESC LIMIT %s",
            (after, limit),
        ).fetchall()
        clock = conn.execute("SELECT now() AS at").fetchone()
    assert clock is not None
    events = [
        LoggedEvent(
            seq=str(r["seq"]),
            event_id=r["event_id"],
            payload=r["payload"],
            logged_at=r["logged_at"],
        )
        for r in rows
    ]
    return EventLogGap(events=events, read_at=clock["at"])


@router.get("/events/head", name="crud_agent_event_log_head")
async def event_log_head(state: StateDep) -> EventLogHead:
    return await asyncio.to_thread(_event_log_head, state.settings_store.pool)


@router.get("/events", name="crud_agent_event_log_gap")
async def event_log_gap(
    state: StateDep,
    after: Annotated[int, Query(ge=0, le=SEQ_MAX)],
    limit: Annotated[int, Query(ge=1, le=EVENTS_PAGE_MAX)] = 1001,
) -> EventLogGap:
    return await asyncio.to_thread(_event_log_gap, state.settings_store.pool, after, limit)


class StartedBy(_Answer):
    """``workflow_runs.started_by`` (`core/authorship.py`), each field only as a string."""

    kind: str | None
    principal: str | None
    session: str | None


class RunStarter(_Answer):
    #: Running or waiting, and the execution is not one a Reset replaced.
    open: bool
    #: The execution is neither the row's nor one a Reset replaced.
    unrecorded: bool
    started_by: StartedBy


class RunStarterAnswer(_Answer):
    #: None for no such run.
    run: RunStarter | None


@router.get("/flow-runs/{run_id}/starter", name="crud_agent_flow_run_starter")
async def flow_run_starter(
    run_id: RunId,
    state: StateDep,
    execution: Annotated[str, Query(max_length=ID_MAX)],
) -> RunStarterAnswer:
    found = await asyncio.to_thread(
        agent_reads.run_starter, state.settings_store.pool, run_id, execution
    )
    return RunStarterAnswer(run=None if found is None else RunStarter.model_validate(found))


class DecisionChecks(_Answer):
    approved: bool
    approved_unresolved: bool
    by_person: bool
    lapsed: bool


@router.get("/flow-decisions", name="crud_agent_flow_decision_checks")
async def flow_decision_checks(
    state: StateDep,
    request_id: Annotated[str, Query(min_length=1, max_length=REQUEST_ID_MAX)],
    input_hash: Annotated[str, Query(min_length=1, max_length=INPUT_HASH_MAX)],
) -> DecisionChecks:
    checks = await asyncio.to_thread(
        agent_reads.decision_checks, state.settings_store.pool, request_id, input_hash
    )
    return DecisionChecks.model_validate(checks)


class GrantInForce(_Answer):
    in_force: bool


@router.get("/flow-grants/in-force", name="crud_agent_flow_grant_in_force")
async def flow_grant_in_force(
    state: StateDep, grant_id: Annotated[str, Query(max_length=ID_MAX)]
) -> GrantInForce:
    held = await asyncio.to_thread(agent_reads.grant_in_force, state.settings_store.pool, grant_id)
    return GrantInForce(in_force=held)


class HopOut(_Answer):
    """One step of a parked call's way up (`workflows/flow_models.py` `HopRecord`)."""

    hop: str | None
    action: str | None
    actor: str | None
    at: str | None
    reason: str | None


class WaitingCall(_Answer):
    """A run's parked call (`FlowWaiting`), its times as the projection wrote them."""

    call_id: str
    kind: Literal["approval", "answer"]
    fn: str
    prompt: str | None
    since: str | None
    hop: Literal["parent", "person"] | None
    hop_until: str | None
    reason: str | None
    proposed: bool
    hops: list[HopOut]


class RunStep(_Answer):
    """A run's step (`FlowStep`), the fields the agent reads."""

    seq: int
    fn: str
    call_id: str
    status: str
    session_id: str | None


def _kept(model: type[BaseModel], entries: list[dict[str, Any]], run_id: str) -> list[Any]:
    """The entries ``model`` takes; a malformed one is logged and left out, so one bad entry
    of a run's projection never fails the whole answer (the agent's SQL read passed it on)."""
    kept: list[Any] = []
    for entry in entries:
        try:
            kept.append(model.model_validate(entry))
        except ValidationError:
            logger.warning("left out a malformed %s of flow run %s", model.__name__, run_id)
    return kept


def _run_entries(row: dict[str, Any], calls: str = "waiting_on") -> dict[str, Any]:
    """``row`` with its parked calls (under ``calls``) and steps each checked by `_kept`."""
    return {
        **row,
        calls: _kept(WaitingCall, row[calls], row["id"]),
        "steps": _kept(RunStep, row["steps"], row["id"]),
    }


class RequestedGrant(_Answer):
    id: str
    created_at: datetime


class QueueRun(_Answer):
    id: str
    name: str
    status: str
    workflow_run_id: str
    #: The session that started the run, when one did.
    starter_session: str | None
    waiting_on: list[WaitingCall]
    steps: list[RunStep]
    #: Its grant while it is requested.
    requested_grant: RequestedGrant | None


class QueueRunAnswer(_Answer):
    #: None for no such run.
    run: QueueRun | None


@router.get("/flow-runs/{run_id}/queue", name="crud_agent_flow_run_queue")
async def flow_run_queue(run_id: RunId, state: StateDep) -> QueueRunAnswer:
    found = await asyncio.to_thread(agent_reads.run_queue, state.settings_store.pool, run_id)
    return QueueRunAnswer(
        run=None if found is None else QueueRun.model_validate(_run_entries(found))
    )


class RunSteps(_Answer):
    #: Empty for no such run.
    steps: list[RunStep]


@router.get("/flow-runs/{run_id}/steps", name="crud_agent_flow_run_steps")
async def flow_run_steps(run_id: RunId, state: StateDep) -> RunSteps:
    steps = await asyncio.to_thread(agent_reads.run_steps, state.settings_store.pool, run_id)
    return RunSteps(steps=_kept(RunStep, steps, run_id))


class WaitingRun(_Answer):
    id: str
    name: str
    status: str
    workflow_run_id: str
    starter_session: str | None
    waiting_on: list[WaitingCall]
    steps: list[RunStep]
    updated_at: datetime


class WaitingRuns(_Answer):
    #: The most recently moved first, `agent_reads.WAITING_RUNS_MAX` at most.
    runs: list[WaitingRun]


@router.get("/flow-runs/waiting", name="crud_agent_flow_runs_waiting")
async def flow_runs_waiting(state: StateDep) -> WaitingRuns:
    runs = await asyncio.to_thread(agent_reads.waiting_runs, state.settings_store.pool)
    return WaitingRuns.model_validate({"runs": [_run_entries(run) for run in runs]})


class ParentHopRun(_Answer):
    id: str
    workflow_run_id: str
    #: ``started_by``'s session and principal, each only as a string.
    session: str | None
    principal: str | None
    #: Its parked approvals at hop ``parent`` whose hop is live.
    calls: list[WaitingCall]
    steps: list[RunStep]


class ParentHopCursor(_Answer):
    after_updated_at: datetime
    after_id: str


class ParentHopRuns(_Answer):
    runs: list[ParentHopRun]
    #: Where the next page starts, for a full page; None on the last.
    next: ParentHopCursor | None


@router.get("/flow-runs/parent-hop-approvals", name="crud_agent_flow_parent_hop_approvals")
async def flow_parent_hop_approvals(
    state: StateDep,
    after_updated_at: Annotated[datetime | None, Query()] = None,
    after_id: Annotated[str | None, Query(max_length=ID_MAX)] = None,
    limit: Annotated[int, Query(ge=1, le=PARENT_HOP_PAGE_MAX)] = PARENT_HOP_PAGE_MAX,
) -> ParentHopRuns:
    if (after_updated_at is None) != (after_id is None):
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "after_updated_at and after_id come together"
        )
    after = None if after_updated_at is None or after_id is None else (after_updated_at, after_id)
    rows = await asyncio.to_thread(
        agent_reads.parent_hop_approvals, state.settings_store.pool, after, limit
    )
    runs = [
        ParentHopRun.model_validate(
            _run_entries({k: v for k, v in row.items() if k != "updated_at"}, "calls")
        )
        for row in rows
    ]
    last = rows[-1] if len(rows) == limit else None
    cursor = (
        None
        if last is None
        else ParentHopCursor(after_updated_at=last["updated_at"], after_id=last["id"])
    )
    return ParentHopRuns(runs=runs, next=cursor)


class StillWaitingAsk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_ids: list[Annotated[str, Field(max_length=REQUEST_ID_MAX)]] = Field(
        max_length=STILL_WAITING_MAX
    )


class StillWaiting(_Answer):
    #: The ids asked about that still wait at hop ``parent``, in the order asked.
    waiting: list[str]


@router.post("/flow-runs/parent-hop/waiting", name="crud_agent_flow_parent_hop_waiting")
async def flow_parent_hop_waiting(body: StillWaitingAsk, state: StateDep) -> StillWaiting:
    waiting = await asyncio.to_thread(
        agent_reads.still_at_parent_hop, state.settings_store.pool, body.request_ids
    )
    return StillWaiting(waiting=waiting)
