"""What the spool-first print dialog offers (spec §2-3), read from Bambuddy once."""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from scadbuddy.bambuddy.catalogue import PresetChoice, _Catalogue, _catalogue
from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.filaments import FilamentOptions
from scadbuddy.bambuddy.hardware import (
    InstalledNozzle,
    installed_nozzles,
    last_bed_type,
    nozzle_warning,
    plate_warning,
)
from scadbuddy.bambuddy.models import Archive, PresetRef, Printer, PrinterStatus
from scadbuddy.bambuddy.pipelines import BED_TYPES, filament_options_for_output
from scadbuddy.bambuddy.resolver import _SOURCE_ORDER, DEFAULT_BED, TIERS, Tier
from scadbuddy.core.problems import ApiError
from scadbuddy.library.outputs import OutputMeta, OutputStore
from scadbuddy.library.settings_store import ModelPrintChoices, StoredSettings

logger = logging.getLogger(__name__)

SIZES = ["0.2", "0.4", "0.6", "0.8"]

#: Re-exported: the helpers moved to ``hardware`` so the run can use them without an
#: import cycle through ``pipelines``.
__all__ = [
    "ChoicesView",
    "InstalledNozzle",
    "TierOption",
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


class ChoicesView(BaseModel):
    printer_id: int | None = None
    printers: list[Printer] = Field(default_factory=list)
    nozzle_sizes: list[str] = Field(default_factory=lambda: list(SIZES))
    installed: list[InstalledNozzle] = Field(default_factory=list)
    tiers: dict[str, list[TierOption]] = Field(default_factory=dict)
    processes: dict[str, list[str]] = Field(default_factory=dict)
    bed_types: list[str] = Field(default_factory=lambda: list(BED_TYPES))
    last_bed_type: str | None = None
    bed_type: str = DEFAULT_BED
    filaments: FilamentOptions
    #: Task 9 R5 — the filament presets each nozzle size takes, for Advanced mode's
    #: per-slot override. One row per name, the resolver's preferred tier winning.
    filament_presets: dict[str, list[FilamentPresetOption]] = Field(default_factory=dict)
    #: What this model last printed with (#78), so the dialog reopens on those spools.
    model_choices: ModelPrintChoices = Field(default_factory=ModelPrintChoices)


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
            FilamentPresetOption(ref=row.ref, name=row.name)
            for row in sorted(best.values(), key=lambda row: row.name.lower())
        ]
    return out


async def choices_for_output(
    client: BambuddyClient,
    store: OutputStore,
    meta: OutputMeta,
    settings: StoredSettings,
    *,
    printer_id: int | None,
) -> ChoicesView:
    printers = [row for row in await client.printers() if row.is_active]
    remembered = settings.model_print_choices.get(meta.slug)
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
    archives: list[Archive] = []
    if printer_id is not None:
        try:
            status = await client.printer_status(printer_id)
        except (ApiError, ValueError):
            logger.info("printer status unreadable; offering every nozzle size unmarked")
        try:
            archives = await client.archives(printer_id=printer_id)
        except (ApiError, ValueError):
            logger.info("archives unreadable; the plate falls back to the remembered one")
    catalogue = await _catalogue(client)
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
    last = last_bed_type(archives, printer_id=printer_id) if printer_id is not None else None
    bed = (
        last
        or (settings.printer_bed_types.get(str(printer_id)) if printer_id is not None else None)
        or DEFAULT_BED
    )
    filaments = await filament_options_for_output(
        client, store, meta, settings, printer_id=printer_id
    )
    return ChoicesView(
        printer_id=printer_id,
        printers=printers,
        installed=installed_nozzles(status),
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
    )
