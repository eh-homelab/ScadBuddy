"""The print dialog's background slice read back from the native slicer (plan
2026-10-16-native-slicer-rust-worker Task 12): a `native_slices` row and its sliced blob,
in the shapes the Bambuddy reads answer (`bambuddy/preview.py`). The routes pick these
by the request's ``?slicer=native``, never by the live setting, so a switch while a
dialog is open cannot misroute, and a native id equal to a Bambuddy row's reads its own
slicer's row (Review Focus 8)."""

from __future__ import annotations

import asyncio
import tempfile
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing, asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, BinaryIO, Literal

from fastapi import Query, status

from scadbuddy.bambuddy.preview import (
    MAX_SLICED_BYTES,
    PreviewSlot,
    SlicePreview,
    cached_toolpath,
)
from scadbuddy.core.problems import ApiError
from scadbuddy.slicing import slice_info
from scadbuddy.slicing.component import NO_CONTENT_STORE, Slicing
from scadbuddy.slicing.start import LOST_AFTER, slice_progress, stop_lost
from scadbuddy.slicing.store import NativeSlice
from scadbuddy.store.content import ContentStore

#: The read routes' ``?slicer=``: the slicer `PreviewStarted` named.
SlicerQuery = Annotated[
    Literal["bambuddy", "native"],
    Query(description="Who sliced it: the `slicer` the start answered with."),
]

#: Completed reads at once, each a sliced 3MF spooled to a file (Task 12 review M4); a
#: read that cannot get a turn within `SLICE_READ_WAIT_SECONDS` is told to come back.
_READING_SLICES = asyncio.Semaphore(2)
SLICE_READ_WAIT_SECONDS = 10.0
#: A completed row never changes, so what its file says is kept, by row and blob.
_READS: OrderedDict[tuple[int, str], slice_info.NativeSliceRead] = OrderedDict()
READS_KEPT = 64


def _content(slicing: Slicing) -> ContentStore:
    """The blob store the slices are in; a named 503 on ``store_backend=local``, where
    a leftover native row's file cannot be read (Task 12 review N-c)."""
    content = slicing.core.store.content
    if content is None:
        raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, NO_CONTENT_STORE)
    return content


async def _row(slicing: Slicing, slice_id: int, subject: str) -> NativeSlice:
    row = await slicing.store.get(slice_id, subject)
    if row is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"there is no background slice {slice_id}")
    return row


@asynccontextmanager
async def _sliced(slicing: Slicing, row: NativeSlice) -> AsyncIterator[BinaryIO]:
    """The sliced 3MF, streamed to a file and never held whole (review M4): 404 once it
    is gone, 422 past `MAX_SLICED_BYTES`."""
    content = _content(slicing)
    stat = await content.stat(row.sliced_blob_key or "")
    if stat is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"background slice {row.id}'s file is gone")
    if stat.ref.size > MAX_SLICED_BYTES:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "the sliced file is too large to read"
        )
    await asyncio.to_thread(content.spool.mkdir, parents=True, exist_ok=True)
    with tempfile.TemporaryFile(dir=content.spool) as body:
        async with aclosing(content.get(stat.ref)) as chunks:
            async for chunk in chunks:
                await asyncio.to_thread(body.write, chunk)
        yield body


async def _in_turn[T](read: Callable[[], Awaitable[T]]) -> T:
    try:
        async with asyncio.timeout(SLICE_READ_WAIT_SECONDS):
            await _READING_SLICES.acquire()
    except TimeoutError:
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE, "other slices are being read; try again"
        ) from None
    try:
        return await read()
    finally:
        _READING_SLICES.release()


async def _summary(slicing: Slicing, row: NativeSlice) -> slice_info.NativeSliceRead:
    key = (row.id, row.sliced_blob_key or "")
    if (cached := _READS.get(key)) is not None:
        _READS.move_to_end(key)
        return cached

    async def read() -> slice_info.NativeSliceRead:
        async with _sliced(slicing, row) as body:
            return await asyncio.to_thread(slice_info.read_sliced, body, row.plate_id)

    found = await _in_turn(read)
    _READS[key] = found
    while len(_READS) > READS_KEPT:
        _READS.popitem(last=False)
    return found


async def read_native_preview(slicing: Slicing, slice_id: int, subject: str) -> SlicePreview:
    """How native slice ``slice_id`` for ``subject`` stands, and once completed, what it
    came to. ``side`` is unknown on native (a follow-up). A running row older than
    `LOST_AFTER` that no execution writes is stopped here as the prune's pass would
    (review M1), so the dialog never polls a lost slice for good."""
    row = await _row(slicing, slice_id, subject)
    if row.status == "running":
        temporal = slicing.temporal()
        if datetime.now(UTC) - row.started_at > LOST_AFTER and await stop_lost(
            temporal, slicing.store, row
        ):
            row = await _row(slicing, slice_id, subject)
        else:
            progress = await slice_progress(temporal, row.workflow_id)
            return SlicePreview(
                job_id=row.id,
                status="running",
                slicer="native",
                progress=None if progress is None else progress.total_percent,
            )
    answer = SlicePreview(job_id=row.id, status=row.status, slicer="native")
    if row.status != "completed":
        answer.failure = row.failure
        return answer
    read = await _summary(slicing, row)
    answer.print_time_seconds = read.print_time_seconds
    answer.slots = [
        PreviewSlot(slot_id=slot_id, grams=grams or None, side=None)
        for slot_id, grams in sorted(read.grams_by_slot.items())
    ]
    answer.filament_used_g = read.filament_used_g
    answer.filament_changes = read.filament_changes
    answer.warnings = row.warnings
    return answer


async def read_native_toolpath(slicing: Slicing, slice_id: int, subject: str) -> bytes:
    """As `bambuddy.preview.read_toolpath`: 404 for no such slice, 409 until it has
    completed, 422 when its G-code cannot be read. Cached under a key no Bambuddy entry
    can equal (a Bambuddy one names its sliced library file, never 0); the file is read
    under the toolpaths' own turns."""
    row = await _row(slicing, slice_id, subject)
    if row.status != "completed":
        raise ApiError(status.HTTP_409_CONFLICT, f"background slice {slice_id} has not finished")

    async def fetch() -> bytes | None:
        async with _sliced(slicing, row) as body:
            return await asyncio.to_thread(slice_info.plate_gcode, body, row.plate_id)

    return await cached_toolpath((row.id, 0, row.sliced_blob_key, row.plate_id), fetch)
