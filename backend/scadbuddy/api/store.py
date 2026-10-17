"""The blob store's usage for the Settings page (spec 2026-09-27 §10), and the
migration between its backends (spec 2026-10-16 §4.3)."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, status
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ValidationError
from temporalio.client import WorkflowExecutionStatus
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.service import RPCError

from scadbuddy.api.deps import StateDep
from scadbuddy.core.config import StoreBackend
from scadbuddy.library.settings_store import StoreNotReadyError, load_render_store_settings
from scadbuddy.operations.component import OperationsDep
from scadbuddy.store import migrate
from scadbuddy.store.content import StoreUsage
from scadbuddy.store.factory import store_usage
from scadbuddy.store.index import BlobIndex
from scadbuddy.workflows.commands import execution_gone
from scadbuddy.workflows.store_migration import StoreMigration
from scadbuddy.workflows.store_migration_models import (
    StoreMigrationInput,
    StoreMigrationParams,
    migration_id,
)

router = APIRouter(tags=["store"])


class StoreMigrationStarted(BaseModel):
    workflow_id: str


@router.get(
    "/store/usage",
    response_model=StoreUsage,
    summary="Blob store usage",
    description="What the store holds against SCADBUDDY_STORE_MAX_TOTAL_BYTES / _MAX_COUNT.",
)
async def get_store_usage(state: StateDep) -> StoreUsage:
    return await asyncio.to_thread(store_usage, state.store, state.config)


@router.post(
    "/store/migrate",
    response_model=StoreMigrationStarted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Migrate the blob store",
    description=(
        "Copy every recorded blob from `source` to `target`, `batch` at a time; each row"
        " switches to its copy when ScadBuddy next starts with `store_backend` set to"
        " `target`. A second request while one runs joins it; one after it ended retries"
        " what failed. Save the target's settings first: 422, saying what is missing,"
        " while either backend cannot be built. 409 once `store_backend` is already"
        " `target`."
    ),
    responses={
        status.HTTP_409_CONFLICT: {"description": "`store_backend` is already `target`."},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "A backend cannot be built from the saved settings."
        },
    },
)
async def migrate_store(
    params: StoreMigrationParams, state: StateDep, ops: OperationsDep
) -> StoreMigrationStarted:
    pool = state.settings_store.pool
    saved = await asyncio.to_thread(load_render_store_settings, pool, state.settings)
    if saved.store_backend == params.target:
        # The copy is for before the flip; after it, the start-up switch has run.
        raise HTTPException(status.HTTP_409_CONFLICT, f"store_backend is already {params.target}")
    try:
        await migrate.check_backends(saved, pool, state.settings, params.source, params.target)
    except StoreNotReadyError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error
    started = await asyncio.to_thread(BlobIndex(pool).now)
    # No update to answer, so not `start_command`: a plain start that joins a running one.
    handle = await ops.client.start_workflow(
        StoreMigration.run,
        StoreMigrationInput(**params.model_dump(), started=started),
        id=migration_id(params.source, params.target),
        task_queue=state.settings.temporal_task_queue_library,
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
    )
    return StoreMigrationStarted(workflow_id=handle.id)


#: The most failed rows `GET /store/migrate` lists; `failed_count` counts them all.
MAX_FAILED = 200


class FailedCopy(BaseModel):
    key: str
    error: str


class StoreMigrationStatus(BaseModel):
    source: StoreBackend
    target: StoreBackend
    #: Rows on `source` still to copy to `target`, but those whose copy failed.
    remaining: int
    #: Rows copied: each switches to its copy at the next start on `target`.
    copied: int
    #: Rows whose copy failed, and why (at most `MAX_FAILED`); a new run retries them.
    failed: list[FailedCopy]
    failed_count: int
    running: bool


@router.get(
    "/store/migrate",
    response_model=StoreMigrationStatus,
    summary="Blob store migration progress",
    description=(
        "The pair's rows still to copy, copied and failed (with why), and whether its"
        " migration runs; 404 when none ever ran for the pair."
    ),
)
async def get_store_migration(
    state: StateDep,
    ops: OperationsDep,
    source: StoreBackend = "bambuddy",
    target: StoreBackend = "s3",
) -> StoreMigrationStatus:
    try:
        StoreMigrationParams(source=source, target=target)
    except ValidationError as error:
        raise RequestValidationError(error.errors()) from error
    handle = ops.client.get_workflow_handle(migration_id(source, target))
    try:
        described = await handle.describe()
    except RPCError as error:
        if execution_gone(error):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no store migration ran") from error
        raise
    index = BlobIndex(state.settings_store.pool)
    remaining, failed_count, copied = await asyncio.to_thread(
        index.migration_counts, source, target
    )
    failed = await asyncio.to_thread(index.migration_failures, source, target, limit=MAX_FAILED)
    return StoreMigrationStatus(
        source=source,
        target=target,
        remaining=remaining,
        copied=copied,
        failed=[FailedCopy(key=key, error=error) for key, error in failed],
        failed_count=failed_count,
        running=described.status == WorkflowExecutionStatus.RUNNING,
    )
