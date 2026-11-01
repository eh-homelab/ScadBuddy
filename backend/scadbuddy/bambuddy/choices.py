"""What the spool-first print dialog offers (spec §2-3), read from Bambuddy once."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import status as http
from pydantic import BaseModel, Field

from scadbuddy.bambuddy.catalogue import (
    Catalogue,
    PresetChoice,
    RefreshClaim,
    cached_catalogue,
)
from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.concurrent import settled
from scadbuddy.bambuddy.extruders import default_nozzles, one_flow
from scadbuddy.bambuddy.filaments import FilamentOptions
from scadbuddy.bambuddy.filaments3mf import SlicedProfile
from scadbuddy.bambuddy.hardware import (
    InstalledNozzle,
    bed_type_labels,
    installed_nozzles,
    last_bed_type,
    nozzle_warning,
    plate_warning,
)
from scadbuddy.bambuddy.models import (
    DEFAULT_ALGORITHM,
    NozzleChoice,
    PresetRef,
    Printer,
    PrinterStatus,
    RackAlgorithm,
)
from scadbuddy.bambuddy.print_run import (
    NATIVE_SLICER_UNAVAILABLE,
    LanBlock,
    RunRoute,
    filament_options,
    lan_printers,
    lan_reach,
    read_status,
)
from scadbuddy.bambuddy.print_source import OutputSource, PrintSource
from scadbuddy.bambuddy.resolver import _SOURCE_ORDER, DEFAULT_BED, TIERS, Tier
from scadbuddy.bambuddy.uploads import BambuddyUploadStore
from scadbuddy.core.problems import ApiError
from scadbuddy.library.outputs import OutputMeta, OutputStore
from scadbuddy.library.settings_store import ModelPrintChoices, StoredSettings
from scadbuddy.printers.hot_path import native_backends
from scadbuddy.printers.models import PrinterRow
from scadbuddy.rack.usage import RackUsage, record_seen
from scadbuddy.render.plate import bed_types_for
from scadbuddy.slicing.catalogue import catalogue_for
from scadbuddy.slicing.profiles import Profiles
from scadbuddy.spools.models import Overlay

if TYPE_CHECKING:
    from scadbuddy.printers.component import Printers

logger = logging.getLogger(__name__)

SIZES = ["0.2", "0.4", "0.6", "0.8"]

#: How long the dialog waits on the printer's archives, which only preselect a plate
#: (#2354): a stalled list costs the preselection, not the dialog a 504.
ARCHIVES_BUDGET = 5.0

#: Re-exported: the helpers moved to ``hardware`` so the run can use them without an
#: import cycle through ``print_run``.
__all__ = [
    "ChoicesView",
    "InstalledNozzle",
    "TierOption",
    "choices_for",
    "choices_for_output",
    "installed_nozzles",
    "last_bed_type",
    "nozzle_warning",
    "plate_warning",
]


class TierOption(BaseModel):
    tier: Tier
    process_name: str


class FilamentPresetOption(BaseModel):
    """One row of Advanced mode's per-slot override: all the dialog reads of a preset."""

    ref: PresetRef
    name: str
    #: The preset's material ("PLA"), so a slot is offered only presets of its own (#2166).
    filament_type: str | None = None


