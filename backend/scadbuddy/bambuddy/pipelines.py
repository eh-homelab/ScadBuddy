"""Slicing an output for print: the filament step's options, and the spool-first run.

ScadBuddy owns no slicing settings. The print dialog's run (:func:`run_for_output`)
derives every preset from the dialog's choices — spools, nozzles, quality and plate
(spec 2026-09-27 §4) — and always slices then queues; there is no pipeline to run or
choose from here. Bambuddy's own slicer pipelines are still what the send bar runs
(``scadbuddy.bambuddy.send``), which is unaffected by this module.
"""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import status
from pydantic import BaseModel, Field

from scadbuddy.bambuddy.catalogue import _catalogue
from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.dispatch import QueueOutcome, SlicePlan, slice_and_queue
from scadbuddy.bambuddy.errors import not_configured
from scadbuddy.bambuddy.filaments import (
    FilamentOptions,
    FilamentPlan,
    FilamentWarning,
    across_plates,
    check,
    every_plate,
    gather_options,
    normalise_colour,
    queue_filaments,
)
from scadbuddy.bambuddy.hardware import (
    installed_nozzles,
    last_bed_type,
    nozzle_warning,
    plate_warning,
)
from scadbuddy.bambuddy.options import PrintOptions
from scadbuddy.bambuddy.projects import folder_for
from scadbuddy.bambuddy.resolver import (
    PRINTER_MODEL,
    PrintChoices,
    Resolved,
    choice_errors,
    resolve,
)
from scadbuddy.bambuddy.send import (
    copy_to_read,
    ensure_uploaded,
    request_scope,
    resolve_print_options,
    target_for,
)
from scadbuddy.bambuddy.uploads import BambuddyUploadStore, SlicedCopy
from scadbuddy.core.problems import ApiError
from scadbuddy.library.outputs import MODEL_NAME, OutputMeta, OutputStore, PlateSend
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.render.bambu3mf import plates_of

logger = logging.getLogger(__name__)

QUEUE_PATH = "/queue"

#: Bambuddy documents these in ``SliceRequest.bed_type``'s own description as the
#: canonical BambuStudio / OrcaSlicer values. The field itself is a free 64-character
#: string, not an enum, so this is a menu rather than a validation rule — an unknown
#: value a pipeline already carries is still shown as it stands.
BED_TYPES: tuple[str, ...] = (
    "Cool Plate",
    "Cool Plate (SuperTack)",
    "Supertack Plate",
    "Engineering Plate",
    "High Temp Plate",
    "Textured PEI Plate",
    "Smooth PEI Plate",
)


class PrintRunRequest(BaseModel):
    """The print dialog's choices (spec 2026-09-27 §2, §4): spools, nozzles, quality and
    plate. Every slicer preset is derived from them by the resolver; there is no
    pipeline to name, and the run always slices then queues.
    """

    #: The printer to queue on, whose loaded spools and rack the dialog showed. Omitted
    #: means the configured printer. It is also the printer the per-printer option scope
    #: keys on.
    printer_id: int | None = None
    #: One spool per plate slot. Every slot a plate uses needs one: a slot with no spool
    #: has no filament preset, which the resolver reports as an error (spec §4.3).
    filament_plan: FilamentPlan
    #: Nozzles, quality tier or process, plate type and per-slot preset overrides.
    choices: PrintChoices
    #: Omitted means "the remembered quantity, else 1" (#124): a number here is the
    #: caller's explicit choice for this print and wins over any remembered one.
    copies: int | None = Field(default=None, ge=1, le=1000)
    plate_id: int = Field(default=1, ge=1)
    #: Every plate of the output, each sliced and queued as an item of its own, in place
    #: of ``plate_id`` alone (#83).
    all_plates: bool = False
    #: The Bambuddy project this print belongs to (#79). Omitted means "the project the
    #: last send went to"; an explicit ``null`` cannot be expressed and does not need to
    #: be — a print with no project is simply one nobody filed.
    project_id: int | None = None
    #: Per-print overrides from the dialog's options disclosure (#78), the most specific
    #: scope, as on ``SendRequest``. Nothing here is remembered, and ``copies`` wins over
    #: a ``quantity`` sent alongside it.
    options: PrintOptions = Field(default_factory=PrintOptions)


