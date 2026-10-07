"""Slicing an output for print: the filament step's options, and the spool-first run.

ScadBuddy owns no slicing settings. The print dialog's run (:func:`execute_run`)
derives every preset from the dialog's choices — spools, nozzles, quality and plate
(spec 2026-09-27 §4) — and always slices then queues; there is no pipeline to run or
choose from here. The send bar only uploads (#312); this is the only path that prints.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from fastapi import status
from pydantic import BaseModel, Field

from scadbuddy.bambuddy.catalogue import _Catalogue, _catalogue
from scadbuddy.bambuddy.client import BambuddyClient, BambuddyConfig
from scadbuddy.bambuddy.dispatch import QueueOutcome, RackChoice, SlicePlan, enqueue_plate
from scadbuddy.bambuddy.errors import not_configured
from scadbuddy.bambuddy.extruders import high_flow_warnings, slicer_nozzle_stats, with_sides
from scadbuddy.bambuddy.filaments import (
    FilamentOptions,
    FilamentPlan,
    FilamentWarning,
    QueueFilaments,
    SpoolOption,
    across_plates,
    check,
    every_plate,
    gather_plate_options,
    queue_filaments,
)
from scadbuddy.bambuddy.hardware import (
    installed_nozzles,
    last_bed_type,
    nozzle_warning,
    plate_warning,
)
from scadbuddy.bambuddy.models import (
    DEFAULT_ALGORITHM,
    FilamentRequirements,
    FlowType,
    PrinterStatus,
    RackAlgorithm,
    Spool,
)
from scadbuddy.bambuddy.options import PrintOptions
from scadbuddy.bambuddy.print_source import LibrarySource, OutputSource, PrintSource
from scadbuddy.bambuddy.resolver import (
    PRINTER_MODEL,
    PrintChoices,
    Resolved,
    choice_errors,
    resolve,
)
from scadbuddy.bambuddy.send import request_scope, resolve_print_options
from scadbuddy.bambuddy.uploads import BambuddyUploadStore
from scadbuddy.core.problems import ApiError
from scadbuddy.library.catalogue import PrintSequence
from scadbuddy.library.outputs import OutputMeta, OutputStore
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.rack.rank import (
    SLICED_VOLUME_TYPE,
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
from scadbuddy.rack.usage import PickedHotend, RackUsage, record_seen, save_picks

logger = logging.getLogger(__name__)

#: The three rack fallbacks that swallow an exception and log only its type (§7). Named
#: so the test guard reads these, not copies of them (#1081, #1086 review).
RACK_PICK_FALLBACK = "rack pick left to Bambuddy"
RACK_PREVIEW_FALLBACK = "the rack preview could not be built"
RACK_USAGE_FALLBACK = "rack usage unreadable; ranked without it"
RACK_FALLBACKS = frozenset({RACK_PICK_FALLBACK, RACK_PREVIEW_FALLBACK, RACK_USAGE_FALLBACK})

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


class RackOption(BaseModel):
    """One eligible rack position, as the dialog lists it (spec 2026-10-01 §5). Built
    field by field from a :class:`RackCandidate`, which carries no serial (§7)."""

    position: int
    nozzle_diameter: str
    flow: FlowType
    color: str | None = None
    nozzle_type: str
    #: Decoded from the code through ``rack.rank.NOZZLE_MATERIALS``; ``None`` is unknown.
    material: str | None = None
    prints: int = 0
    print_seconds: int = 0


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


async def filament_options(
    client: BambuddyClient,
    source: PrintSource,
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
    read_file = await source.file_to_read(client)
    library_file_id = read_file.id
    plate_ids = ((await source.plate_ids(client)) or [1]) if all_plates else [plate_id]
    read = await gather_plate_options(
        client,
        library_file_id=library_file_id,
        printer_id=printer_id,
        plate_ids=plate_ids,
        fallback_colours=list(source.colours),
        own_colours=read_file.own_colours,
    )
    options = read[0] if len(read) == 1 else every_plate(read)
    if printer_id is None:
        return options
    try:
        printer_status = await client.printer_status(printer_id)
    except (ApiError, ValueError):
        logger.info("printer status unreadable; the filament step opens with no nozzles known")
        options.nozzles = []
        return options
    options.nozzles = printer_status.nozzles
    # Each loaded spool's side, as a label in the picker (#469); nothing is checked
    # against it (#768).
    return with_sides(options, printer_status)


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
        OutputSource(store, uploads, meta, settings),
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


async def _read_status(client: BambuddyClient, printer_id: int) -> PrinterStatus | None:
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
                        PickedHotend(group_id=group_id, position=pick.position, serial=pick.serial)
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
    slice through the one 0.2 mm nozzle. It warns only of a mounted High Flow nozzle of
    the size, whatever flow is chosen, since the slice is always Standard flow (#723,
    #797, #484). It never carries the resolver's own ``hf-unsupported`` note for a
    chosen High Flow nozzle (#862): that one is left to the run and the nozzle step.
    What needs the uploaded file is still found by the run. Only the run's own refusals
    (:class:`RunRefusalError`) become ``errors``: a failed read of Bambuddy fails the
    check, as it would fail the run. With no printer chosen or configured there is
    nothing to judge, and the run says why."""
    if (request.printer_id or settings.printer_id) is None:
        return PrintCheck()
    try:
        prepared = await prepare_run(
            client, source, settings, request, rack=rack, refuse_manual_pick=False
        )
    except RunRefusalError as refused:
        return PrintCheck(errors=[refused.detail])
    rack_view, rack_notes = await rack_preview(
        client,
        request,
        settings,
        printer_id=prepared.printer_id,
        status=prepared.printer_status,
        rack=rack,
    )
    # A refused manual pick still previews the rack, so the dialog can offer another,
    # and is said once: as the error, not again as a rack-manual-partial warning.
    try:
        _check_manual_pick(request, prepared.printer_status)
    except RunRefusalError as refused:
        errors = [refused.detail]
        rack_notes = [note for note in rack_notes if note.kind != "rack-manual-partial"]
    else:
        errors = []
    # The one mounted-nozzle advisory kept (#723): a warning, never a refusal.
    return PrintCheck(
        errors=errors,
        warnings=[
            *high_flow_warnings(prepared.printer_status, request.choices.nozzles),
            *rack_notes,
        ],
        rack=rack_view,
    )


