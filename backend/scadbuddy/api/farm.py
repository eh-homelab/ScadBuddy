"""``/api/v1/farm/…`` — the farm's context from Bambuddy (#1912): the queue, every
archive with its outcome, aggregate stats and the spool inventory, and (#3742) the
pictures of the queue's items and the printers' jobs.

Read only: nothing here writes to Bambuddy, and every call needs only the ``Read
Status`` scope, which a 401/403 names (``bambuddy/errors.py``).
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Path, Query, Request, Response
from fastapi.responses import StreamingResponse

from scadbuddy.api.deps import PrintLinksDep, SettingsStoreDep
from scadbuddy.api.prints import MEDIA_RESPONSES, _proxy
from scadbuddy.bambuddy.client import client_for
from scadbuddy.bambuddy.farm import (
    ArchiveOutcome,
    InventoryView,
    SpoolSummary,
    inventory_view,
    spool_summary,
)
from scadbuddy.bambuddy.models import ArchiveStats, QueueItem
from scadbuddy.bambuddy.pictures import queue_item_picture
from scadbuddy.bambuddy.print_links import JobMatches, PrintLinkStore, ScadBuddyJob
from scadbuddy.core.problems import DATABASE_ERRORS

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/farm", tags=["farm"])

#: The states a queue item takes (Bambuddy's ``PrintQueueItem.status``); only these are
#: forwarded, never the caller's own text.
QueueStatus = Literal["pending", "printing", "completed", "failed", "cancelled"]

PrinterFilter = Annotated[int | None, Query(description="One Bambuddy printer's id")]
DateFrom = Annotated[date | None, Query(description="First day, inclusive (created_at)")]
DateTo = Annotated[date | None, Query(description="Last day, inclusive (created_at)")]


#: The queue item states that can carry the "Queued from ScadBuddy" mark (#3741, #5833):
#: a job still to print or printing. Bambuddy's queue item states are pending, printing,
#: completed, failed, skipped and cancelled; it has no "paused" state. Source:
#: maziggy/bambuddy `backend/app/models/print_queue.py` line 140 at commit 918353a
#: (https://github.com/maziggy/bambuddy/blob/918353a945d2993a475d5cf1ce135c52c9e11932/backend/app/models/print_queue.py#L140).
#: A finished or skipped item is history and is not looked up.
MARKED_STATUSES = frozenset({"pending", "printing"})


class FarmQueueItem(QueueItem):
    """A queue item, and whether ScadBuddy queued it (#3741)."""

    #: The output or library file ScadBuddy queued this item for, found only by a record
    #: of this item: the send a run or ScadBuddy's reprint recorded, or a link that names
    #: the item. Set only on a pending or printing item (`MARKED_STATUSES`).
    #: ``None`` for a finished item, for an item queued from Bambuddy (its reprint of an
    #: archive ScadBuddy linked too), or when ScadBuddy's records cannot be read. Never
    #: taken from Bambuddy's answer.
    scadbuddy: ScadBuddyJob | None = None
    #: The item's picture (#3742), a URL on ScadBuddy's own origin: ScadBuddy's for its
    #: own item (`scadbuddy`), else Bambuddy's thumbnail of the item's library file or
    #: archive through this router (`bambuddy/pictures.py`). ``None`` when there is none.
    picture: str | None = None


@router.get("/queue", response_model=list[FarmQueueItem], summary="Bambuddy's print queue")
async def get_queue(
    store: SettingsStoreDep,
    links: PrintLinksDep,
    printer_id: Annotated[
        int | None, Query(description="One printer's items; -1 for items not assigned to one")
    ] = None,
    status: Annotated[QueueStatus | None, Query(description="Only items in this state")] = None,
) -> list[FarmQueueItem]:
    """The queue in Bambuddy's order. Finished items stay in it, so an unfiltered read
    is mostly history: ask for ``status=pending`` or ``printing`` for what is to come.
    Each pending or printing item ScadBuddy queued carries ``scadbuddy`` (#3741); only
    those items are looked up (#5833). Each item's ``picture`` (#3742) is built from
    this one read."""
    async with client_for(store.load()) as client:
        items = await client.queue(printer_id=printer_id, status=status)
    matches = await _jobs(links, [item for item in items if item.status in MARKED_STATUSES])

    def row(item: QueueItem) -> FarmQueueItem:
        job = matches.queue_items.get(item.id)
        picture = queue_item_picture(item, job)
        return FarmQueueItem(**item.model_dump(), scadbuddy=job, picture=picture)

    return [row(item) for item in items]


#: How long the browser keeps a picture (#3742). A library file's or an archive's
#: thumbnail is the same image while its id stands; a printer's cover is kept only when
#: its URL names the job's archive, one per print (`get_printer_cover`).
PICTURE_CACHE_CONTROL = "private, max-age=3600"
PictureId = Annotated[int, Path(ge=1)]


@router.get(
    "/library/{file_id}/thumbnail",
    response_class=StreamingResponse,
    responses=MEDIA_RESPONSES,
    summary="A Bambuddy library file's thumbnail",
)
async def get_library_thumbnail(
    file_id: PictureId, request: Request, store: SettingsStoreDep
) -> Response:
    """Bambuddy's thumbnail of a file in its own library (#3742), for a queue item not
    ScadBuddy's. Always Bambuddy's library, whichever library ScadBuddy serves: the
    queue's ids are Bambuddy's."""
    return await _proxy(
        store,
        request,
        f"/library/files/{file_id}/thumbnail",
        operation="library.thumbnail",
        what="show the file's thumbnail",
        cache_control=PICTURE_CACHE_CONTROL,
    )


@router.get(
    "/archives/{archive_id}/thumbnail",
    response_class=StreamingResponse,
    responses=MEDIA_RESPONSES,
    summary="A Bambuddy archive's thumbnail",
)
async def get_archive_thumbnail(
    archive_id: PictureId, request: Request, store: SettingsStoreDep
) -> Response:
    """Bambuddy's thumbnail of any of its archives (#3742), for a queue item not
    ScadBuddy's. ``/archives`` above already lists every archive; this serves its
    thumbnail only, none of its other files (those stay ``/prints/{id}``'s, which takes
    only an archive ScadBuddy printed)."""
    return await _proxy(
        store,
        request,
        f"/archives/{archive_id}/thumbnail",
        operation="media.thumbnail",
        what="show the thumbnail",
        cache_control=PICTURE_CACHE_CONTROL,
    )


@router.get(
    "/printers/{printer_id}/cover",
    response_class=StreamingResponse,
    responses=MEDIA_RESPONSES,
    summary="The cover of a printer's job",
)
async def get_printer_cover(
    printer_id: PictureId,
    request: Request,
    store: SettingsStoreDep,
    archive: Annotated[
        int | None,
        Query(ge=1, description="The job's archive, one per print: the browser keeps it"),
    ] = None,
    job: Annotated[
        str | None,
        Query(max_length=512, description="The job's name, with no archive: never kept"),
    ] = None,
) -> Response:
    """Bambuddy's cover image of the job a Bambuddy printer prints (#3742), the status's
    ``cover_url``: maziggy/bambuddy f9080d1 ``backend/app/api/routes/printers.py``
    lines 506-509 (set while it runs or is paused) and 1128 (the route, which takes an
    API key with ``printers:read``). Neither ``archive`` nor ``job`` is sent to Bambuddy:
    they key the browser's copy (`bambuddy/pictures.py` `printer_cover`). Only a cover
    keyed by its archive is kept; job names repeat, so one keyed by its name, or by
    nothing, is fetched again on each load (``no-store``)."""
    del job
    return await _proxy(
        store,
        request,
        f"/printers/{printer_id}/cover",
        operation="printers.cover",
        what="show the cover of the printer's job",
        cache_control=PICTURE_CACHE_CONTROL if archive is not None else "no-store",
    )


async def _jobs(links: PrintLinkStore, items: list[QueueItem]) -> JobMatches:
    """Which of ``items`` ScadBuddy queued, in one read; none when its records cannot be
    read, so a database blip costs the marks, never the queue."""
    if not items:
        return JobMatches()
    try:
        return await links.jobs(queue_items=[item.id for item in items])
    except DATABASE_ERRORS:
        logger.warning("print links unreadable; the queue shows no ScadBuddy marks")
        return JobMatches()


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
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[ArchiveOutcome]:
    """Every archive Bambuddy has, ScadBuddy's or not (``GET /prints`` is ScadBuddy's
    own), each with its status, times, filament, cost, failure reason and run counts.
    A page shorter than ``limit`` is the last."""
    async with client_for(store.load()) as client:
        # Strict, unlike the print history's list read (#3477 review): this page *is*
        # the rows, so one that does not parse fails the read rather than vanish from it.
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
        int | None,
        Query(description="Only this printer's loaded slots; every spool keeps its placement"),
    ] = None,
) -> InventoryView:
    """Every spool in Bambuddy's inventory, with where it is loaded, and every loaded
    slot of each active printer with its remaining grams."""
    async with client_for(store.load()) as client:
        return await inventory_view(
            client, include_archived=include_archived, printer_id=printer_id
        )


@router.get(
    "/spools/{spool_id}/summary",
    response_model=SpoolSummary,
    summary="One spool's stock and price, and its filament's",
)
async def get_spool_summary(
    spool_id: int,
    store: SettingsStoreDep,
    grams: Annotated[
        float | None,
        Query(ge=0, description="Grams to price at this spool's cost, e.g. a plate's use"),
    ] = None,
) -> SpoolSummary:
    """What a filament hover card shows (#785): the spool's remaining grams, where it is
    loaded and its price per kg, and every unarchived spool of the same filament
    (material, subtype, brand and colour) with their total remaining grams and where
    they are loaded. Prices are Bambuddy's own ``cost_per_kg``; with none, every price
    and cost is ``null``."""
    async with client_for(store.load()) as client:
        return await spool_summary(client, spool_id, grams=grams)
