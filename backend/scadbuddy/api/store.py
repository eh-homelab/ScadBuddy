"""The blob store's usage for the Settings page (spec 2026-09-27 §10)."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter

from scadbuddy.api.deps import StateDep
from scadbuddy.store.content import StoreUsage
from scadbuddy.store.factory import store_usage

router = APIRouter(tags=["store"])


@router.get(
    "/store/usage",
    response_model=StoreUsage,
    summary="Blob store usage",
    description="What the store holds against SCADBUDDY_STORE_MAX_TOTAL_BYTES / _MAX_COUNT.",
)
async def get_store_usage(state: StateDep) -> StoreUsage:
    return await asyncio.to_thread(store_usage, state.store, state.config)
