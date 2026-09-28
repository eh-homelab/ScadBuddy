"""Slicing one plate with resolved presets and queueing it against one printer.

**Why this exists at all.** ``filament_overrides`` and ``required_filament_types``
are fields of ``PrintQueueItemCreate`` and of nothing else.
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

from fastapi import status
from pydantic import BaseModel, Field

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.filaments import QueueFilaments
from scadbuddy.bambuddy.models import PresetRef, QueueItemCreate, SliceRequest
from scadbuddy.bambuddy.options import PrintOptions
from scadbuddy.core.problems import ApiError

logger = logging.getLogger(__name__)


class QueueOutcome(BaseModel):
    """What the slice-and-queue route produced, in the shape the run route reports."""

    slice_job_id: int
    sliced_library_file_id: int
    #: :attr:`SliceRequest.preset_key` of the slice, recorded with the sliced file (#316).
    preset_key: str | None = None
    queue_item_ids: list[int] = Field(default_factory=list)
    printer_id: int


class SlicePlan(BaseModel):
    """Everything the slice needs, already resolved (spec 2026-09-27 §4)."""

    printer_preset: PresetRef
    process_preset: PresetRef
    filament_presets: list[PresetRef]
    filament_colours: list[str]
    bed_type: str


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
    """
    request = SliceRequest(
        printer_preset=plan.printer_preset,
        process_preset=plan.process_preset,
        filament_presets=plan.filament_presets,
        filament_colours=plan.filament_colours,
        bed_type=plan.bed_type,
        plate=plate_id,
    )
    accepted = await client.slice(library_file_id, request)
    job = await client.await_slice(accepted.job_id)
    failure = job.failure
    if failure is not None:
        # Bambuddy's own words, not a paraphrase: the slicer's message is what tells the
        # user whether to change the mapping, the plate or the model.
        raise ApiError(
            status.HTTP_502_BAD_GATEWAY,
            f"Bambuddy failed to slice the plate: {failure}",
            slice_job_id=accepted.job_id,
        )
    sliced = job.result.library_file_id if job.result else None
    if sliced is None:
        raise ApiError(
            status.HTTP_502_BAD_GATEWAY,
            f"Bambuddy slice job {accepted.job_id} completed without a sliced file",
            slice_job_id=accepted.job_id,
        )

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
        )
    )
    return QueueOutcome(
        slice_job_id=accepted.job_id,
        sliced_library_file_id=sliced,
        preset_key=request.preset_key,
        queue_item_ids=[item.id],
        printer_id=printer_id,
    )