class PrintRunResult(BaseModel):
    """What a run queued. ``route`` is always ``"slice_queue"`` now; it stays so a
    reader of the result need not change until the dialog does (following it to
    completion is #89).
    """

    library_file_id: int
    route: Literal["slice_queue"] = "slice_queue"
    slice_job_id: int | None = None
    sliced_library_file_id: int | None = None
    queue_item_ids: list[int] = Field(default_factory=list)
    printer_id: int | None = None
    #: How many copies were asked for (#148): the request's own ``copies`` when it set
    #: one, else the remembered quantity, else 1. That is the item's ``quantity``, not
    #: ``len(queue_item_ids)``.
    copies: int
    #: ScadBuddy's own advisories — the resolver's, the chosen spools', and the chosen
    #: nozzle and plate against the printer — carried through so the dialog can keep
    #: showing them after the click.
    warnings: list[FilamentWarning] = Field(default_factory=list)
    #: The project this print was filed under, and the folder its 3MF went into (#79).
    project_id: int | None = None
    folder_id: int | None = None
    bambuddy_url: str


async def filament_options_for_output(
    client: BambuddyClient,
    store: OutputStore,
    uploads: BambuddyUploadStore,
    meta: OutputMeta,
    settings: StoredSettings,
    *,
    printer_id: int | None = None,
    plate_id: int = 1,
    all_plates: bool = False,
) -> FilamentOptions:
    """The filament step's whole payload for one output (#87).

    ``all_plates`` answers for every plate of the output at once — one row per slot any
    plate uses, in place of ``plate_id``'s own — which is what an all-plates print
    needs a spool for.

    Uploads the 3MF only if Bambuddy has no copy of it at all: the plate's slots are
    read out of a *library file*, and any copy of this output has the same slots, so a
    run's copy is reused (#457).

    With a printer it also carries that printer's mounted nozzles (#78). Without one
    there are no nozzles to read. An offline printer's status is unreadable the same way (spec §3):
    the step still opens, with no mounted nozzles to compare against.
    """
    copy = await copy_to_read(client, store, uploads, meta, settings)
    library_file_id = copy.id
    plate_ids = (
        [plate.index for plate in plates_of(store.directory(meta.id) / MODEL_NAME)] or [1]
        if all_plates
        else [plate_id]
    )
    read = [
        await gather_options(
            client,
            library_file_id=library_file_id,
            printer_id=printer_id,
            plate_id=plate,
            fallback_colours=list(meta.colors),
            own_colours=list(meta.colors) if copy.recolored else None,
        )
        for plate in plate_ids
    ]
    options = read[0] if len(read) == 1 else every_plate(read)
    if printer_id is None:
        return options
    try:
        options.nozzles = (await client.printer_status(printer_id)).nozzles
    except (ApiError, ValueError):
        logger.info("printer status unreadable; the filament step opens with no nozzles known")
        options.nozzles = []
    return options


async def _spool_colours(
    client: BambuddyClient, meta: OutputMeta, plan: FilamentPlan
) -> list[str] | None:
    """One colour per filament of the output: the chosen spool's, or the model's own
    for a slot with no spool (#476). ``None`` when no spool is chosen at all, which
    leaves the file in the model's colours."""
    if not plan.slots:
        return None
    rgba = {spool.id: normalise_colour(spool.rgba) for spool in await client.spools()}
    return [
        rgba.get(plan.spool_for(index + 1) or 0) or colour
        for index, colour in enumerate(meta.colors)
    ]


