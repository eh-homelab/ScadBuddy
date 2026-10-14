"""Slicing an output for print: the filament step's options, and the spool-first run.

ScadBuddy owns no slicing settings. The print dialog's run (:func:`execute_run`)
derives every preset from the dialog's choices — spools, nozzles, quality and plate
(spec 2026-09-27 §4) — and always slices then queues; there is no pipeline to run or
choose from here. The send bar only uploads (#312); this is the only path that prints.
"""

from __future__ import annotations

import asyncio
import logging
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal, Protocol

from fastapi import status
from pydantic import BaseModel, Field, field_validator

from scadbuddy.bambuddy.catalogue import _Catalogue, _catalogue
from scadbuddy.bambuddy.clearance import (
    check_plates,
    clearance_for,
)
from scadbuddy.bambuddy.client import BambuddyClient, BambuddyConfig, read_once
from scadbuddy.bambuddy.concurrent import settled
from scadbuddy.bambuddy.dispatch import (
    QueueOutcome,
    RackChoice,
    SlicePlan,
    enqueue_plate,
    slice_request,
)
from scadbuddy.bambuddy.errors import not_configured
from scadbuddy.bambuddy.extruders import (
    RACK_SIDE,
    VOLUME_TYPE,
    high_flow_warning,
    high_flow_warnings,
    rack_volume_type,
    side_of,
    slicer_nozzle_stats,
    slicer_volume_types,
    with_sides,
)
from scadbuddy.bambuddy.filament_profiles import profile_overrides, unknown_profiles
from scadbuddy.bambuddy.filaments import (
    FilamentOptions,
    FilamentPlan,
    FilamentWarning,
    Inventory,
    QueueFilaments,
    SlotNeed,
    SpoolOption,
    across_plates,
    build_options,
    check,
    every_plate,
    family,
    gather_plate_options,
    queue_filaments,
    read_inventory,
)
from scadbuddy.bambuddy.filaments3mf import SlicedProfile, diameter, model_key
from scadbuddy.bambuddy.hardware import (
    installed_nozzles,
    last_bed_type,
    nozzle_warning,
    plate_unlisted_warning,
    plate_warning,
)
from scadbuddy.bambuddy.models import (
    DEFAULT_ALGORITHM,
    FilamentRequirements,
    FlowType,
    Printer,
    PrinterStatus,
    RackAlgorithm,
    Spool,
)
from scadbuddy.bambuddy.nozzle_plan import NozzlePlan, plan_for
from scadbuddy.bambuddy.options import PrintOptions
from scadbuddy.bambuddy.print_source import (
    FilamentReader,
    LibrarySource,
    OutputSource,
    PrintSource,
)
from scadbuddy.bambuddy.resolver import (
    PRINTER_MODEL,
    PrintChoices,
    Resolved,
    choice_errors,
    process_preset_for,
    resolve,
)
from scadbuddy.bambuddy.send import request_scope, resolve_print_options
from scadbuddy.bambuddy.supports import (
    SUPPORT_KEYS,
    SupportsChoice,
    support_overrides,
    wanted_settings,
)
from scadbuddy.bambuddy.trays import (
    colour_word,
    tray_colours,
    tray_material_colour,
    tray_of_spool_id,
    with_trays,
)
from scadbuddy.bambuddy.uploads import BambuddyUploadStore
from scadbuddy.core.problems import ApiError
from scadbuddy.library.catalogue import PrintSequence
from scadbuddy.library.outputs import OutputMeta, OutputStore
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.rack.rank import (
    RackCandidate,
    RackGroup,
    Usage,
    abrasive_type,
    candidates_for,
    eligible,
    glow,
    manual_for,
    rack_color,
    rack_groups,
    rack_positions,
    rack_serials,
    rack_warnings,
    rank_rack,
)
from scadbuddy.rack.usage import (
    PickedHotend,
    RackUsage,
    group_spools,
    record_seen,
    save_picks,
)
from scadbuddy.render.bambu3mf import FilamentMap

logger = logging.getLogger(__name__)

#: The three rack fallbacks that swallow an exception and log only its type (§7). Named
#: so the test guard reads these, not copies of them (#1081, #1086 review).
RACK_PICK_FALLBACK = "rack pick left to Bambuddy"
RACK_PREVIEW_FALLBACK = "the rack preview could not be built"
RACK_USAGE_FALLBACK = "rack usage unreadable; ranked without it"
RACK_FALLBACKS = frozenset({RACK_PICK_FALLBACK, RACK_PREVIEW_FALLBACK, RACK_USAGE_FALLBACK})

QUEUE_PATH = "/queue"


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
    #: The Bambuddy project this print belongs to (#79). Omitted means the remembered
    #: one (``last_project_id``); an explicit ``null`` is "No project" and wins over it,
    #: because the picker's PUT that remembers the choice may not have landed yet.
    project_id: int | None = None
    #: Per-print overrides from the dialog's options disclosure (#78), the most specific
    #: scope. Nothing here is remembered, and ``copies`` wins over a ``quantity`` sent
    #: alongside it.
    options: PrintOptions = Field(default_factory=PrintOptions)
    #: The caller's idempotency key for this print (#470): a new one (a UUID) per
    #: deliberate Print, the same one on every retry of it. Part of the run's key, so a
    #: retry re-attaches to its run while a reprint with the same choices is a new
    #: print. Omitted (an older client), the key is the output and choices alone.
    request_id: str | None = Field(default=None, min_length=1, max_length=128)
    #: Bambu's ``print_sequence`` for this print (#907), a process override over the
    #: template's ``print_settings``. Omitted means whatever those and the process say.
    print_sequence: PrintSequence | None = None
    #: A rack position (1-6) chosen by hand for the rack side (#836, spec 2026-10-01 §5).
    #: Omitted is Automatic: ScadBuddy ranks the rack.
    rack_position: int | None = Field(default=None, ge=1, le=6)
    #: How to rank the rack for this print (#836). Omitted means the printer's remembered
    #: algorithm, else Least used.
    rack_algorithm: RackAlgorithm | None = None
    #: Filament profiles this print applies (#717), by id, as the dialog suggested them
    #: and the user left them on. Their changes go over the template's
    #: ``print_settings`` and under ``print_sequence``.
    filament_profiles: list[str] = Field(default_factory=list, max_length=8)
    #: Support structures for this print (#1899), over the template's
    #: ``print_settings``. Omitted means whatever those and the process say.
    supports: SupportsChoice | None = None

    @field_validator("filament_profiles")
    @classmethod
    def _known_profiles(cls, ids: list[str]) -> list[str]:
        unknown = unknown_profiles(ids)
        if unknown:
            raise ValueError(f"no filament profile {', '.join(map(repr, unknown))}")
        return list(dict.fromkeys(ids))


class RackOption(BaseModel):
    """One eligible rack position, as the dialog lists it (spec 2026-10-01 §5). Built
    field by field from a :class:`RackCandidate`, which carries no serial (§7)."""

    position: int
    nozzle_diameter: str
    flow: FlowType
    color: str | None = None
    nozzle_type: str
    #: The filament it last ran (#2166), so the dialog can name it; None unknown.
    filament_type: str | None = None
    #: That filament's colour as a word ("black"), so the dialog never shows a hex.
    color_word: str | None = None
    #: Decoded from the code through ``rack.rank.NOZZLE_MATERIALS``; ``None`` is unknown.
    material: str | None = None
    prints: int = 0
    print_seconds: int = 0
    #: Prints queued or running on it that have not settled yet (#1079): Least used
    #: counts each as one more print.
    pending: int = 0


class RackSentPick(BaseModel):
    """A rack position the run sent for one filament group of one plate (spec 2026-10-01
    §5), never with the hotend's serial (§7). Group ids repeat across plates."""

    plate_id: int
    group_id: int
    position: int


class RackPickView(BaseModel):
    """The check's preview of the rack side's pick (spec 2026-10-01 §5), never with the
    hotend's serial (§7). It ranks the side as one group; the real pick is made per
    sliced group and can differ when the slicer splits it."""

    #: Always ``None``: the preview ranks the side as one group.
    group_id: int | None = None
    position: int | None = None
    reason: str | None = None
    unsafe_material: bool = False
    #: A chosen spool is not in the inventory, so Glow could not be checked (spec §3).
    glow_unchecked: bool = False
    options: list[RackOption] = Field(default_factory=list)


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
    #: Bambuddy's queue; ``None`` when its settings were removed while the run queued.
    bambuddy_url: str | None = None
    #: The rack positions actually sent, one per filament group that printed from the
    #: rack (#836). A pick can go stale before the print starts; that shows in progress,
    #: not here (spec §10).
    rack_picks: list[RackSentPick] = Field(default_factory=list)


