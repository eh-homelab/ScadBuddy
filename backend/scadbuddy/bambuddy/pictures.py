"""The picture of each Up next row and Now printing card on /prints (#3742).

A job ScadBuddy queued or started (``scadbuddy``, #3741) shows ScadBuddy's own picture:
its output's thumbnail (the viewer's capture, or plate 1's cover for an arranged
output), or its library file's. Any other job shows Bambuddy's thumbnail through
ScadBuddy's farm proxy (`api/farm.py`): the queue item's library file's, else its
archive's, and a printer's cover of the job it prints. The URLs are built from fields
the polled read already has, so a poll asks Bambuddy nothing more. Each image is a
request of its own, which the browser can cache. ``None`` is no picture: the page shows
a placeholder.
"""

from __future__ import annotations

from urllib.parse import quote, urlencode

from scadbuddy.bambuddy.models import QueueItem
from scadbuddy.bambuddy.print_links import ScadBuddyJob

#: The farm's image proxy (`api/farm.py`).
FARM = "/api/v1/farm"


def scadbuddy_picture(job: ScadBuddyJob) -> str | None:
    """ScadBuddy's own picture of its job: the output's thumbnail, else the library
    file's, from the library that serves (`api/library_print.py`)."""
    if job.output_id is not None:
        return f"/api/v1/outputs/{quote(job.output_id, safe='')}/thumbnail"
    if job.library_file_id is not None:
        return f"/api/v1/print/library/{job.library_file_id}/thumbnail"
    return None


def queue_item_picture(item: QueueItem, job: ScadBuddyJob | None) -> str | None:
    """An Up next row's picture: ScadBuddy's for its own job, else Bambuddy's thumbnail
    of the item's library file, else of its archive, where Bambuddy has one."""
    ours = scadbuddy_picture(job) if job is not None else None
    if ours is not None:
        return ours
    if item.library_file_id is not None and item.library_file_thumbnail:
        return f"{FARM}/library/{item.library_file_id}/thumbnail"
    if item.archive_id is not None and item.archive_thumbnail and not item.archive_deleted:
        return f"{FARM}/archives/{item.archive_id}/thumbnail"
    return None


def printer_cover(printer_id: int, archive_id: int | None, job_name: str | None) -> str:
    """Bambuddy's cover of the job a printer prints, keyed for the browser's cache.

    The job's archive keys it: Bambuddy makes one archive per print and its status names
    it while the job runs or is paused (``current_archive_id``, maziggy/bambuddy f9080d1
    ``backend/app/api/routes/printers.py`` lines 730-748), so the browser's copy of one
    print's cover is never shown for another. Job names repeat (every keychain variant
    prints as ``Name_Keychain_Font_Basic_Plate_1``), so with no archive the name only
    changes the URL when the job's name changes, and the route lets no browser keep that
    cover (`api/farm.py` `get_printer_cover`); with neither, nothing keys it."""
    url = f"{FARM}/printers/{printer_id}/cover"
    if archive_id is not None:
        return f"{url}?{urlencode({'archive': archive_id})}"
    return f"{url}?{urlencode({'job': job_name})}" if job_name else url
