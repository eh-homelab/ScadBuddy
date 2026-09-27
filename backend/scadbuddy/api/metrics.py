from __future__ import annotations

import logging

from fastapi import APIRouter, Response

from scadbuddy.api.deps import StateDep
from scadbuddy.core.metrics import CONTENT_TYPE_LATEST

router = APIRouter(tags=["health"])

logger = logging.getLogger(__name__)


# Beside /healthz rather than under /api/v1: it is for the scraper, not the SPA, and
# its body is Prometheus text rather than anything the generated client could type.
@router.get("/metrics", include_in_schema=False)
def metrics(state: StateDep) -> Response:
    # The queue gauges are read from the job store per scrape. A database outage
    # must not cost the scrape: the counters and histograms are still worth having.
    try:
        state.queue.refresh_metrics()
    except Exception:
        logger.exception("could not read the render queue's gauges")
    return Response(state.metrics.exposition(), media_type=CONTENT_TYPE_LATEST)