def known_slots(source: PrintSource) -> Callable[[int | None], FilamentRequirements] | None:
    """A library file's (#2550) or an output's (#2720) slots as read from the file
    itself, for every plate: every copy of it has the same slots, so they answer for
    whichever copy is read. ``None`` when they came from Bambuddy, which is then asked
    per plate."""
    if isinstance(source, LibrarySource | OutputSource) and source.local is not None:
        return source.local.for_plate
    return None


async def filament_options(
    client: BambuddyClient,
    source: PrintSource,
    *,
    printer_id: int | None = None,
    plate_id: int = 1,
    all_plates: bool = False,
    status: asyncio.Future[PrinterStatus | None] | None = None,
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

    The file, the spools and the printer's status are read concurrently (#2504);
    ``status`` is that read when the caller has started it already: the caller's to
    keep, and to cancel if it must, since this call never does, even when it is itself
    cancelled. That promise is this call's alone: in ``choices_for`` a cancelled request
    still cancels the read, through the rack's ``_record_seen`` awaiting it, which is
    right, since the read is the request's (#2630).
    """
    reading: asyncio.Future[PrinterStatus | None] | None
    if status is not None:
        # A contract, not a mypy hint: an assert would be gone under `python -O` (#2814).
        if printer_id is None:
            raise ValueError("a shared status read is some printer's: pass its printer_id")
        reading = status
    elif printer_id is not None:
        reading = asyncio.ensure_future(read_status(client, printer_id))
    else:
        reading = None
    try:
        read_file, inventory = await asyncio.gather(
            source.file_to_read(client), read_inventory(client), return_exceptions=True
        )
        file = settled(read_file)
        plate_ids = ((await source.plate_ids(client)) or [1]) if all_plates else [plate_id]
        read = await gather_plate_options(
            client,
            library_file_id=file.id,
            printer_id=printer_id,
            plate_ids=plate_ids,
            fallback_colours=list(source.colours),
            own_colours=file.own_colours,
            inventory=settled(inventory),
            known=known_slots(source),
        )
    finally:
        # Awaited even when a read failed, so the status read never outlives the call.
        # One the caller started (``choices_for`` shares it with the rack) is the
        # caller's to cancel: `wait` leaves it running when this call is cancelled,
        # where `gather` would cancel it (#2509 review).
        if reading is not None and status is not None:
            await asyncio.wait([reading])
        elif reading is not None:
            await asyncio.gather(reading, return_exceptions=True)
    options = read[0] if len(read) == 1 else every_plate(read)
    if reading is None or printer_id is None:
        return options
    printer_status = reading.result()
    if printer_status is None:
        options.nozzles = []
        return options
    options.nozzles = printer_status.nozzles
    # Each loaded spool's side, as a label in the picker (#469); nothing is checked
    # against it (#768).
    # A filled tray Bambuddy has no spool for is asked about, and offered (#2164).
    return with_trays(with_sides(options, printer_status), printer_status, printer_id)


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
    """:func:`filament_options` for an output ScadBuddy rendered."""
    return await filament_options(
        client,
        OutputSource(store=store, meta=meta, uploads=uploads, settings=settings),
        printer_id=printer_id,
        plate_id=plate_id,
        all_plates=all_plates,
    )


class ChoosesProject(Protocol):
    """A request body with an optional ``project_id``."""

    @property
    def project_id(self) -> int | None: ...

    @property
    def model_fields_set(self) -> set[str]: ...


def chosen_project(request: ChoosesProject, settings: StoredSettings) -> int | None:
    """The request's ``project_id`` when it sent one, ``null`` included ("No project");
    the remembered ``last_project_id`` only when it left the field out (#317)."""
    if "project_id" in request.model_fields_set:
        return request.project_id
    return settings.last_project_id


async def read_status(client: BambuddyClient, printer_id: int) -> PrinterStatus | None:
    """The printer's live status, read once per run; unreadable is ``None``."""
    try:
        return await client.printer_status(printer_id)
    except (ApiError, ValueError):
        logger.info("printer status unreadable; no nozzle is known")
        return None


class RunRefusalError(ApiError):
    """A 422 the run decides itself before anything is uploaded (#765 review).

    Kept apart from a 422 Bambuddy answers on a plain read, which ``errors.py`` passes
    through as an :class:`ApiError` of the same status: the check says this one before
    Print, and lets that one fail the check as an outage."""

    def __init__(self, detail: str) -> None:
        super().__init__(status.HTTP_422_UNPROCESSABLE_CONTENT, detail)


class PrintCheck(BaseModel):
    """What the run would refuse for the dialog's choices before Print (#755, #760).
    ``errors`` holds the run's 422 detail word for word (#758 review); ``warnings`` what
    it would carry back as advisories."""

    errors: list[str] = Field(default_factory=list)
    warnings: list[FilamentWarning] = Field(default_factory=list)
    #: The rack side's preview (#836); ``None`` with no readable rack.
    rack: RackPickView | None = None
    #: Which nozzle each filament prints from, as the run will state it (#2166);
    #: ``None`` when the slicer is left to choose (one extruder, no readable status).
    nozzle_plan: NozzlePlan | None = None
    #: How many objects the checked plate holds, read only for a by-object print
    #: (#956); ``None`` when it was not read, or when every plate was checked at once
    #: (#2976), whose warnings name each plate.
    objects: int | None = None


ChooseRack = Callable[[int], Awaitable[RackChoice | None]]


class RackReads(Protocol):
    """The two reads ``choose_rack`` makes; ``BambuddyClient`` is one."""

    async def filament_requirements(
        self, file_id: int, *, plate_id: int | None = None
    ) -> FilamentRequirements: ...

    async def printer_status(self, printer_id: int) -> PrinterStatus: ...


async def _usage_or_empty(
    rack: RackUsage | None, status: PrinterStatus, printer_id: int
) -> dict[str, Usage]:
    """The rack's usage, or none when it cannot be read: usage only ranks, so a failed
    read ranks as if no hotend had printed and keeps a hand pick (claude-review on #1043).
    Logged by type only, since a database error's text can carry a serial (§7)."""
    if rack is None:
        return {}
    try:
        return await rack.usage(rack_serials(status.nozzle_rack))
    except Exception as exc:
        logger.warning(
            RACK_USAGE_FALLBACK,
            extra={"printer_id": printer_id, "error": type(exc).__name__},
        )
        return {}


def rack_chooser(
    client: RackReads,
    *,
    printer_id: int,
    plate_id: int,
    spools: Mapping[int, SpoolOption],
    algorithm: RackAlgorithm,
    manual_position: int | None,
    rack: RackUsage | None,
    warnings: list[FilamentWarning],
) -> ChooseRack:
    """The plate's ``choose_rack`` (spec 2026-10-01 §5). It reads the sliced file's
    requirements for ``plate_id`` and then, only when a group prints from the rack, a
    fresh status, once per plate and never ``PreparedRun.printer_status``. It ranks, and
    appends its warnings to ``warnings``, the plate's list. It **never raises**: the whole
    body is one ``try``, and on any exception it logs a fixed message with the exception's
    type, appends ``rack-left-to-bambuddy`` and returns ``None``, so Bambuddy picks."""

    async def choose(sliced: int) -> RackChoice | None:
        stage = "requirements unreadable"
        try:
            requirements = await client.filament_requirements(sliced, plate_id=plate_id)
            stage = "rack pick failed"
            groups = rack_groups(requirements.filaments, spools)
            manual, notes = manual_for(groups, manual_position, algorithm)
            if not groups:
                warnings.extend(notes)
                return None
            stage = "status unreadable"
            status_read = await client.printer_status(printer_id)
            # The order no longer decides a hotend's age: prepare_run (the run's prepare
            # activity) and choices_for record the rack as seen before this (#1081).
            usage = (
                await _usage_or_empty(rack, status_read, printer_id)
                if algorithm != "bambuddy"
                else {}
            )
            # Recorded again on purpose: this is a fresh read, per plate (each plate's
            # ``print_enqueue`` activity), and can show a hotend swapped in since
            # prepare_run's. An unchanged rack makes no new row
            # version (#1082), though the upsert still locks its rows.
            await record_seen(rack, printer_id, status_read)
            stage = "rack pick failed"
            picks = rank_rack(groups, status_read.nozzle_rack, algorithm, usage, manual)
            found = rack_warnings(groups, status_read.nozzle_rack, algorithm, picks, manual)
            if any(g not in picks or not picks[g].manual for g in manual):
                # ``rack_warnings`` says the manual position was refused, so drop
                # ``manual_for``'s "group N got position P" (final review minor 1).
                notes = [note for note in notes if note.kind != "rack-manual-partial"]
            ordered = sorted(picks.items())
            choice = (
                RackChoice(
                    nozzle_rack_choice={str(group_id): pick.position for group_id, pick in ordered},
                    picks=[
                        PickedHotend(
                            group_id=group_id,
                            position=pick.position,
                            serial=pick.serial,
                            spools=group_spools(requirements.filaments, spools, group_id),
                        )
                        for group_id, pick in ordered
                    ],
                )
                if ordered
                else None
            )
        except Exception as exc:
            # Never str(exc) or a traceback: an error's text can carry a serial (§7).
            logger.warning(
                RACK_PICK_FALLBACK,
                extra={"printer_id": printer_id, "stage": stage, "error": type(exc).__name__},
            )
            warnings.append(
                FilamentWarning(
                    kind="rack-left-to-bambuddy", message=f"Rack pick left to Bambuddy: {stage}."
                )
            )
            return None
        # Only once the pick is made: a read that failed above says only why (F8).
        warnings.extend(notes)
        warnings.extend(found)
        return choice

    return choose


def _spools_by_slot(options: FilamentOptions, plan: FilamentPlan) -> dict[int, SpoolOption]:
    """Each plate slot's chosen inventory spool, for the Glow test (spec §3)."""
    by_id = {spool.spool_id: spool for spool in options.spools}
    return {slot.slot_id: by_id[slot.spool_id] for slot in plan.slots if slot.spool_id in by_id}


async def resolve_plates(
    client: BambuddyClient,
    plate_ids: list[int],
    per_plate: list[FilamentOptions],
    plan: FilamentPlan,
    choices: PrintChoices,
    catalogue: _Catalogue,
) -> tuple[list[Resolved], list[str]]:
    """Each plate resolved for ``plan``, and every slot refusal among them in the run's
    own words: what :func:`plan_run` refuses with, and what :func:`check_print` reports
    (#1050). A spool the inventory lacks resolves as no spool chosen, so its presets
    are not read: Bambuddy would answer for a spool it does not have with an error."""
    inventory = {spool.spool_id for options in per_plate for spool in options.spools}
    # Read once for every plate: the plan's spools are the same on every plate.
    spool_presets = {
        spool_id: await client.spool_filament_presets(spool_id)
        # A tray chosen for itself (#2164) has no spool, so no presets of its own.
        for spool_id in sorted({slot.spool_id for slot in plan.slots} & inventory)
        if spool_id > 0
    }
    every: list[Resolved] = []
    errors: list[str] = []
    for plate_id, options in zip(plate_ids, per_plate, strict=True):
        resolved = resolve(options, plan, choices, catalogue, spool_presets)
        for error in resolved.errors:
            message = (
                f"Plate {plate_id}: {error.message}"
                if error.slot_id is not None and len(plate_ids) > 1
                else error.message
            )
            if message not in errors:
                errors.append(message)
        every.append(resolved)
    return every, errors


async def _slot_errors(
    client: BambuddyClient,
    source: PrintSource,
    request: PrintRunRequest,
    prepared: PreparedRun,
    *,
    inventory: Inventory | None = None,
) -> list[str]:
    """The run's slot refusals for ``request``, judged before anything is uploaded for it
    (#1050): read from a copy Bambuddy already has, which has the same slots as the one
    the run prints, as :func:`filament_options` reads them. An output's slots and a
    library file's are read from the file itself (#2720, #2550); with neither and no copy
    there is nothing to read without an upload, and the run still judges the slots
    after its own. ``inventory`` is the check's own read, when it made one (#2722)."""
    if source.is_sliced or prepared.catalogue is None:
        # Nothing resolves a preset for a sliced file (#2671); Bambuddy maps its slots.
        return []
    read_file = await source.file_read_already(client)
    if read_file is None:
        return []
    per_plate = await gather_plate_options(
        client,
        library_file_id=read_file.id,
        printer_id=prepared.printer_id,
        plate_ids=prepared.plate_ids,
        fallback_colours=list(source.colours),
        own_colours=read_file.own_colours,
        inventory=inventory,
        known=known_slots(source),
    )
    per_plate = [
        with_trays(options, prepared.printer_status, prepared.printer_id) for options in per_plate
    ]
    _, errors = await resolve_plates(
        client,
        prepared.plate_ids,
        per_plate,
        request.filament_plan,
        request.choices,
        prepared.catalogue,
    )
    return errors


async def check_print(
    client: BambuddyClient,
    source: PrintSource,
    settings: StoredSettings,
    request: PrintRunRequest,
    *,
    rack: RackUsage | None = None,
) -> PrintCheck:
    """What the run would refuse for ``request``, with nothing uploaded, sliced or queued.

    It is :func:`prepare_run` itself (#760), so the check makes every refusal the run
    makes before it answers 202 — no plate, a printer the resolver cannot serve, choices
    the catalogue refuses — in the run's own words. It no longer refuses by the mounted
    nozzles (#768): the maintainer's test print, 2026-09-29, printed a two-colour 0.2 mm
    slice through the one 0.2 mm nozzle. It warns only of a side the slice may use whose
    mounted nozzle of the size is not of the flow sliced there (#723, #797, #484), and
    not of the rack side once the preview picks it a hotend (#1238). The run's slot
    refusals (no spool, an override Bambuddy lacks) are judged from a copy Bambuddy
    already has, so they are errors here before the run uploads (#1050). The dialog
    sends no check before its choices read, which is what makes that copy. Only the
    run's own refusals (:class:`RunRefusalError`) and slot refusals become ``errors``: a
    failed read of Bambuddy, the slot reads included, fails the check, as it would fail
    the run. With no printer chosen or configured there is nothing to judge, and the
    run says why. For a plan with slots that includes the inventory read (#2722): a
    failed spool or assignment read fails the check, where the rack preview alone used
    to swallow it and preview nothing, since the run would fail on the same read."""
    if (request.printer_id or settings.printer_id) is None:
        return PrintCheck()
    try:
        prepared = await prepare_run(
            client, source, settings, request, rack=rack, refuse_manual_pick=False
        )
    except RunRefusalError as refused:
        return PrintCheck(errors=[refused.detail])
    if source.is_sliced:
        # Queued as it is, never refused (#2671): what to set right first is a warning,
        # and there is no nozzle plan, slots or rack side to judge before it.
        return PrintCheck(warnings=prepared.warnings)
    laid_out = await source.states_nozzles(client)
    # One read of the inventory for the plan, the rack, the slot refusals and the spool
    # warnings (#2722); none for a plan with no slots, whose slot refusals read their own.
    inventory = await read_inventory(client) if request.filament_plan.slots else None
    nozzle_plan = (
        await plan_for(
            client,
            prepared.printer_status,
            prepared.printer_id,
            request.filament_plan,
            request.choices.nozzles,
            request.choices.sides,
            inventory=inventory,
        )
        if laid_out
        else None
    )
    rack_view, rack_notes = await rack_preview(
        client,
        request,
        settings,
        printer_id=prepared.printer_id,
        status=prepared.printer_status,
        rack=rack,
        laid_out=laid_out,
        nozzle_plan=nozzle_plan,
        inventory=inventory,
    )
    # A refused manual pick still previews the rack, so the dialog can offer another,
    # and is said once: as the error, not again as a rack-manual-partial warning.
    try:
        _check_manual_pick(request, prepared.printer_status, laid_out=laid_out)
    except RunRefusalError as refused:
        errors = [refused.detail]
        rack_notes = [note for note in rack_notes if note.kind != "rack-manual-partial"]
    else:
        errors = []
    # The run's 422 after its upload (#1050): every slot refusal, joined as it joins them.
    slot_errors = await _slot_errors(client, source, request, prepared, inventory=inventory)
    if slot_errors:
        errors.append(" ".join(slot_errors))
    # The one mounted-nozzle advisory kept (#723, #797): a warning, never a refusal. Not
    # for the rack side when the preview picks it a hotend (#1238).
    rack_picked = rack_view is not None and rack_view.position is not None
    objects, clearance = await _sequence_clearance(client, source, request, prepared)
    # One read of the preset's values for the profiles and the supports (#2804 review).
    # A sliced file, the one with no catalogue, has returned above (#2671).
    process = (
        process_preset_for(request.choices, prepared.catalogue)
        if prepared.catalogue is not None
        else None
    )
    read = read_once(client)
    return PrintCheck(
        errors=errors,
        objects=objects,
        warnings=[
            *clearance,
            *high_flow_warnings(
                prepared.printer_status,
                request.choices.nozzles,
                rack_picked=rack_picked,
                laid_out=laid_out,
                sides=nozzle_plan.sides if nozzle_plan is not None else None,
            ),
            *rack_notes,
            *_listed(
                plate_unlisted_warning(
                    request.choices.bed_type, prepared.printer_model, prepared.printer_name
                )
            ),
            *(
                plan_warnings(
                    request.filament_plan,
                    inventory,
                    printer_id=prepared.printer_id,
                    printer_name=prepared.printer_name,
                    status=prepared.printer_status,
                    nozzle_plan=nozzle_plan,
                )
                if inventory is not None
                else []
            ),
            # #717: what the run will say of a profile it cannot apply.
            *(await profile_overrides(client, process, request.filament_profiles, read=read))[1],
            # #1899: and of a support setting it cannot apply.
            *(
                await support_overrides(
                    client,
                    process,
                    wanted_settings(source.print_settings, request.supports),
                    read=read,
                )
            )[1],
        ],
        rack=rack_view,
        nozzle_plan=nozzle_plan,
    )


def _listed(warning: FilamentWarning | None) -> list[FilamentWarning]:
    return [] if warning is None else [warning]


async def _sequence_clearance(
    client: BambuddyClient,
    source: PrintSource,
    request: PrintRunRequest,
    prepared: PreparedRun,
) -> tuple[int | None, list[FilamentWarning]]:
    """#956: for a by-object print (chosen here, else the template's default), the
    objects on each plate printed, and what the toolhead or the gantry would hit, as
    warnings, never a refusal. Only then is the file read for its objects; an unreadable
    one says nothing, and the slicer still has the last word."""
    sequence = request.print_sequence or source.print_settings.get("print_sequence")
    if sequence != "by object":
        return None, []
    # The resolved printer's, as the plate warnings are (#3024 review); a run accepted
    # before the model was kept is judged as before.
    clearance = clearance_for(prepared.printer_model or PRINTER_MODEL)
    try:
        # The source's own read, which the nozzle check made already (#2976).
        payload = await source.fetched_3mf(client)
        if payload is None:
            return None, []
        # One read of the file for every plate (#2875 review), against one budget, and
        # its pairs compared in the same thread, never on the event loop (#3251).
        by_plate, warnings = await asyncio.to_thread(
            check_plates, payload, prepared.plate_ids, clearance
        )
        per_plate = [(plate_id, by_plate[plate_id]) for plate_id in prepared.plate_ids]
    except (ValueError, zipfile.BadZipFile, ET.ParseError):
        return None, []
    # One count only for one plate: the dialog says "this plate" (#2976).
    return len(per_plate[0][1]) if len(per_plate) == 1 else None, warnings


def plan_warnings(
    plan: FilamentPlan,
    inventory: Inventory,
    *,
    printer_id: int,
    printer_name: str | None,
    status: PrinterStatus | None,
    nozzle_plan: NozzlePlan | None = None,
) -> list[FilamentWarning]:
    """What the chosen spools themselves say, for Checks (#2722): a spool not loaded in
    this printer, in the picker's own words, and a plan that mixes material families.
    Mixing is planned for (each family gets a hotend of its own), so it is a warning,
    never a refusal; but a TPU among PLA is more often a slip than a choice. A tray
    chosen as itself is loaded by definition, and counts by its own material. A spool
    the inventory no longer has (gone, or archived) says nothing here: the run refuses
    it by name.

    The picker still says ``not-loaded`` under the slot. Checks saying it too is on
    purpose: Checks is the list read before Print, so do not dedupe either away.

    Every slot of ``plan`` counts toward the mix: the dialog's plan holds only the
    chosen plate's slots, or every plate's for "all plates" (``useFilamentPlan``, pinned
    by its "a plate change seeds only slots new to it" test).

    The warning says each family prints from a nozzle of its own only when
    ``nozzle_plan`` puts them so; with no plan, or one that shares a side between
    families (a hand-picked side can), it says only that they mix (#2733 review)."""
    spools, assignments = inventory
    # A tray chosen as itself, by slot: its material from the printer's status (None
    # when the status is not known). The one place a slot is told to be a tray, so the
    # not-loaded check and the families below cannot disagree on it (#2733 review).
    trays = {
        slot.slot_id: tray_material_colour(slot.spool_id, status)
        for slot in plan.slots
        if tray_of_spool_id(slot.spool_id) is not None
    }
    chosen = [slot for slot in plan.slots if slot.slot_id not in trays]
    # No file: only the spools are judged here, so there are no grams or slots of a file.
    options = build_options(
        library_file_id=None,
        spools=list(spools),
        assignments=list(assignments),
        requirements=[SlotNeed(slot_id=slot.slot_id) for slot in chosen],
    ).model_copy(update={"printer_id": printer_id, "printer_name": printer_name})
    found = [
        warning
        for warning in check(options, FilamentPlan(slots=chosen))
        if warning.kind == "not-loaded"
    ]
    by_id = {option.spool_id: option for option in options.spools}
    families: list[str] = []
    families_on: dict[str | None, set[str]] = {}
    for slot in plan.slots:
        if slot.slot_id in trays:
            tray = trays[slot.slot_id]
            material = tray[0] if tray else None
        else:
            option = by_id.get(slot.spool_id)
            material = option.material if option else None
        named_family = family(material) if material else None
        if not named_family:
            continue
        if named_family not in families:
            families.append(named_family)
        side = nozzle_plan.side_of_slot(slot.slot_id) if nozzle_plan is not None else None
        families_on.setdefault(side, set()).add(named_family)
    if len(families) > 1:
        mix = f"This print mixes {', '.join(families[:-1])} and {families[-1]}."
        # "A nozzle of its own" holds for the H2C's two nozzles, the one PRINTER_MODEL
        # resolves for (resolver.py), and only where the plan puts each family on its
        # own side: none unplaced, no side shared.
        separate = None not in families_on and all(len(on) == 1 for on in families_on.values())
        found.append(
            FilamentWarning(
                kind="mixed-materials",
                message=(
                    f"{mix} Each prints from a nozzle of its own; check that is what you meant."
                    if separate
                    else f"{mix} Check that is what you meant."
                ),
            )
        )
    return found


def _rack_option(candidate: RackCandidate) -> RackOption:
    return RackOption(
        position=candidate.position,
        nozzle_diameter=candidate.nozzle_diameter,
        flow="high_flow" if candidate.high_flow else "standard",
        color=candidate.color,
        filament_type=candidate.filament_type or None,
        color_word=colour_word(candidate.color),
        nozzle_type=candidate.nozzle_type,
        material=candidate.material,
        prints=candidate.prints,
        print_seconds=candidate.print_seconds,
        pending=candidate.pending,
    )


def _spool_material(spool: Spool) -> str:
    return f"{spool.material}-{spool.subtype}" if spool.subtype else spool.material


async def rack_preview(
    client: BambuddyClient,
    request: PrintRunRequest,
    settings: StoredSettings,
    *,
    printer_id: int,
    status: PrinterStatus | None,
    rack: RackUsage | None,
    laid_out: bool,
    nozzle_plan: NozzlePlan | None = None,
    inventory: Inventory | None = None,
) -> tuple[RackPickView | None, list[FilamentWarning]]:
    """The rack side ranked as one group from the dialog's size and spools (spec §5):
    the preview ``/check`` shows. Judged on the flow the slice will carry on the rack side
    (#484): the one chosen there for a file the run lays out, Standard for a library file
    (``laid_out``, :func:`~scadbuddy.bambuddy.extruders.rack_volume_type`). Every chosen
    spool counts toward the material test, since the slice may put any of them on the
    rack side, unless ScadBuddy's nozzle plan (#2166) says which do: then only those
    on the rack side count. Advisory: a failure previews nothing. ``inventory`` is the
    check's own read, reused so a check reads the spools once (#2936); without it the
    spools are read here."""
    if status is None or not rack_positions(status.nozzle_rack):
        return None, []
    rack_letter = side_of(RACK_SIDE)
    slots = [
        slot
        for slot in request.filament_plan.slots
        if nozzle_plan is None or nozzle_plan.side_of_slot(slot.slot_id) in (None, rack_letter)
    ]
    try:
        spools = inventory[0] if inventory is not None else await client.spools()
        by_id = {spool.id: spool for spool in spools}
        chosen = [by_id.get(slot.spool_id) for slot in slots]
        known = [spool for spool in chosen if spool is not None]
        first = known[0].rgba if known else None
        group = RackGroup(
            group_id=0,
            nozzle_diameter=request.choices.nozzles[0].size,
            volume_type=rack_volume_type(request.choices.nozzles, laid_out=laid_out),
            # Zero alpha (a Clear spool's 00000000) is no color, never black.
            color=rack_color(first) if first else None,
            materials=tuple(dict.fromkeys(_spool_material(spool) for spool in known)),
            abrasive=any(
                abrasive_type(f"{spool.material} {spool.subtype or ''}")
                or glow(spool.material, spool.subtype)
                for spool in known
            ),
            glow_unchecked=len(known) < len(chosen),
            label="the rack side",
        )
        algorithm = request.rack_algorithm or settings.rack_algorithm(printer_id)
        usage = await _usage_or_empty(rack, status, printer_id)
        manual = {0: request.rack_position} if request.rack_position is not None else {}
        picks = rank_rack([group], status.nozzle_rack, algorithm, usage, manual)
        options = candidates_for(group, status.nozzle_rack, algorithm, usage)
        warnings = rack_warnings([group], status.nozzle_rack, algorithm, picks, manual)
    except Exception as exc:
        # Never str(exc): an error's text can carry a serial (§7).
        logger.warning(
            RACK_PREVIEW_FALLBACK,
            extra={"printer_id": printer_id, "error": type(exc).__name__},
        )
        return None, []
    pick = picks.get(0)
    return (
        RackPickView(
            position=pick.position if pick else None,
            reason=pick.reason if pick else None,
            unsafe_material=pick.unsafe_material if pick else False,
            glow_unchecked=group.glow_unchecked,
            options=[_rack_option(c) for c in sorted(options, key=lambda c: c.position)],
        ),
        warnings,
    )


def _check_manual_pick(
    request: PrintRunRequest, status: PrinterStatus | None, *, laid_out: bool
) -> None:
    """Spec §5: a manual pick that cannot print this is a 422 before anything is sliced.
    Judged on the flow the slice will carry, which is what Bambuddy re-checks at
    dispatch (``laid_out`` as for :func:`rack_preview`). An unreadable rack refuses
    nothing: Bambuddy still re-checks it then. A readable status with no rack at all is
    refused as that, not as one empty position."""
    if request.rack_position is None or status is None:
        return
    positions = rack_positions(status.nozzle_rack)
    if not positions:
        raise RunRefusalError(
            f"Rack position {request.rack_position} was chosen, but this printer reports no "
            "nozzle rack. Choose Automatic."
        )
    size = request.choices.nozzles[0].size
    flow = rack_volume_type(request.choices.nozzles, laid_out=laid_out)
    held = positions.get(request.rack_position)
    if held is not None and eligible(held, size, flow):
        return
    if held is None:
        holds = "holds no hotend"
    else:
        kind = "High Flow" if held.high_flow else "Standard"
        holds = f"holds a {held.nozzle_diameter} mm {kind} nozzle"
    raise RunRefusalError(
        f"Rack position {request.rack_position} {holds}, and this prints with a {size} mm "
        f"{flow} nozzle. Choose another position, or Automatic."
    )


async def check_for_output(
    client: BambuddyClient,
    store: OutputStore,
    uploads: BambuddyUploadStore,
    meta: OutputMeta,
    settings: StoredSettings,
    request: PrintRunRequest,
    *,
    rack: RackUsage | None = None,
    print_settings: dict[str, str] | None = None,
) -> PrintCheck:
    """:func:`check_print` for an output ScadBuddy rendered, with its template's
    ``print_settings`` as the run reads them, so a template that prints by object is
    checked by object (#2976)."""
    return await check_print(
        client,
        OutputSource(
            store=store,
            meta=meta,
            uploads=uploads,
            settings=settings,
            print_settings=print_settings or {},
        ),
        settings,
        request,
        rack=rack,
    )


async def check_for_library(
    client: BambuddyClient,
    uploads: BambuddyUploadStore,
    settings: StoredSettings,
    file_id: int,
    request: PrintRunRequest,
    *,
    rack: RackUsage | None = None,
    filaments: FilamentReader | None = None,
) -> PrintCheck:
    """:func:`check_print` for a file already in Bambuddy's library; with ``filaments``,
    its slots read from the file itself (#2550)."""
    return await check_print(
        client,
        await LibrarySource.load(
            client, file_id, uploads=uploads, settings=settings, filaments=filaments
        ),
        settings,
        request,
        rack=rack,
    )


@dataclass(frozen=True)
class PreparedRun:
    """What :func:`prepare_run` checked, handed on to :func:`execute_run`."""

    plate_ids: list[int]
    printer_id: int
    #: Read once, before the 202, and reused by the run rather than read again; ``None``
    #: for a sliced file, which takes no presets (#2671).
    catalogue: _Catalogue | None
    #: The printer's live status, read once for the run's hardware warnings; ``None``
    #: when it was unreadable.
    printer_status: PrinterStatus | None
    printer_name: str | None = None
    #: What a sliced file's run carries back instead of refusing (#2671).
    warnings: list[FilamentWarning] = field(default_factory=list)
    #: The resolved printer's model, which the plate warnings are judged against (#2858).
    printer_model: str | None = None


async def prepare_run(
    client: BambuddyClient,
    source: PrintSource,
    settings: StoredSettings,
    request: PrintRunRequest,
    *,
    rack: RackUsage | None = None,
    refuse_manual_pick: bool = True,
) -> PreparedRun:
    """Every refusal the request alone decides, before anything is uploaded (#470).

    This is what ``POST .../run`` makes before it answers 202: the plates exist, a
    printer is chosen and the resolver can serve it, the choices resolve to a
    printer and process preset. The mounted nozzles are not checked (#768): the printer
    handles its nozzles itself. Each is a read (the source's plates, ``/printers/``, the
    preset catalogue, the printer's status), none waits on a slice, so it stays well
    inside a proxy's timeout. What needs a plate's slots is left to
    :func:`execute_run`, because only a library file answers those. A manual rack
    position that does not fit is refused too (spec 2026-10-01 §5), unless
    ``refuse_manual_pick`` is off: the check says it as an error beside its preview.
    """
    laid_out = await source.plate_ids(client)
    plate_ids = laid_out if request.all_plates else [request.plate_id]
    if not plate_ids:
        # ScadBuddy's writer always lays out one; a 3MF edited to list none has nothing
        # to queue, and every route below reads the first plate's outcome. Read
        # from the local 3MF before anything touches Bambuddy.
        raise RunRefusalError(
            "This output's 3MF lays out no plates, so there is nothing to print.",
        )
    plate_warnings: list[FilamentWarning] = []
    if (
        source.is_sliced
        and laid_out
        and request.plate_id not in laid_out
        and not request.all_plates
    ):
        # A sliced file is never refused (#2671): the dialog offers only its plates, so
        # a request for another one prints the file's first instead, and says so.
        plate_ids = laid_out[:1]
        plate_warnings.append(
            FilamentWarning(
                kind="sliced-plate-missing",
                message=(
                    f"This file holds no plate {request.plate_id}, so plate {laid_out[0]}"
                    " is queued instead."
                ),
            )
        )
    elif laid_out and request.plate_id not in laid_out and not request.all_plates:
        # Bambuddy does not check it either: the run would upload, then fail in the
        # slicer with no reason given (#1320).
        count = f"{len(laid_out)} plate{'s' if len(laid_out) != 1 else ''}"
        raise RunRefusalError(
            f"The 3MF has {count} ({', '.join(map(str, laid_out))}); "
            f"there is no plate {request.plate_id}.",
        )
    printer_id = request.printer_id or settings.printer_id
    if printer_id is None:
        raise not_configured(
            "no printer is chosen and none is configured, so there is nothing to print on"
        )
    if source.is_sliced:
        # Nothing is sliced, so no preset need resolve (#2671): any active printer the
        # file was sliced for prints it.
        printer = await _require_resolvable_printer(client, printer_id, any_model=True)
        printer_status = await read_status(client, printer_id)
        await record_seen(rack, printer_id, printer_status)
        return PreparedRun(
            plate_ids=plate_ids,
            printer_id=printer_id,
            catalogue=None,
            printer_status=printer_status,
            printer_name=printer.name,
            printer_model=printer.model,
            warnings=plate_warnings
            + sliced_warnings(source.sliced_profile, printer, printer_status, plate_ids),
        )
    printer = await _require_resolvable_printer(client, printer_id)
    # Read once for every plate: the catalogue is ~4000 presets on the live instance.
    # Read before the upload, so that what the choices alone refuse — mixed nozzle
    # sizes, no printer or process preset — is a 422 that leaves nothing in Bambuddy's
    # library. Slot errors need the plate's slots, which only a library file answers,
    # so those are still found after the upload, by `resolve` in `execute_run`. It is
    # two GETs, the same the dialog's own choices read makes to open (`choices.py`).
    catalogue = await _catalogue(client)
    refused = choice_errors(request.choices, catalogue)
    if refused:
        raise RunRefusalError(" ".join(error.message for error in refused))
    printer_status = await read_status(client, printer_id)
    await record_seen(rack, printer_id, printer_status)
    if refuse_manual_pick and request.rack_position is not None:
        # Only a manual pick needs the file judged, which reads its 3MF (#1752).
        _check_manual_pick(
            request,
            printer_status,
            laid_out=await source.states_nozzles(client),
        )
    return PreparedRun(
        plate_ids=plate_ids,
        printer_id=printer_id,
        catalogue=catalogue,
        printer_status=printer_status,
        printer_name=printer.name,
        printer_model=printer.model,
    )


class PreparedPlates(BaseModel):
    """What :func:`prepare_run` checked, without the preset catalogue: the part that
    crosses a workflow's history (#1052). :func:`plan_run` reads the catalogue again."""

    plate_ids: list[int]
    printer_id: int
    #: Without the rack's serials: this crosses the history, and a hotend's serial goes
    #: only into the database (spec 2026-10-01 §7, #1032). Nothing after the check
    #: needs one: the enqueue ranks the rack on a status it reads itself.
    printer_status: PrinterStatus | None = None
    #: A sliced file's warnings (#2671), carried to the run's own.
    warnings: list[FilamentWarning] = Field(default_factory=list)
    #: As :attr:`PreparedRun.printer_model`. ``None`` in a run accepted before it was
    #: carried, which then says nothing of an unlisted plate.
    printer_model: str | None = None

    @field_validator("printer_status", mode="after")
    @classmethod
    def _without_serials(cls, status: PrinterStatus | None) -> PrinterStatus | None:
        if status is None or not any(slot.serial_number for slot in status.nozzle_rack):
            return status
        rack = [slot.model_copy(update={"serial_number": ""}) for slot in status.nozzle_rack]
        return status.model_copy(update={"nozzle_rack": rack})

    @classmethod
    def of(cls, prepared: PreparedRun) -> PreparedPlates:
        return cls(
            plate_ids=prepared.plate_ids,
            printer_id=prepared.printer_id,
            printer_status=prepared.printer_status,
            warnings=prepared.warnings,
            printer_model=prepared.printer_model,
        )


class PlatePlan(BaseModel):
    """One plate, resolved and ready to slice and queue."""

    plate_id: int
    #: ``None`` when nothing is sliced (:attr:`sliced_file_id`).
    plan: SlicePlan | None = None
    #: The sliced file queued as it is, with no slice (#2671): a sliced library file
    #: itself, or ScadBuddy's own earlier slice of the same copy with the same presets.
    sliced_file_id: int | None = None
    filaments: QueueFilaments | None = None
    #: Each slot's chosen spool, for the rack's Glow test (#836, spec §3).
    spools: dict[int, SpoolOption] = Field(default_factory=dict)


class PlannedRun(BaseModel):
    """Everything a run decides before its first slice (:func:`plan_run`): what was
    uploaded, each plate's plan, and every warning the result reports."""

    library_file_id: int
    folder_id: int | None = None
    project_id: int | None = None
    printer_id: int
    nozzle_size: str
    copies: int
    options: PrintOptions
    plates: list[PlatePlan]
    warnings: list[FilamentWarning] = Field(default_factory=list)
    #: How the rack is ranked, a hand-picked position, and whether there is a rack (#836).
    rack_algorithm: RackAlgorithm = DEFAULT_ALGORITHM
    rack_position: int | None = None
    has_rack: bool = False
    #: Which nozzle each filament prints from, as the 3MF states it (#2166).
    nozzle_plan: NozzlePlan | None = None


class QueuedPlate(BaseModel):
    """One plate on the queue (:func:`queue_plate`): its item, and the rack positions
    sent for it, never with a hotend's serial (spec 2026-10-01 §7), so it may cross a
    workflow's history."""

    item_id: int
    picks: list[RackSentPick] = Field(default_factory=list)
    warnings: list[FilamentWarning] = Field(default_factory=list)


async def queue_plate(
    client: BambuddyClient,
    *,
    planned: PlannedRun,
    plate: PlatePlan,
    sliced: int,
    rack: RackUsage | None,
) -> QueuedPlate:
    """Choose the rack hotends for ``plate``'s sliced file, ``POST /queue/`` once, then
    save the picks, whatever the print's subject: every print is followed and settled
    (#1073). The pick and the save never raise (spec 2026-10-01 §5): only the queue call
    can fail."""
    notes: list[FilamentWarning] = []
    choice = (
        await rack_chooser(
            client,
            printer_id=planned.printer_id,
            plate_id=plate.plate_id,
            spools=plate.spools,
            algorithm=planned.rack_algorithm,
            manual_position=planned.rack_position,
            rack=rack,
            warnings=notes,
        )(sliced)
        if planned.has_rack
        else None
    )
    item = await enqueue_plate(
        client,
        sliced=sliced,
        printer_id=planned.printer_id,
        plate_id=plate.plate_id,
        copies=planned.copies,
        project_id=planned.project_id,
        options=planned.options,
        filaments=plate.filaments,
        nozzle_rack_choice=choice.nozzle_rack_choice if choice is not None else None,
    )
    picks = list(choice.picks) if choice is not None else []
    await save_picks(rack, planned.printer_id, [item], picks, copies=planned.copies)
    return QueuedPlate(
        item_id=item,
        # Group ids repeat across plates, so each pick names its plate.
        picks=[
            RackSentPick(plate_id=plate.plate_id, group_id=pick.group_id, position=pick.position)
            for pick in picks
        ],
        warnings=notes,
    )


async def plan_run(
    client: BambuddyClient,
    source: PrintSource,
    settings: StoredSettings,
    request: PrintRunRequest,
    prepared: PreparedPlates,
    *,
    reuse_slices: bool = True,
) -> PlannedRun:
    """Upload, then resolve every plate, before anything is sliced (spec §4).

    With ``reuse_slices``, a plate ScadBuddy sliced before with the same presets queues
    that slice (#2671). The dialog's background slice passes ``False``: it slices to
    show the toolpath, and a lookup there only costs GETs (#2691 review).

    A print into a project also records its printer and nozzle for that project
    (:meth:`PrintSource.remember_project`, in the ``print_finish`` activity), which is what the
    next Generate into it lays its file out for.

    Always the slice-and-queue route: there is no pipeline to run. Bambuddy still
    decides AMS tray and extruder placement at dispatch — no ``ams_mapping`` is sent
    (spec §6).

    Every plate is resolved before any is sliced, so a slot error is a 422 with nothing
    on Bambuddy's queue, however many plates the print has. The upload reuses a copy
    Bambuddy already has (``ensure_uploaded``), so running this again uploads nothing
    new.
    """
    if source.is_sliced:
        return await _plan_sliced(client, source, settings, request, prepared)
    plate_ids = prepared.plate_ids
    printer_id = prepared.printer_id
    printer_status = prepared.printer_status
    choices = request.choices
    catalogue = await _catalogue(client)
    process = process_preset_for(choices, catalogue)
    # #717: a profile that cannot apply is skipped with a warning, never refused.
    # One read of the preset's values for the profiles and the supports (#2804 review).
    read = read_once(client)
    from_profiles, profile_warnings = await profile_overrides(
        client, process, request.filament_profiles, read=read
    )
    # The source places, recolors and uploads what it prints (#105, #126, #476), into
    # the project's folder when there is one (#79, #316).
    project_id = chosen_project(request, settings)
    # ScadBuddy plans each filament's nozzle (#2166) and states it in the 3MF.
    # One read of the inventory for the plan and every plate's options (#480).
    inventory = await read_inventory(client)
    nozzle_plan = await plan_for(
        client,
        printer_status,
        printer_id,
        request.filament_plan,
        choices.nozzles,
        choices.sides,
        inventory=inventory,
    )
    # Never refused (#1899): a support setting that cannot be shaped is left out.
    supports, support_warnings = await support_overrides(
        client, process, wanted_settings(source.print_settings, request.supports), read=read
    )
    printed = await source.file_to_print(
        client,
        printer_id=printer_id,
        nozzle_size=choices.nozzles[0].size,
        plan=request.filament_plan,
        project_id=project_id,
        # Only the sides the plan uses, or else the sides with the nozzle, are offered
        # to the slicer (#834, #2181), and each side states the flow chosen for it (#484).
        nozzle_stats=(
            nozzle_plan.nozzle_stats(choices.nozzles)
            if nozzle_plan is not None
            else slicer_nozzle_stats(printer_status, choices.nozzles)
        ),
        nozzle_volume_type=slicer_volume_types(choices.nozzles),
        tray_colours=tray_colours(
            [slot.spool_id for slot in request.filament_plan.slots], printer_status
        ),
        filament_map=_filament_map(nozzle_plan, len(source.colours)),
    )
    library_file_id = printed.id
    # The picker's project is its own control (ProjectPicker, defaulting to the last
    # one), so a remembered project_id is dropped here rather than half-applied.
    print_options = resolve_print_options(
        settings, source.options_scope, printer_id, request_scope(request.copies, request.options)
    ).model_copy(update={"project_id": None})
    copies = print_options.quantity or 1

    # Each plate's slots are read on their own: a plate uses only some of the project's
    # filaments (#83). The one plan applies to every plate because a slot is a project
    # filament, not a plate position (#180), so slot 2 is the same colour on every plate.
    ready: list[tuple[int, FilamentOptions, Resolved, SlicePlan]] = []
    per_plate = await gather_plate_options(
        client,
        library_file_id=library_file_id,
        printer_id=printer_id,
        plate_ids=plate_ids,
        fallback_colours=list(source.colours),
        inventory=inventory,
        # The file's own slots when it was read (#2550, #2720): a plate's colours are the
        # model's, not the run's recoloured copy's, as the dialog showed them. Intended.
        known=known_slots(source),
    )
    per_plate = [with_trays(options, printer_status, printer_id) for options in per_plate]
    every, errors = await resolve_plates(
        client, plate_ids, per_plate, request.filament_plan, choices, catalogue
    )
    for plate_id, options, resolved in zip(plate_ids, per_plate, every, strict=True):
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
            whole_file=source.slices_whole_file,
            printer_preset=resolved.printer_preset,
            process_preset=resolved.process_preset,
            filament_presets=resolved.filament_presets,
            filament_colours=resolved.filament_colours,
            bed_type=resolved.bed_type,
            process_overrides={
                **from_profiles,
                **{k: v for k, v in source.print_settings.items() if k not in SUPPORT_KEYS},
                **supports,
                **(
                    {}
                    if request.print_sequence is None
                    else {"print_sequence": request.print_sequence}
                ),
            },
        )
        ready.append((plate_id, options, resolved, plan))
    if errors:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, " ".join(errors))
    # ScadBuddy's own slice of this copy with these presets, when it has one (#2671):
    # every plate's looked up together, each a few GETs (#2691 review).
    reused: list[int | None] = (
        list(
            await asyncio.gather(
                *(
                    source.recorded_slice(
                        client, library_file_id, slice_request(plan, plate_id).preset_key
                    )
                    for plate_id, _, _, plan in ready
                )
            )
        )
        if reuse_slices
        else [None] * len(ready)
    )
    planned = [(*plate, sliced) for plate, sliced in zip(ready, reused, strict=True)]

    hardware = await _hardware_warnings(
        client,
        printer_id,
        choices,
        printer_status,
        printer_name=planned[0][1].printer_name,
        printer_model=prepared.printer_model,
    )
    hardware += high_flow_warnings(
        printer_status,
        choices.nozzles,
        laid_out=await source.states_nozzles(client),
        sides=nozzle_plan.sides if nozzle_plan is not None else None,
    )
    warnings = profile_warnings + _filament_warnings(
        [(options, resolved.warnings) for _, options, resolved, _, _ in planned],
        request.filament_plan,
        copies,
    )
    return PlannedRun(
        library_file_id=library_file_id,
        folder_id=printed.folder_id,
        project_id=project_id,
        printer_id=printer_id,
        nozzle_size=choices.nozzles[0].size,
        copies=copies,
        options=print_options,
        plates=[
            PlatePlan(
                plate_id=plate_id,
                plan=plan,
                sliced_file_id=reused,
                filaments=queue_filaments(options, request.filament_plan),
                spools=_spools_by_slot(options, request.filament_plan),
            )
            for plate_id, options, _, plan, reused in planned
        ],
        warnings=warnings + hardware + support_warnings + _reused_notes(planned),
        rack_algorithm=request.rack_algorithm or settings.rack_algorithm(printer_id),
        rack_position=request.rack_position,
        # A readable status with no rack (any printer but an H2C) has nothing to pick: no
        # extra read per plate, and no rack warning on a printer without one. An
        # unreadable status still tries, since the pick reads a fresh one (#1043).
        has_rack=printer_status is None or bool(rack_positions(printer_status.nozzle_rack)),
        nozzle_plan=nozzle_plan,
    )


