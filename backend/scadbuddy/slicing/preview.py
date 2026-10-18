"""The print dialog's background slice read back from the native slicer (plan
2026-10-16-native-slicer-rust-worker Task 12): a `native_slices` row and its sliced blob,
in the shapes the Bambuddy reads answer (`bambuddy/preview.py`). The routes pick these
by the request's ``?slicer=native``, never by the live setting, so a switch while a
dialog is open cannot misroute, and a native id equal to a Bambuddy row's reads its own
slicer's row (Review Focus 8)."""

from __future__ import annotations

import asyncio
from typing import Annotated, Literal

from fastapi import Query, status

from scadbuddy.bambuddy.preview import (
    MAX_SLICED_BYTES,
    PreviewSlot,
    SlicePreview,
    _toolpath,
)
from scadbuddy.core.problems import ApiError
from scadbuddy.slicing import slice_info
from scadbuddy.slicing.component import Slicing
from scadbuddy.slicing.start import slice_progress
from scadbuddy.slicing.store import NativeSlice

#: The read routes' ``?slicer=``: the slicer `PreviewStarted` named.
SlicerQuery = Annotated[
    Literal["bambuddy", "native"],
    Query(description="Who sliced it: the `slicer` the start answered with."),
]


async def _row(slicing: Slicing, slice_id: int, subject: str) -> NativeSlice:
    row = await slicing.store.get(slice_id, subject)
    if row is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"there is no background slice {slice_id}")
    return row


async def _sliced(slicing: Slicing, row: NativeSlice) -> bytes:
    """The sliced 3MF, whole: 404 once it is gone, 422 past `MAX_SLICED_BYTES`."""
    content = slicing.transfers().content
    stat = await content.stat(row.sliced_blob_key or "")
    if stat is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"background slice {row.id}'s file is gone")
    if stat.ref.size > MAX_SLICED_BYTES:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "the sliced file is too large to read"
        )
    return await content.read(stat.ref)


async def read_native_preview(slicing: Slicing, slice_id: int, subject: str) -> SlicePreview:
    """How native slice ``slice_id`` for ``subject`` stands, and once completed, what it
    came to. ``side`` is unknown on native (a follow-up)."""
    row = await _row(slicing, slice_id, subject)
    answer = SlicePreview(job_id=row.id, status=row.status, slicer="native")
    if row.status == "running":
        progress = await slice_progress(slicing.temporal(), row.workflow_id)
        answer.progress = None if progress is None else progress.total_percent
        return answer
    if row.status != "completed":
        answer.failure = row.failure
        return answer
    read = await asyncio.to_thread(
        slice_info.read_sliced, await _sliced(slicing, row), row.plate_id
    )
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
    can equal (a Bambuddy one names its sliced library file, never 0)."""
    row = await _row(slicing, slice_id, subject)
    if row.status != "completed":
        raise ApiError(status.HTTP_409_CONFLICT, f"background slice {slice_id} has not finished")

    async def fetch() -> bytes | None:
        payload = await _sliced(slicing, row)
        return await asyncio.to_thread(slice_info.plate_gcode, payload, row.plate_id)

    return await _toolpath((row.id, 0, row.sliced_blob_key, row.plate_id), fetch)