class ChoicesView(BaseModel):
    printer_id: int | None = None
    printers: list[Printer] = Field(default_factory=list)
    nozzle_sizes: list[str] = Field(default_factory=lambda: list(SIZES))
    installed: list[InstalledNozzle] = Field(default_factory=list)
    #: What the dialog opens on when the model remembers no nozzles (#1895): one flow
    #: for both sides (#4272), High Flow when either side has a High Flow nozzle of the
    #: size, else Standard.
    default_nozzles: list[NozzleChoice] = Field(default_factory=lambda: default_nozzles(None))
    tiers: dict[str, list[TierOption]] = Field(default_factory=dict)
    processes: dict[str, list[str]] = Field(default_factory=dict)
    #: The plates the chosen printer's model takes, by ``curr_bed_type`` (#2366); every
    #: plate for a model the profiles do not know.
    bed_types: list[str] = Field(default_factory=lambda: list(bed_types_for(None)))
    #: The label Bambu Studio shows for each of ``bed_types``.
    bed_type_labels: dict[str, str] = Field(default_factory=dict)
    last_bed_type: str | None = None
    bed_type: str = DEFAULT_BED
    filaments: FilamentOptions
    #: Task 9 R5 — the filament presets each nozzle size takes, for Advanced mode's
    #: per-slot override. One row per name, the resolver's preferred tier winning.
    filament_presets: dict[str, list[FilamentPresetOption]] = Field(default_factory=dict)
    #: What this model last printed with (#78), so the dialog reopens on those spools.
    model_choices: ModelPrintChoices = Field(default_factory=ModelPrintChoices)
    #: How the chosen printer's rack nozzle is ranked (#836): remembered per printer,
    #: else Least used. The dialog's Advanced selector opens on it.
    rack_algorithm: RackAlgorithm = DEFAULT_ALGORITHM
    #: A sliced file (#2671): what it was sliced for, which the dialog shows read-only in
    #: place of the slicer steps; empty when that could not be read. ``None`` for a file
    #: the print slices.
    sliced: SlicedProfile | None = None
    #: #3818 — the registry printers a run would reach over the LAN
    #: (`print_run.lan_printers`): a print on one is filed in a native folder with no
    #: project. Empty until plan 3a Task 9, while every run goes through Bambuddy.
    lan_printers: list[str] = Field(default_factory=list)
    #: #3823 — every other physical registry printer, with why a run on it is not native
    #: (`print_run.lan_block`): the dialog says what a LAN-only one lacks, never guessing.
    lan_blocked: list[LanBlock] = Field(default_factory=list)


def filament_presets_by_size(catalogue: Catalogue) -> dict[str, list[FilamentPresetOption]]:
    out: dict[str, list[FilamentPresetOption]] = {}
    for size in SIZES:
        printer = f"Bambu Lab H2C {size} nozzle"
        best: dict[str, PresetChoice] = {}
        for row in catalogue.filament:
            if printer not in row.compatible_printers:
                continue
            held = best.get(row.name)
            if held is None or _SOURCE_ORDER.get(row.ref.source, 9) < _SOURCE_ORDER.get(
                held.ref.source, 9
            ):
                best[row.name] = row
        out[size] = [
            FilamentPresetOption(ref=row.ref, name=row.name, filament_type=row.filament_type)
            for row in sorted(best.values(), key=lambda row: row.name.lower())
        ]
    return out


async def choices_for(
    client: BambuddyClient,
    source: Awaitable[PrintSource],
    settings: StoredSettings,
    *,
    remembered: ModelPrintChoices | None,
    printer_id: int | None,
    cache: Path,
    # Required, so a caller chooses the file name's key rather than falling back to the
    # bare hash without saying so (#3816 review): None only when there is no KEK.
    catalogue_key: bytes | None,
    rack: RackUsage | None = None,
    native: Profiles | None = None,
    refresh_claim: RefreshClaim | None = None,
) -> ChoicesView:
    """The dialog's choices, read in two waves (#2504): ``source`` and the printers
    together, then everything that needs the chosen printer together, its status read
    once. The preset catalogue is read beside both waves, never waited on before the
    second (#2354), and kept between opens under ``cache`` (#2361, #2978). A failure
    surfaces as it did when the reads ran one by one: the source's, then the printers',
    the presets' and the filament step's. So a library file that is gone or unprintable
    still answers its 404 or 422, though the printers have been read beside it by then:
    a few wasted reads on a rare path, for not waiting on the file every time. The
    rack's state is recorded by the second wave, so a failed catalogue read raises after
    it is written, not before (#2907). With ``native`` (the image's profiles) the
    catalogue is theirs (``slicing/catalogue.py``), and no preset is read from Bambuddy."""
    presets = asyncio.ensure_future(
        # A native run's presets are the image's own: nothing read from Bambuddy, and
        # nothing kept under `cache` (#3818 tier A).
        catalogue_for(None, native=native)
        if native is not None
        else cached_catalogue(client, cache, name_key=catalogue_key, refresh_claim=refresh_claim)
    )
    try:
        return await _choices(
            client,
            source,
            settings,
            presets,
            remembered=remembered,
            printer_id=printer_id,
            rack=rack,
        )
    finally:
        # A failed source or printer list answers without the catalogue; a catalogue
        # read that failed by then is the error nobody reached, not a lost one.
        if presets.done() and not presets.cancelled():
            presets.exception()
        presets.cancel()


