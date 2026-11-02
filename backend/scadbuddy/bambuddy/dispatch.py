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
from scadbuddy.bambuddy.errors import UNAVAILABLE_PROBLEM
from scadbuddy.bambuddy.filaments import QueueFilaments
from scadbuddy.bambuddy.models import PresetRef, ProcessValue, QueueItemCreate, SliceRequest
from scadbuddy.bambuddy.options import PrintOptions
from scadbuddy.core.problems import ApiError
from scadbuddy.printers.lan_models import LanStarted
from scadbuddy.rack.usage import PickedHotend

logger = logging.getLogger(__name__)


class QueueOutcome(BaseModel):
    """What the slice-and-queue route produced, in the shape the run route reports."""

    #: ``None`` for a plate queued with no slice (a sliced file, #2671), and for a
    #: native slice: a Bambuddy job id only, never a `native_slices` row's.
    slice_job_id: int | None = None
    #: The `native_slices` row a native slice was (plan 2026-10-16 Task 13), and the
    #: blob it sliced to, uploaded as ``sliced_library_file_id``.
    native_slice_id: int | None = None
    sliced_blob_key: str | None = None
    sliced_library_file_id: int
    #: :attr:`SliceRequest.preset_key` of the slice, recorded with the sliced file (#316).
    preset_key: str | None = None
    queue_item_ids: list[int] = Field(default_factory=list)
    printer_id: int
    #: What ``choose_rack`` picked, with each hotend's serial, for ``print_run.py`` to
    #: record (spec 2026-10-01 §5). Never reported: ``_queued`` builds the run's result
    #: from named fields and never dumps this model (spec §6, §7).
    rack_picks: list[PickedHotend] = Field(default_factory=list)
    #: A plate started over the LAN (plan 2026-10-16 Task 9), with no queue item: the
    #: native follow reads it from the printer's report.
    lan: LanStarted | None = None


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
    #: Accepted analyzer fixes never land here yet: their process-level target
    #: (``derived_process_preset``) is unverified (``analyzers/model.py``); the verified
    #: ones change the run's request (``analyzers/apply.py``). Once one does, an explicit
    #: fix goes over these.
    process_overrides: dict[str, ProcessValue] = Field(default_factory=dict)
    #: Filament settings by slot (from 1), written into that slot's filament file: a
    #: filament profile's nozzle temperature (#2799). Only the native slicer has the
    #: channel (``slicing/presets.py``); Bambuddy's ``SliceRequest`` has no filament
    #: field, so :func:`slice_request` never sends them, and a plan for Bambuddy's slicer
    #: carries none. Defaults empty, so a plan in a workflow's history reads as before.
    filament_overrides: dict[int, dict[str, str]] = Field(default_factory=dict)
    #: Slice the file as a whole (``plate: 0``) rather than by its plate number: set
    #: for a library file of one plate (#2180). Naming a plate makes Bambuddy repaint
    #: any slot it thinks that plate leaves unused, and it cannot see colour painted
    #: inline on a mesh, so queue 268's two-colour file sliced in one colour. The
    #: queue item still names the plate. Defaults off, so a plan in a workflow's
    #: history from before it reads as before.
    whole_file: bool = False


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


#: How Bambuddy words a slice job its slicer sidecar never answered: its
#: ``SlicerApiUnavailableError``, raised on a transport error to the sidecar (a sidecar
#: OOM-killed, evicted or mid-rollout), reported on the job as ``error_status`` 502.
SIDECAR_UNREACHABLE = "Slicer sidecar"


class SliceLostError(ApiError):
    """A slice that left nothing behind, so it may be started again (#2081): the
    sidecar died under it, Bambuddy forgot it (a restart: its jobs live in memory), or
    Bambuddy could not be reached to start it. A second slice of the same plate costs
    only slicer time; what must happen once is the enqueue, which a run does once
    whatever its slices did."""

    @classmethod
    def of(cls, error: ApiError) -> SliceLostError:
        lost = cls(
            error.status, error.detail, title=error.title, type_=error.type, **error.extensions
        )
        lost.upstream = error.upstream
        return lost