async def run_for_output(
    client: BambuddyClient,
    store: OutputStore,
    uploads: BambuddyUploadStore,
    meta: OutputMeta,
    settings: StoredSettings,
    request: PrintRunRequest,
) -> PrintRunResult:
    """Slice with presets derived from the dialog's choices, then queue (spec §4).

    Always the slice-and-queue route: there is no pipeline to run. Bambuddy still
    decides AMS tray and extruder placement at dispatch — no ``ams_mapping`` is sent
    (spec §6).

    What the choices alone decide (nozzle sizes, printer and process preset) is
    refused before the 3MF is uploaded. Every plate is then resolved before any is
    sliced, so a slot error is a 422 with nothing on Bambuddy's queue, however many
    plates the print has.
    """
    plate_ids = (
        [plate.index for plate in plates_of(store.directory(meta.id) / MODEL_NAME)]
        if request.all_plates
        else [request.plate_id]
    )
    if not plate_ids:
        # ScadBuddy's writer always lays out one; a 3MF edited to list none has nothing
        # to queue, and every route below reads the first plate's outcome. Read
        # from the local 3MF before anything touches Bambuddy.
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "This output's 3MF lays out no plates, so there is nothing to print.",
        )
    printer_id = request.printer_id or settings.printer_id
    if printer_id is None:
        raise not_configured(
            "no printer is chosen and none is configured, so there is nothing to print on"
        )
    await _require_resolvable_printer(client, printer_id)
    choices = request.choices
    # Read once for every plate: the catalogue is ~4000 presets on the live instance.
    # Read before the upload, so that what the choices alone refuse — mixed nozzle
    # sizes, no printer or process preset — is a 422 that leaves nothing in Bambuddy's
    # library. Slot errors need the plate's slots, which only a library file answers,
    # so those are still found after the upload, by `resolve` below.
    catalogue = await _catalogue(client)
    refused = choice_errors(choices, catalogue)
    if refused:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT, " ".join(error.message for error in refused)
        )
    # Placed for the chosen printer's plate, stating the chosen nozzle (#105, #126).
    target = await target_for(
        client,
        settings,
        meta.slug,
        printer_id=printer_id,
        nozzle_diameter=choices.nozzles[0].size,
        colours=await _spool_colours(client, meta, request.filament_plan),
    )
    # A project's folder replaces the one from Settings for this send, which is what
    # puts the 3MF on Bambuddy's project page (#79). Resolved before the upload, because
    # the copy is looked up by (folder, target): a project gets a copy of its own, and
    # one another project printed from is neither moved nor deleted (#316).
    project_id = request.project_id or settings.last_project_id
    folder_id = await folder_for(client, project_id) if project_id is not None else None
    library_file_id = await ensure_uploaded(
        client, store, uploads, meta, settings, target=target, folder_id=folder_id
    )
    # The picker's project is its own control (ProjectPicker, defaulting to the last
    # one), so a remembered project_id is dropped here rather than half-applied.
    print_options = resolve_print_options(
        settings, meta.slug, printer_id, request_scope(request.copies, request.options)
    ).model_copy(update={"project_id": None})
    copies = print_options.quantity or 1

    # Read once for every plate: the plan's spools are the same on every plate.
    spool_presets = {
        spool_id: await client.spool_filament_presets(spool_id)
        for spool_id in sorted({slot.spool_id for slot in request.filament_plan.slots})
    }
    # Each plate's slots are read on their own: a plate uses only some of the project's
    # filaments (#83). The one plan applies to every plate because a slot is a project
    # filament, not a plate position (#180), so slot 2 is the same colour on every plate.
    planned: list[tuple[int, FilamentOptions, Resolved, SlicePlan]] = []
    errors: list[str] = []
    for plate_id in plate_ids:
        options = await gather_options(
            client,
            library_file_id=library_file_id,
            printer_id=printer_id,
            plate_id=plate_id,
            fallback_colours=list(meta.colors),
        )
        resolved = resolve(options, request.filament_plan, choices, catalogue, spool_presets)
        for error in resolved.errors:
            message = (
                f"Plate {plate_id}: {error.message}"
                if error.slot_id is not None and len(plate_ids) > 1
                else error.message
            )
            if message not in errors:
                errors.append(message)
        if resolved.errors:
            continue
        if resolved.printer_preset is None or resolved.process_preset is None:
            # The resolver reports both as errors, so this only narrows the types; it
            # is still a refusal, never an assertion.
            raise ApiError(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"Bambuddy has no printer preset {resolved.printer_preset_name!r} or no "
                "process preset for the chosen nozzle.",
            )
        plan = SlicePlan(
            printer_preset=resolved.printer_preset,
            process_preset=resolved.process_preset,
            filament_presets=resolved.filament_presets,
            filament_colours=resolved.filament_colours,
            bed_type=resolved.bed_type,
        )
        planned.append((plate_id, options, resolved, plan))
    if errors:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, " ".join(errors))

    hardware = await _hardware_warnings(
        client, printer_id, choices, printer_name=planned[0][1].printer_name
    )
    outcomes: list[QueueOutcome] = []
    sent: list[PlateSend] = []
    warnings: list[FilamentWarning] = []
    for plate_id, options, resolved, plan in planned:
        outcome = await slice_and_queue(
            client,
            library_file_id=library_file_id,
            plan=plan,
            printer_id=printer_id,
            filaments=queue_filaments(options, request.filament_plan),
            plate_id=plate_id,
            copies=copies,
            project_id=project_id,
            options=print_options,
        )
        sent = await _record_queued(
            store, uploads, meta, library_file_id, plate_id, outcome, project_id, sent
        )
        outcomes.append(outcome)
        for warning in [*resolved.warnings, *check(options, request.filament_plan, copies=copies)]:
            # Checked once below, against what every plate needs together.
            if warning.kind != "low-filament" and warning not in warnings:
                warnings.append(warning)
    warnings += [
        warning
        for warning in check(
            across_plates([options for _, options, _, _ in planned]),
            request.filament_plan,
            copies=copies,
        )
        if warning.kind == "low-filament"
    ]
    return _queued(
        client,
        outcomes,
        library_file_id,
        project_id,
        folder_id,
        copies=copies,
        warnings=warnings + hardware,
    )