async def _choices(
    client: BambuddyClient,
    source: Awaitable[PrintSource],
    settings: StoredSettings,
    presets: asyncio.Future[Catalogue],
    *,
    remembered: ModelPrintChoices | None,
    printer_id: int | None,
    rack: RackUsage | None,
) -> ChoicesView:
    loaded, listed = await asyncio.gather(source, client.printers(), return_exceptions=True)
    ready = settled(loaded)
    printers = [row for row in settled(listed) if row.is_active]
    active = {row.id for row in printers}

    def still_active(candidate: int | None) -> int | None:
        # A remembered or configured printer that was removed or deactivated would open
        # the dialog on a printer it does not list, with no way to pick another.
        return candidate if candidate in active else None

    printer_id = (
        printer_id
        or still_active(remembered.printer_id if remembered else None)
        or still_active(settings.printer_id)
        or (printers[0].id if printers else None)
    )
    status: PrinterStatus | None = None
    last: str | None = None
    if printer_id is None:
        read_presets, read = await asyncio.gather(
            presets, filament_options(client, ready, printer_id=None), return_exceptions=True
        )
        catalogue = settled(read_presets)
        filaments = settled(read)
    else:
        reading = asyncio.ensure_future(read_status(client, printer_id))
        read_presets, seen, plate, read = await asyncio.gather(
            presets,
            _record_seen(rack, printer_id, reading),
            _last_bed_type(client, printer_id),
            filament_options(client, ready, printer_id=printer_id, status=reading),
            return_exceptions=True,
        )
        catalogue = settled(read_presets)
        settled(seen)
        last = settled(plate)
        filaments = settled(read)
        status = reading.result()
    return _view(
        settings,
        catalogue,
        ready,
        filaments,
        printers=printers,
        printer_id=printer_id,
        status=status,
        last=last,
        model=next((row.model for row in printers if row.id == printer_id), None),
        remembered=remembered,
    )


def _view(
    settings: StoredSettings,
    catalogue: Catalogue,
    ready: PrintSource,
    filaments: FilamentOptions,
    *,
    printers: list[Printer],
    printer_id: int | None,
    status: PrinterStatus | None,
    last: str | None,
    model: str | None,
    remembered: ModelPrintChoices | None,
    registry_printer: str | None = None,
) -> ChoicesView:
    processes = {
        size: sorted(
            {
                row.name
                for row in catalogue.process
                if f"Bambu Lab H2C {size} nozzle" in row.compatible_printers
            }
        )
        for size in SIZES
    }
    bed = (
        last
        or settings.bed_type_of(printer=registry_printer, bambuddy_printer_id=printer_id)
        or DEFAULT_BED
    )
    bed_types = list(bed_types_for(model))
    return ChoicesView(
        printer_id=printer_id,
        printers=printers,
        bed_types=bed_types,
        bed_type_labels=bed_type_labels(bed_types),
        installed=installed_nozzles(status),
        default_nozzles=default_nozzles(status),
        tiers={
            size: [TierOption(tier=tier, process_name=name) for tier, name in names.items()]  # type: ignore[arg-type]
            for size, names in TIERS.items()
        },
        processes=processes,
        last_bed_type=last,
        bed_type=bed,
        filaments=filaments,
        filament_presets=filament_presets_by_size(catalogue),
        # One flow, as the resolver requires (#4272): a pair remembered from before it
        # opens the dialog and print_output on what would print, not on a refusal.
        model_choices=(
            remembered.model_copy(update={"nozzles": one_flow(remembered.nozzles)})
            if remembered is not None
            else ModelPrintChoices()
        ),
        rack_algorithm=settings.rack_algorithm(printer_id, registry_printer),
        sliced=(ready.sliced_profile or SlicedProfile()) if ready.is_sliced else None,
    )


async def native_choices_for(
    source: PrintSource,
    settings: StoredSettings,
    route: RunRoute,
    *,
    remembered: ModelPrintChoices | None,
    profiles: Profiles | None,
    rows: list[PrinterRow],
    overlay: Overlay | None,
    provider_labels: Mapping[str, str],
    rack: RackUsage | None = None,
) -> ChoicesView:
    """:func:`choices_for` on a native run (#3818 tier A, plan 2026-10-18 D2-D5), with no
    Bambuddy call: the printers are the registry's ``rows`` that link one, the status is
    ``route``'s from the printer's LAN report (D4), the presets the image's (D5) and the
    filaments the loaded trays with ``overlay`` over them (D1). No plate is preselected
    from Bambuddy's archives: the remembered one is, as when those are unreadable."""
    if profiles is None:
        raise ApiError(http.HTTP_503_SERVICE_UNAVAILABLE, NATIVE_SLICER_UNAVAILABLE)
    assert route.row is not None and route.status is not None, "a native route is a report's"
    status = route.status
    catalogue = await catalogue_for(None, native=profiles)
    filaments = await filament_options(
        None,
        source,
        printer_id=route.printer_id,
        trays_from=status,
        overlay=overlay,
        provider_labels=provider_labels,
    )
    await record_seen(rack, route.printer_id, status, route.row.id)
    printers = [
        Printer(id=row.bambuddy_printer_id, name=row.name, model=row.model)
        for row in rows
        if row.bambuddy_printer_id is not None and row.bambuddy_active
    ]
    return _view(
        settings,
        catalogue,
        source,
        filaments,
        printers=printers,
        printer_id=route.printer_id,
        status=status,
        last=None,
        model=route.row.model,
        remembered=remembered,
        registry_printer=route.row.id,
    )


