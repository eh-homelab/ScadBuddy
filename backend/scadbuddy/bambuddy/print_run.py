"""Slicing an output for print: the filament step's options, and the spool-first run.

ScadBuddy owns no slicing settings. The print dialog's run (:func:`execute_run`)
derives every preset from the dialog's choices — spools, nozzles, quality and plate
(spec 2026-09-27 §4) — and always slices then queues; there is no pipeline to run or
choose from here. The send bar only uploads (#312); this is the only path that prints.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Annotated, Literal, Protocol, Self

from fastapi import status
from pydantic import BaseModel, Field, field_validator, model_validator

from scadbuddy.bambuddy.catalogue import Catalogue, ReadCatalogue
from scadbuddy.bambuddy.clearance import (
    check_plates,
    clearance_for,
)
from scadbuddy.bambuddy.client import BambuddyClient, BambuddyConfig
from scadbuddy.bambuddy.concurrent import settled
from scadbuddy.bambuddy.dispatch import (
    QueueOutcome,
    RackChoice,
    SlicePlan,
    enqueue_plate,
    slice_request,
)
from scadbuddy.bambuddy.errors import is_transient, not_configured
from scadbuddy.bambuddy.extruders import (
    LEFT,
    RACK_SIDE,
    RIGHT,
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
    MAX_PLATES,
    FilamentOptions,
    FilamentPlan,
    FilamentWarning,
    Inventory,
    QueueFilaments,
    SlotNeed,
    SpoolOption,
    across_plates,
    asked_plates,
    build_options,
    check,
    every_plate,
    family,
    fold_plates,
    gather_plate_options,
    normalise_colour,
    plates_named,
    queue_filaments,
    read_inventory,
)
from scadbuddy.bambuddy.filaments3mf import SlicedProfile, diameter, model_key, same_family
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
    SpoolFilamentPreset,
)
from scadbuddy.bambuddy.nozzle_plan import NozzlePlan, PlanInput, plan_for, plan_inputs
from scadbuddy.bambuddy.options import PrintOptions
from scadbuddy.bambuddy.prime_tower import (
    PrimeTowerChoice,
    not_resliced,
    prime_tower_overrides,
    prime_tower_warnings,
    template_keys_dropped,
    tower_dropped,
)
from scadbuddy.bambuddy.print_source import (
    FilamentReader,
    LibrarySource,
    OutputSource,
    PrintSource,
    ReadOutOfMemoryError,
    SharedFilamentReader,
    bambuddy_client,
)
from scadbuddy.bambuddy.projects import require_destination_folder
from scadbuddy.bambuddy.resolver import (
    PRINTER_MODEL,
    PrintChoices,
    Resolved,
    choice_errors,
    process_preset_for,
    resolve,
)
from scadbuddy.bambuddy.send import request_scope, resolve_print_options
from scadbuddy.bambuddy.slot_presets import SlotPresets
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
from scadbuddy.print_library.copies import (
    NATIVE_PROJECT_REFUSAL,
    native_destination,
    require_native_folder,
)
from scadbuddy.print_library.reads import LibraryReads, NativeCopies
from scadbuddy.printers.hot_path import hot_path_native, native_backends
from scadbuddy.printers.lan_models import LanReport, LanStarted
from scadbuddy.printers.models import PrinterRow
from scadbuddy.printers.status import is_fresh, status_from_report
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
from scadbuddy.slicing.catalogue import catalogue_for
from scadbuddy.slicing.profiles import ProfileError, ProfileImageError, Profiles
from scadbuddy.slicing.values import values_for
from scadbuddy.spools.models import Overlay

if TYPE_CHECKING:
    from scadbuddy.printers.reports import PrinterReportStore
    from scadbuddy.printers.store import PrinterStore

    # `slicing.start` imports the slice workflow, whose models import this module.
    from scadbuddy.slicing.start import NativeSliceStarter

logger = logging.getLogger(__name__)

#: How often a check that runs out of memory logs its traceback, per subject (#4632):
#: the dialog checks again on every change, so a file too large to hold here would log
#: the same stack each time. Between two, it logs one line.
OUT_OF_MEMORY_TRACEBACK_INTERVAL = 600.0
#: When each subject's last traceback was logged, by `time.monotonic()`.
_out_of_memory_logged: dict[str, float] = {}


def _log_out_of_memory(
    subject: str, where: str, *, clock: Callable[[], float] = time.monotonic
) -> None:
    """Log a check's `MemoryError` (#4598): with its traceback at most once per
    :data:`OUT_OF_MEMORY_TRACEBACK_INTERVAL` per subject, else one line. Called inside
    the ``except``, so the traceback is the error being handled. ``clock`` is what the
    interval is read from; a test hands in its own."""
    now = clock()
    last = _out_of_memory_logged.get(subject)
    traceback = last is None or now - last >= OUT_OF_MEMORY_TRACEBACK_INTERVAL
    if traceback:
        # Kept bounded in a long-lived process (#4659 review): an entry past the interval
        # would log its traceback anyway, so it goes.
        for stale in [
            key
            for key, at in _out_of_memory_logged.items()
            if now - at >= OUT_OF_MEMORY_TRACEBACK_INTERVAL
        ]:
            del _out_of_memory_logged[stale]
        _out_of_memory_logged[subject] = now
    logger.warning(
        "print check ran out of memory %s",
        where,
        extra={"subject": subject, "traceback_logged": traceback},
        exc_info=traceback,
    )


def forget_out_of_memory_logs() -> None:
    """Forget when each traceback was logged: each test starts with none."""
    _out_of_memory_logged.clear()


#: The three rack fallbacks that swallow an exception and log only its type (§7). Named
#: so the test guard reads these, not copies of them (#1081, #1086 review).
RACK_PICK_FALLBACK = "rack pick left to Bambuddy"
RACK_PREVIEW_FALLBACK = "the rack preview could not be built"
RACK_USAGE_FALLBACK = "rack usage unreadable; ranked without it"
RACK_FALLBACKS = frozenset({RACK_PICK_FALLBACK, RACK_PREVIEW_FALLBACK, RACK_USAGE_FALLBACK})

QUEUE_PATH = "/queue"

#: The most spools whose presets one check or run reads at once (#4196).
SPOOL_PRESET_READS = 4


class PrintRunRequest(BaseModel):
    """The print dialog's choices (spec 2026-09-27 §2, §4): spools, nozzles, quality and
    plate. Every slicer preset is derived from them by the resolver; there is no
    pipeline to name, and the run always slices then queues.
    """

    #: The printer to queue on, whose loaded spools and rack the dialog showed. Omitted
    #: means the configured printer. It is also the printer the per-printer option scope
    #: keys on.
    printer_id: int | None = None
    #: The registry printer to print on (#3823, plan 2026-10-16 Task 8), by its id. Sent
    #: with ``printer_id``, the row must link that Bambuddy printer, or the run is
    #: refused (``prepare_run``). Omitted, the row is the one for ``printer_id`` (or the
    #: configured printer), made from Bambuddy's listing if there is none yet.
    printer: str | None = None
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
    #: Several plates of the output (#3652), each sliced and queued as an item of its own,
    #: in plate order, in place of ``plate_id`` alone. ``all_plates`` wins over it.
    plate_ids: list[Annotated[int, Field(ge=1)]] | None = Field(
        default=None, min_length=1, max_length=MAX_PLATES
    )
    #: The Bambuddy project this print belongs to (#79). Omitted means the remembered
    #: one (``last_project_id``); an explicit ``null`` is "No project" and wins over it,
    #: because the picker's PUT that remembers the choice may not have landed yet.
    project_id: int | None = None
    #: A library folder of no project to put the 3MF in (#3651), in place of a project:
    #: sent, the print is filed under no project. Refused together with ``project_id``.
    folder_id: int | None = None
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
    #: The prime tower for this print (#3628), over the template's ``print_settings``.
    #: Omitted means whatever those and the process say (on, unless the plate prints
    #: one filament).
    prime_tower: PrimeTowerChoice | None = None

    @field_validator("filament_profiles")
    @classmethod
    def _known_profiles(cls, ids: list[str]) -> list[str]:
        unknown = unknown_profiles(ids)
        if unknown:
            raise ValueError(f"no filament profile {', '.join(map(repr, unknown))}")
        return list(dict.fromkeys(ids))

    @field_validator("printer")
    @classmethod
    def _registry_id(cls, value: str | None) -> str | None:
        # A uuid, as the registry keys its rows; one spelling, so one run key.
        return None if value is None else str(uuid.UUID(value))

    @field_validator("plate_ids")
    @classmethod
    def _plates_in_order(cls, ids: list[int] | None) -> list[int] | None:
        # Each once and in plate order, so the same plates are the same run's key.
        return None if ids is None else sorted(set(ids))

    @model_validator(mode="after")
    def _one_destination(self) -> Self:
        if self.folder_id is not None and self.project_id is not None:
            raise ValueError("choose a project or a library folder, not both")
        return self

    @model_validator(mode="after")
    def _one_plate_is_plate_id(self) -> PrintRunRequest:
        # One chosen plate is that plate's print, under the key `plate_id` gives it
        # (#3762 review): `plate_ids=[2]` and `plate_id=2` are the same run.
        fold_plates(self)
        return self

    def asked_plates(self) -> list[int] | None:
        """The plates asked for, in plate order; ``None`` for every plate."""
        return asked_plates(self.plate_id, self.all_plates, self.plate_ids)


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


#: How a run reaches its printer (#3823, plan Task 8).
RunPath = Literal["bambuddy", "native"]


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
    #: How the run reached its printer (#3818): ``bambuddy`` through Bambuddy's queue;
    #: ``native`` over the LAN (plan 3a Task 9), with ``bambuddy_url`` ``None``. The
    #: dialog's "sent over the LAN" reads this, never its own guess at the path.
    path: RunPath = "bambuddy"
    #: Each plate the printer took over the LAN, by the ``subtask_name`` the native
    #: follow reads its progress by (`printers/progress.py`).
    lan: list[LanStarted] = Field(default_factory=list)


def known_slots(source: PrintSource) -> Callable[[int | None], FilamentRequirements] | None:
    """A library file's (#2550) or an output's (#2720) slots as read from the file
    itself, for every plate: every copy of it has the same slots, so they answer for
    whichever copy is read. ``None`` when they came from Bambuddy, which is then asked
    per plate."""
    if isinstance(source, LibrarySource | OutputSource) and source.local is not None:
        return source.local.for_plate
    return None


async def filament_options(
    client: BambuddyClient | None,
    source: PrintSource,
    *,
    printer_id: int | None = None,
    plate_id: int = 1,
    all_plates: bool = False,
    plate_ids: Sequence[int] | None = None,
    status: asyncio.Future[PrinterStatus | None] | None = None,
    trays_from: PrinterStatus | None = None,
    overlay: Overlay | None = None,
    provider_labels: Mapping[str, str] | None = None,
) -> FilamentOptions:
    """The filament step's whole payload for one output (#87).

    ``all_plates`` answers for every plate of the output at once — one row per slot any
    plate uses, in place of ``plate_id``'s own — which is what an all-plates print
    needs a spool for. ``plate_ids`` answers the same way for just those plates (#3652).

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

    ``trays_from`` is a native run's printer status from its LAN report (#3818 tier A,
    plan 2026-10-18 D1, D4): the choices are its loaded trays with ``overlay`` (the spool
    providers' records, a Postgres read) laid over them, and nothing is read of Bambuddy:
    ``client`` is ``None`` then (D3).
    """
    if trays_from is not None:
        return await _tray_options(
            client,
            source,
            printer_id=printer_id,
            asked=asked_plates(plate_id, all_plates, plate_ids),
            status=trays_from,
            overlay=overlay,
            labels=provider_labels,
        )
    client = bambuddy_client(client)
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
        asked = asked_plates(plate_id, all_plates, plate_ids)
        read = await gather_plate_options(
            client,
            library_file_id=file.id,
            printer_id=printer_id,
            plate_ids=((await source.plate_ids(client)) or [1]) if asked is None else asked,
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


async def _tray_options(
    client: BambuddyClient | None,
    source: PrintSource,
    *,
    printer_id: int | None,
    asked: Sequence[int] | None,
    status: PrinterStatus,
    overlay: Overlay | None,
    labels: Mapping[str, str] | None,
) -> FilamentOptions:
    """:func:`filament_options` on a native run: the plates' slots from the file itself
    (or the native library's copy), the trays from ``status``."""
    file = await source.file_to_read(client)
    read = await gather_plate_options(
        client,
        library_file_id=file.id,
        printer_id=printer_id,
        plate_ids=((await source.plate_ids(client)) or [1]) if asked is None else asked,
        fallback_colours=list(source.colours),
        own_colours=file.own_colours,
        known=known_slots(source),
        trays_from=status,
        overlay=overlay,
        provider_labels=labels,
        now=datetime.now(UTC),
    )
    options = read[0] if len(read) == 1 else every_plate(read)
    options.nozzles = status.nozzles
    return with_sides(options, status)


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
    plate_ids: Sequence[int] | None = None,
) -> FilamentOptions:
    """:func:`filament_options` for an output ScadBuddy rendered."""
    return await filament_options(
        client,
        OutputSource(store=store, meta=meta, uploads=uploads, settings=settings),
        printer_id=printer_id,
        plate_id=plate_id,
        all_plates=all_plates,
        plate_ids=plate_ids,
    )


class ChoosesProject(Protocol):
    """A request body with an optional ``project_id``."""

    @property
    def project_id(self) -> int | None: ...

    @property
    def model_fields_set(self) -> set[str]: ...


def chosen_project(request: ChoosesProject, settings: StoredSettings) -> int | None:
    """The request's ``project_id`` when it sent one, ``null`` included ("No project");
    when it left the field out, none if it chose a library folder instead (#3651), else
    the default project (``last_project_id``, #317), which only Settings changes (#3188)."""
    if "project_id" in request.model_fields_set:
        return request.project_id
    if getattr(request, "folder_id", None) is not None:
        return None
    return settings.last_project_id


class ResolvedProject(BaseModel):
    """:func:`resolve_project`'s answer, carried from a run's check to its plan so the
    project list is read once (#3790 review); ``None`` is "No project"."""

    project_id: int | None = None


async def resolve_project(
    client: BambuddyClient, request: ChoosesProject, settings: StoredSettings
) -> int | None:
    """:func:`chosen_project`, with a project left out resolved only to one Bambuddy still
    has, as ``destinations.default_destination`` does: a default project that was deleted
    falls through to none rather than failing the print on its folder (#3651 review). A
    project the request names is used as sent."""
    project_id = chosen_project(request, settings)
    if project_id is None or "project_id" in request.model_fields_set:
        return project_id
    known = {project.id for project in await client.projects()}
    return project_id if project_id in known else None


async def planned_project(
    client: BambuddyClient,
    request: PrintRunRequest,
    settings: StoredSettings,
    prepared: PreparedPlates,
) -> int | None:
    """The project the check resolved, else :func:`resolve_project` now."""
    if prepared.project is not None:
        return prepared.project.project_id
    return await resolve_project(client, request, settings)


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
    requirements: FilamentRequirements | None = None,
) -> ChooseRack:
    """The plate's ``choose_rack`` (spec 2026-10-01 §5). It reads the sliced file's
    requirements for ``plate_id`` and then, only when a group prints from the rack, a
    fresh status, once per plate and never ``PreparedRun.printer_status``. It ranks, and
    appends its warnings to ``warnings``, the plate's list. It **never raises**: the whole
    body is one ``try``, and on any exception it logs a fixed message with the exception's
    type, appends ``rack-left-to-bambuddy`` and returns ``None``, so Bambuddy picks.
    ``requirements``, when the run already read the slice's (#4013), is used instead."""

    known = requirements

    async def choose(sliced: int) -> RackChoice | None:
        stage = "requirements unreadable"
        try:
            read = (
                known
                if known is not None
                else await client.filament_requirements(sliced, plate_id=plate_id)
            )
            stage = "rack pick failed"
            groups = rack_groups(read.filaments, spools)
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
                            spools=group_spools(read.filaments, spools, group_id),
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
    client: BambuddyClient | None,
    plate_ids: list[int],
    per_plate: list[FilamentOptions],
    plan: FilamentPlan,
    choices: PrintChoices,
    catalogue: Catalogue,
) -> tuple[list[Resolved], list[str]]:
    """Each plate resolved for ``plan``, and every slot refusal among them in the run's
    own words: what :func:`plan_run` refuses with, and what :func:`check_print` reports
    (#1050). A spool the inventory lacks resolves as no spool chosen, so its presets
    are not read: Bambuddy would answer for a spool it does not have with an error.
    A native run's options are trays only (#3818 D1), so no spool presets are read and
    ``client`` may be ``None``: a remembered Bambuddy spool there is a slot with no
    choice."""
    inventory = {spool.spool_id for options in per_plate for spool in options.spools}
    # Read once for every plate: the plan's spools are the same on every plate.
    # A tray chosen for itself (#2164) has no spool, so no presets of its own.
    spool_ids = [
        spool_id
        for spool_id in sorted({slot.spool_id for slot in plan.slots} & inventory)
        if spool_id > 0
    ]
    # Concurrently (#4196); settled in spool order, so a failure is the one the serial reads raised.
    # A body's plan is not capped, only matched to the inventory, so this call caps one
    # plan's fan-out at SPOOL_PRESET_READS reads at a time. Not a bound on Bambuddy's
    # load, which every API replica and the print worker share (#4213).
    gate = asyncio.Semaphore(SPOOL_PRESET_READS)

    async def presets(spool_id: int) -> list[SpoolFilamentPreset]:
        if client is None:
            # Only an inventory spool has presets of its own, and only Bambuddy's
            # inventory has spools: a client-less (native) run has trays alone.
            raise TypeError(
                f"spool {spool_id}'s presets are Bambuddy's: reading them needs a client"
            )
        async with gate:
            return await client.spool_filament_presets(spool_id)

    read = await asyncio.gather(
        *(presets(spool_id) for spool_id in spool_ids), return_exceptions=True
    )
    spool_presets = {
        spool_id: settled(result) for spool_id, result in zip(spool_ids, read, strict=True)
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
    client: BambuddyClient | None,
    source: PrintSource,
    request: PrintRunRequest,
    prepared: PreparedRun,
    *,
    inventory: Inventory | None = None,
    native: bool = False,
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
        inventory=None if native else inventory,
        known=known_slots(source),
        native=source.native,
        # A native run's choices are its report's trays (D1), as the run resolves them.
        trays_from=prepared.printer_status if native else None,
    )
    if not native:
        per_plate = [
            with_trays(options, prepared.printer_status, prepared.printer_id)
            for options in per_plate
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
    client: BambuddyClient | None,
    source: PrintSource,
    settings: StoredSettings,
    request: PrintRunRequest,
    *,
    rack: RackUsage | None = None,
    printers: PrinterStore | None = None,
    reports: PrinterReportStore | None = None,
    catalogue: ReadCatalogue | None = None,
    profiles: Profiles | None = None,
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
    to swallow it and preview nothing, since the run would fail on the same read.
    ``catalogue`` is where the presets are read from, the dialog's kept copy of
    Bambuddy's (#4196): the check slices nothing, so it judges the choices against what
    the dialog's choices read, and only the run reads them fresh. A dialog served
    natively offers the image's catalogue instead, and its check still reads Bambuddy's.
    So everything the check derives from its :class:`PreparedRun` judges a copy that may
    be old: never hand a check's ``PreparedRun`` to a run.

    ``printers`` and ``reports`` are the registry, read as the run reads it (#4442 I2):
    a ``printer`` it refuses is an error here in the run's words, and on the LAN path
    the status judged is the LAN report's. A print worker without the key is the run's
    503, raised as it is. Like the run, a check makes the registry row of a Bambuddy
    printer that has none (`PrinterStore.ensure_bambuddy`): that row is the printer's
    identity, made once and idempotently, as the printers strip makes it on a read
    (`printers_now`), so a check that writes it changes nothing a run would not.

    On a printer a run reaches natively (`run_route`, plan 2026-10-18 D2) the check is
    the native run's own, and reads nothing of Bambuddy (D3): the image's presets
    (``profiles``, D5), the report's trays (D1) and ``source``'s native library for the
    folder (D6). ``client`` is used only off that path."""
    named = request.printer is not None and printers is not None
    if not named and (request.printer_id or settings.printer_id) is None:
        return PrintCheck()
    route: RunRoute | None = None
    if printers is not None and native_backends(settings):
        try:
            route = await run_route(
                settings,
                printers,
                reports,
                printer=request.printer,
                printer_id=request.printer_id,
                say_fallback=False,
            )
        except RunRefusalError as error:
            return PrintCheck(errors=[error.detail])
    if route is not None and route.native:
        return await _native_check(source, settings, request, route, rack=rack, profiles=profiles)
    client = bambuddy_client(client)
    # The 3MF's download and the inventory wait on nothing `prepare_run` reads, so they
    # are read beside it (#4196): awaited after it in the order they were read in, so a
    # failure surfaces where it did, and cancelled when it refuses. One inventory
    # read for the plan, the rack, the slot refusals and the spool warnings (#2722);
    # none for a plan with no slots, whose slot refusals read their own. Every read of the
    # 3MF, `prepare_run`'s included, waits on the source's one fetch, even while it runs,
    # and its failure (out of memory, #4581 review) answers them all
    # (`test_a_library_files_check_fetches_the_file_once`). Started before the check
    # knows whether it refuses, these reads may reach Bambuddy on a refused check too: the
    # inventory's spools and assignments may already be asked for, then are cancelled,
    # and a fetch no other read waits on stops with its task.
    laying_out = None if source.is_sliced else asyncio.create_task(source.states_nozzles(client))
    reading = (
        asyncio.create_task(read_inventory(client))
        if request.filament_plan.slots and not source.is_sliced
        else None
    )
    try:
        prepared = await prepare_run(
            client,
            source,
            settings,
            request,
            rack=rack,
            refuse_manual_pick=False,
            printers=printers,
            reports=reports,
            catalogue=catalogue,
        )
    except BaseException as error:
        early = [task for task in (laying_out, reading) if task is not None]
        for task in early:
            task.cancel()
        await asyncio.gather(*early, return_exceptions=True)
        if isinstance(error, RunRefusalError):
            return PrintCheck(errors=[error.detail])
        if isinstance(error, MemoryError):
            # As the layout's read below (#4576 review): a read of the file that runs out
            # of memory is the check's refusal, never a 500. Logged, as the allocation
            # that failed may not have been a read of the file.
            _log_out_of_memory(source.subject.key, "preparing the run")
            return PrintCheck(errors=[str(ReadOutOfMemoryError())])
        raise
    if laying_out is None:
        # Queued as it is, never refused (#2671): what to set right first is a warning,
        # and there is no nozzle plan, slots or rack side to judge before it.
        return PrintCheck(warnings=prepared.warnings + not_resliced(request.prime_tower))
    try:
        laid_out = await laying_out
    except BaseException as error:
        if reading is not None:
            reading.cancel()
            await asyncio.gather(reading, return_exceptions=True)
        if isinstance(error, MemoryError):
            # The file too large to hold here (#4562 review): refused as a read of it is,
            # saying how to bound it, never a 500.
            _log_out_of_memory(source.subject.key, "laying out the file")
            return PrintCheck(errors=[str(ReadOutOfMemoryError())])
        raise
    inventory = await reading if reading is not None else None
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
    read = values_for(client, native=None)
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
                    one_plate=len(await source.plate_ids(client)) <= 1,
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
            # #3628: and of a prime tower setting the preset has no value for.
            *(await prime_tower_warnings(client, process, request.prime_tower, read=read)),
        ],
        rack=rack_view,
        nozzle_plan=nozzle_plan,
    )


async def _native_check(
    source: PrintSource,
    settings: StoredSettings,
    request: PrintRunRequest,
    route: RunRoute,
    *,
    rack: RackUsage | None,
    profiles: Profiles | None,
) -> PrintCheck:
    """:func:`check_print` on a native run: its refusals and advisories, judged on the
    report's status, the trays and the image's presets, with no Bambuddy client."""
    if profiles is None:
        raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, NATIVE_SLICER_UNAVAILABLE)
    if request.project_id is not None:
        return PrintCheck(errors=[NATIVE_PROJECT_REFUSAL])

    async def presets() -> Catalogue:
        return await catalogue_for(None, native=profiles)

    try:
        prepared = await prepare_run(
            None,
            source,
            settings,
            request,
            rack=rack,
            refuse_manual_pick=False,
            native=source.native,
            catalogue=presets,
            route=route,
        )
    except RunRefusalError as error:
        return PrintCheck(errors=[error.detail])
    if source.is_sliced:
        return PrintCheck(warnings=prepared.warnings + not_resliced(request.prime_tower))
    laid_out = await source.states_nozzles(None)
    # The trays are the choices (D1): no inventory to read, and none to plan from.
    inventory: Inventory = ([], [])
    nozzle_plan = (
        await plan_for(
            None,
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
        None,
        request,
        settings,
        printer_id=prepared.printer_id,
        status=prepared.printer_status,
        rack=rack,
        laid_out=laid_out,
        nozzle_plan=nozzle_plan,
        inventory=inventory,
        picker="the printer",
    )
    try:
        _check_manual_pick(request, prepared.printer_status, laid_out=laid_out)
    except RunRefusalError as refused:
        errors = [refused.detail]
        rack_notes = [note for note in rack_notes if note.kind != "rack-manual-partial"]
    else:
        errors = []
    slot_errors = await _slot_errors(None, source, request, prepared, native=True)
    if slot_errors:
        errors.append(" ".join(slot_errors))
    rack_picked = rack_view is not None and rack_view.position is not None
    objects, clearance = await _sequence_clearance(None, source, request, prepared)
    process = (
        process_preset_for(request.choices, prepared.catalogue)
        if prepared.catalogue is not None
        else None
    )
    read = values_for(None, native=profiles)
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
            *(await profile_overrides(None, process, request.filament_profiles, read=read))[1],
            *(
                await support_overrides(
                    None,
                    process,
                    wanted_settings(source.print_settings, request.supports),
                    read=read,
                )
            )[1],
            *(await prime_tower_warnings(None, process, request.prime_tower, read=read)),
        ],
        rack=rack_view,
        nozzle_plan=nozzle_plan,
    )


def _listed(warning: FilamentWarning | None) -> list[FilamentWarning]:
    return [] if warning is None else [warning]


async def _sequence_clearance(
    client: BambuddyClient | None,
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
    # The resolved printer's, as the plate warnings are (#3024 review). Only a printer
    # Bambuddy reports no model for is judged as the configured model. Reached only from
    # check_print, which always has a fresh PreparedRun, so no run from an older history
    # (with no printer model) gets here (#3783, #3877 review).
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
    one_plate: bool = True,
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
        # Only a known family counts, toward the mix and the sides alike: a slot whose
        # material cannot be read (an unknown spool, a tray with no status) is left out
        # of both, so it never makes a plan "mixed" nor stops "a nozzle of its own".
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
    if nozzle_plan is not None:
        found += inlet_warnings(
            nozzle_plan,
            plan_inputs(plan, spools, assignments, status, printer_id),
            kept=one_plate,
        )
    return found


#: The Track Switch inlet that rests on each side (``extruders.FTS_INLET_EXTRUDER``).
_INLET_ON = {LEFT: "A", RIGHT: "B"}
_SIDE_WORD = {"L": "left", "R": "right"}


def inlet_warnings(
    nozzle_plan: NozzlePlan, filaments: list[PlanInput], *, kept: bool = True
) -> list[FilamentWarning]:
    """#4272: a Track Switch inlet feeding both extruders in one print. Print #355
    stalled so: the switch flipped inlet A between the extruders at every colour change.
    The plan keeps each inlet on its own side (``nozzle_plan``), so only a side chosen by
    hand splits one; each spool printing away from its inlet's side is named, with the
    inlet to move it to.

    Not ``kept`` (a file of more than one plate, whose map cannot be pinned,
    :attr:`NozzlePlan.keeps_sides`), the slicer regroups the filaments itself, so an
    inlet holding two or more spools may be split whatever the plan says: said too."""
    if not nozzle_plan.track_switch or len(nozzle_plan.sides) < 2:
        return []
    rests = {filament.slot_id: filament.rests for filament in filaments}
    found: list[FilamentWarning] = []
    for rest, inlet in _INLET_ON.items():
        on_inlet = [slot for slot in nozzle_plan.slots if rests.get(slot.slot_id) == rest]
        if not kept and len(on_inlet) > 1:
            other = _INLET_ON[RIGHT if rest == LEFT else LEFT]
            names = ", ".join(slot.name for slot in on_inlet[:-1]) + f" and {on_inlet[-1].name}"
            every = "both" if len(on_inlet) == 2 else "all"
            found.append(
                FilamentWarning(
                    kind="inlet-shared",
                    message=(
                        f"This file has more than one plate, so the slicer groups the "
                        f"filaments itself and may put {names}, {every} fed through Track "
                        f"Switch inlet {inlet}, on different extruders. The switch would "
                        f"then move inlet {inlet} between them at every colour change, "
                        f"which can stall the print: print one plate at a time, or move "
                        f"all but one of them to an AMS on inlet {other}."
                    ),
                )
            )
            continue
        home = [slot for slot in on_inlet if slot.side == side_of(rest)]
        away = [slot for slot in on_inlet if slot.side != side_of(rest)]
        if not home or not away:
            continue
        other = _INLET_ON[RIGHT if rest == LEFT else LEFT]
        for slot in away:
            found.append(
                FilamentWarning(
                    kind="inlet-shared",
                    message=(
                        f"{slot.name} prints on the {_SIDE_WORD[slot.side]} but is fed "
                        f"through Track Switch inlet {inlet}, which also feeds "
                        f"{home[0].name} on the {_SIDE_WORD[home[0].side]}. The switch "
                        f"would move inlet {inlet} between the extruders at every colour "
                        f"change, which can stall the print: move {slot.name} to an AMS on "
                        f"inlet {other}, or print it on the {_SIDE_WORD[home[0].side]}."
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
    client: BambuddyClient | None,
    request: PrintRunRequest,
    settings: StoredSettings,
    *,
    printer_id: int,
    status: PrinterStatus | None,
    rack: RackUsage | None,
    laid_out: bool,
    nozzle_plan: NozzlePlan | None = None,
    inventory: Inventory | None = None,
    picker: str = "Bambuddy",
) -> tuple[RackPickView | None, list[FilamentWarning]]:
    """The rack side ranked as one group from the dialog's size and spools (spec §5):
    the preview ``/check`` shows. Judged on the flow the slice will carry on the rack side
    (#484): the one chosen there for a file the run lays out, Standard for a library file
    (``laid_out``, :func:`~scadbuddy.bambuddy.extruders.rack_volume_type`). Every chosen
    spool counts toward the material test, since the slice may put any of them on the
    rack side, unless ScadBuddy's nozzle plan (#2166) says which do: then only those
    on the rack side count. Advisory: a failure previews nothing. ``inventory`` is the
    check's own read, reused so a check reads the spools once (#2936); without it the
    spools are read here. ``picker`` is who picks what the ranking leaves: "the printer"
    on a native run (#4601), which no Bambuddy picks for."""
    if status is None or not rack_positions(status.nozzle_rack):
        return None, []
    rack_letter = side_of(RACK_SIDE)
    slots = [
        slot
        for slot in request.filament_plan.slots
        if nozzle_plan is None or nozzle_plan.side_of_slot(slot.slot_id) in (None, rack_letter)
    ]
    try:
        spools = inventory[0] if inventory is not None else await bambuddy_client(client).spools()
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
        warnings = rack_warnings(
            [group], status.nozzle_rack, algorithm, picks, manual, picker=picker
        )
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
    client: BambuddyClient | None,
    store: OutputStore,
    uploads: BambuddyUploadStore,
    meta: OutputMeta,
    settings: StoredSettings,
    request: PrintRunRequest,
    *,
    rack: RackUsage | None = None,
    print_settings: dict[str, str] | None = None,
    printers: PrinterStore | None = None,
    reports: PrinterReportStore | None = None,
    catalogue: ReadCatalogue | None = None,
    profiles: Profiles | None = None,
    native: NativeCopies | None = None,
) -> PrintCheck:
    """:func:`check_print` for an output ScadBuddy rendered, with its template's
    ``print_settings`` as the run reads them, so a template that prints by object is
    checked by object (#2976). ``native`` is the native library a native run's folder is
    checked in (D6)."""
    return await check_print(
        client,
        OutputSource(
            store=store,
            meta=meta,
            uploads=uploads,
            settings=settings,
            print_settings=print_settings or {},
            native=native,
        ),
        settings,
        request,
        rack=rack,
        printers=printers,
        reports=reports,
        catalogue=catalogue,
        profiles=profiles,
    )


async def check_for_library(
    client: BambuddyClient | None,
    uploads: BambuddyUploadStore,
    settings: StoredSettings,
    file_id: int,
    request: PrintRunRequest,
    *,
    rack: RackUsage | None = None,
    filaments: SharedFilamentReader | None = None,
    files: LibraryReads | None = None,
    printers: PrinterStore | None = None,
    reports: PrinterReportStore | None = None,
    catalogue: ReadCatalogue | None = None,
    profiles: Profiles | None = None,
) -> PrintCheck:
    """:func:`check_print` for a file already in the library; with ``filaments``, its
    slots read from the file itself (#2550): from their cache, else from the download
    the check's layout reads anyway (#4495); ``files`` reads a native one (#3827).
    ``profiles`` are a native run's presets (D5), as :func:`check_for_output`'s."""
    return await check_print(
        client,
        await LibrarySource.load(
            client,
            file_id,
            uploads=uploads,
            settings=settings,
            shared=filaments,
            files=files,
        ),
        settings,
        request,
        rack=rack,
        printers=printers,
        reports=reports,
        catalogue=catalogue,
        profiles=profiles,
    )


@dataclass(frozen=True)
class PreparedRun:
    """What :func:`prepare_run` checked, handed on to :func:`execute_run`."""

    plate_ids: list[int]
    printer_id: int
    #: Read once, before the 202, and reused by the run rather than read again; ``None``
    #: for a sliced file, which takes no presets (#2671).
    catalogue: Catalogue | None
    #: The printer's live status, read once for the run's hardware warnings; ``None``
    #: when it was unreadable.
    printer_status: PrinterStatus | None
    printer_name: str | None = None
    #: What a sliced file's run carries back instead of refusing (#2671).
    warnings: list[FilamentWarning] = field(default_factory=list)
    #: The resolved printer's model, which the plate warnings are judged against (#2858).
    printer_model: str | None = None
    #: The project the caller resolved already, if it did; :func:`plan_run` reads it
    #: rather than Bambuddy's project list again.
    project: ResolvedProject | None = None
    #: The registry printer the run prints on (#3823), and how it reaches it: ``""``
    #: and ``bambuddy`` where there is no registry (a process without Postgres's
    #: printers, or a run accepted before it).
    printer: str = ""
    path: RunPath = "bambuddy"


def run_path(row: PrinterRow, seen_at: datetime | None, *, now: datetime) -> RunPath:
    """How a run reaches ``row`` (#3823, plan Task 8): its ``preferred_path`` as set,
    and for ``auto`` the LAN when it has a LAN link whose last report (``seen_at``) is
    fresh (`is_fresh`), else Bambuddy."""
    if row.preferred_path != "auto":
        return row.preferred_path
    fresh = seen_at is not None and is_fresh(seen_at, now)
    return "native" if row.lan_host is not None and fresh else "bambuddy"


async def lan_printers(
    settings: StoredSettings,
    printers: PrinterStore | None,
    reports: PrinterReportStore | None,
) -> list[str]:
    """The registry rows a run would reach over the LAN (#3818): those `run_path` puts on
    the LAN, when the stored backends make such a run native (`hot_path_native`, D2).
    None otherwise, so the dialog keeps its project, Bambuddy's folders and "queued" for
    every printer, and the registry is not read. The reports are one query (#4516):
    this is on every choices read."""
    if printers is None or not native_backends(settings):
        return []
    rows = await asyncio.to_thread(printers.list)
    seen = {} if reports is None else await asyncio.to_thread(reports.all)
    now = datetime.now(UTC)
    out: list[str] = []
    for row in rows:
        report = seen.get(row.id)
        path = run_path(row, None if report is None else report[1], now=now)
        if hot_path_native(settings, path):
            out.append(row.id)
    return out


async def all_native(
    settings: StoredSettings, printers: PrinterStore, reports: PrinterReportStore | None
) -> bool:
    """Whether every registry printer is one a run reaches natively (D2), so the
    printers strip asks Bambuddy nothing (D4): a printer on Bambuddy's path still reads
    Bambuddy's status, and its listing finds a printer the registry has not seen yet."""
    if not native_backends(settings):
        return False
    rows = await asyncio.to_thread(printers.list)
    return bool(rows) and len(await lan_printers(settings, printers, reports)) == len(rows)


@dataclass(frozen=True)
class RunRoute:
    """Which printer a run prints on and how, decided before any Bambuddy client is
    built (#3818 tier A, plan 2026-10-18 D2, D3): the registry row, the Bambuddy printer
    (``None`` for a row Bambuddy does not reach), the row's path and, on the LAN path, its
    status from the report (D4). ``native`` is `hot_path_native` of the path. ``listed``
    is Bambuddy's printer when :func:`ensured_route` read it, so `prepare_run` does not
    read it again on Bambuddy's path (#4551)."""

    row: PrinterRow | None
    printer_id: int | None
    path: RunPath
    status: PrinterStatus | None
    native: bool
    listed: Printer | None = None

    @property
    def printer(self) -> Printer:
        """The row as the run's checks read a Bambuddy printer, on a native run: its
        Bambuddy id when it links one, else 0, which nothing keys on (#4336 review M7)."""
        assert self.row is not None, "a native run is a registry row's"
        return Printer(
            id=self.printer_id or 0, name=self.row.name, model=self.row.model, is_active=True
        )


async def run_route(
    settings: StoredSettings,
    printers: PrinterStore | None,
    reports: PrinterReportStore | None,
    *,
    printer: str | None,
    printer_id: int | None,
    say_fallback: bool = False,
    decide: Callable[[StoredSettings, RunPath], bool] | None = None,
) -> RunRoute:
    """The run's :class:`RunRoute`, from the registry and its reports only: the row
    ``printer`` names, else the row linking ``printer_id`` (or Settings' printer). No
    Bambuddy call: a row Bambuddy has not been asked about yet is :func:`ensured_route`'s
    to make. ``decide`` is `hot_path_native` unless a caller's own
    (`PrintDeps.native_run`). ``say_fallback`` is off by default: on Bambuddy's path
    `prepare_run` reads the path again and says the key's fallback itself, once."""
    decide = decide or hot_path_native
    named, bambuddy_id = await _named_printer(printer, printer_id, settings, printers)
    row = named
    if row is None and printers is not None and bambuddy_id is not None:
        row = await asyncio.to_thread(printers.by_bambuddy_id, bambuddy_id)
    path, report_status = await _registry_path(row, printers, reports, say_fallback=say_fallback)
    native = decide(settings, path)
    if not native and bambuddy_id is None:
        # A row Bambuddy does not reach, on Bambuddy's path: nothing prints it there
        # (#4336 review M7, #4442 M4).
        assert named is not None  # `_named_printer` refuses a request naming nothing
        raise RunRefusalError(_not_on_bambuddy(named))
    return RunRoute(
        row=row,
        printer_id=bambuddy_id,
        path=path,
        status=report_status,
        native=native,
    )


async def ensured_route(
    client: BambuddyClient,
    settings: StoredSettings,
    printers: PrinterStore | None,
    reports: PrinterReportStore | None,
    route: RunRoute,
    *,
    decide: Callable[[StoredSettings, RunPath], bool] | None = None,
) -> RunRoute:
    """``route`` once its row is made or merged, as the run's first check makes it
    (`_ensure_row`, #4442 M2): a Bambuddy printer no row links yet, whose serial a
    LAN-linked row holds, takes that row's path from its first run. Only on the hot
    path's backends, and only then is Bambuddy asked for its printer: a row the
    registry knows needs no read."""
    if (
        route.row is not None
        or route.printer_id is None
        or printers is None
        or not native_backends(settings)
    ):
        return route
    printer = await _require_resolvable_printer(client, route.printer_id, any_model=True)
    row = await _ensure_row(printers, None, printer)
    path, report_status = await _registry_path(row, printers, reports, say_fallback=False)
    return RunRoute(
        row=row,
        printer_id=route.printer_id,
        path=path,
        status=report_status,
        native=(decide or hot_path_native)(settings, path),
        listed=printer,
    )


async def dialog_route(
    settings: StoredSettings,
    printers: PrinterStore | None,
    reports: PrinterReportStore | None,
    *,
    printer: str | None,
    printer_id: int | None,
) -> RunRoute | None:
    """The dialog's reads' :class:`RunRoute` when a run on that printer would be native,
    else ``None``: the reads then go through Bambuddy as before. Nothing is read of the
    registry unless the stored backends are the hot path's (D2), and a printer the
    registry cannot place (none chosen, a stale report) reads as before too: the check
    and the run say why, as they always have."""
    if printers is None or not native_backends(settings):
        return None
    try:
        route = await run_route(
            settings, printers, reports, printer=printer, printer_id=printer_id, say_fallback=False
        )
    except ApiError:
        return None
    return route if route.native else None


async def first_native_route(
    settings: StoredSettings,
    printers: PrinterStore | None,
    reports: PrinterReportStore | None,
) -> RunRoute | None:
    """The :class:`RunRoute` of the first registry row a run reaches natively (D2), or
    ``None``: where a dialog opens with no printer named, Bambuddy configured or not
    (#3818 tier A, AC1 and AC2, #4601). The rows are read once, and each row's report at
    most once; a row a run could not start on now (no report, a stale one) is passed
    over."""
    if printers is None or not native_backends(settings):
        return None
    for row in await asyncio.to_thread(printers.list):
        try:
            path, report_status = await _registry_path(row, printers, reports, say_fallback=False)
        except ApiError:
            continue
        if hot_path_native(settings, path):
            return RunRoute(
                row=row,
                printer_id=row.bambuddy_printer_id,
                path=path,
                status=report_status,
                native=True,
            )
    return None


async def prepare_run(
    client: BambuddyClient | None,
    source: PrintSource,
    settings: StoredSettings,
    request: PrintRunRequest,
    *,
    rack: RackUsage | None = None,
    refuse_manual_pick: bool = True,
    project: ResolvedProject | None = None,
    native: NativeCopies | None = None,
    printers: PrinterStore | None = None,
    reports: PrinterReportStore | None = None,
    catalogue: ReadCatalogue | None = None,
    route: RunRoute | None = None,
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

    ``native`` is ScadBuddy's own library on a native run (#3818 tier A Task 5, D6): a
    project is refused and the folder is checked there, never in Bambuddy.

    ``printers`` is the printer registry (#3823, plan 2026-10-16 Task 8): the run names
    a row (``request.printer``), else the row for its Bambuddy printer, made from
    Bambuddy's listing when there is none, and takes the row's path (`run_path`). On the
    native path the status is the row's LAN report (``reports``), never Bambuddy's (D4).
    Without ``printers`` the run is Bambuddy's, as before the registry.

    ``catalogue`` reads the presets in place of a fresh read: the check's, never the run's
    (:func:`check_print`). The kept copy is served however old it is, so a preset removed
    in Bambuddy since can pass the check and still be refused by the run's fresh read:
    the check is advice, and the run's read is the one that decides.

    ``route`` is the caller's `run_route`; a native one (D2) takes the row, its model and
    its report's status as they are, and lists or reads no Bambuddy printer: ``client``
    is ``None`` then (D3), and ``catalogue`` the image's presets (D5).
    """
    if native is not None:
        if request.project_id is not None:
            raise RunRefusalError(NATIVE_PROJECT_REFUSAL)
        if request.folder_id is not None:
            await require_native_folder(native, request.folder_id)
    elif request.folder_id is not None:
        await require_destination_folder(
            bambuddy_client(client), request.folder_id, settings.library_folder_id
        )
    laid_out = await source.plate_ids(client)
    asked = request.asked_plates()
    plate_ids = laid_out if asked is None else asked
    if not plate_ids:
        # ScadBuddy's writer always lays out one; a 3MF edited to list none has nothing
        # to queue, and every route below reads the first plate's outcome. Read
        # from the local 3MF before anything touches Bambuddy.
        raise RunRefusalError(
            "This output's 3MF lays out no plates, so there is nothing to print.",
        )
    plate_warnings: list[FilamentWarning] = []
    missing = [plate for plate in plate_ids if plate not in laid_out] if laid_out else []
    if source.is_sliced and missing:
        # A sliced file is never refused (#2671): the dialog offers only its plates, so
        # a request for another one prints the rest, or the file's first, and says so.
        kept = [plate for plate in plate_ids if plate not in missing]
        if kept:
            instead = "so it is left out" if len(missing) == 1 else "so they are left out"
        else:
            instead = f"so plate {laid_out[0]} is queued instead"
        plate_ids = kept or laid_out[:1]
        plate_warnings.append(
            FilamentWarning(
                kind="sliced-plate-missing",
                message=(
                    f"This file holds no {plates_named(missing)}, {instead}."
                    " Held for a manual start."
                ),
            )
        )
    elif missing:
        # Bambuddy does not check it either: the run would upload, then fail in the
        # slicer with no reason given (#1320).
        count = f"{len(laid_out)} plate{'s' if len(laid_out) != 1 else ''}"
        raise RunRefusalError(
            f"The 3MF has {count} ({', '.join(map(str, laid_out))}); "
            f"there is no {plates_named(missing)}.",
        )
    if route is not None and route.native:
        # The row, its model and its status from the registry and its report (D4): no
        # Bambuddy printer is listed or read, and a row Bambuddy does not reach prints.
        printer = route.printer
        printer_id = printer.id
        row = route.row
        path: RunPath = route.path
        report_status = route.status
        _check_resolvable(printer, any_model=source.is_sliced)
    else:
        named, bambuddy_id = await _named_printer(
            request.printer, request.printer_id, settings, printers
        )
        if bambuddy_id is None:
            # A row Bambuddy does not reach: its status would carry printer id 0, which
            # nothing may key on (#4336 review M7). Refused before the key's 503 (#4442
            # M4): off the hot path, linking it to Bambuddy is what prints it.
            assert named is not None  # `_named_printer` refuses a request naming nothing
            raise RunRefusalError(_not_on_bambuddy(named))
        printer_id = bambuddy_id
        listed = None if route is None else route.listed
        if listed is not None and listed.id == printer_id:
            # `ensured_route` read it for this run's first check (#4551): one read.
            printer = listed
            _check_resolvable(printer, any_model=source.is_sliced)
        else:
            # Resolved before the path is decided (#4442 M2), so a row this run links or
            # merges takes its own path from its first run.
            printer = await _require_resolvable_printer(
                bambuddy_client(client), printer_id, any_model=source.is_sliced
            )
        row = await _ensure_row(printers, named, printer)
        path, report_status = await _registry_path(
            row, printers, reports, say_fallback=refuse_manual_pick
        )
    if source.is_sliced:
        # Nothing is sliced, so no preset need resolve (#2671): any active printer the
        # file was sliced for prints it (`any_model` above).
        printer_status = report_status or await read_status(bambuddy_client(client), printer_id)
        await _record_seen(rack, printer_id, printer_status)
        return PreparedRun(
            plate_ids=plate_ids,
            printer_id=printer_id,
            catalogue=None,
            printer_status=printer_status,
            printer_name=printer.name,
            printer_model=printer.model,
            warnings=plate_warnings
            + sliced_warnings(source.sliced_profile, printer, printer_status, plate_ids),
            project=project,
            printer="" if row is None else row.id,
            path=path,
        )
    # Read once for every plate: the catalogue is ~4000 presets on the live instance.
    # Read before the upload, so that what the choices alone refuse — mixed nozzle
    # sizes, no printer or process preset — is a 422 that leaves nothing in Bambuddy's
    # library. Slot errors need the plate's slots, which only a library file answers,
    # so those are still found after the upload, by `resolve` in `execute_run`. The run
    # reads it fresh, two GETs; the check reads the dialog's kept copy (#4196). A native
    # run's caller passes the image's (D5).
    read = (
        partial(catalogue_for, bambuddy_client(client), native=None)
        if catalogue is None
        else catalogue
    )
    presets = await read()
    refused = choice_errors(request.choices, presets)
    if refused:
        raise RunRefusalError(" ".join(error.message for error in refused))
    printer_status = report_status or await read_status(bambuddy_client(client), printer_id)
    await _record_seen(rack, printer_id, printer_status)
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
        catalogue=presets,
        printer_status=printer_status,
        printer_name=printer.name,
        printer_model=printer.model,
        project=project,
        printer="" if row is None else row.id,
        path=path,
    )


async def _record_seen(
    rack: RackUsage | None, printer_id: int, printer_status: PrinterStatus | None
) -> None:
    """`record_seen`, keyed by the Bambuddy printer: a LAN-only row (0) has none."""
    if printer_id:
        await record_seen(rack, printer_id, printer_status)


#: How a disagreeing printer's refusal ends.
PICK_AGAIN = "Pick the printer again."
#: The problem type of a disagreeing printer's refusal. It survives the run's refusal
#: (`PrintRunError.type`), so a batch print says it of its own step from the type and
#: ``reason``, never from the wording (`print_batches._queue_one`, #4551).
DISAGREEING_PRINTER_PROBLEM = "https://scadbuddy.dev/problems/disagreeing-printer"


class DisagreeingPrinterError(RunRefusalError):
    """A named row that links another Bambuddy printer, or none, than the one sent beside
    it (#4442 I1, #4516). ``reason`` is the detail without `PICK_AGAIN`, as an
    extension member, for a caller that says what to pick again itself."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"{reason} {PICK_AGAIN}")
        self.type = DISAGREEING_PRINTER_PROBLEM
        self.extensions["reason"] = reason


async def _named_printer(
    printer: str | None,
    sent: int | None,
    settings: StoredSettings,
    printers: PrinterStore | None,
) -> tuple[PrinterRow | None, int | None]:
    """The registry row the request names, if it names one, and the Bambuddy printer the
    run prints on (``None`` for a row Bambuddy does not reach).

    Sent with ``printer_id``, ``printer`` is a cross-check (#4442 I1): every other choice
    in the body was made for that Bambuddy printer. A named row merged away or deleted
    since the body was made (a stored batch step, a dialog that has not re-read the
    registry) falls back to the row for ``printer_id``; a row that links another
    Bambuddy printer, or none (a row only the LAN reaches, #4516), is refused, so a
    disagreeing pair never prints on the Bambuddy printer."""
    if printers is None or printer is None:
        printer_id = sent or settings.printer_id
        if printer_id is None:
            raise not_configured(
                "no printer is chosen and none is configured, so there is nothing to print on"
            )
        return None, printer_id
    named = await asyncio.to_thread(printers.get, printer)
    if sent is None:
        if named is None:
            raise RunRefusalError(
                f"There is no printer {printer} in the registry. Pick another printer."
            )
        return named, named.bambuddy_printer_id
    if named is None:
        logger.info(
            "the named printer is gone; printing on Bambuddy printer %s's row",
            sent,
            extra={"printer": printer, "printer_id": sent},
        )
        return None, sent
    if named.bambuddy_printer_id is None:
        # Never printed on the Bambuddy printer instead (#4516): the row may be one only
        # the LAN reaches, whose run the dialog asked for.
        raise DisagreeingPrinterError(
            f"disagreeing printer: {named.name} is not linked to Bambuddy printer {sent}."
        )
    if named.bambuddy_printer_id != sent:
        raise DisagreeingPrinterError(
            f"disagreeing printer: {named.name} is Bambuddy printer"
            f" {named.bambuddy_printer_id} now, not {sent}."
        )
    return named, sent


def _not_on_bambuddy(row: PrinterRow) -> str:
    if row.lan_host is not None:
        # The LAN prints only on the hot path's backends (`hot_path_native`, D2).
        return (
            f"{row.name} is reached only over the LAN, which ScadBuddy prints to only with"
            " its own slicer and library. Choose those in Settings, or link it to Bambuddy"
            " in Settings > Printers to print through Bambuddy."
        )
    return f"{row.name} is not linked to Bambuddy. Link it in Settings > Printers."


#: A native run on a print worker that cannot open the access code (Review Focus 4).
LAN_NEEDS_KEK = "LAN printing needs SCADBUDDY_SECRET_KEY_FILE on the print worker"


async def _registry_path(
    row: PrinterRow | None,
    printers: PrinterStore | None,
    reports: PrinterReportStore | None,
    *,
    say_fallback: bool = True,
) -> tuple[RunPath, PrinterStatus | None]:
    """The run's path to ``row`` and, on the native path, its status from the LAN
    report, which must be as fresh as `printers_now` needs it to call the printer
    online (#4442 I3): an older one is no report.

    A print worker without the key cannot open the access code, so an ``auto`` row it
    reaches through Bambuddy too takes that path (#4442 review): the run still queues
    through Bambuddy until LAN printing lands (plan Task 9). Only a row set to
    ``native`` is the key's 503. ``say_fallback`` logs that fallback: on for a run, off
    for the dialog's checks, which would repeat it on every re-check."""
    if printers is None or row is None:
        return "bambuddy", None
    report = None if reports is None else await asyncio.to_thread(reports.get, row.id)
    now = datetime.now(UTC)
    path = run_path(row, None if report is None else report[1], now=now)
    if path == "bambuddy":
        return path, None
    if printers.kek is None:
        if row.preferred_path == "auto" and row.bambuddy_printer_id is not None:
            # Said, so a worker that lost its key is not silently on Bambuddy's path.
            if say_fallback:
                logger.info(
                    "printer %s takes Bambuddy's path: the print worker has no key for the LAN",
                    row.id,
                )
            return "bambuddy", None
        raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, LAN_NEEDS_KEK)
    if report is None:
        raise RunRefusalError(
            f"{row.name} has not reported over the LAN yet, so its trays and nozzles are not"
            " known. Wait for it to come online, or set its path to Bambuddy."
        )
    data, seen_at = report
    if not is_fresh(seen_at, now):
        minutes = max(1, int((now - seen_at).total_seconds() // 60))
        raise RunRefusalError(
            f"{row.name} has not reported over the LAN for {minutes} min, so its trays and"
            " nozzles are not known. Wait for it to come online, or set its path to Bambuddy."
        )
    # Parsed here, never from a dumped status, so nothing it drops is relied on (#4336
    # review M6).
    return path, status_from_report(
        row, LanReport(serial=row.serial, received_at=seen_at, print=data), now=now
    )


async def _ensure_row(
    printers: PrinterStore | None, row: PrinterRow | None, printer: Printer
) -> PrinterRow | None:
    """``row``, else the row for the Bambuddy ``printer``, made from its listing when
    there is none (`PrinterStore.ensure_bambuddy`)."""
    if printers is None or row is not None:
        return row
    return await asyncio.to_thread(
        printers.ensure_bambuddy,
        printer.id,
        serial=printer.serial_number,
        name=printer.name,
        model=printer.model,
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
    #: As :attr:`PreparedRun.project`. ``None`` in a run accepted before it was carried,
    #: whose plan resolves the project itself. The plan trusts it: a default project
    #: deleted between the check and the plan fails the run on its folder rather than
    #: falling through to "No project", a window of seconds accepted for one read
    #: (#4008 review).
    project: ResolvedProject | None = None
    #: As :attr:`PreparedRun.printer` and :attr:`PreparedRun.path`; the defaults are a
    #: run accepted before the registry (#3823), which is Bambuddy's.
    printer: str = ""
    path: RunPath = "bambuddy"
    #: Set only by a check that put the run on the LAN (plan 3a Task 9): what the
    #: workflow's LAN branch keys on. A run checked by a build before it, which could
    #: plan ``path="native"`` without the check's gate, stays on Bambuddy's branch.
    lan_start: bool = False

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
            project=prepared.project,
            printer=prepared.printer,
            path=prepared.path,
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
    #: Whether two of :attr:`plan`'s filament slots still share a preset name after
    #: ``SlotPresets.planned`` (#4649): only then can Bambuddy's slicer have
    #: dropped the prime tower, so only then is the slice read for it. ``None`` (a plan
    #: from before it, or one no ``SlotPresets`` saw) reads every multi-slot plate.
    slots_share_a_name: bool | None = None


#: A run on the native slicer without Bambu Studio's profiles (a process outside the
#: image): nothing can slice it.
NATIVE_SLICER_UNAVAILABLE = "the native slicer is not set up here: Bambu Studio's profiles"


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
    #: Who slices its plates (plan 2026-10-16 Task 13): ``native`` runs ``slice_run``
    #: on ``library_queue``, which a workflow cannot read from the settings. The defaults
    #: keep every recorded ``print_plan`` result reading as Bambuddy's.
    slicer: Literal["bambuddy", "native"] = "bambuddy"
    library_queue: str = "library"
    #: The registry printer (#3823, plan Task 8) and the path the run takes to it. The
    #: defaults keep every recorded ``print_plan`` result reading as Bambuddy's.
    printer: str = ""
    path: RunPath = "bambuddy"
    #: As :attr:`PreparedPlates.lan_start`: the plates are started over the LAN.
    lan_start: bool = False


class QueuedPlate(BaseModel):
    """One plate on the queue (:func:`queue_plate`): its item, and the rack positions
    sent for it, never with a hotend's serial (spec 2026-10-01 §7), so it may cross a
    workflow's history."""

    item_id: int
    picks: list[RackSentPick] = Field(default_factory=list)
    warnings: list[FilamentWarning] = Field(default_factory=list)


class SlicedRequirements(Protocol):
    """The one read :func:`sliced_as_planned` makes; ``BambuddyClient`` is one."""

    async def filament_requirements(
        self, file_id: int, *, plate_id: int | None = None
    ) -> FilamentRequirements: ...


async def sliced_as_planned(
    client: SlicedRequirements, *, sliced: int, plate_id: int, colours: Sequence[str]
) -> FilamentRequirements | None:
    """Refuse a slice that gave a slot another slot's filament (#4013), before it is
    queued: a slot the plate prints with that comes back in the colour chosen for
    another slot, not its own.

    That is what Bambuddy's ``substitute_unused_plate_filaments`` does to a slot it
    thinks the plate leaves unused: it takes another slot's preset and colour, and the
    slice still succeeds. A black spool then ran a glow profile, and the run said it had
    printed what was chosen. Only that is judged: a colour that is no other slot's (one
    Bambuddy spells or rounds otherwise, or a slot with none chosen or read back) is not
    evidence of a swap, and refusing on it would refuse prints that are right.
    The plan pads a slot with no colour chosen as ``#FFFFFF`` (``resolver``), so a
    slot coming back white counts as another's only where white was really chosen or
    padded; both are slots the plan sliced in white.

    Returns what it read, for the rack pick to rank from rather than read again, or
    ``None`` when Bambuddy did not answer."""
    try:
        requirements = await client.filament_requirements(sliced, plate_id=plate_id)
    except ApiError as error:
        if not is_transient(error):
            raise
        # A large 3MF can outlast this read (#2550), and the run is built to go ahead
        # without one: not answering is no evidence of a swap.
        logger.warning(
            "could not check sliced file %s's filaments, queueing it unchecked: %s",
            sliced,
            error.detail,
        )
        return None
    chosen = [normalise_colour(colour) for colour in colours]
    for filament in requirements.filaments:
        slot = filament.slot_id
        if not filament.used_in_plate or not 1 <= slot <= len(chosen):
            continue
        own, got = chosen[slot - 1], normalise_colour(filament.color)
        others = {
            colour
            for index, colour in enumerate(chosen, start=1)
            if index != slot and colour is not None and colour != own
        }
        if own is not None and got is not None and got != own and got in others:
            raise ApiError(
                status.HTTP_502_BAD_GATEWAY,
                f"Bambuddy sliced plate {plate_id} with filament {slot} in {got}, the colour"
                f" chosen for another filament, not its own {own}, so it was not queued: the"
                " slice would print that spool with another filament's profile.",
                sliced_library_file_id=sliced,
            )
    return requirements


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
    (#1073). The pick and the save never raise (spec 2026-10-01 §5): only the check that
    the slice holds the filaments sent for it (:func:`sliced_as_planned`, #4013) and the
    queue call can fail."""
    read = (
        await sliced_as_planned(
            client, sliced=sliced, plate_id=plate.plate_id, colours=plate.plan.filament_colours
        )
        if plate.plan is not None
        else None
    )
    notes: list[FilamentWarning] = []
    if (
        planned.slicer == "bambuddy"
        and plate.plan is not None
        and len(plate.plan.filament_presets) > 1
        and plate.slots_share_a_name is not False
    ):
        # Before the queue call, which is made once: this never raises (#3962). Slots
        # that each reach the CLI under a name of their own keep the tower, so their
        # slice is not downloaded to look (#4649).
        notes += await tower_dropped(
            client,
            sliced=sliced,
            plate_id=plate.plate_id,
            process=plate.plan.process_preset,
            process_overrides=plate.plan.process_overrides,
        )
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
            requirements=read,
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
    client: BambuddyClient | None,
    source: PrintSource,
    settings: StoredSettings,
    request: PrintRunRequest,
    prepared: PreparedPlates,
    *,
    reuse_slices: bool = True,
    native: NativeSliceStarter | None = None,
    title: str | None = None,
) -> PlannedRun:
    """Upload, then resolve every plate, before anything is sliced (spec §4).

    With ``slicer_backend`` ``native`` the plates are sliced by the native slicer
    (``native``, the image's profiles; a 503 without them), and a reprint reuses a
    native slice's upload, looked up under its native preset key (plan 2026-10-16 Task
    13).

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

    A native run (``prepared.lan_start``, the check's decision, plan 2026-10-18 D2) reads
    nothing of Bambuddy, and ``client`` is ``None`` (D3): the presets are the image's
    (D5), the choices the report's trays (D1), and the copy goes into the native library
    (D6), in the request's folder or the ``title``'s template folder, never a project.
    """
    if source.is_sliced:
        return await _plan_sliced(bambuddy_client(client), source, settings, request, prepared)
    native_slicing = settings.slicer_backend == "native"
    if native_slicing and native is None:
        raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, NATIVE_SLICER_UNAVAILABLE)
    native_run = prepared.lan_start
    if native_run and native is None:
        raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, NATIVE_SLICER_UNAVAILABLE)
    profiles = native.profiles if native_run and native is not None else None
    plate_ids = prepared.plate_ids
    printer_id = prepared.printer_id
    printer_status = prepared.printer_status
    choices = request.choices
    catalogue = await catalogue_for(None if native_run else client, native=profiles)
    process = process_preset_for(choices, catalogue)
    # #717: a profile that cannot apply is skipped with a warning, never refused.
    # One read of the preset's values for the profiles and the supports (#2804 review).
    read = values_for(None if native_run else client, native=profiles)
    from_profiles, profile_warnings = await profile_overrides(
        client, process, request.filament_profiles, read=read
    )
    # The source places, recolors and uploads what it prints (#105, #126, #476), into
    # the project's folder when there is one (#79, #316). Never `planned_project` on a
    # native run: it falls back to Bambuddy's project list (D6).
    project_id = (
        None
        if native_run
        else await planned_project(bambuddy_client(client), request, settings, prepared)
    )
    # ScadBuddy plans each filament's nozzle (#2166) and states it in the 3MF.
    # One read of the inventory for the plan and every plate's options (#480); a native
    # run's choices are the trays, so its plan reads none (D1).
    inventory: Inventory = ([], []) if native_run else await read_inventory(bambuddy_client(client))
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
    # Nor a prime tower setting (#3628): it is sent, with a warning when the preset lacks it.
    # No second round trip: `read` (values_for) answers again what supports just read.
    tower_warnings = await prime_tower_warnings(client, process, request.prime_tower, read=read)
    tower_dropped = template_keys_dropped(request.prime_tower)
    # The plan's sides (one chosen by hand, or each Track Switch inlet's, #4272) are
    # kept only in a file of one plate (#3955 review): the map it pins is read by a slice
    # of the whole file, which for more plates prints them all.
    filament_map = _filament_map(
        nozzle_plan,
        len(source.colours),
        one_plate=nozzle_plan is not None
        and nozzle_plan.keeps_sides
        and len(await source.plate_ids(client)) <= 1,
    )
    folder_id = request.folder_id
    if native_run:
        library = source.native
        if library is None:
            raise ApiError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "a native print needs ScadBuddy's own library, which does not serve here",
            )
        folder_id = await native_destination(library, folder_id=folder_id, title=title)
    printed = await source.file_to_print(
        client,
        printer_id=printer_id,
        nozzle_size=choices.nozzles[0].size,
        plan=request.filament_plan,
        project_id=project_id,
        folder_id=folder_id,
        native_copy=native_run,
        printer_model=prepared.printer_model,
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
        filament_map=filament_map,
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
    if native_run and printer_status is None:
        raise ApiError(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "a native run was checked without its printer's report",
        )
    per_plate = await gather_plate_options(
        client,
        library_file_id=library_file_id,
        printer_id=printer_id,
        plate_ids=plate_ids,
        fallback_colours=list(source.colours),
        inventory=None if native_run else inventory,
        # The file's own slots when it was read (#2550, #2720): a plate's colours are the
        # model's, not the run's recoloured copy's, as the dialog showed them. Intended.
        known=known_slots(source),
        native=source.native,
        # A native run's choices are the report's trays (D1). The overlay adds only
        # weights and names, not presets (`trays.loaded_trays`), so the plan reads none.
        trays_from=printer_status if native_run else None,
    )
    if not native_run:
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
            # A map chosen by hand is read only by a slice of the whole file (#3955).
            whole_file=source.slices_whole_file
            or (filament_map is not None and filament_map.pinned),
            printer_preset=resolved.printer_preset,
            process_preset=resolved.process_preset,
            filament_presets=resolved.filament_presets,
            filament_colours=resolved.filament_colours,
            bed_type=resolved.bed_type,
            process_overrides={
                **from_profiles,
                **{
                    k: v
                    for k, v in source.print_settings.items()
                    if k not in SUPPORT_KEYS and k not in tower_dropped
                },
                **supports,
                **prime_tower_overrides(request.prime_tower),
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
    shared_names: dict[int, bool] = {}
    if not native_slicing and not native_run and client is not None:
        # Bambuddy's slicer drops the prime tower when slots share one preset name
        # (#3962): each repeat gets a preset of its own, before any key is taken, so
        # the preview, the slice and a reprint's lookup all name the same refs.
        slot_presets = SlotPresets(client, catalogue)
        slotted: list[tuple[int, FilamentOptions, Resolved, SlicePlan]] = []
        for plate_id, options, resolved, unslotted in ready:
            slotted_plan, shared_names[plate_id] = await slot_presets.planned(unslotted)
            slotted.append((plate_id, options, resolved, slotted_plan))
        ready = slotted

    # Here, not at the top: see the `TYPE_CHECKING` import.
    from scadbuddy.slicing.start import NativeSliceKeys

    native_keys = (
        NativeSliceKeys(native, None if native_run else client) if native is not None else None
    )

    async def reuse_key(plan: SlicePlan, plate_id: int) -> str:
        if native_keys is None or not native_slicing:
            return slice_request(plan, plate_id).preset_key
        try:
            return await native_keys.key(plan, plate_id)
        except ProfileImageError as error:
            raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, str(error)) from None
        except ProfileError as error:
            raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None

    async def recorded(plan: SlicePlan, plate_id: int) -> int | None:
        return await source.recorded_slice(client, library_file_id, await reuse_key(plan, plate_id))

    # ScadBuddy's own slice of this copy with these presets, when it has one (#2671):
    # every plate's looked up together, each a few GETs (#2691 review).
    reused: list[int | None] = (
        list(await asyncio.gather(*(recorded(plan, plate_id) for plate_id, _, _, plan in ready)))
        if reuse_slices
        else [None] * len(ready)
    )
    planned = [(*plate, sliced) for plate, sliced in zip(ready, reused, strict=True)]

    hardware = await _hardware_warnings(
        None if native_run else client,
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
                slots_share_a_name=shared_names.get(plate_id),
            )
            for plate_id, options, _, plan, reused in planned
        ],
        warnings=warnings + hardware + support_warnings + tower_warnings + _reused_notes(planned),
        rack_algorithm=request.rack_algorithm or settings.rack_algorithm(printer_id),
        rack_position=request.rack_position,
        # A readable status with no rack (any printer but an H2C) has nothing to pick: no
        # extra read per plate, and no rack warning on a printer without one. An
        # unreadable status still tries, since the pick reads a fresh one (#1043).
        has_rack=printer_status is None or bool(rack_positions(printer_status.nozzle_rack)),
        nozzle_plan=nozzle_plan,
        slicer="native" if native_slicing else "bambuddy",
        library_queue=native.library_queue if native is not None else "library",
        printer=prepared.printer,
        path=prepared.path,
        lan_start=prepared.lan_start,
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
    project_id = await planned_project(client, request, settings, prepared)
    profile = source.sliced_profile
    nozzle_size = sliced_nozzle_size(profile, prepared, request)
    printed = await source.file_to_print(
        client,
        printer_id=printer_id,
        nozzle_size=nozzle_size,
        plan=request.filament_plan,
        project_id=project_id,
        folder_id=request.folder_id,
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
        )
        + not_resliced(request.prime_tower),
        rack_algorithm=request.rack_algorithm or settings.rack_algorithm(printer_id),
        has_rack=status is None or bool(rack_positions(status.nozzle_rack)),
        printer=prepared.printer,
        path=prepared.path,
        lan_start=prepared.lan_start,
    )


def _filament_map(
    plan: NozzlePlan | None, filaments: int, *, one_plate: bool
) -> FilamentMap | None:
    """The plan as the 3MF states it, for a file of ``filaments`` filaments."""
    return plan.stated_map(filaments, one_plate=one_plate) if plan is not None else None


def finish_run(
    config: BambuddyConfig | None,
    planned: PlannedRun,
    outcomes: list[QueueOutcome],
    queued: list[QueuedPlate] | None = None,
) -> PrintRunResult:
    """Report what was queued, with each plate's rack picks and warnings (``queued``,
    one per outcome, and a LAN start's own, #4545), linked to Bambuddy's queue unless
    its settings are gone (``None``)."""
    warnings = list(planned.warnings)
    if queued and all(plate.picks for plate in queued):
        # Every plate had the rack side's hotend picked from the rack, which is always one
        # of the size and of the flow sliced there: the one mounted now is swapped out
        # (#1238), whichever flow the slice is for.
        swapped = [high_flow_warning(RACK_SIDE, flow) for flow in VOLUME_TYPE]
        warnings = [warning for warning in warnings if warning not in swapped]
    lan = [outcome.lan.warnings for outcome in outcomes if outcome.lan is not None]
    for plate_warnings in [*(plate.warnings for plate in queued or []), *lan]:
        # A rack warning repeated on every plate is one fact, shown once (spec §6).
        warnings += [warning for warning in plate_warnings if warning not in warnings]
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


def _require_model(printer: Printer) -> None:
    """The resolver knows only the H2C's presets (``PRINTER_MODEL``): a 422 before
    anything is laid out for any other model."""
    if (printer.model or "").upper() != PRINTER_MODEL:
        raise RunRefusalError(
            f"ScadBuddy can only choose slicer presets for a Bambu Lab {PRINTER_MODEL} so "
            f"far, and {printer.name}'s model is {printer.model or 'not reported'}.",
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
    _check_resolvable(printer, any_model=any_model)
    return printer


def _check_resolvable(printer: Printer, *, any_model: bool) -> None:
    """What `_require_resolvable_printer` checks of a printer already read: its model,
    unless ``any_model``."""
    if not any_model:
        _require_model(printer)


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


HELD_SLICED_KINDS = frozenset(
    {"sliced-other-model", "sliced-nozzle-missing", "sliced-plate-missing", "sliced-unchecked"}
)
"""Sliced-file warnings that hold the print for a manual start (#2954): another plate
than the one asked for, or a file whose fit could not be checked, too (#3436)."""


def sliced_warnings(
    profile: SlicedProfile | None,
    printer: Printer,
    status: PrinterStatus | None,
    plate_ids: list[int],
) -> list[FilamentWarning]:
    """What to set right before a sliced file prints on ``printer`` (#2671): sliced for
    another model, or for a nozzle diameter the printer does not have. Never a refusal:
    it is queued as it is, held for a manual start (:data:`HELD_SLICED_KINDS`), and the
    user swaps the nozzle, or picks another printer, before starting it. A profile that
    could not be read is held too, as nothing in it was checked (#3436); a printer that
    reports no model says nothing of the model, and one that reports no nozzles is said
    to leave the nozzle unchecked. Another model of the same G-code family (the X1 and
    P1 series) is said, not held: Bambuddy dispatches its G-code across them (#2956)."""
    if profile is None:
        return [
            FilamentWarning(
                kind="sliced-unchecked",
                message=(
                    "ScadBuddy could not read this sliced file's settings, so its printer"
                    " model and nozzle were not checked. Held for a manual start: check"
                    " them before you start the print."
                ),
            )
        ]
    warnings: list[FilamentWarning] = []
    wanted, have = model_key(profile.printer_model), model_key(printer.model)
    if same_family(wanted, have):
        warnings.append(
            FilamentWarning(
                kind="sliced-same-family",
                message=(
                    # Both by their short name: Bambuddy may hold an SSDP code ("C12"), and
                    # the file a Studio name ("Bambu Lab X1 Carbon") (#3495 review).
                    f"Sliced for a {wanted}; {printer.name} is a {have},"
                    " whose G-code family takes it."
                ),
            )
        )
    elif wanted is not None and have is not None and wanted != have:
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
    client: BambuddyClient | None,
    printer_id: int,
    choices: PrintChoices,
    printer_status: PrinterStatus | None,
    *,
    printer_name: str | None,
    printer_model: str | None,
) -> list[FilamentWarning]:
    """The chosen nozzles and plate against what the printer reports (spec §4.4-4.5).

    Advisory only: an offline printer's status and an unreadable archive list mean
    "unknown", which says nothing rather than failing the print. A native run has no
    ``client`` (plan 2026-10-18 D3): its archives are Bambuddy's, so it is skipped as an
    unreadable archive list is.
    """
    installed = installed_nozzles(printer_status)
    last: str | None = None
    if client is not None:
        try:
            last = last_bed_type(
                await client.archives(printer_id=printer_id), printer_id=printer_id
            )
        except (ApiError, ValueError):
            logger.info("archives unreadable; the plate is not compared with the last print")
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
    lan = [outcome.lan for outcome in outcomes if outcome.lan is not None]
    if lan:
        # Over the LAN: nothing is on Bambuddy's queue to link to.
        config = None
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
        path="native" if lan else "bambuddy",
        lan=lan,
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
    plate_ids: Sequence[int] | None = None,
    filaments: FilamentReader | None = None,
    files: LibraryReads | None = None,
) -> FilamentOptions:
    """:func:`filament_options` for a file already in the library (#313); with
    ``filaments``, its slots read from the file itself (#2550); ``files`` reads a native
    one (#3827)."""
    return await filament_options(
        client,
        await LibrarySource.load(
            client, file_id, uploads=uploads, settings=settings, filaments=filaments, files=files
        ),
        printer_id=printer_id,
        plate_id=plate_id,
        all_plates=all_plates,
        plate_ids=plate_ids,
    )