def _reused_notes(
    planned: list[tuple[int, FilamentOptions, Resolved, SlicePlan, int | None]],
) -> list[FilamentWarning]:
    """Say which plates queue an earlier slice rather than slicing again (#2955): the
    run's ``slice_job_id`` is ``None`` then, which would otherwise read as a slice
    skipped by mistake."""
    return [
        FilamentWarning(
            kind="slice-reused",
            message=f"Plate {plate_id} queues ScadBuddy's earlier slice of the same choices;"
            " nothing was sliced again.",
        )
        for plate_id, _, _, _, reused in planned
        if reused is not None
    ]


def _filament_warnings(
    plates: list[tuple[FilamentOptions, list[FilamentWarning]]],
    plan: FilamentPlan,
    copies: int,
) -> list[FilamentWarning]:
    """Each plate's warnings (``plates`` pairs its options with the resolver's), once
    each, then low filament judged against what every plate needs together."""
    warnings: list[FilamentWarning] = []
    for options, resolved in plates:
        for warning in [*resolved, *check(options, plan, copies=copies)]:
            # Checked once below, against what every plate needs together.
            if warning.kind != "low-filament" and warning not in warnings:
                warnings.append(warning)
    warnings += [
        warning
        for warning in check(across_plates([options for options, _ in plates]), plan, copies=copies)
        if warning.kind == "low-filament"
    ]
    return warnings


