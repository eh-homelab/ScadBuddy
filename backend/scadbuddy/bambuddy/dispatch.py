"""Slicing one plate with resolved presets and queueing it against one printer.

**Why this exists at all.** ``filament_overrides``, ``required_filament_types`` and
``nozzle_rack_choice`` (#836) are fields of ``PrintQueueItemCreate`` and of nothing else.
``PipelineRunCreateRequest`` carries ``source_library_file_id`` / ``source_archive_id``
/ ``copies`` / ``force`` — no printer and no filament mapping — and the run's background
task then builds each copy's queue entry from Bambuddy's own defaults. So a print that
names the spools it must use cannot go through ``run``; it has to be sliced and queued.

Presets come from the resolver (spec 2026-09-27 §4), which supersedes the earlier rule
that the slice borrows a pipeline's presets. This module invents none: a
:class:`SlicePlan` arrives with every preset already chosen.

A queue item names one printer, so there is no fan-out across a printer class on this
route. The spools are loaded in one machine, which is the printer the item goes to.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from fastapi import status
from pydantic import BaseModel, Field

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.filaments import QueueFilaments
from scadbuddy.bambuddy.models import PresetRef, QueueItemCreate, SliceRequest
from scadbuddy.bambuddy.options import PrintOptions
from scadbuddy.core.problems import ApiError
from scadbuddy.rack.usage import PickedHotend

logger = logging.getLogger(__name__)


class QueueOutcome(BaseModel):
    """What the slice-and-queue route produced, in the shape the run route reports."""

    slice_job_id: int
    sliced_library_file_id: int
    #: :attr:`SliceRequest.preset_key` of the slice, recorded with the sliced file (#316).
    preset_key: str | None = None
    queue_item_ids: list[int] = Field(default_factory=list)
    printer_id: int
    #: What ``choose_rack`` picked, with each hotend's serial, for ``print_run.py`` to
    #: record (spec 2026-10-01 §5). Never reported: ``_queued`` builds the run's result
    #: from named fields and never dumps this model (spec §6, §7).
    rack_picks: list[PickedHotend] = Field(default_factory=list)


class RackChoice(BaseModel):
    """What ``choose_rack`` answers: the queue field, keyed by stringified filament group
    id, and the picks behind it (spec 2026-10-01 §5)."""

    nozzle_rack_choice: dict[str, int]
    picks: list[PickedHotend] = Field(default_factory=list)


class SlicePlan(BaseModel):
    """Everything the slice needs, already resolved (spec 2026-09-27 §4)."""

    printer_preset: PresetRef
    process_preset: PresetRef
    filament_presets: list[PresetRef]
    filament_colours: list[str]
    bed_type: str
    #: The template's ``print_settings`` (#770), written over the process preset.
    #: Accepted analyzer fixes are not consumed by a print yet (``api/analyzers.py``);
    #: when they are, an explicit fix goes over these.
    process_overrides: dict[str, str] = Field(default_factory=dict)


async def slice_and_queue(
    client: BambuddyClient,
    *,
    library_file_id: int,
    plan: SlicePlan,
    printer_id: int,
    filaments: QueueFilaments | None = None,
    plate_id: int = 1,
    copies: int = 1,
    project_id: int | None = None,
    options: PrintOptions | None = None,
    before_enqueue: Callable[[], Awaitable[None]] | None = None,
    choose_rack: Callable[[int], Awaitable[RackChoice | None]] | None = None,
) -> QueueOutcome:
    """Slice ``plate_id`` with ``plan``, wait for it, then queue the result once.

    ``quantity`` rather than one queue item per copy: Bambuddy's queue models repeats
    itself, and N identical items would show up as N rows the user has to cancel one at
    a time.

    ``plan.bed_type`` goes on the slice, not the queue item: the item has no such field,
    and the slice is what sets the first layer for the plate (#83).

    ``options`` are the resolved print options (#88); ``copies`` and ``project_id``
    still win over the quantity and project they carry, because those two are what
    the caller asked for on this request.

    ``before_enqueue`` is awaited just before ``POST /queue/``: past it, the print may
    be on the queue whatever this raises (#470).

    ``choose_rack`` is awaited with the sliced file's id after the slice and before
    ``before_enqueue``; its choice goes on the item and its picks on the outcome. It is
    awaited bare, like ``before_enqueue``: the callback is what never raises (spec
    2026-10-01 §5), so a ``try`` here would hide a broken one.
    """
    started = await start_slice(
        client, library_file_id=library_file_id, plan=plan, plate_id=plate_id
    )
    sliced = await wait_slice(client, started.job_id)
    rack = await choose_rack(sliced) if choose_rack is not None else None
    if before_enqueue is not None:
        await before_enqueue()
    item = await enqueue_plate(
        client,
        sliced=sliced,
        printer_id=printer_id,
        filaments=filaments,
        plate_id=plate_id,
        copies=copies,
        project_id=project_id,
        options=options,
        nozzle_rack_choice=rack.nozzle_rack_choice if rack is not None else None,
    )
    return QueueOutcome(
        slice_job_id=started.job_id,
        sliced_library_file_id=sliced,
        preset_key=started.preset_key,
        queue_item_ids=[item],
        printer_id=printer_id,
        rack_picks=list(rack.picks) if rack is not None else [],
    )


class SliceStarted(BaseModel):
    """A slice Bambuddy accepted: its job, and the key the sliced file is recorded by."""

    job_id: int
    preset_key: str | None = None


async def start_slice(
    client: BambuddyClient, *, library_file_id: int, plan: SlicePlan, plate_id: int = 1
) -> SliceStarted:
    """``POST /library/files/{id}/slice``. Every call starts a **new** job, so the
    workflow runs this once (spec 2026-10-01 §5.3, ``maximum_attempts = 1``)."""
    request = SliceRequest(
        printer_preset=plan.printer_preset,
        process_preset=plan.process_preset,
        filament_presets=plan.filament_presets,
        filament_colours=plan.filament_colours,
        bed_type=plan.bed_type,
        plate=plate_id,
        process_overrides=plan.process_overrides or None,
    )
    accepted = await client.slice(library_file_id, request)
    return SliceStarted(job_id=accepted.job_id, preset_key=request.preset_key)


async def wait_slice(client: BambuddyClient, job_id: int) -> int:
    """Wait for slice job ``job_id``; the sliced file's id. Reading a job again is
    safe, so this may be retried."""
    job = await client.await_slice(job_id)
    failure = job.failure
    if failure is not None:
        # Bambuddy's own words, not a paraphrase: the slicer's message is what tells the
        # user whether to change the mapping, the plate or the model.
        raise ApiError(
            status.HTTP_502_BAD_GATEWAY,
            f"Bambuddy failed to slice the plate: {failure}",
            slice_job_id=job_id,
        )
    sliced = job.result.library_file_id if job.result else None
    if sliced is None:
        raise ApiError(
            status.HTTP_502_BAD_GATEWAY,
            f"Bambuddy slice job {job_id} completed without a sliced file",
            slice_job_id=job_id,
        )
    return sliced


async def enqueue_plate(
    client: BambuddyClient,
    *,
    sliced: int,
    printer_id: int,
    plate_id: int,
    copies: int,
    project_id: int | None,
    options: PrintOptions | None,
    filaments: QueueFilaments | None = None,
    nozzle_rack_choice: dict[str, int] | None = None,
) -> int:
    """``POST /queue/`` for one sliced plate, once; the item's id. Past this call the
    print may be on the queue whatever it raised (#470), so it is never retried.
    ``nozzle_rack_choice`` is the rack pick (#836), by stringified filament group id."""
    remembered = options.queue_fields() if options is not None else {}
    remembered.pop("quantity", None)
    remembered.pop("project_id", None)
    item = await client.enqueue(
        QueueItemCreate(
            **remembered,
            printer_id=printer_id,
            library_file_id=sliced,
            quantity=copies,
            plate_id=plate_id,
            filament_overrides=filaments.filament_overrides if filaments else None,
            required_filament_types=filaments.required_filament_types if filaments else None,
            # On this route the project can ride on the item itself, so there is no
            # window in which the entry exists unfiled (#79).
            project_id=project_id,
            nozzle_rack_choice=nozzle_rack_choice,
        )
    )
    return item.id
