"""ScadBuddy's own spool inventory (#4643, tier B of #3818): the records of the native
spool provider (`spools/native.py`, `spools/spool_providers.py`), plain reads and writes.

Every write refreshes the native provider's cached records at once
(`SpoolStore.replace_records`, as the refresh does), so the print dialog's overlay shows
the edit without waiting for the ``spool-providers`` Schedule. It does so even when the
shelf is now empty, which the Schedule skips (`NativeProvider.configured`), so the last
spool deleted or archived leaves the overlay too. A refresh that fails is recorded as
the provider's, as the Schedule's would be, and the write stands. Nothing here reaches
Bambuddy.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Response, status

from scadbuddy.core.problems import ApiError
from scadbuddy.spools.component import Spools, SpoolsDep
from scadbuddy.spools.native import (
    NativeSpool,
    NativeSpoolCreate,
    NativeSpoolExistsError,
    NativeSpoolPatch,
)
from scadbuddy.spools.spool_providers import NAME, native_provider

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/native-spools", tags=["spools"])


def _missing(spool_id: UUID) -> ApiError:
    return ApiError(status.HTTP_404_NOT_FOUND, f"no spool {spool_id} on ScadBuddy's shelf")


def _taken() -> ApiError:
    return ApiError(status.HTTP_409_CONFLICT, str(NativeSpoolExistsError()))


async def _refreshed(spools: Spools) -> None:
    provider = native_provider(spools)
    try:
        records = await provider.records()
        await spools.store.replace_records(NAME, records, datetime.now(UTC))
    except Exception as error:
        failure = type(error).__name__
        logger.warning(
            "could not refresh ScadBuddy's own spools", extra={"provider": NAME, "error": failure}
        )
        await spools.store.failed(NAME, datetime.now(UTC), failure)


@router.get(
    "",
    response_model=list[NativeSpool],
    summary="List ScadBuddy's own spools",
    description=(
        "The spools ScadBuddy keeps itself (the native spool provider, #4643), oldest first; "
        "archived ones only with `include_archived`. Bambuddy's inventory is not among them."
    ),
)
async def list_native_spools(
    spools: SpoolsDep, include_archived: bool = False
) -> list[NativeSpool]:
    store = native_provider(spools).store
    return await asyncio.to_thread(store.list, include_archived=include_archived)


@router.post(
    "",
    response_model=NativeSpool,
    status_code=status.HTTP_201_CREATED,
    summary="Add a spool to ScadBuddy's shelf",
    description=(
        "A spool ScadBuddy keeps itself: material, `#RRGGBB` colour, brand, name, label and "
        "remaining grams, the RFID `tray_uuid`/`tag_uid` a printer reports for it (all zeros "
        "is none) and the filament preset id. 409 when an unarchived spool carries the tag."
    ),
)
async def create_native_spool(spools: SpoolsDep, body: NativeSpoolCreate) -> NativeSpool:
    store = native_provider(spools).store
    try:
        made = await asyncio.to_thread(store.create, body)
    except NativeSpoolExistsError:
        raise _taken() from None
    await _refreshed(spools)
    return made


@router.get(
    "/{spool_id}",
    response_model=NativeSpool,
    summary="Get one of ScadBuddy's own spools",
)
async def get_native_spool(spools: SpoolsDep, spool_id: UUID) -> NativeSpool:
    found = await asyncio.to_thread(native_provider(spools).store.get, spool_id)
    if found is None:
        raise _missing(spool_id)
    return found


@router.patch(
    "/{spool_id}",
    response_model=NativeSpool,
    summary="Change one of ScadBuddy's own spools",
    description=(
        "Only the fields sent change; `null` clears one. `archived: true` takes the spool off "
        "the shelf (used up, given away) and `false` puts it back. 404 for no such spool, 409 "
        "when another unarchived spool carries the tag."
    ),
)
async def patch_native_spool(
    spools: SpoolsDep, spool_id: UUID, body: NativeSpoolPatch
) -> NativeSpool:
    store = native_provider(spools).store
    try:
        changed = await asyncio.to_thread(store.update, spool_id, body)
    except NativeSpoolExistsError:
        raise _taken() from None
    if changed is None:
        raise _missing(spool_id)
    await _refreshed(spools)
    return changed


@router.delete(
    "/{spool_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Forget one of ScadBuddy's own spools",
    description="Gone for good; archive it instead to keep its record. 404 for no such spool.",
)
async def delete_native_spool(spools: SpoolsDep, spool_id: UUID) -> Response:
    if not await asyncio.to_thread(native_provider(spools).store.delete, spool_id):
        raise _missing(spool_id)
    await _refreshed(spools)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
