from __future__ import annotations

from fastapi import APIRouter, Response

from scadbuddy.api.deps import StateDep
from scadbuddy.core.metrics import CONTENT_TYPE_LATEST

router = APIRouter(tags=["health"])


# Beside /healthz rather than under /api/v1: it is for the scraper, not the SPA, and
# its body is Prometheus text rather than anything the generated client could type.
@router.get("/metrics", include_in_schema=False)
def metrics(state: StateDep) -> Response:
    return Response(state.metrics.exposition(), media_type=CONTENT_TYPE_LATEST)
