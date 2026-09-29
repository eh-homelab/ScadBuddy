from __future__ import annotations

import logging

from fastapi import APIRouter, Response

from scadbuddy.api.deps import AppState, StateDep
from scadbuddy.core.metrics import CONTENT_TYPE_LATEST

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


def refresh_asset_metrics(state: AppState) -> None:
    """The upload store's usage gauges (#296), read per scrape. A failed read keeps
    the last values rather than failing the scrape."""
    try:
        usage = state.assets.usage()
    except OSError:
        logger.exception("could not read the upload store's usage")
        return
    state.metrics.assets_stored.set(usage.count)
    state.metrics.assets_bytes.set(usage.bytes)
    state.metrics.assets_max_count.set(usage.max_count)
    state.metrics.assets_max_bytes.set(usage.max_total_bytes)


# Beside /healthz rather than under /api/v1: it is for the scraper, not the SPA, and
# its body is Prometheus text rather than anything the generated client could type.
@router.get("/metrics", include_in_schema=False)
def metrics(state: StateDep) -> Response:
    # The queue gauges are read from the job store per scrape; a failed read is
    # reported as scadbuddy_render_store_up 0 rather than failing the scrape.
    state.render.refresh_metrics()
    refresh_asset_metrics(state)
    return Response(state.metrics.exposition(), media_type=CONTENT_TYPE_LATEST)
