"""The print dialog's background slice (#2169): what the choices on screen would print.

The dialog reslices after a change settles. The slice goes through the run's own path
(:func:`~scadbuddy.bambuddy.print_run.prepare_run` and
:func:`~scadbuddy.bambuddy.print_run.plan_run`): the same copy is laid out and uploaded
(``ensure_uploaded`` reuses one Bambuddy already has), the same presets are resolved,
and the same plate is sliced. Nothing is queued. Each slice is recorded
(``print_preview_slices``), and a run with the same copy and presets queues it rather
than slicing again (:func:`reusable_slice`), so the slice shown is the one printed.

A slice of the same copy and presets that already finished is answered again rather
than sliced twice, so going back to an earlier choice costs nothing. Bambuddy's job ids
are not durable (they restart from 1 with Bambuddy), so a recorded job is reused or read
only while Bambuddy still says it slices the recorded file, was created when it was,
and made a sliced file that is still there under the name it made it with.
"""

from __future__ import annotations

import io
import logging
import re
import zipfile
from contextlib import aclosing
from datetime import timedelta
from typing import Literal

from fastapi import status
from pydantic import BaseModel, Field

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.dispatch import ReuseSlice, slice_request
from scadbuddy.bambuddy.models import SliceJob
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
    *,
    subject: str,
) -> PreviewStarted:
    """Lay out, upload and slice what ``request`` would print, as the run does, and
    record the slice for ``subject``; the first plate when every plate prints.

    The copy goes to the inbox even when a project is chosen: the dialog slices on every
    change, and a copy filed in a project's folder is that project's record, never
    removed (#317), so trying spools would fill the project. A print into the project
    then slices its own copy."""
    request = request.model_copy(update={"project_id": None})
    prepared = await prepare_run(client, source, settings, request, refuse_manual_pick=False)
    planned = await plan_run(client, source, settings, request, PreparedPlates.of(prepared))
    if not planned.plates:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, "There is no plate to slice.")
    plate = planned.plates[0]
    key = slice_request(plate.plan, plate.plate_id).preset_key
    job_id = await reusable_slice(client, uploads)(planned.library_file_id, key)
    if job_id is None:
        accepted = await client.slice(
            planned.library_file_id, slice_request(plate.plan, plate.plate_id)
        )
        job_id = accepted.job_id
        # What Bambuddy says of the job now, so a job of the same id after a restart of
        # Bambuddy's is never taken for this one.
        job = await client.slice_job(job_id)
        await uploads.record_preview(
            PreviewSlice(
                job_id=job_id,
                subject=subject,
                library_file_id=planned.library_file_id,
                preset_key=key,
                plate_id=plate.plate_id,
                job_created=job.created_at,
            )
        )
    return PreviewStarted(job_id=job_id, plate_id=plate.plate_id, nozzle_plan=planned.nozzle_plan)


def _same_job(job: SliceJob, row: PreviewSlice) -> bool:
    """Whether Bambuddy's job is still the slice ``row`` recorded. Bambuddy keeps its
    jobs in memory and numbers them from 1 again after a restart
    (``backend/app/api/routes/slice_jobs.py``: "the in-memory slice-job dispatcher"),
    so the id alone does not say: the file it slices and the time Bambuddy created it
    must be the ones recorded."""
    return (
        row.job_created is not None
        and job.created_at == row.job_created
        and job.source_id == row.library_file_id
    )