async def _require_resolvable_printer(client: BambuddyClient, printer_id: int) -> None:
    """A 422 before anything is uploaded or sliced when the resolver cannot serve this
    printer: one Bambuddy does not list, one it has deactivated (#479; the dialog never
    offers those), or any model but the H2C, whose presets are the only ones the
    resolver knows (``PRINTER_MODEL``)."""
    printer = next((row for row in await client.printers() if row.id == printer_id), None)
    if printer is None:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"Bambuddy has no printer {printer_id}. Pick another printer.",
        )
    if not printer.is_active:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"{printer.name} is deactivated in Bambuddy. Activate it there, or pick "
            "another printer.",
        )
    if (printer.model or "").upper() != PRINTER_MODEL:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"ScadBuddy can only choose slicer presets for a Bambu Lab {PRINTER_MODEL} so "
            f"far, and {printer.name}'s model is {printer.model or 'not reported'}.",
        )


async def _hardware_warnings(
    client: BambuddyClient,
    printer_id: int,
    choices: PrintChoices,
    *,
    printer_name: str | None,
) -> list[FilamentWarning]:
    """The chosen nozzles and plate against what the printer reports (spec §4.4-4.5).

    Advisory only: an offline printer's status and an unreadable archive list mean
    "unknown", which says nothing rather than failing the print.
    """
    try:
        installed = installed_nozzles(await client.printer_status(printer_id))
    except (ApiError, ValueError):
        logger.info("printer status unreadable; no nozzle is warned about")
        installed = []
    try:
        last = last_bed_type(await client.archives(printer_id=printer_id), printer_id=printer_id)
    except (ApiError, ValueError):
        logger.info("archives unreadable; the plate is not compared with the last print")
        last = None
    found = [
        nozzle_warning(size, installed)
        for size in dict.fromkeys(nozzle.size for nozzle in choices.nozzles)
    ]
    found.append(plate_warning(choices.bed_type, last, printer_name))
    return [warning for warning in found if warning is not None]


async def _record_queued(
    store: OutputStore,
    uploads: BambuddyUploadStore,
    meta: OutputMeta,
    library_file_id: int,
    plate_id: int,
    outcome: QueueOutcome,
    project_id: int | None,
    sent: list[PlateSend],
) -> list[PlateSend]:
    """Record one plate's queue items as soon as it is queued; returns every plate so far.

    Recorded per plate, not after the last one: a later plate failing to slice must not
    leave the plates already on Bambuddy's queue unknown to the output (#83). ``plates``
    carries every plate of this print, since the single ids hold only the last. The
    plate's sliced file is recorded against the copy it was sliced from (#316).
    """
    await uploads.record_sliced(
        meta.id,
        library_file_id,
        SlicedCopy(id=outcome.sliced_library_file_id, preset_key=outcome.preset_key),
    )
    sent = sent + [
        PlateSend(plate_id=plate_id, queue_item_id=item, slice_job_id=outcome.slice_job_id)
        for item in outcome.queue_item_ids
    ]
    for queue_item_id in outcome.queue_item_ids:
        store.record_send(
            meta.id,
            queue_item_id=queue_item_id,
            print_route="slice_queue",
            slice_job_id=outcome.slice_job_id,
            project_id=project_id,
            plates=sent,
        )
    return sent


def _queued(
    client: BambuddyClient,
    outcomes: list[QueueOutcome],
    library_file_id: int,
    project_id: int | None,
    folder_id: int | None,
    copies: int,
    warnings: list[FilamentWarning] | None = None,
) -> PrintRunResult:
    """Report the slice-and-queue run; each plate was recorded as it was queued.

    One outcome per plate queued (#83). The slice job and sliced file reported are the
    first plate's; every plate's queue items are listed.
    """
    first = outcomes[0]
    return PrintRunResult(
        library_file_id=library_file_id,
        slice_job_id=first.slice_job_id,
        sliced_library_file_id=first.sliced_library_file_id,
        queue_item_ids=[item for outcome in outcomes for item in outcome.queue_item_ids],
        printer_id=first.printer_id,
        copies=copies,
        warnings=warnings or [],
        project_id=project_id,
        folder_id=folder_id,
        bambuddy_url=client.config.web_url(QUEUE_PATH),
    )
