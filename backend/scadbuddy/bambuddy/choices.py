"""What the spool-first print dialog offers (spec §2-3), read from Bambuddy once."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable

from pydantic import BaseModel, Field

from scadbuddy.bambuddy.catalogue import PresetChoice, _Catalogue, cached_catalogue
from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.concurrent import settled
from scadbuddy.bambuddy.extruders import default_nozzles
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
from scadbuddy.bambuddy.print_run import filament_options, read_status
from scadbuddy.bambuddy.print_source import OutputSource, PrintSource
from scadbuddy.bambuddy.resolver import _SOURCE_ORDER, DEFAULT_BED, TIERS, Tier
from scadbuddy.bambuddy.uploads import BambuddyUploadStore
from scadbuddy.core.problems import ApiError
from scadbuddy.library.outputs import OutputMeta, OutputStore
from scadbuddy.library.settings_store import ModelPrintChoices, StoredSettings
from scadbuddy.rack.usage import RackUsage, record_seen
from scadbuddy.render.plate import bed_types_for

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
    #: What the dialog opens on when the model remembers no nozzles (#1895): High Flow
    #: on each side that has a High Flow nozzle of the size, else Standard.
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


def filament_presets_by_size(catalogue: _Catalogue) -> dict[str, list[FilamentPresetOption]]:
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
    rack: RackUsage | None = None,
) -> ChoicesView:
    """The dialog's choices, read in two waves (#2504): ``source`` and the printers
    together, then everything that needs the chosen printer together, its status read
    once. The preset catalogue is read beside both waves, never waited on before the
    second (#2354), and kept between opens (#2361). A failure surfaces as it did when
    the reads ran one by one: the source's, then the printers', the presets' and the
    filament step's. So a library file that is gone or unprintable still answers its
    404 or 422, though the printers have been read beside it by then: a few wasted
    reads on a rare path, for not waiting on the file every time."""
    presets = asyncio.ensure_future(cached_catalogue(client))
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
    presets: asyncio.Future[_Catalogue],
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
        or (settings.printer_bed_types.get(str(printer_id)) if printer_id is not None else None)
        or DEFAULT_BED
    )
    model = next((row.model for row in printers if row.id == printer_id), None)
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
        model_choices=remembered or ModelPrintChoices(),
        rack_algorithm=settings.rack_algorithm(printer_id),
        sliced=(ready.sliced_profile or SlicedProfile()) if ready.is_sliced else None,
    )


async def choices_for_output(
    client: BambuddyClient,
    store: OutputStore,
    uploads: BambuddyUploadStore,
    meta: OutputMeta,
    settings: StoredSettings,
    *,
    printer_id: int | None,
    rack: RackUsage | None = None,
) -> ChoicesView:
    return await choices_for(
        client,
        _given(OutputSource(store=store, meta=meta, uploads=uploads, settings=settings)),
        settings,
        remembered=settings.model_print_choices.get(meta.slug),
        printer_id=printer_id,
        rack=rack,
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
