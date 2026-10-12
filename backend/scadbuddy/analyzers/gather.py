"""Reading an :class:`AnalysisContext` with the calls ScadBuddy already makes.

Nothing here writes. In particular nothing uploads: the plate's slots and the
reconciled remaining weights are read only for an output that already has a
library file, which the print dialog's filament step or a send has made. An output
with none is judged on what can be read without one, and the context says what is
missing.

Every Bambuddy read goes through :class:`~scadbuddy.bambuddy.client.BambuddyClient`,
so a missing API-key scope is reported by name (``bambuddy/errors.py``), as the reason
an input is unavailable rather than as a failed run.
"""

from __future__ import annotations

import asyncio
import logging
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from scadbuddy.analyzers.context import (
    AnalysisContext,
    AnalysisRequest,
    FilamentSlot,
    InputName,
    base_profile,
)
from scadbuddy.bambuddy.client import BambuddyClient, client_for
from scadbuddy.bambuddy.filaments import every_plate, gather_plate_options
from scadbuddy.bambuddy.library_view import NotViewableError, library_geometry
from scadbuddy.bambuddy.models import Printer
from scadbuddy.bambuddy.resolver import DEFAULT_BED, PrintChoices
from scadbuddy.core.problems import ApiError
from scadbuddy.library.outputs import MODEL_NAME, OutputMeta, OutputStore
from scadbuddy.library.settings_store import ModelPrintChoices, StoredSettings
from scadbuddy.render.bambu3mf import plates_of
from scadbuddy.render.geometry import NoSuchPlateError
from scadbuddy.render.plate import plate_for
from scadbuddy.render.read_budget import ReadBudget
from scadbuddy.render.schema import ParamValue

logger = logging.getLogger(__name__)

NO_BAMBUDDY = "Bambuddy is not configured"
#: Why a library file's print has no output: what plate fit reads (#1753).
LIBRARY_NO_OUTPUT = "a library file prints where its author placed it; ScadBuddy made no output"
BAMBUDDY_INPUTS: tuple[InputName, ...] = ("printer", "filaments", "inventory")


async def gather_context(
    *,
    outputs: OutputStore,
    settings: StoredSettings,
    slug: str | None,
    params: dict[str, ParamValue],
    request: AnalysisRequest,
    meta: OutputMeta | None,
    library_file_id: int | None = None,
    remembered: ModelPrintChoices | None = None,
    library_subject: int | None = None,
    cache: Path | None = None,
    budget: ReadBudget | None = None,
) -> AnalysisContext:
    """``library_file_id`` is one of the output's copies in Bambuddy's library (#316):
    the plate's filament slots are read off an uploaded file. ``remembered`` is what the
    dialog reopens with for this subject.

    ``library_subject`` judges a file in Bambuddy's library instead (#1753), as any
    print is judged: its mesh is read from the 3MF the print path fetches (cached under
    ``cache``), and its slots off the file itself. It has no output, so the checks that
    read one (plate fit: the file prints where its author placed it) say so."""
    context = AnalysisContext(
        slug=slug, params=params, request=request, output=meta, library_file_id=library_subject
    )
    if library_subject is not None:
        library_file_id = library_subject
        context.unavailable["output"] = LIBRARY_NO_OUTPUT
    elif meta is None:
        context.unavailable["output"] = "this configuration has not been rendered yet"
        context.unavailable["geometry"] = "this configuration has not been rendered yet"
    else:
        context.model_3mf = await asyncio.to_thread(_model_path, outputs, meta)
        await _read_geometry(context, outputs, meta)

    if not settings.bambuddy_url:
        for name in BAMBUDDY_INPUTS:
            context.unavailable[name] = NO_BAMBUDDY
        if library_subject is not None:
            context.unavailable["geometry"] = NO_BAMBUDDY
        _read_choices(context, settings, remembered, request.printer_id or settings.printer_id)
    else:
        async with client_for(settings) as client:
            if library_subject is not None:
                assert cache is not None, "a library file's mesh is cached"
                await _read_library_geometry(context, client, cache, library_subject, budget)
            await _read_bambuddy(context, client, settings, remembered, library_file_id)

    model = context.printer.model if context.printer and context.printer.model else None
    plate = plate_for(model)
    # As the customizer does: an unknown or unchosen printer is the configured default.
    context.plate = plate if plate.model is not None else plate_for(settings.default_plate)
    context.base = base_profile(
        request, context.choices, context.printer, context.filaments, context.plate
    )
    if context.choices is not None:
        context.base.choices_origin = context.choices_origin
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


