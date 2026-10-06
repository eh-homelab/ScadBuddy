"""Find the Bambuddy archives an output's prints produced, and record them (#306).

Verified on the live Bambuddy 1.2.5.6 (print-history plan §1, L1-L3 and L8-L10):

- The scheduler sets ``queue_item.archive_id`` when it dispatches a library-file item,
  and the item outlives its print. So the queue items ScadBuddy created are read for it
  wherever ScadBuddy already reads them: the progress poll and the project attach.
  ScadBuddy never queues *from* an archive, so any ``archive_id`` on one of its items is
  the print's own.
- If an item is gone before its archive was seen, the archive is found by hash: its
  ``content_hash`` equals the ``file_hash`` of the sliced file it printed. A slice is
  unique to its output (the source 3MF carries the output id), so every archive with
  that hash is a print of this output, reprints made inside Bambuddy included. The
  scan reads only the days around the output's sends, so a reprint made long after
  the last one is not found this way.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Collection
from datetime import timedelta

import psycopg
from fastapi import status

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.models import QueueItem
from scadbuddy.bambuddy.print_links import PrintLink, PrintLinkStore
from scadbuddy.bambuddy.uploads import BambuddyUploadStore, DatabaseRequiredError
from scadbuddy.core.problems import ApiError
from scadbuddy.library.outputs import OutputMeta

logger = logging.getLogger(__name__)

#: One page of ``GET /archives/``; Bambuddy has no filter by hash, so the scan pages.
ARCHIVE_PAGE = 100
#: How many rows consecutive pages share. Bambuddy's order is not documented and the
#: list can change between two page reads: an archive created or deleted ahead of the
#: scan's position shifts every later row by one. Stepping the offset by less than a
#: page keeps a row that moved by up to this many from falling between pages; a row
#: read twice is matched once (#557).
ARCHIVE_OVERLAP = 10
#: How many pages one scan reads at most: 910 archives in its window.
MAX_ARCHIVE_PAGES = 10
#: How far the scan's window reaches past the output's last slice: a queued print can
#: wait for a printer. Whole days, as Bambuddy's ``date_from``/``date_to`` filters are.
SCAN_AFTER = timedelta(days=14)
#: And before its first upload, so a date filter in another time zone cannot cut the
#: first print off.
SCAN_BEFORE = timedelta(days=1)
#: How long a library file's queue item is read for its archive (#976): as long as an
#: output's print is looked for after its last slice.
LIBRARY_LINK_WINDOW = SCAN_AFTER
#: How many such items one call reads at most, and how many at once.
LIBRARY_LINK_LIMIT = 50
LIBRARY_LINK_CONCURRENCY = 8


async def link_item(
    links: PrintLinkStore, output_id: str, item: QueueItem, *, plate_id: int | None = None
) -> PrintLink | None:
    """Record the archive a queue item reports, if it has been dispatched."""
    if item.archive_id is None:
        return None
    link = PrintLink(
        archive_id=item.archive_id,
        matched_by="queue_item",
        queue_item_id=item.id,
        plate_id=plate_id if plate_id is not None else item.plate_id,
        printer_id=item.printer_id,
    )
    await links.record(output_id, link)
    return link


async def owned_queue_items(
    client: BambuddyClient,
    meta: OutputMeta,
    links: PrintLinkStore,
    wanted: Collection[int],
) -> set[int]:
    """Which of ``wanted`` are queue items ScadBuddy created for this output's prints:
    its plates' and its last item on the slice-and-queue route.

    Only these may be linked from outside a progress read. An id a caller names is
    otherwise any queue item in Bambuddy, and linking its archive would open the media
    proxy to a print ScadBuddy never made (#522 review).

    An item already linked to the output is one of these, since links are recorded
    only from them.
    """
    owned = {plate.queue_item_id for plate in meta.plates}
    if meta.queue_item_id is not None:
        owned.add(meta.queue_item_id)
    try:
        known = await links.for_output(meta.id)
    except (psycopg.Error, DatabaseRequiredError):
        logger.exception("could not read an output's print links", extra={"output_id": meta.id})
    else:
        owned.update(link.queue_item_id for link in known if link.queue_item_id is not None)
    return owned.intersection(wanted)


async def _slice_hashes(
    client: BambuddyClient, uploads: BambuddyUploadStore, output_id: str
) -> set[str]:
    """The hash of every sliced file of the output, read from Bambuddy once and kept."""
    hashes: set[str] = set()
    for copy in await uploads.for_output(output_id):
        for sliced in copy.sliced:
            file_hash = sliced.file_hash
            if file_hash is None:
                try:
                    file_hash = (await client.library_file(sliced.id)).file_hash
                except ApiError as error:
                    if error.status != status.HTTP_404_NOT_FOUND:
                        raise
                    continue
                if file_hash is None:
                    continue
                await uploads.record_slice_hash(output_id, sliced.id, file_hash)
            hashes.add(file_hash)
    return hashes


async def link_by_hash(
    client: BambuddyClient,
    uploads: BambuddyUploadStore,
    links: PrintLinkStore,
    meta: OutputMeta,
) -> list[PrintLink]:
    """Link every archive whose ``content_hash`` is one of the output's sliced files,
    made from the day before its first upload to ``SCAN_AFTER`` past its last slice.
    Returns the links it found, new or not.

    The window is bounded at both ends so an output made long ago does not have to
    page through every print since (#522 review): Bambuddy's order is not documented,
    and newest first would put the output's own prints past the last page read.
    """
    hashes = await _slice_hashes(client, uploads, meta.id)
    if not hashes:
        return []
    window = await uploads.sent_between(meta.id)
    first, last = window if window is not None else (meta.created_at, meta.created_at)
    since = (first - SCAN_BEFORE).date()
    until = (last + SCAN_AFTER).date()
    found: list[PrintLink] = []
    seen: set[int] = set()
    for page in range(MAX_ARCHIVE_PAGES):
        rows = await client.archives(
            date_from=since,
            date_to=until,
            limit=ARCHIVE_PAGE,
            offset=page * (ARCHIVE_PAGE - ARCHIVE_OVERLAP),
        )
        for row in rows:
            if row.id in seen:
                continue
            seen.add(row.id)
            if row.content_hash in hashes:
                link = PrintLink(
                    archive_id=row.id,
                    matched_by="content_hash",
                    plate_id=row.plate_id,
                    printer_id=row.printer_id,
                )
                await links.record(meta.id, link)
                found.append(link)
        if len(rows) < ARCHIVE_PAGE:
            break
    else:
        logger.warning(
            "stopped scanning archives for an output's prints",
            extra={
                "output_id": meta.id,
                "pages": MAX_ARCHIVE_PAGES,
                "date_from": since.isoformat(),
                "date_to": until.isoformat(),
            },
        )
    return found


async def link_library_prints(client: BambuddyClient, links: PrintLinkStore) -> None:
    """Link the archives of the library files' queue items that have one now (#976).

    A library-file run has no output, so no progress read follows it the way an
    output's print is linked: the prints list calls this before it reads the links.
    An item Bambuddy has dropped before naming an archive is marked gone and not read
    again; any other failure is logged and leaves the item to the next call.
    """
    pending = await links.pending_library(LIBRARY_LINK_WINDOW, LIBRARY_LINK_LIMIT)
    gate = asyncio.Semaphore(LIBRARY_LINK_CONCURRENCY)

    async def link(queue_item_id: int) -> None:
        async with gate:
            try:
                item = await client.queue_item(queue_item_id)
            except ApiError as error:
                if error.status == status.HTTP_404_NOT_FOUND:
                    await links.library_gone(queue_item_id)
                else:
                    logger.warning(
                        "could not read a library print's queue item",
                        extra={"queue_item_id": queue_item_id, "status": error.status},
                    )
                return
            if item.archive_id is not None:
                await links.link_library(queue_item_id, item.archive_id, item.library_file_name)

    await asyncio.gather(*(link(row.queue_item_id) for row in pending))
