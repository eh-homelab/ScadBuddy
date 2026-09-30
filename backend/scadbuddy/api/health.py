from __future__ import annotations

import os
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from scadbuddy.api.deps import StateDep
from scadbuddy.store.factory import StoreHealth, store_health

router = APIRouter(tags=["health"])


class TemporalHealth(BaseModel):
    """Where this process sends its renders (SCADBUDDY_TEMPORAL_*)."""

    address: str
    namespace: str
    task_queue: str
    worker_inprocess: bool


class Health(BaseModel):
    status: Literal["ok", "degraded"]
    openscad_version: str | None
    data_dir_writable: bool
    # Build provenance, from SCADBUDDY_REVISION / SCADBUDDY_VERSION. A deploy is
    # verified from outside by comparing `revision` with the commit that was
    # pinned, so keep these exact and unformatted.
    revision: str
    version: str
    temporal: TemporalHealth
    #: The blob store this process runs on, against the one stored in Settings (#426).
    store: StoreHealth


@router.get("/healthz", response_model=Health, summary="Liveness")
async def healthz(state: StateDep) -> Health:
    writable = state.paths.root.is_dir() and os.access(state.paths.root, os.W_OK)
    healthy = writable and state.openscad_version is not None
    settings = state.settings
    return Health(
        status="ok" if healthy else "degraded",
        openscad_version=state.openscad_version,
        data_dir_writable=writable,
        revision=settings.revision,
        version=settings.version,
        store=await store_health(state.store),
        temporal=TemporalHealth(
            address=settings.temporal_address,
            namespace=settings.temporal_namespace,
            task_queue=settings.temporal_task_queue_render,
            worker_inprocess=settings.temporal_worker_inprocess,
        ),
    )
