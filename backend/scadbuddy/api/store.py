"""The blob store's usage for the Settings page (spec 2026-09-27 §10), and the
migration between its backends (spec 2026-10-16 §4.3)."""

from __future__ import annotations

import asyncio
from itertools import permutations
from typing import get_args

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from temporalio.client import WorkflowExecutionStatus
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.service import RPCError

from scadbuddy.api.deps import StateDep
from scadbuddy.core.config import StoreBackend
from scadbuddy.operations.component import OperationsDep
from scadbuddy.store.content import StoreUsage
from scadbuddy.store.factory import store_usage
from scadbuddy.workflows.commands import execution_gone
from scadbuddy.workflows.store_migration import StoreMigration
from scadbuddy.workflows.store_migration_models import (
    StoreMigrationParams,
    StoreMigrationProgress,
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
        "Move every recorded blob from `source` to `target`, `batch` at a time. A second"
        " request while one runs joins it. Save the target's settings first."
    ),
)
async def migrate_store(
    params: StoreMigrationParams, state: StateDep, ops: OperationsDep
) -> StoreMigrationStarted:
    # No update to answer, so not `start_command`: a plain start that joins a running one.
    handle = await ops.client.start_workflow(
        StoreMigration.run,
        # Both: with fewer payloads than parameters the worker decodes them untyped.
        args=[params, None],
        id=migration_id(params.source, params.target),
        task_queue=state.settings.temporal_task_queue_library,
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
    )
    return StoreMigrationStarted(workflow_id=handle.id)


@router.get(
    "/store/migrate",
    response_model=StoreMigrationProgress,
    summary="Blob store migration progress",
    description="The running migration's blobs moved, left, and failed; 404 when none runs.",
)
async def get_store_migration(ops: OperationsDep) -> StoreMigrationProgress:
    remote = [name for name in get_args(StoreBackend) if name != "local"]
    for source, target in permutations(remote, 2):
        handle = ops.client.get_workflow_handle(migration_id(source, target))
        try:
            described = await handle.describe()
        except RPCError as error:
            if execution_gone(error):
                continue
            raise
        if described.status == WorkflowExecutionStatus.RUNNING:
            return await handle.query(StoreMigration.progress)
    raise HTTPException(status.HTTP_404_NOT_FOUND, "no store migration is running")
