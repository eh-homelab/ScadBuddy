"""Reading an :class:`AnalysisContext` with the calls ScadBuddy already makes.

Nothing here writes. In particular nothing uploads: Bambuddy's plate slots are read
only for an output that already has a library file, which the print dialog's filament
step or a send has made. An output with none is
judged on what can be read without one, and the context says what is missing.

Every Bambuddy read goes through :class:`~scadbuddy.bambuddy.client.BambuddyClient`,
so a missing API-key scope is reported by name (``bambuddy/errors.py``), as the reason
an input is unavailable rather than as a failed run.
"""

from __future__ import annotations

import asyncio
import logging
import zipfile
from pathlib import Path

from scadbuddy.analyzers.context import (
    AnalysisContext,
    AnalysisRequest,
    FilamentSlot,
    InputName,
)
from scadbuddy.bambuddy.client import BambuddyClient, client_for
from scadbuddy.bambuddy.filaments import gather_options
from scadbuddy.bambuddy.models import Printer
from scadbuddy.bambuddy.resolver import DEFAULT_BED, PrintChoices
from scadbuddy.core.problems import ApiError
from scadbuddy.library.outputs import MODEL_NAME, OutputMeta, OutputStore
from scadbuddy.library.settings_store import ModelPrintChoices, StoredSettings
from scadbuddy.render.geometry import NoSuchPlateError
from scadbuddy.render.plate import plate_for
from scadbuddy.render.schema import ParamValue

logger = logging.getLogger(__name__)

NO_BAMBUDDY = "Bambuddy is not configured"
BAMBUDDY_INPUTS: tuple[InputName, ...] = ("printer", "filaments", "inventory")


async def gather_context(
    *,
    outputs: OutputStore,
    settings: StoredSettings,
    slug: str,
    params: dict[str, ParamValue],
    request: AnalysisRequest,
    meta: OutputMeta | None,
) -> AnalysisContext:
    context = AnalysisContext(slug=slug, params=params, request=request, output=meta)
    if meta is None:
        context.unavailable["output"] = "this configuration has not been rendered yet"
        context.unavailable["geometry"] = "this configuration has not been rendered yet"
    else:
        context.model_3mf = await asyncio.to_thread(_model_path, outputs, meta)
        await _read_geometry(context, outputs, meta)

    if not settings.bambuddy_url:
        for name in BAMBUDDY_INPUTS:
            context.unavailable[name] = NO_BAMBUDDY
        remembered = settings.model_print_choices.get(slug)
        _read_choices(context, settings, remembered, request.printer_id or settings.printer_id)
    else:
        async with client_for(settings) as client:
            await _read_bambuddy(context, client, settings)

    model = context.printer.model if context.printer and context.printer.model else None
    plate = plate_for(model)
    # As the customizer does: an unknown or unchosen printer is the configured default.
    context.plate = plate if plate.model is not None else plate_for(settings.default_plate)
    return context


def _model_path(outputs: OutputStore, meta: OutputMeta) -> Path | None:
    path = outputs.directory(meta.id) / MODEL_NAME
    return path if path.is_file() else None


async def _read_geometry(context: AnalysisContext, outputs: OutputStore, meta: OutputMeta) -> None:
    # The plate this print sends (#289): each plate of a multi-plate output is
    # measured on its own, as ``GET /outputs/{id}/geometry?plate=`` does.
    plate = context.request.plate_id
    try:
        context.geometry = await asyncio.to_thread(outputs.geometry, meta.id, plate)
    except NoSuchPlateError as error:
        context.unavailable["geometry"] = str(error)
    except FileNotFoundError:
        context.unavailable["geometry"] = "this output has no 3MF to analyse"
    except (ValueError, zipfile.BadZipFile) as error:
        context.unavailable["geometry"] = f"the 3MF cannot be analysed: {error}"


async def _read_bambuddy(
    context: AnalysisContext, client: BambuddyClient, settings: StoredSettings
) -> None:
    request = context.request
    meta = context.output
    remembered = settings.model_print_choices.get(context.slug)

    printers: list[Printer] = []
    try:
        printers = [row for row in await client.printers() if row.is_active]
    except ApiError as error:
        context.unavailable["printer"] = error.detail
    # As the print dialog's choices route picks it (spool-first spec §7, amendment 3).
    active = {row.id for row in printers}
    printer_id = (
        request.printer_id
        or next(
            (
                candidate
                for candidate in (
                    remembered.printer_id if remembered else None,
                    settings.printer_id,
                )
                if candidate in active
            ),
            None,
        )
        or (printers[0].id if printers else None)
    )
    if "printer" not in context.unavailable:
        context.printer = next((row for row in printers if row.id == printer_id), None)
        if context.printer is None:
            context.unavailable["printer"] = (
                "no printer is chosen" if printer_id is None else f"no active printer {printer_id}"
            )
    _read_choices(context, settings, remembered, printer_id)

    await _read_filaments(context, client)

    library_file_id = meta.library_file_id if meta is not None else None
    if library_file_id is None:
        context.unavailable["inventory"] = "this output has not been uploaded to Bambuddy yet"
        return
    if request.filament_plan is None:
        context.unavailable["inventory"] = "no filament plan was chosen"
        return
    try:
        context.filament_options = await gather_options(
            client,
            library_file_id=library_file_id,
            printer_id=context.printer.id if context.printer else None,
            plate_id=request.plate_id,
            fallback_colours=list(meta.colors) if meta is not None else None,
        )
    except ApiError as error:
        context.unavailable["inventory"] = error.detail


def _read_choices(
    context: AnalysisContext,
    settings: StoredSettings,
    remembered: ModelPrintChoices | None,
    printer_id: int | None,
) -> None:
    """The request's choices, else the ones the dialog reopens with for this model."""
    if context.request.choices is not None:
        context.choices = context.request.choices
        return
    if remembered is None or not remembered.nozzles:
        context.unavailable["choices"] = "no nozzle or quality is chosen for this model"
        return
    bed = settings.printer_bed_types.get(str(printer_id)) if printer_id is not None else None
    context.choices = PrintChoices(
        nozzles=remembered.nozzles,
        tier=remembered.tier or "standard",
        process_name=remembered.process_name,
        bed_type=bed or DEFAULT_BED,
    )
    context.choices_origin = "remembered"


async def _read_filaments(context: AnalysisContext, client: BambuddyClient) -> None:
    """Each slot's material, from the spool the plan chose for it."""
    plan = context.request.filament_plan
    if plan is None or not plan.slots:
        context.unavailable["filaments"] = "no filament plan was chosen"
        return
    try:
        spools = {spool.id: spool for spool in await client.spools()}
    except ApiError as error:
        context.unavailable["filaments"] = error.detail
        return
    for choice in sorted(plan.slots, key=lambda row: row.slot_id):
        spool = spools.get(choice.spool_id)
        if spool is None:
            continue
        context.filaments.append(
            FilamentSlot(
                slot_id=choice.slot_id,
                spool_id=spool.id,
                material=spool.material,
                subtype=spool.subtype,
                brand=spool.brand,
                preset_name=spool.slicer_filament_name,
            )
        )
    if not context.filaments:
        context.unavailable["filaments"] = "none of the plan's spools is in the inventory"