def _rack_option(candidate: RackCandidate) -> RackOption:
    return RackOption(
        position=candidate.position,
        nozzle_diameter=candidate.nozzle_diameter,
        flow="high_flow" if candidate.high_flow else "standard",
        color=candidate.color,
        nozzle_type=candidate.nozzle_type,
        material=candidate.material,
        prints=candidate.prints,
        print_seconds=candidate.print_seconds,
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
) -> tuple[RackPickView | None, list[FilamentWarning]]:
    """The rack side ranked as one group from the dialog's size and spools (spec §5):
    the preview ``/check`` shows. Judged on the flow the slice will carry (Standard until
    #484). Every chosen spool counts toward the material test, since the slice may put any
    of them on the rack side. Advisory: a failure previews nothing."""
    if status is None or not rack_positions(status.nozzle_rack):
        return None, []
    try:
        inventory = {spool.id: spool for spool in await client.spools()}
        chosen = [inventory.get(slot.spool_id) for slot in request.filament_plan.slots]
        known = [spool for spool in chosen if spool is not None]
        first = known[0].rgba if known else None
        group = RackGroup(
            group_id=0,
            nozzle_diameter=request.choices.nozzles[0].size,
            volume_type=SLICED_VOLUME_TYPE,
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


def _check_manual_pick(request: PrintRunRequest, status: PrinterStatus | None) -> None:
    """Spec §5: a manual pick that cannot print this is a 422 before anything is sliced.
    Judged on the flow the slice will carry, which is what Bambuddy re-checks at
    dispatch. An unreadable rack refuses nothing: Bambuddy still re-checks it then. A
    readable status with no rack at all is refused as that, not as one empty position."""
    if request.rack_position is None or status is None:
        return
    positions = rack_positions(status.nozzle_rack)
    if not positions:
        raise RunRefusalError(
            f"Rack position {request.rack_position} was chosen, but this printer reports no "
            "nozzle rack. Choose Automatic."
        )
    size = request.choices.nozzles[0].size
    held = positions.get(request.rack_position)
    if held is not None and eligible(held, size, SLICED_VOLUME_TYPE):
        return
    if held is None:
        holds = "holds no hotend"
    else:
        flow = "High Flow" if held.high_flow else "Standard"
        holds = f"holds a {held.nozzle_diameter} mm {flow} nozzle"
    raise RunRefusalError(
        f"Rack position {request.rack_position} {holds}, and this prints with a {size} mm "
        f"{SLICED_VOLUME_TYPE} nozzle. Choose another position, or Automatic."
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
) -> PrintCheck:
    """:func:`check_print` for an output ScadBuddy rendered."""
    return await check_print(
        client, OutputSource(store, uploads, meta, settings), settings, request, rack=rack
    )


async def check_for_library(
    client: BambuddyClient,
    settings: StoredSettings,
    file_id: int,
    request: PrintRunRequest,
    *,
    rack: RackUsage | None = None,
) -> PrintCheck:
    """:func:`check_print` for a file already in Bambuddy's library."""
    return await check_print(
        client, await LibrarySource.load(client, file_id), settings, request, rack=rack
    )


@dataclass(frozen=True)
class PreparedRun:
    """What :func:`prepare_run` checked, handed on to :func:`execute_run`."""

    plate_ids: list[int]
    printer_id: int
    #: Read once, before the 202, and reused by the run rather than read again.
    catalogue: _Catalogue
    #: The printer's live status, read once for the run's hardware warnings; ``None``
    #: when it was unreadable.
    printer_status: PrinterStatus | None


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
    if laid_out and request.plate_id not in laid_out and not request.all_plates:
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
    await _require_resolvable_printer(client, printer_id)
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
    printer_status = await _read_status(client, printer_id)
    await record_seen(rack, printer_id, printer_status)
    if refuse_manual_pick:
        _check_manual_pick(request, printer_status)
    return PreparedRun(
        plate_ids=plate_ids,
        printer_id=printer_id,
        catalogue=catalogue,
        printer_status=printer_status,
    )


class PreparedPlates(BaseModel):
    """What :func:`prepare_run` checked, without the preset catalogue: the part that
    crosses a workflow's history (#1052). :func:`plan_run` reads the catalogue again."""

    plate_ids: list[int]
    printer_id: int
    printer_status: PrinterStatus | None = None

    @classmethod
    def of(cls, prepared: PreparedRun) -> PreparedPlates:
        return cls(
            plate_ids=prepared.plate_ids,
            printer_id=prepared.printer_id,
            printer_status=prepared.printer_status,
        )


class PlatePlan(BaseModel):
    """One plate, resolved and ready to slice and queue."""

    plate_id: int
    plan: SlicePlan
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
    credit: bool,
) -> QueuedPlate:
    """Choose the rack hotends for ``plate``'s sliced file, ``POST /queue/`` once, then
    save the picks (``credit``: only an output's print is ever settled, so a pick row
    for a library run would never be credited). The pick and the save never raise
    (spec 2026-10-01 §5): only the queue call can fail."""
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
    if credit:
        await save_picks(rack, planned.printer_id, [item], picks)
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
) -> PlannedRun:
    """Upload, then resolve every plate, before anything is sliced (spec §4).

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
    plate_ids = prepared.plate_ids
    printer_id = prepared.printer_id
    printer_status = prepared.printer_status
    choices = request.choices
    catalogue = await _catalogue(client)
    # The source places, recolors and uploads what it prints (#105, #126, #476), into
    # the project's folder when there is one (#79, #316).
    project_id = chosen_project(request, settings)
    printed = await source.file_to_print(
        client,
        printer_id=printer_id,
        nozzle_size=choices.nozzles[0].size,
        plan=request.filament_plan,
        project_id=project_id,
        # Only the side with the nozzle is offered to the slicer (#834).
        nozzle_stats=slicer_nozzle_stats(printer_status, choices.nozzles[0].size),
    )
    library_file_id = printed.id
    # The picker's project is its own control (ProjectPicker, defaulting to the last
    # one), so a remembered project_id is dropped here rather than half-applied.
    print_options = resolve_print_options(
        settings, source.options_slug, printer_id, request_scope(request.copies, request.options)
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
    per_plate = await gather_plate_options(
        client,
        library_file_id=library_file_id,
        printer_id=printer_id,
        plate_ids=plate_ids,
        fallback_colours=list(source.colours),
    )
    for plate_id, options in zip(plate_ids, per_plate, strict=True):
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
            process_overrides=(
                source.print_settings
                if request.print_sequence is None
                else {**source.print_settings, "print_sequence": request.print_sequence}
            ),
        )
        planned.append((plate_id, options, resolved, plan))
    if errors:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, " ".join(errors))

    hardware = await _hardware_warnings(
        client, printer_id, choices, printer_status, printer_name=planned[0][1].printer_name
    )
    hardware += high_flow_warnings(printer_status, choices.nozzles)
    warnings: list[FilamentWarning] = []
    for _, options, resolved, _ in planned:
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
                filaments=queue_filaments(options, request.filament_plan),
                spools=_spools_by_slot(options, request.filament_plan),
            )
            for plate_id, options, _, plan in planned
        ],
        warnings=warnings + hardware,
        rack_algorithm=request.rack_algorithm or settings.rack_algorithm(printer_id),
        rack_position=request.rack_position,
        # A readable status with no rack (any printer but an H2C) has nothing to pick: no
        # extra read per plate, and no rack warning on a printer without one. An
        # unreadable status still tries, since the pick reads a fresh one (#1043).
        has_rack=printer_status is None or bool(rack_positions(printer_status.nozzle_rack)),
    )


def finish_run(
    config: BambuddyConfig | None,
    planned: PlannedRun,
    outcomes: list[QueueOutcome],
    queued: list[QueuedPlate] | None = None,
) -> PrintRunResult:
    """Report what was queued, with each plate's rack picks and warnings (``queued``,
    one per outcome), linked to Bambuddy's queue unless its settings are gone (``None``)."""
    warnings = list(planned.warnings)
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


async def _require_resolvable_printer(client: BambuddyClient, printer_id: int) -> None:
    """A 422 before anything is uploaded or sliced when the resolver cannot serve this
    printer: one Bambuddy does not list, one it has deactivated (#479; the dialog never
    offers those), or any model but the H2C, whose presets are the only ones the
    resolver knows (``PRINTER_MODEL``)."""
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
    if (printer.model or "").upper() != PRINTER_MODEL:
        raise RunRefusalError(
            f"ScadBuddy can only choose slicer presets for a Bambu Lab {PRINTER_MODEL} so "
            f"far, and {printer.name}'s model is {printer.model or 'not reported'}.",
        )


async def _hardware_warnings(
    client: BambuddyClient,
    printer_id: int,
    choices: PrintChoices,
    printer_status: PrinterStatus | None,
    *,
    printer_name: str | None,
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
    file_id: int,
    *,
    printer_id: int | None = None,
    plate_id: int = 1,
    all_plates: bool = False,
) -> FilamentOptions:
    """:func:`filament_options` for a file already in Bambuddy's library (#313)."""
    return await filament_options(
        client,
        await LibrarySource.load(client, file_id),
        printer_id=printer_id,
        plate_id=plate_id,
        all_plates=all_plates,
    )
