"""Slicing a pipeline's plate and queueing it against one printer.

**Why this exists at all.** ``filament_overrides`` and ``required_filament_types``
are fields of ``PrintQueueItemCreate`` and of nothing else.
``PipelineRunCreateRequest`` carries ``source_library_file_id`` / ``source_archive_id``
/ ``copies`` / ``force`` — no printer and no filament mapping — and the run's background
task then builds each copy's queue entry from Bambuddy's own defaults. So a print that
names the spools it must use cannot go through ``run``; it has to be sliced and queued.

**The pipeline is not bypassed.** The slice borrows the chosen pipeline's own
``printer_preset``, ``process_preset``, ``bed_type`` and target, so "which pipeline"
still means exactly what it meant on the pipeline path; only the filament presets are
swapped per slot, and only where the chosen spool resolves to a preset Bambuddy knows.
Nothing here invents a slicing setting, which is the same rule the rest of ScadBuddy
follows.

**What is lost by taking this route**, stated so the UI can say it before the click: a
class-targeted pipeline fans out across every printer of the class, and a queue item
does not. Naming the spools therefore also names the printer.
"""

from __future__ import annotations

import logging

from fastapi import status
from pydantic import BaseModel, Field

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.filaments import QueueFilaments
from scadbuddy.bambuddy.models import Pipeline, PresetRef, QueueItemCreate, SliceRequest
from scadbuddy.core.problems import ApiError

logger = logging.getLogger(__name__)


class QueueOutcome(BaseModel):
    """What the slice-and-queue route produced, in the shape the run route reports."""

    slice_job_id: int
    sliced_library_file_id: int
    queue_item_ids: list[int] = Field(default_factory=list)
    printer_id: int | None = None
    target_model: str | None = None


def target_of(pipeline: Pipeline, printer_id: int | None) -> tuple[int | None, str | None]:
    """``(printer_id, target_model)`` — exactly one of the two, as Bambuddy expects.

    An explicit printer wins, because on this route the caller has chosen spools that
    are loaded in one particular machine. With no explicit printer the pipeline's own
    target is used: its printer if it names one, otherwise its model class, which lets
    Bambuddy's scheduler pick as it would have — matching on the overrides it was sent.
    """
    if printer_id is not None:
        return printer_id, None
    if pipeline.target_kind == "specific_printer" and pipeline.target_printer_id is not None:
        return pipeline.target_printer_id, None
    return None, pipeline.target_model_class


async def slice_and_queue(
    client: BambuddyClient,
    *,
    library_file_id: int,
    pipeline: Pipeline,
    printer_id: int | None,
    filament_presets: list[PresetRef],
    filament_colours: list[str],
    filaments: QueueFilaments | None = None,
    plate_id: int = 1,
    copies: int = 1,
) -> QueueOutcome:
    """Slice with the pipeline's presets, wait for it, then queue the result once.

    ``quantity`` rather than one queue item per copy: Bambuddy's queue models repeats
    itself, and N identical items would show up as N rows the user has to cancel one at
    a time.
    """
    if pipeline.printer_preset is None or pipeline.process_preset is None:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            f"pipeline {pipeline.name!r} has no printer or process preset, so its plate "
            "cannot be sliced with the chosen filaments",
        )

    accepted = await client.slice(
        library_file_id,
        SliceRequest(
            printer_preset=pipeline.printer_preset,
            process_preset=pipeline.process_preset,
            filament_presets=filament_presets,
            filament_colours=filament_colours,
            bed_type=pipeline.bed_type,
            plate=plate_id,
        ),
    )
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

    printer, target_model = target_of(pipeline, printer_id)
    item = await client.enqueue(
        QueueItemCreate(
            printer_id=printer,
            target_model=target_model,
            library_file_id=sliced,
            quantity=copies,
            plate_id=plate_id,
            filament_overrides=filaments.filament_overrides if filaments else None,
            required_filament_types=filaments.required_filament_types if filaments else None,
        )
    )
    return QueueOutcome(
        slice_job_id=accepted.job_id,
        sliced_library_file_id=sliced,
        queue_item_ids=[item.id],
        printer_id=printer,
        target_model=target_model,
    )