async def _plan_sliced(
    client: BambuddyClient,
    source: PrintSource,
    settings: StoredSettings,
    request: PrintRunRequest,
    prepared: PreparedPlates,
) -> PlannedRun:
    """:func:`plan_run` for a sliced file (#2671): nothing is uploaded, resolved or
    sliced. Each plate queues the file itself, with the spools chosen for its slots as
    any print's are, and with the options remembered for it, held for a manual start
    among them. A file sliced for another model or a nozzle the printer lacks is held
    for a manual start whatever was remembered (#2954): Bambuddy would otherwise start
    it as soon as the printer is idle, before anyone read the warning."""
    printer_id = prepared.printer_id
    project_id = chosen_project(request, settings)
    profile = source.sliced_profile
    nozzle_size = sliced_nozzle_size(profile, prepared, request)
    printed = await source.file_to_print(
        client,
        printer_id=printer_id,
        nozzle_size=nozzle_size,
        plan=request.filament_plan,
        project_id=project_id,
    )
    per_plate = await gather_plate_options(
        client,
        library_file_id=printed.id,
        printer_id=printer_id,
        plate_ids=prepared.plate_ids,
        fallback_colours=list(source.colours),
        known=known_slots(source),
    )
    per_plate = [with_trays(options, prepared.printer_status, printer_id) for options in per_plate]
    print_options = resolve_print_options(
        settings, source.options_scope, printer_id, request_scope(request.copies, request.options)
    ).model_copy(update={"project_id": None})
    if any(warning.kind in HELD_SLICED_KINDS for warning in prepared.warnings):
        print_options = print_options.model_copy(update={"manual_start": True})
    copies = print_options.quantity or 1
    status = prepared.printer_status
    return PlannedRun(
        library_file_id=printed.id,
        project_id=project_id,
        printer_id=printer_id,
        nozzle_size=nozzle_size,
        copies=copies,
        options=print_options,
        plates=[
            PlatePlan(
                plate_id=plate_id,
                sliced_file_id=printed.id,
                filaments=queue_filaments(options, request.filament_plan),
                spools=_spools_by_slot(options, request.filament_plan),
            )
            for plate_id, options in zip(prepared.plate_ids, per_plate, strict=True)
        ],
        warnings=prepared.warnings
        + _filament_warnings(
            [(options, []) for options in per_plate], request.filament_plan, copies
        ),
        rack_algorithm=request.rack_algorithm or settings.rack_algorithm(printer_id),
        has_rack=status is None or bool(rack_positions(status.nozzle_rack)),
    )


