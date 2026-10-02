from __future__ import annotations

import asyncio
import logging

import psycopg
from fastapi import APIRouter, Response

from scadbuddy.api.deps import AppState, StateDep
from scadbuddy.core.metrics import CONTENT_TYPE_LATEST
from scadbuddy.library.assets import AssetStoreUnavailableError
from scadbuddy.store.cache import CachedBlobStore
from scadbuddy.store.factory import store_health, store_usage

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


def refresh_asset_metrics(state: AppState) -> None:
    """The upload store's usage gauges (#296), read per scrape. A failed read keeps
    the last values rather than failing the scrape, as the render queue's gauges do
    when their store is unavailable -- a database error, or no database at all."""
    try:
        usage = state.assets.usage()
    except (OSError, psycopg.Error, AssetStoreUnavailableError):
        logger.exception("could not read the upload store's usage")
        return
    state.metrics.assets_stored.set(usage.count)
    state.metrics.assets_bytes.set(usage.bytes)
    state.metrics.assets_max_count.set(usage.max_count)
    state.metrics.assets_max_bytes.set(usage.max_total_bytes)


#: Seconds a local store walk answers the scraper for.
STORE_USAGE_MAX_AGE = 60.0


async def refresh_store_metrics(state: AppState) -> None:
    # A scrape every 15-30s would otherwise walk the whole local store each time.
    usage = await asyncio.to_thread(
        store_usage, state.store, state.config, max_age=STORE_USAGE_MAX_AGE
    )
    state.metrics.store_blobs.set(usage.count)
    for kind, size in usage.by_kind.items():
        state.metrics.store_bytes.labels(kind).set(size)
    state.metrics.store_max_blobs.set(usage.max_count)
    state.metrics.store_max_bytes.set(usage.max_total_bytes)
    health = await store_health(state.store)
    state.metrics.store_render_key_fallback.set(1 if health.render_key_fallback else 0)
    if isinstance(state.store.blobs, CachedBlobStore):
        state.metrics.worker_cache_bytes.set(
            await asyncio.to_thread(state.store.blobs.cached_bytes)
        )


# Beside /healthz rather than under /api/v1: it is for the scraper, not the SPA, and
# its body is Prometheus text rather than anything the generated client could type.
@router.get("/metrics", include_in_schema=False)
async def metrics(state: StateDep) -> Response:
    # The queue gauges are read from the job store per scrape; a failed read is
    # reported as scadbuddy_render_store_up 0 rather than failing the scrape.
    await asyncio.to_thread(state.render.refresh_metrics)
    await asyncio.to_thread(refresh_asset_metrics, state)
    try:
        await refresh_store_metrics(state)
    except Exception:
        # Like the upload store's gauges: keep the last values, never fail the scrape.
        logger.exception("could not read the blob store's usage")
    return Response(state.metrics.exposition(), media_type=CONTENT_TYPE_LATEST)
