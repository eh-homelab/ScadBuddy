"""Copies between Bambuddy's library and ScadBuddy's own (#5355): the start, its
progress and per-file results, the recent jobs, and Bambuddy's library as a source to
browse while ScadBuddy's own serves ``/print/library``. Beside ``/print/library``, not
under it: its ``/{file_id}`` routes would match these paths too, and two modules must
never match one request (``tests/api/test_routes.py``)."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from temporalio.client import WorkflowExecutionDescription, WorkflowExecutionStatus
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.service import RPCError

from scadbuddy.api.deps import SettingsStoreDep, StateDep
from scadbuddy.bambuddy.client import BambuddyConfig, client_for
from scadbuddy.bambuddy.library_listing import LibraryListing, list_library
from scadbuddy.operations.component import OperationsDep
from scadbuddy.print_library.component import PrintLibraryDep
from scadbuddy.print_library.copy import NOT_NATIVE, check_request, check_target_folder
from scadbuddy.print_library.copy_models import (
    LibraryCopyInput,
    LibraryCopyRequest,
    LibraryCopyStarted,
    LibraryCopyStatus,
    LibraryCopySummary,
    copy_id,
)
from scadbuddy.print_library.copy_store import LibraryCopyStore
from scadbuddy.workflows.commands import execution_gone
from scadbuddy.workflows.library_copy import LibraryCopy

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/print", tags=["print"])


@router.post(
    "/library-copies",
    response_model=LibraryCopyStarted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Copy files between Bambuddy's library and ScadBuddy's own",
    description=(
        "Start a server-side copy of `file_ids` and the subtrees of `folder_ids` from one"
        " library into the other (`direction`), under `target_folder_id` (the target's top"
        " level without it). A file whose SHA-256 the target already holds is skipped;"
        " each selected folder's path is kept and made at the target; nothing at the"
        " target is deleted or overwritten. The id names the request: a second start of"
        " the same request joins the running copy, and one after it ended runs it again."
        " Follow it with `GET /print/library-copies/{copy_id}`. 409 unless"
        " `library_backend` is `native` and Bambuddy is configured; 422 for an id on the"
        " wrong side."
    ),
    responses={
        status.HTTP_409_CONFLICT: {"description": "No native library, or no Bambuddy."},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "An id on the wrong side."},
        status.HTTP_404_NOT_FOUND: {"description": "`target_folder_id` is not in the target."},
    },
)
async def post_library_copy(
    request: LibraryCopyRequest,
    state: StateDep,
    ops: OperationsDep,
    library: PrintLibraryDep,
    store: SettingsStoreDep,
) -> LibraryCopyStarted:
    native = await library.serving_native()
    if native is None:
        raise HTTPException(status.HTTP_409_CONFLICT, NOT_NATIVE)
    settings = await asyncio.to_thread(store.load)
    BambuddyConfig.from_settings(settings)  # the 409 when unset
    check_request(request)
    if request.direction == "bb_to_sb":
        await check_target_folder(request, native)
    else:
        async with client_for(settings) as client:
            await check_target_folder(request, client)
    job_id = copy_id(request)
    rows = LibraryCopyStore(state.settings_store.pool)
    # The row before the start, so a status read right after the 202 finds it. A start
    # that joins a running copy rewrites nothing but the request it already holds.
    handle = ops.client.get_workflow_handle(job_id)
    running = False
    try:
        described = await handle.describe()
        running = described.status == WorkflowExecutionStatus.RUNNING
    except RPCError as error:
        if not execution_gone(error):
            raise
    if running:
        # A row with no run id (written before #5424, or a start whose `started` write
        # never happened) is given the running chain's: `_settled` then matches it.
        await asyncio.to_thread(rows.started, job_id, _first_run(described), only_unset=True)
    if not running:
        await asyncio.to_thread(rows.start, job_id, request)
    # No update to answer, so not `start_command`: a plain start that joins a running one.
    try:
        started = await ops.client.start_workflow(
            LibraryCopy.run,
            LibraryCopyInput(copy_id=job_id, request=request),
            id=job_id,
            task_queue=state.settings.temporal_task_queue_library,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        )
    except Exception as error:
        # The row would otherwise say `running` with no execution to ever end it. A
        # row write that fails too is logged, so the start's own error is the one seen.
        if not running:
            try:
                await asyncio.to_thread(rows.finish, job_id, "failed", f"could not start: {error}")
            except Exception:
                logger.warning("could not end the row of a copy that failed to start: %s", job_id)
        raise
    if not running and started.result_run_id is not None:
        # The run this start began, or, for a start that raced another, the run it
        # joined: `_settled` reads the job as ended only when that run's chain closed.
        # The copy started either way: a failed write is logged, not answered. A later
        # start that joins writes the run id, and `NO_RUN_ID_MARGIN` covers the rest.
        try:
            await asyncio.to_thread(rows.started, job_id, started.result_run_id)
        except Exception:
            logger.warning("could not record the run of a started copy: %s", job_id, exc_info=True)
    return LibraryCopyStarted(copy_id=started.id)


@router.get(
    "/library-copies",
    response_model=list[LibraryCopySummary],
    summary="Recent library copies",
    description="The last copies started, newest first, each with its counts.",
)
async def get_library_copies(
    state: StateDep,
    ops: OperationsDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[LibraryCopySummary]:
    rows = LibraryCopyStore(state.settings_store.pool)
    recent = await asyncio.to_thread(rows.recent, limit=limit)
    return [_settled(row, await _execution(ops, row.copy_id)) for row in recent]


@router.get(
    "/library-copies/{copy_id}",
    response_model=LibraryCopyStatus,
    summary="A library copy's progress",
    description=(
        "The job's counts, whether it still runs, and up to 500 of its files with how"
        " each ended (`copied`, `skipped_same_hash`, `failed` with `reason`, or still"
        " `pending`); 404 for an id no copy has."
    ),
)
async def get_library_copy(copy_id: str, state: StateDep, ops: OperationsDep) -> LibraryCopyStatus:
    rows = LibraryCopyStore(state.settings_store.pool)
    summary = await asyncio.to_thread(rows.summary, copy_id)
    if summary is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no library copy {copy_id}")
    files = await asyncio.to_thread(rows.files, copy_id)
    settled = _settled(summary, await _execution(ops, copy_id))
    return LibraryCopyStatus(**settled.model_dump(), files=files)


#: What a job whose execution ended without its finish activity (terminated, timed out)
#: shows: its row still says `running`, which the execution's state contradicts.
ENDED_UNFINISHED = "the copy's workflow ended before it finished; start it again"
#: For a row with no run id only (one written before #5424 shipped, or a start whose
#: `started` write never happened): its execution reads as ended once it closed this
#: long after the row's `started_at`. The two times come from two clocks (Temporal's
#: and Postgres'). Hosts kept by NTP differ by well under a second, and 5 minutes also
#: covers a host whose clock jumps. So the run before a rerun, which closed before the
#: rerun's row was written, never reads as this row's end. The cost: such a row whose
#: execution ended in its first 5 minutes keeps showing `running` until a rerun.
NO_RUN_ID_MARGIN = timedelta(minutes=5)


def _settled(
    summary: LibraryCopySummary, described: WorkflowExecutionDescription | None
) -> LibraryCopySummary:
    """``summary`` with the execution's word on whether it runs. A row left `running`
    by the execution that its start began (`LibraryCopySummary.run_id`), now closed,
    is shown `failed`, saying so. For a row with a run id, the match is by run id, on
    Temporal's side only, because Temporal's ``close_time`` and Postgres' ``started_at``
    come from two clocks that can differ (#5424). The row is written before the
    execution starts (and reset before a rerun), so a closed execution of another run
    (the one before a rerun) is a copy about to run, not one that ended. A row with no
    run id falls back to ``close_time >= started_at + NO_RUN_ID_MARGIN``."""
    running = described is not None and described.status == WorkflowExecutionStatus.RUNNING
    update: dict[str, object] = {"running": running}
    if (
        summary.status == "running"
        and described is not None
        and described.close_time is not None
        and (
            summary.run_id in _chain(described)
            if summary.run_id is not None
            else described.close_time >= summary.started_at + NO_RUN_ID_MARGIN
        )
    ):
        update.update(status="failed", error=ENDED_UNFINISHED)
    return summary.model_copy(update=update)


def _chain(described: WorkflowExecutionDescription) -> set[str]:
    """The run ids that name the described execution's chain: its own and the first
    run's (a copy continues as new every `BATCHES_PER_RUN` batches)."""
    return {described.run_id, _first_run(described)}


def _first_run(described: WorkflowExecutionDescription) -> str:
    """The first run of the described execution's chain."""
    return described.raw_info.first_run_id or described.run_id


async def _execution(ops: OperationsDep, copy_id: str) -> WorkflowExecutionDescription | None:
    """The copy's execution, or ``None`` when none has run under its id."""
    try:
        return await ops.client.get_workflow_handle(copy_id).describe()
    except RPCError as error:
        if execution_gone(error):
            return None
        raise


@router.get(
    "/bambuddy-library",
    response_model=LibraryListing,
    summary="Bambuddy's library, as a copy's source",
    description=(
        "Bambuddy's folder tree and one folder's files (the top level without"
        " `folder_id`), every file type, whichever library `/print/library` serves: what"
        " the Library page browses to pick a Bambuddy-to-ScadBuddy copy's source. 409"
        " when no Bambuddy is configured."
    ),
)
async def get_bambuddy_library(
    store: SettingsStoreDep,
    folder_id: Annotated[int | None, Query(ge=1)] = None,
) -> LibraryListing:
    async with client_for(await asyncio.to_thread(store.load)) as client:
        return await list_library(client, folder_id=folder_id, show_all=True)