def _filament_map(plan: NozzlePlan | None, filaments: int) -> FilamentMap | None:
    """The plan as the 3MF states it, for a file of ``filaments`` filaments."""
    return plan.stated_map(filaments) if plan is not None else None


def finish_run(
    config: BambuddyConfig | None,
    planned: PlannedRun,
    outcomes: list[QueueOutcome],
    queued: list[QueuedPlate] | None = None,
) -> PrintRunResult:
    """Report what was queued, with each plate's rack picks and warnings (``queued``,
    one per outcome), linked to Bambuddy's queue unless its settings are gone (``None``)."""
    warnings = list(planned.warnings)
    if queued and all(plate.picks for plate in queued):
        # Every plate had the rack side's hotend picked from the rack, which is always one
        # of the size and of the flow sliced there: the one mounted now is swapped out
        # (#1238), whichever flow the slice is for.
        swapped = [high_flow_warning(RACK_SIDE, flow) for flow in VOLUME_TYPE]
        warnings = [warning for warning in warnings if warning not in swapped]
    for plate in queued or []:
        # A rack warning repeated on every plate is one fact, shown once (spec §6).
        warnings += [warning for warning in plate.warnings if warning not in warnings]
    return _queued(
        config,
        outcomes,
        planned.library_file_id,
        planned.project_id,
        planned.folder_id,
        copies=planned.copies,
        rack_picks=[pick for plate in queued or [] for pick in plate.picks],
        warnings=warnings,
    )