async def _still_sliced(
    client: BambuddyClient, uploads: BambuddyUploadStore, row: PreviewSlice
) -> SliceJob | None:
    """Bambuddy's job for ``row`` while it is still that slice, and once completed, its
    sliced file still the one it made; else ``None``, and a row that no longer
    describes its job is retired, for :func:`sweep_preview_slices`."""
    try:
        job = await client.slice_job(row.job_id)
    except ApiError:
        # Expired or gone with a restart: nothing to reuse or read.
        await uploads.retire_preview(row.id or 0)
        return None
    if not _same_job(job, row):
        await uploads.retire_preview(row.id or 0)
        return None
    if job.status != "completed":
        return job
    sliced = job.result.library_file_id if job.result else None
    if sliced is None or (row.sliced_file_id is not None and sliced != row.sliced_file_id):
        await uploads.retire_preview(row.id or 0)
        return None
    try:
        made = await client.library_file(sliced)
    except ApiError:
        # The sliced file was deleted.
        await uploads.retire_preview(row.id or 0)
        return None
    name = job.result.name if job.result else None
    if name is not None and made.filename != name:
        # Another file under the id of the one the job made.
        await uploads.retire_preview(row.id or 0)
        return None
    if row.sliced_file_id is None and row.id is not None:
        await uploads.preview_sliced(row.id, sliced, name)
    return job


def reusable_slice(
    client: BambuddyClient, uploads: BambuddyUploadStore, *, printing: bool = False
) -> ReuseSlice:
    """A finished background slice of this copy with these presets that Bambuddy's
    job still describes and whose sliced file it still has, newest first; ``None``
    when there is none (#2169), and the run slices again. ``printing``: a run queues
    it, so it is marked printed, and the sweep never removes its sliced file."""

    async def find(library_file_id: int, preset_key: str) -> int | None:
        for row in await uploads.previews_for(library_file_id, preset_key):
            job = await _still_sliced(client, uploads, row)
            if job is None or job.status != "completed":
                continue
            if printing and not await uploads.claim_preview(row.id or 0):
                continue
            return row.job_id
        return None

    return find


async def read_preview(
    client: BambuddyClient, uploads: BambuddyUploadStore, job_id: int, subject: str
) -> SlicePreview:
    """How background slice ``job_id`` for ``subject`` stands, and once finished,
    what it came to."""
    row = await uploads.preview(job_id, subject)
    job = await _still_sliced(client, uploads, row) if row is not None else None
    if row is None or job is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"there is no background slice {job_id}")
    answer = SlicePreview(job_id=job_id, status=str(job.status), failure=job.failure)
    sliced = job.result.library_file_id if job.result else None
    if job.status != "completed" or job.result is None or sliced is None:
        return answer
    answer.print_time_seconds = job.result.print_time_seconds
    answer.filament_used_g = job.result.filament_used_g
    requirements = await client.filament_requirements(sliced, plate_id=row.plate_id)
    answer.slots = [
        PreviewSlot(
            slot_id=need.slot_id,
            grams=need.used_grams or None,
            side=None if need.group is None else ("R" if need.group.on_rack else "L"),
        )
        for need in requirements.filaments
        if need.used_in_plate
    ]
    answer.filament_changes = await _filament_changes(client, sliced, row.plate_id)
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


#: How long a background slice no run queued is kept: a dialog left open that long
#: slices again when its print starts.
PREVIEW_GRACE = timedelta(hours=6)


async def sweep_preview_slices(
    client: BambuddyClient, uploads: BambuddyUploadStore, *, grace: timedelta = PREVIEW_GRACE
) -> int:
    """Remove from Bambuddy's library the sliced files of background slices no run
    queued, once retired or past ``grace``, with their rows; a printed one's row only
    (its file is the print's). A file is removed only while it still carries the name
    its job gave it, so nothing else under a reused id goes. How many were removed."""
    removed = 0
    for row in await uploads.previews_to_sweep(grace):
        if row.id is None:
            continue
        if row.printed:
            await uploads.forget_preview(row.id)
            continue
        if not await uploads.take_preview(row.id):
            continue  # a run claimed it meanwhile
        sliced, name = row.sliced_file_id, row.sliced_name
        if sliced is None and not row.retired:
            try:
                job = await client.slice_job(row.job_id)
            except ApiError:
                continue
            if _same_job(job, row) and job.status == "completed" and job.result is not None:
                sliced, name = job.result.library_file_id, job.result.name
        if sliced is None or name is None:
            continue
        try:
            made = await client.library_file(sliced)
        except ApiError:
            continue
        if made.filename != name:
            continue
        await client.delete_library_file(sliced)
        removed += 1
    return removed
