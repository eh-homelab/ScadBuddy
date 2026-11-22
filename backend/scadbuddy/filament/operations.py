"""The colour catalog's syncs as an operation (#6907, plan
`docs/superpowers/plans/2026-11-18-filament-bb-sync.md` Decision 210).

``filament_colour_sync`` runs on the ``library`` worker, in the API process, which holds the
database: filamentcolors.xyz is read from the CRUD service, never from a worker without
SQL, and Bambu Studio's colour codes are the image's own file (`colour_catalog.py`). A
filamentcolors run reads about 23 pages, so the route answers 202 past the command deadline
and a caller follows ``GET /operations/{id}``; ``GET /filament/colour-syncs`` shows the
run's progress meanwhile.

The effect may run again: a second run writes what the first wrote, as unchanged.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from fastapi import status

from scadbuddy.core.problems import ApiError
from scadbuddy.filament.colour_catalog import (
    ColourSyncRequest,
    ColourSyncRunningError,
    ColourSyncs,
    http_client,
    run_bambu,
    run_filamentcolors,
)
from scadbuddy.operations.kinds import KindsBuild, OperationKind

if TYPE_CHECKING:
    from scadbuddy.core.components import Components, Core

KIND = "filament_colour_sync"
#: 200 pages at most, each up to three tries of 30 s with their waits: far more than the 23
#: pages read in a few seconds each; under `colour_catalog.RUN_STALE`. The run heartbeats
#: while it waits (`workflows.operation_activities._heartbeating`), so this timeout or a
#: worker's shutdown cancels it between pages (a page's write in its thread still lands),
#: and the run's row ends ``failed`` "the run was interrupted". The
#: ``filament_colour_syncs`` row, not the operation, is the run's real state: a run whose
#: process died ends there once `RUN_STALE` has passed (`ColourSyncs.latest`, ``start``).
SYNC_TIMEOUT = timedelta(minutes=20)


def subject(request: ColourSyncRequest) -> str:
    return f"filament_colours:{request.source}"


def running_error(source: str) -> ApiError:
    return ApiError(
        status.HTTP_409_CONFLICT,
        f"a {source} colour sync is running; follow it on GET /filament/colour-syncs",
        reason="colour_sync_running",
    )


def _kinds(core: Core, components: Components) -> list[OperationKind]:
    syncs = ColourSyncs(core.settings_store.pool)

    async def check(request: dict[str, Any]) -> dict[str, Any]:
        body = ColourSyncRequest.model_validate(request)
        if await asyncio.to_thread(syncs.running, body.source):
            raise running_error(body.source)
        return {}

    async def run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        body = ColourSyncRequest.model_validate(request)
        try:
            sync_id = await asyncio.to_thread(syncs.start, body.source)
        except ColourSyncRunningError:
            raise running_error(body.source) from None
        if body.source == "bambu":
            done = await run_bambu(syncs, sync_id, core.settings.slicer_profiles_dir)
        else:
            async with http_client() as http:
                done = await run_filamentcolors(syncs, sync_id, http, force=body.force)
        dumped: dict[str, Any] = done.model_dump(mode="json")
        return dumped

    return [
        OperationKind(
            KIND,
            check,
            run,
            queue="library",
            run_timeout=SYNC_TIMEOUT,
            where="the colour catalog (Filament → Catalog → Colours)",
        )
    ]


OPERATION_KINDS: KindsBuild = _kinds