async def _require_resolvable_printer(
    client: BambuddyClient, printer_id: int, *, any_model: bool = False
) -> Printer:
    """A 422 before anything is uploaded or sliced when the resolver cannot serve this
    printer: one Bambuddy does not list, one it has deactivated (#479; the dialog never
    offers those), or any model but the H2C, whose presets are the only ones the
    resolver knows (``PRINTER_MODEL``). ``any_model``: nothing is resolved (a sliced
    file, #2671), so any model will do."""
    printer = next((row for row in await client.printers() if row.id == printer_id), None)
    if printer is None:
        raise RunRefusalError(
            f"Bambuddy has no printer {printer_id}. Pick another printer.",
        )
    if not printer.is_active:
        raise RunRefusalError(
            f"{printer.name} is deactivated in Bambuddy. Activate it there, or pick "
            "another printer.",
        )
    if not any_model and (printer.model or "").upper() != PRINTER_MODEL:
        raise RunRefusalError(
            f"ScadBuddy can only choose slicer presets for a Bambu Lab {PRINTER_MODEL} so "
            f"far, and {printer.name}'s model is {printer.model or 'not reported'}.",
        )
    return printer


def sliced_nozzle_size(
    profile: SlicedProfile | None, prepared: PreparedPlates, request: PrintRunRequest
) -> str:
    """The nozzle a sliced file's run records: the file's own, else the printer's. The
    dialog's choice is hidden for a sliced file and says nothing of it (#2691 review),
    so it is taken only when neither is known."""
    sizes = profile.diameters_for(prepared.plate_ids) if profile is not None else []
    if sizes:
        return sizes[0]
    mounted = sorted(_mounted_diameters(prepared.printer_status))
    return mounted[0] if mounted else request.choices.nozzles[0].size