def _start_lost(error: ApiError) -> bool:
    """A start Bambuddy never answered, or answered with a server error: a later
    attempt may meet a Bambuddy (and a sidecar) that is up. Any other 4xx is the
    request's, and would be refused again."""
    answered = error.extensions.get("bambuddy_status")
    return error.type == UNAVAILABLE_PROBLEM and (
        answered is None
        or answered >= status.HTTP_500_INTERNAL_SERVER_ERROR
        or answered == status.HTTP_429_TOO_MANY_REQUESTS
    )


class SliceStarted(BaseModel):
    """A slice Bambuddy accepted: its job, and the key the sliced file is recorded by."""

    job_id: int
    preset_key: str | None = None


#: A finished slice of this copy with these presets, if one may be queued as it is:
#: called with the copy's id and :attr:`SliceRequest.preset_key` (#2169).
ReuseSlice = Callable[[int, str], Awaitable[int | None]]


async def start_slice(
    client: BambuddyClient,
    *,
    library_file_id: int,
    plan: SlicePlan,
    plate_id: int = 1,
    reuse: ReuseSlice | None = None,
) -> SliceStarted:
    """``POST /library/files/{id}/slice``. Every call starts a **new** job, so the
    workflow runs this once (spec 2026-10-01 §5.3, ``maximum_attempts = 1``).

    ``reuse`` names a finished job of the same copy and presets, which the print
    dialog's preview sliced (#2169): its job is answered instead, and nothing is
    sliced again, so the slice the dialog showed is the one that prints."""
    request = slice_request(plan, plate_id)
    if reuse is not None:
        job_id = await reuse(library_file_id, request.preset_key)
        if job_id is not None:
            return SliceStarted(job_id=job_id, preset_key=request.preset_key)
    try:
        accepted = await client.slice(library_file_id, request)
    except ApiError as error:
        if _start_lost(error):
            raise SliceLostError.of(error) from None
        raise
    return SliceStarted(job_id=accepted.job_id, preset_key=request.preset_key)


def slice_request(plan: SlicePlan, plate_id: int) -> SliceRequest:
    """What :func:`start_slice` posts for ``plan`` and ``plate_id``."""
    return SliceRequest(
        printer_preset=plan.printer_preset,
        process_preset=plan.process_preset,
        filament_presets=plan.filament_presets,
        filament_colours=plan.filament_colours,
        bed_type=plan.bed_type,
        plate=0 if plan.whole_file else plate_id,
        process_overrides=plan.process_overrides or None,
    )


async def wait_slice(client: BambuddyClient, job_id: int) -> int:
    """Wait for slice job ``job_id``; the sliced file's id. Reading a job again is
    safe, so this may be retried. A job the sidecar died under, or one Bambuddy no
    longer has, is :class:`SliceLostError` (#2081). A job still running at the deadline
    is not: it may yet finish, and a second slice would wait behind it."""
    try:
        job = await client.await_slice(job_id)
    except ApiError as error:
        if error.status == status.HTTP_404_NOT_FOUND:
            raise SliceLostError(
                status.HTTP_502_BAD_GATEWAY,
                f"Bambuddy no longer has slice job {job_id}; it restarted while slicing",
                type_=UNAVAILABLE_PROBLEM,
                slice_job_id=job_id,
            ) from None
        raise
    failure = job.failure
    if (
        failure is not None
        and job.error_status == status.HTTP_502_BAD_GATEWAY
        and (failure.startswith(SIDECAR_UNREACHABLE))
    ):
        raise SliceLostError(
            status.HTTP_502_BAD_GATEWAY,
            f"Bambuddy failed to slice the plate: {failure}",
            type_=UNAVAILABLE_PROBLEM,
            slice_job_id=job_id,
        )
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
