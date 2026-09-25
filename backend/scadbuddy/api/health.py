from __future__ import annotations

import os
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from scadbuddy.api.deps import StateDep

router = APIRouter(tags=["health"])


class Health(BaseModel):
    status: Literal["ok", "degraded"]
    openscad_version: str | None
    data_dir_writable: bool
    # Build provenance, from SCADBUDDY_REVISION / SCADBUDDY_VERSION. A deploy is
    # verified from outside by comparing `revision` with the commit that was
    # pinned, so keep these exact and unformatted.
    revision: str
    version: str


@router.get("/healthz", response_model=Health, summary="Liveness")
def healthz(state: StateDep) -> Health:
    writable = state.paths.root.is_dir() and os.access(state.paths.root, os.W_OK)
    healthy = writable and state.openscad_version is not None
    return Health(
        status="ok" if healthy else "degraded",
        openscad_version=state.openscad_version,
        data_dir_writable=writable,
        revision=state.settings.revision,
        version=state.settings.version,
    )