def _mounted_diameters(status: PrinterStatus | None) -> set[str]:
    """Every nozzle diameter the printer reports, mounted or in its rack."""
    if status is None:
        return set()
    return {
        size
        for nozzle in [*status.nozzles, *status.nozzle_rack]
        if (size := diameter(nozzle.nozzle_diameter)) is not None
    }


HELD_SLICED_KINDS = frozenset({"sliced-other-model", "sliced-nozzle-missing"})
"""Sliced-file warnings that hold the print for a manual start (#2954)."""


def sliced_warnings(
    profile: SlicedProfile | None,
    printer: Printer,
    status: PrinterStatus | None,
    plate_ids: list[int],
) -> list[FilamentWarning]:
    """What to set right before a sliced file prints on ``printer`` (#2671): sliced for
    another model, or for a nozzle diameter the printer does not have. Never a refusal:
    it is queued as it is, held for a manual start (:data:`HELD_SLICED_KINDS`), and the
    user swaps the nozzle, or picks another printer, before starting it. Only a known
    mismatch warns, and an unread profile, or a printer that reports no model, says
    nothing; a printer that reports no nozzles is said to leave the nozzle unchecked."""
    if profile is None:
        return []
    warnings: list[FilamentWarning] = []
    wanted, have = model_key(profile.printer_model), model_key(printer.model)
    if wanted is not None and have is not None and wanted != have:
        warnings.append(
            FilamentWarning(
                kind="sliced-other-model",
                message=(
                    f"Sliced for a {profile.printer_model}; {printer.name} is a {printer.model}."
                    " Held for a manual start."
                ),
            )
        )
    mounted = _mounted_diameters(status)
    wanted_sizes = profile.diameters_for(plate_ids)
    if wanted_sizes and not mounted:
        # Unchecked is not the same as fine: say so, and hold it (#2691 review).
        warnings.append(
            FilamentWarning(
                kind="sliced-nozzle-missing",
                message=(
                    f"Sliced for a {' and '.join(wanted_sizes)} mm nozzle; {printer.name}"
                    " reports no nozzle, so it could not be checked."
                    " Held for a manual start: check the nozzle before you start the print."
                ),
            )
        )
    missing = [size for size in wanted_sizes if size not in mounted]
    if mounted and missing:
        warnings.append(
            FilamentWarning(
                kind="sliced-nozzle-missing",
                message=(
                    f"Sliced for a {' and '.join(missing)} mm nozzle; "
                    f"{', '.join(sorted(mounted))} mm is mounted on {printer.name}."
                    " Held for a manual start: swap it before you start the print."
                ),
            )
        )
    return warnings


