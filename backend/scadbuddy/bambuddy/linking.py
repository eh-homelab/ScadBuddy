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

A Bambuddy library file's print is linked the same way (#1755): its slices are recorded
under its subject (``library:<file id>``) as an output's are under its id (#1882), so
the same scan finds its reprints. It runs when one of the file's queue items is gone,
as an output's does, and when the history is filtered to the file.

A print started over the LAN (#3818) has no queue item, and a native slice is in no
Bambuddy library, so neither way above finds it. Bambuddy still archives it from the
3MF it downloads from the printer, with that file's SHA-256 as ``content_hash``: the
hash of the native slice the run uploaded. `link_lan_starts` scans for it, by the
starts' own days (#5724). The prints list runs it for each subject with a LAN start
that no archive names yet (`print_history._link_lan_prints`).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Collection
from datetime import date, datetime, timedelta

import psycopg
from fastapi import status

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.models import QueueItem
from scadbuddy.bambuddy.print_links import PrintLink, PrintLinkStore
from scadbuddy.bambuddy.stages import stage_of
from scadbuddy.bambuddy.subject import PrintSubject
from scadbuddy.bambuddy.uploads import BambuddyUploadStore
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
#: How many of a library file's pending queue items one call reads at most, newest
#: first, and how many at once (#976).
LIBRARY_LINK_LIMIT = 50
LIBRARY_LINK_CONCURRENCY = 8
#: How many library files one call scans by hash at once (#1755).
LIBRARY_SCAN_CONCURRENCY = 2
#: How many library files one call scans by hash at most. Each scan can page
#: `MAX_ARCHIVE_PAGES` archive pages, and the prints list waits for them; an item past
#: this is left pending, so the next call reads it again and scans its file (#2120).
LIBRARY_SCANS_PER_CALL = 4
#: How long after it was recorded a library file's queue item is read at most. An
#: item that never settles (one waiting on a printer that was removed, or skipped and
#: never resumed) would otherwise cost a read on every list and hold a slot in
#: ``LIBRARY_LINK_LIMIT`` for good; past this it is marked gone (#1703).
LIBRARY_LINK_BACKSTOP = timedelta(days=180)
#: How often one library file's archives are scanned by hash at most, as an output's
#: are (`progress.HASH_SCAN_INTERVAL`): each scan pages Bambuddy's archive list (#1755).
#: Per process, like the output's: the API and the print worker each keep their own, so
#: a file can be scanned once by each in one interval.
LIBRARY_HASH_SCAN_INTERVAL = 600.0
_last_library_scan: dict[int, float] = {}
#: How long after it started a LAN start's archive is looked for (#5724,
#: `link_lan_starts`): as long as an output's scan reaches past its last slice.
LAN_LINK_WINDOW = SCAN_AFTER
#: How many subjects with a LAN start no archive names yet one prints list reads at
#: most, newest start first, and how many of them it scans at most, and how many at once.
LAN_PENDING_LIMIT = 50
LAN_SCANS_PER_CALL = 4
LAN_SCAN_CONCURRENCY = 2
#: How often one subject's LAN starts are looked for at most, per process, as a library
#: file's are (`LIBRARY_HASH_SCAN_INTERVAL`): a start whose print never reached Bambuddy
#: stays unnamed until `LAN_LINK_WINDOW` ends, and each scan pages the archive list.
LAN_SCAN_INTERVAL = 600.0
_last_lan_scan: dict[str, float] = {}
#: The stages a queue item never leaves once it has no archive (#1705). On a
#: library-file item Bambuddy creates the archive and commits ``archive_id`` before it
#: uploads the file or sets ``printing``, so a run that got as far as the printer has
#: its archive by the time it can be ``completed``; an item that settles without one
#: failed or was cancelled before dispatch. Bambuddy's own note on these states: they
#: "never return" (``backend/app/services/print_scheduler.py``, the library branch of
#: ``_start_print`` and ``_repoint_siblings_at_archive``, maziggy/bambuddy@505948f).
#: Its ``skipped`` is not here: ``POST /queue/printer/{id}/resume``
#: (``backend/app/api/routes/print_queue.py``) puts a skipped item back to ``pending``,
#: so it is read on like any pending one, until the backstop (#1704).
_SETTLED = ("done", "failed", "cancelled")


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
    await links.record(PrintSubject.output(output_id), link)
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
        known = await links.for_subject(PrintSubject.output(meta.id))
    except psycopg.Error:
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
    """`link_subject_by_hash` for an output."""
    return await link_subject_by_hash(
        client, uploads, links, PrintSubject.output(meta.id), made_at=meta.created_at
    )


async def link_subject_by_hash(
    client: BambuddyClient,
    uploads: BambuddyUploadStore,
    links: PrintLinkStore,
    subject: PrintSubject,
    *,
    made_at: datetime | None = None,
) -> list[PrintLink]:
    """Link every archive whose ``content_hash`` is one of the subject's sliced files,
    made from the day before its first upload to ``SCAN_AFTER`` past its last slice
    (around ``made_at`` when no window is recorded). Returns the links it found, new
    or not.

    The window is bounded at both ends so an output made long ago does not have to
    page through every print since (#522 review): Bambuddy's order is not documented,
    and newest first would put the output's own prints past the last page read.
    """
    # The uploads are keyed as a run is (`PrintPipeline.record`): an output by its id.
    owner = subject.run_subject
    hashes = await _slice_hashes(client, uploads, owner)
    if not hashes:
        return []
    window = await uploads.sent_between(owner)
    if window is None:
        if made_at is None:
            return []
        window = (made_at, made_at)
    first, last = window
    return await _scan(
        client, links, subject, hashes, (first - SCAN_BEFORE).date(), (last + SCAN_AFTER).date()
    )


async def _scan(
    client: BambuddyClient,
    links: PrintLinkStore,
    subject: PrintSubject,
    hashes: set[str],
    since: date,
    until: date,
) -> list[PrintLink]:
    """Link every archive made from ``since`` to ``until`` whose ``content_hash`` is one
    of ``hashes``. Returns the links it found, new or not."""
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
                await links.record(subject, link)
                found.append(link)
        if len(rows) < ARCHIVE_PAGE:
            break
    else:
        logger.warning(
            "stopped scanning archives for a subject's prints",
            extra={
                "subject": subject.key,
                "pages": MAX_ARCHIVE_PAGES,
                "date_from": since.isoformat(),
                "date_to": until.isoformat(),
            },
        )
    return found


def _claim_library_scan(file_id: int, now: float) -> bool:
    """Claim a library file's hash scan; False when one ran within
    `LIBRARY_HASH_SCAN_INTERVAL`. Entries past it are dropped here."""
    last = _last_library_scan.get(file_id)
    if last is not None and now - last < LIBRARY_HASH_SCAN_INTERVAL:
        return False
    for stale in [
        key for key, at in _last_library_scan.items() if now - at >= LIBRARY_HASH_SCAN_INTERVAL
    ]:
        del _last_library_scan[stale]
    _last_library_scan[file_id] = now
    return True


async def scan_library_by_hash(
    client: BambuddyClient,
    uploads: BambuddyUploadStore,
    links: PrintLinkStore,
    file_id: int,
) -> None:
    """`link_subject_by_hash` for a library file, at most once per process per
    `LIBRARY_HASH_SCAN_INTERVAL` (#1755). Best effort, as an output's scan from the
    progress read is: a failure is logged, never raised."""
    if not _claim_library_scan(file_id, time.monotonic()):
        return
    try:
        await link_subject_by_hash(client, uploads, links, PrintSubject.library(file_id))
    except (ApiError, psycopg.Error):
        # A failed scan found nothing, so the next read may try again.
        _last_library_scan.pop(file_id, None)
        logger.exception(
            "could not scan a library file's prints by hash", extra={"library_file_id": file_id}
        )


def claim_lan_scan(subject: str, now: float) -> bool:
    """Claim a subject's LAN scan (`link_lan_starts`); False when one ran within
    `LAN_SCAN_INTERVAL`. Entries past it are dropped here."""
    last = _last_lan_scan.get(subject)
    if last is not None and now - last < LAN_SCAN_INTERVAL:
        return False
    for stale in [key for key, at in _last_lan_scan.items() if now - at >= LAN_SCAN_INTERVAL]:
        del _last_lan_scan[stale]
    _last_lan_scan[subject] = now
    return True


def release_lan_scan(subject: str) -> None:
    """Give back a claim whose scan failed, so the next read may try again."""
    _last_lan_scan.pop(subject, None)


async def link_lan_starts(
    client: BambuddyClient,
    uploads: BambuddyUploadStore,
    links: PrintLinkStore,
    subject: PrintSubject,
    first: datetime,
    last: datetime,
) -> list[PrintLink]:
    """Link the archives of a subject's LAN starts, made from ``first`` to ``last``
    (#5724). A LAN start has no queue item, and a native run uploads its slice straight
    to the printer, so no Bambuddy library file has the slice's hash. Bambuddy archives
    a print it did not queue from the 3MF it downloads from the printer, and its
    ``content_hash`` is the SHA-256 of that file (``services/archive.py``
    ``archive_print``, ``compute_file_hash``, maziggy/bambuddy@2ce0d93). That file holds
    the bytes the run uploaded (``lan_upload``), which are the slice's blob, and the
    native library row of the slice holds their SHA-256 (``print_sliced_upload``). So
    the hashes are the subject's native slices' (`BambuddyUploadStore.native_slice_hashes`)
    and its Bambuddy slices' (a LAN start from a Bambuddy slice). The window is the
    starts' days, with `SCAN_BEFORE` on each side for a date filter in another time zone:
    Bambuddy makes the archive when the print starts. Returns the links it found, new
    or not."""
    owner = subject.run_subject
    hashes = await uploads.native_slice_hashes(owner) | await _slice_hashes(client, uploads, owner)
    if not hashes:
        return []
    return await _scan(
        client, links, subject, hashes, (first - SCAN_BEFORE).date(), (last + SCAN_BEFORE).date()
    )


async def link_library_prints(
    client: BambuddyClient,
    links: PrintLinkStore,
    *,
    uploads: BambuddyUploadStore | None = None,
) -> None:
    """Link the archives of the library files' queue items that have one now (#976).

    A library-file run has no output, so no progress read follows it the way an
    output's print is linked: the prints list calls this before it reads the links.
    An item is read until it names an archive or Bambuddy is done with it without
    one (it 404s, or it settled first, see `_SETTLED`): then it is marked gone and not
    read again. Only the long `LIBRARY_LINK_BACKSTOP` ages an item out, so a run that
    waits long in the queue, or that nobody lists for a while, is still linked the
    next time the list is opened (#1664, #1703). Any other
    failure, a database one included, is logged and leaves the item to the next call
    (#1662). With ``uploads``, an item that 404s has its file scanned by hash, as an
    output's gone item does (#1755), once: the item is gone, so a scan that fails here
    is tried again only by the file's progress read or its own history. At most
    `LIBRARY_SCANS_PER_CALL` files are scanned per call; a gone item whose file is past
    that is not marked gone, so a later call reads it again and scans then (#2120).
    """
    try:
        pending = await links.pending_library(LIBRARY_LINK_LIMIT, max_age=LIBRARY_LINK_BACKSTOP)
    except psycopg.Error:
        logger.exception("could not read the library prints to link")
        return
    gate = asyncio.Semaphore(LIBRARY_LINK_CONCURRENCY)
    # Each scan pages Bambuddy's archive list: many items gone at once scan a few at a time.
    scans = asyncio.Semaphore(LIBRARY_SCAN_CONCURRENCY)
    # The files this call scans; claimed before an await, so the cap holds under gather.
    scanned: set[int] = set()

    async def link(queue_item_id: int, file_id: int) -> None:
        if await read(queue_item_id, file_id) and uploads is not None:
            async with scans:
                await scan_library_by_hash(client, uploads, links, file_id)

    async def read(queue_item_id: int, file_id: int) -> bool:
        """Link or settle one item; True when Bambuddy no longer has it."""
        async with gate:
            try:
                item = await client.queue_item(queue_item_id)
            except ApiError as error:
                if error.status == status.HTTP_404_NOT_FOUND:
                    if uploads is not None and file_id not in scanned:
                        if len(scanned) >= LIBRARY_SCANS_PER_CALL:
                            # Left pending: the next call reads it again and scans.
                            return False
                        scanned.add(file_id)
                    await links.library_gone(queue_item_id)
                    return True
                logger.warning(
                    "could not read a library print's queue item",
                    extra={"queue_item_id": queue_item_id, "status": error.status},
                )
                return False
            if item.archive_id is not None:
                await links.link_library(queue_item_id, item.archive_id, item.library_file_name)
            elif stage_of(item.status) in _SETTLED:
                # Settled before it was dispatched: it will never name an archive.
                await links.library_gone(queue_item_id)
            return False

    async def guarded(queue_item_id: int, file_id: int) -> None:
        try:
            await link(queue_item_id, file_id)
        except psycopg.Error:
            logger.exception(
                "could not record a library print's link", extra={"queue_item_id": queue_item_id}
            )

    await asyncio.gather(*(guarded(row.queue_item_id, row.library_file_id) for row in pending))