async def choices_for_output(
    client: BambuddyClient,
    store: OutputStore,
    uploads: BambuddyUploadStore,
    meta: OutputMeta,
    settings: StoredSettings,
    *,
    printer_id: int | None,
    cache: Path,
    # Required, so a caller chooses the file name's key rather than falling back to the
    # bare hash without saying so (#3816 review): None only when there is no KEK.
    catalogue_key: bytes | None,
    rack: RackUsage | None = None,
    refresh_claim: RefreshClaim | None = None,
) -> ChoicesView:
    return await choices_for(
        client,
        _given(OutputSource(store=store, meta=meta, uploads=uploads, settings=settings)),
        settings,
        remembered=settings.model_print_choices.get(meta.slug),
        printer_id=printer_id,
        cache=cache,
        catalogue_key=catalogue_key,
        rack=rack,
        refresh_claim=refresh_claim,
    )


async def _given(source: PrintSource) -> PrintSource:
    return source


async def _record_seen(
    rack: RackUsage | None, printer_id: int, status: Awaitable[PrinterStatus | None]
) -> None:
    """Awaits the shared status read directly, so a cancelled ``choices_for`` cancels
    the read with it; ``filament_options``' wait only keeps its own cancellation out."""
    await record_seen(rack, printer_id, await status)


async def _last_bed_type(client: BambuddyClient, printer_id: int) -> str | None:
    """Advisory, like the run's plate warning: an archive list that cannot be read or
    ordered, or does not answer within :data:`ARCHIVES_BUDGET`, preselects nothing
    rather than failing the dialog."""
    try:
        async with asyncio.timeout(ARCHIVES_BUDGET):
            archives = await client.archives(printer_id=printer_id)
        return last_bed_type(archives, printer_id=printer_id)
    except (ApiError, ValueError, TimeoutError):
        logger.info("archives unreadable; the plate falls back to the remembered one")
        return None


async def with_lan_printers(
    view: ChoicesView, settings: StoredSettings, printers: Printers | None
) -> ChoicesView:
    """``view`` with the registry printers a run would reach over the LAN (#3818), and
    why each other one is not reached (#3823). The registry is read on every backend:
    on Bambuddy's no row is native, and the backends are among the reasons."""
    if printers is None:
        return view
    try:
        rows, blocked = await lan_reach(settings, printers.store, printers.reports)
    except Exception as unread:
        if native_backends(settings):
            raise
        # On Bambuddy's backends no run needs the registry: the reasons only add to the
        # view, as `offering_lan_printers`' offer does.
        logger.warning(
            "could not read the registry to say why", extra={"error": type(unread).__name__}
        )
        return view
    return view.model_copy(update={"lan_printers": rows, "lan_blocked": blocked})


@asynccontextmanager
async def offering_lan_printers(
    settings: StoredSettings, printers: Printers | None
) -> AsyncIterator[None]:
    """A dialog read of Bambuddy's that fails names, on its problem, the registry
    printers a run reaches natively (``lan_printers``, #4601): the dialog then offers
    them in place of the read it could not make, rather than only its Retry. The offer is
    advisory: a registry that cannot be read leaves the problem as it was, never in place
    of Bambuddy's."""
    try:
        yield
    except ApiError as error:
        if native_backends(settings) and printers is not None:
            try:
                rows = await lan_printers(settings, printers.store, printers.reports)
            except Exception as unread:
                # Bambuddy's problem is the answer; the offer only adds to it.
                logger.warning(
                    "could not read the LAN printers to offer",
                    extra={"error": type(unread).__name__},
                )
                rows = []
            if rows:
                error.extensions["lan_printers"] = rows
        raise
