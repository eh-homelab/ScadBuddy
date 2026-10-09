"""``/api/v1/farm/…`` — the farm's context from Bambuddy (#1912): the queue, every
archive with its outcome, aggregate stats and the spool inventory.

Read only: nothing here writes to Bambuddy, and every call needs only the ``Read
Status`` scope, which a 401/403 names (``bambuddy/errors.py``).
"""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query

from scadbuddy.api.deps import SettingsStoreDep
from scadbuddy.bambuddy.client import client_for
from scadbuddy.bambuddy.farm import ArchiveOutcome, InventoryView, QueueView, inventory_view
from scadbuddy.bambuddy.models import ArchiveStats

router = APIRouter(prefix="/farm", tags=["farm"])

PrinterFilter = Annotated[int | None, Query(description="One Bambuddy printer's id")]
DateFrom = Annotated[date | None, Query(description="First day, inclusive (created_at)")]
DateTo = Annotated[date | None, Query(description="Last day, inclusive (created_at)")]


@router.get("/queue", response_model=QueueView, summary="Bambuddy's print queue")
async def get_queue(
    store: SettingsStoreDep,
    printer_id: Annotated[
        int | None, Query(description="One printer's items; -1 for items not assigned to one")
    ] = None,
    status: Annotated[
        str | None,
        Query(
            min_length=1,
            max_length=32,
            description="pending, printing, completed, failed or cancelled",
        ),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> QueueView:
    """The queue in Bambuddy's order. Finished items stay in it, so an unfiltered read
    is mostly history: ask for ``status=pending`` or ``printing`` for what is to come."""
    async with client_for(store.load()) as client:
        items = await client.queue(printer_id=printer_id, status=status)
    return QueueView(items=items[:limit], total=len(items))


@router.get("/stats", response_model=ArchiveStats, summary="The farm's print statistics")
async def get_stats(
    store: SettingsStoreDep, date_from: DateFrom = None, date_to: DateTo = None
) -> ArchiveStats:
    """Totals over every archive, or over those created within the window: prints by
    outcome, time, filament, cost, and counts by filament type and by printer."""
    async with client_for(store.load()) as client:
        return await client.archive_stats(date_from=date_from, date_to=date_to)


@router.get(
    "/archives", response_model=list[ArchiveOutcome], summary="Bambuddy's archives with outcomes"
)
async def get_archives(
    store: SettingsStoreDep,
    printer_id: PrinterFilter = None,
    project_id: Annotated[int | None, Query(description="One Bambuddy project's id")] = None,
    date_from: DateFrom = None,
    date_to: DateTo = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[ArchiveOutcome]:
    """Every archive Bambuddy has, ScadBuddy's or not (``GET /prints`` is ScadBuddy's
    own), each with its status, times, filament, cost, failure reason and run counts.
    A page shorter than ``limit`` is the last."""
    async with client_for(store.load()) as client:
        rows = await client.archive_outcomes(
            printer_id=printer_id,
            project_id=project_id,
            limit=limit,
            offset=offset,
            date_from=date_from,
            date_to=date_to,
        )
    return [ArchiveOutcome.of(row) for row in rows]


@router.get("/inventory", response_model=InventoryView, summary="Spools and loaded slots")
async def get_inventory(
    store: SettingsStoreDep,
    include_archived: bool = False,
    printer_id: Annotated[
        int | None, Query(description="Only this printer's slots and assignments")
    ] = None,
) -> InventoryView:
    """Every spool in Bambuddy's inventory, with where it is loaded, and every loaded
    slot of each active printer with its remaining grams."""
    async with client_for(store.load()) as client:
        return await inventory_view(
            client, include_archived=include_archived, printer_id=printer_id
        )
