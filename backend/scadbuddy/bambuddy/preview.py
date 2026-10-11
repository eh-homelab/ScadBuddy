"""The print dialog's background slice (#2169): what the choices on screen would print.

The dialog reslices after a change settles. The slice goes through the run's own path
(:func:`~scadbuddy.bambuddy.print_run.prepare_run` and
:func:`~scadbuddy.bambuddy.print_run.plan_run`): the same copy is laid out and uploaded
(``ensure_uploaded`` reuses one Bambuddy already has), the same presets are resolved,
and the same plate is sliced. Nothing is queued. Each slice is recorded
(``print_preview_slices``), and a run with the same copy and presets queues it rather
than slicing again (:func:`reusable_slice`), so the slice shown is the one printed.

A slice of the same copy and presets that already finished is answered again rather
than sliced twice, so going back to an earlier choice costs nothing.
"""

from __future__ import annotations

import io
import logging
import re
import zipfile
from contextlib import aclosing
from typing import Literal

from fastapi import status
from pydantic import BaseModel, Field

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.dispatch import ReuseSlice, slice_request
from scadbuddy.bambuddy.nozzle_plan import NozzlePlan
from scadbuddy.bambuddy.print_run import (
    PreparedPlates,
    PrintRunRequest,
    plan_run,
    prepare_run,
)
from scadbuddy.bambuddy.print_source import PrintSource
from scadbuddy.bambuddy.uploads import BambuddyUploadStore, PreviewSlice
from scadbuddy.core.problems import ApiError
from scadbuddy.library.settings_store import StoredSettings

logger = logging.getLogger(__name__)

#: A sliced file past this is not read for its filament changes.
MAX_SLICED_BYTES = 128 * 1024 * 1024
#: Bambu's ``change_filament_gcode`` loads each filament with ``M620 S<n>A``; 255 is
#: "no filament" (an unload).
_LOAD = re.compile(rb"^M620 S(\d+)A", re.MULTILINE)


class PreviewStarted(BaseModel):
    """A background slice under way, or one already finished for these choices."""

    job_id: int
    plate_id: int
    nozzle_plan: NozzlePlan | None = None


class PreviewSlot(BaseModel):
    slot_id: int
    grams: float | None = None
    #: The side the slicer put it on: the rack's (right) or the other (left).
    side: Literal["L", "R"] | None = None


class SlicePreview(BaseModel):
    """What a background slice came to, or how far it has got."""

    job_id: int
    status: str
    failure: str | None = None
    print_time_seconds: float | None = None
    filament_used_g: float | None = None
    slots: list[PreviewSlot] = Field(default_factory=list)
    #: Filament loads after the first: each is a purge (``M620 S<n>A`` in the G-code).
    #: ``None`` when the sliced file could not be read.
    filament_changes: int | None = None


async def start_preview(
    client: BambuddyClient,
    source: PrintSource,
    settings: StoredSettings,
    uploads: BambuddyUploadStore,
    request: PrintRunRequest,
) -> PreviewStarted:
    """Lay out, upload and slice what ``request`` would print, as the run does, and
    record the slice; the first plate when every plate prints."""
    prepared = await prepare_run(client, source, settings, request, refuse_manual_pick=False)
    planned = await plan_run(client, source, settings, request, PreparedPlates.of(prepared))
    if not planned.plates:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, "There is no plate to slice.")
    plate = planned.plates[0]
    key = slice_request(plate.plan, plate.plate_id).preset_key
    reused = await reusable_slice(client, uploads)(planned.library_file_id, key)
    if reused is None:
        accepted = await client.slice(
            planned.library_file_id, slice_request(plate.plan, plate.plate_id)
        )
        reused = accepted.job_id
        await uploads.record_preview(
            PreviewSlice(
                job_id=reused,
                library_file_id=planned.library_file_id,
                preset_key=key,
                plate_id=plate.plate_id,
            )
        )
    return PreviewStarted(job_id=reused, plate_id=plate.plate_id, nozzle_plan=planned.nozzle_plan)


def reusable_slice(client: BambuddyClient, uploads: BambuddyUploadStore) -> ReuseSlice:
    """A finished background slice of this copy with these presets whose sliced file
    Bambuddy still has, newest first; ``None`` when there is none (#2169)."""

    async def find(library_file_id: int, preset_key: str) -> int | None:
        for job_id in await uploads.previews_for(library_file_id, preset_key):
            try:
                job = await client.slice_job(job_id)
                sliced = job.result.library_file_id if job.result else None
                if job.status != "completed" or sliced is None:
                    continue
                await client.library_file(sliced)
            except ApiError:
                continue
            return job_id
        return None

    return find


async def read_preview(
    client: BambuddyClient, uploads: BambuddyUploadStore, job_id: int
) -> SlicePreview:
    """How background slice ``job_id`` stands, and once finished, what it came to."""
    preview = await uploads.preview(job_id)
    if preview is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"there is no background slice {job_id}")
    job = await client.slice_job(job_id)
    answer = SlicePreview(job_id=job_id, status=str(job.status), failure=job.failure)
    sliced = job.result.library_file_id if job.result else None
    if job.status != "completed" or job.result is None or sliced is None:
        return answer
    answer.print_time_seconds = job.result.print_time_seconds
    answer.filament_used_g = job.result.filament_used_g
    requirements = await client.filament_requirements(sliced, plate_id=preview.plate_id)
    answer.slots = [
        PreviewSlot(
            slot_id=need.slot_id,
            grams=need.used_grams or None,
            side=None if need.group is None else ("R" if need.group.on_rack else "L"),
        )
        for need in requirements.filaments
        if need.used_in_plate
    ]
    answer.filament_changes = await _filament_changes(client, sliced, preview.plate_id)
    return answer


async def _filament_changes(client: BambuddyClient, sliced: int, plate_id: int) -> int | None:
    """How many times the sliced plate changes filament, read from its G-code."""
    data = bytearray()
    try:
        async with aclosing(client.download_library_file(sliced)) as chunks:
            async for chunk in chunks:
                data += chunk
                if len(data) > MAX_SLICED_BYTES:
                    return None
        with zipfile.ZipFile(io.BytesIO(bytes(data))) as archive:
            names = [
                name
                for name in archive.namelist()
                if re.fullmatch(r"Metadata/plate_\d+\.gcode", name)
            ]
            wanted = f"Metadata/plate_{plate_id}.gcode"
            name = wanted if wanted in names else (names[0] if len(names) == 1 else None)
            if name is None:
                return None
            gcode = archive.read(name)
    except (ApiError, zipfile.BadZipFile, KeyError) as error:
        logger.info("the sliced file could not be read", extra={"error": type(error).__name__})
        return None
    loads = [int(match) for match in _LOAD.findall(gcode) if int(match) != 255]
    return max(len(loads) - 1, 0)