async def _read_library_geometry(
    context: AnalysisContext,
    client: BambuddyClient,
    cache: Path,
    file_id: int,
    budget: ReadBudget | None,
) -> None:
    """The library file's plate, measured as an output's is (#1753)."""
    try:
        context.geometry = await library_geometry(
            client, cache, file_id, context.request.plate_id, budget
        )
    except NotViewableError as error:
        context.unavailable["geometry"] = f"the 3MF cannot be analysed: {error.reason}"
    except NoSuchPlateError as error:
        context.unavailable["geometry"] = str(error)
    except ApiError as error:
        context.unavailable["geometry"] = error.detail


async def _read_bambuddy(
    context: AnalysisContext,
    client: BambuddyClient,
    settings: StoredSettings,
    remembered: ModelPrintChoices | None,
    library_file_id: int | None,
) -> None:
    request = context.request
    meta = context.output
    printers: list[Printer] = []
    try:
        printers = [row for row in await client.printers() if row.is_active]
    except ApiError as error:
        context.unavailable["printer"] = error.detail
    # As the print dialog's choices route picks it (spool-first spec §7): the request's,
    # else the model's remembered printer, then the configured one, while active, else
    # the first active printer.
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
                "no active printer is chosen or configured"
                if printer_id is None
                else f"Bambuddy has no active printer {printer_id}"
            )
    _read_choices(context, settings, remembered, printer_id)
    await _read_filaments(context, client)

    if request.filament_plan is None:
        context.unavailable["inventory"] = "no filament plan was chosen"
        return
    if library_file_id is None:
        context.unavailable["inventory"] = "this output has not been uploaded to Bambuddy yet"
        return
    # An all-plates print needs each slot's total over every plate, as the filament
    # step reads it (`pipelines.filament_options_for_output`). A 3MF whose plates
    # cannot be listed makes the inventory unavailable, as `_read_geometry` treats the
    # same file, rather than failing the run.
    plate_ids = [request.plate_id]
    try:
        if request.all_plates and context.library_file_id is not None:
            # As the library file's filament step reads them: Bambuddy's plates.
            listed = await client.library_plates(context.library_file_id)
            plate_ids = sorted(plate.index for plate in listed.plates) or [1]
        elif request.all_plates and context.model_3mf is not None:
            plates = await asyncio.to_thread(plates_of, context.model_3mf)
            plate_ids = [plate.index for plate in plates] or [1]
    except (KeyError, ValueError, zipfile.BadZipFile, ET.ParseError) as error:
        context.unavailable["inventory"] = f"the 3MF's plates cannot be read: {error}"
        return
    except ApiError as error:
        context.unavailable["inventory"] = error.detail
        return
    try:
        # The spools, where they are loaded and the printer's remaining weights are the
        # same for every plate, so `gather_plate_options` reads them once (#480); then
        # each plate's own slots, concurrently.
        read = await gather_plate_options(
            client,
            library_file_id=library_file_id,
            printer_id=context.printer.id if context.printer else None,
            plate_ids=plate_ids,
            fallback_colours=list(meta.colors) if meta is not None else None,
        )
        context.filament_options = read[0] if len(read) == 1 else every_plate(read)
    except ApiError as error:
        context.unavailable["inventory"] = error.detail


def _read_choices(
    context: AnalysisContext,
    settings: StoredSettings,
    remembered: ModelPrintChoices | None,
    printer_id: int | None,
) -> None:
    """The request's choices, else the ones the dialog reopens with for this subject."""
    if context.request.choices is not None:
        context.choices = context.request.choices
        return
    if remembered is None or not remembered.nozzles:
        context.unavailable["choices"] = "no nozzle, quality or plate was chosen"
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
    """Each slot's spool, as the plan chose it and the inventory describes it."""
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
        context.unavailable["filaments"] = "no spool of the plan is in Bambuddy's inventory"
