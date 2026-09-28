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
  that hash is a print of this output, reprints made inside Bambuddy included.
"""

from __future__ import annotations

import logging
from datetime import date

from fastapi import status

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.models import PipelineRun, QueueItem
from scadbuddy.bambuddy.print_links import PrintLink, PrintLinkStore
from scadbuddy.bambuddy.stages import Stage, stage_of
from scadbuddy.bambuddy.uploads import BambuddyUploadStore
from scadbuddy.core.problems import ApiError
from scadbuddy.library.outputs import OutputMeta

logger = logging.getLogger(__name__)

#: One page of ``GET /archives/``; Bambuddy has no filter by hash, so the scan pages.
ARCHIVE_PAGE = 100
#: How many pages one scan reads at most: a thousand archives since the output was made.
MAX_ARCHIVE_PAGES = 10
#: Pipeline job stages whose queue entry can carry an archive: a copy cancelled
#: mid-print has one too (queue 34 → archive 18 in the spike). Read through the progress
#: read's vocabulary, since the pipeline route's job states were not measured live and
#: Bambuddy may say ``complete`` or ``canceled`` (#522 review).
DISPATCHED_STAGES: frozenset[Stage] = frozenset({"running", "done", "failed", "cancelled"})


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


async def link_run(
    client: BambuddyClient, links: PrintLinkStore, output_id: str, run: PipelineRun
) -> None:
    """A pipeline run's copies reach their archives through their queue entries.

    Only entries not yet linked are read, and only for jobs past dispatch, so a
    settled run costs nothing on later polls.
    """
    linked = await links.linked_queue_items(output_id)
    for job in run.jobs:
        entry = job.queue_entry_id
        if entry is None or entry in linked or stage_of(job.status) not in DISPATCHED_STAGES:
            continue
        try:
            item = await client.queue_item(entry)
        except ApiError as error:
            if error.status != status.HTTP_404_NOT_FOUND:
                raise
            continue
        await link_item(links, output_id, item)


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
    """Link every archive made since the output whose ``content_hash`` is one of the
    output's sliced files. Returns the links it found, new or not."""
    hashes = await _slice_hashes(client, uploads, meta.id)
    if not hashes:
        return []
    since: date = meta.created_at.date()
    found: list[PrintLink] = []
    for page in range(MAX_ARCHIVE_PAGES):
        rows = await client.archives(
            date_from=since, limit=ARCHIVE_PAGE, offset=page * ARCHIVE_PAGE
        )
        for row in rows:
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
            extra={"output_id": meta.id, "pages": MAX_ARCHIVE_PAGES},
        )
    return found