async def _hardware_warnings(
    client: BambuddyClient,
    printer_id: int,
    choices: PrintChoices,
    printer_status: PrinterStatus | None,
    *,
    printer_name: str | None,
    printer_model: str | None,
) -> list[FilamentWarning]:
    """The chosen nozzles and plate against what the printer reports (spec §4.4-4.5).

    Advisory only: an offline printer's status and an unreadable archive list mean
    "unknown", which says nothing rather than failing the print.
    """
    installed = installed_nozzles(printer_status)
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
    found.append(plate_unlisted_warning(choices.bed_type, printer_model, printer_name))
    return [warning for warning in found if warning is not None]


def _queued(
    config: BambuddyConfig | None,
    outcomes: list[QueueOutcome],
    library_file_id: int,
    project_id: int | None,
    folder_id: int | None,
    copies: int,
    rack_picks: list[RackSentPick],
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
        bambuddy_url=config.web_url(QUEUE_PATH) if config is not None else None,
        rack_picks=rack_picks,
    )


async def filament_options_for_library(
    client: BambuddyClient,
    uploads: BambuddyUploadStore,
    settings: StoredSettings,
    file_id: int,
    *,
    printer_id: int | None = None,
    plate_id: int = 1,
    all_plates: bool = False,
    filaments: FilamentReader | None = None,
) -> FilamentOptions:
    """:func:`filament_options` for a file already in Bambuddy's library (#313); with
    ``filaments``, its slots read from the file itself (#2550)."""
    return await filament_options(
        client,
        await LibrarySource.load(
            client, file_id, uploads=uploads, settings=settings, filaments=filaments
        ),
        printer_id=printer_id,
        plate_id=plate_id,
        all_plates=all_plates,
    )
