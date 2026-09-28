"""Reading an :class:`AnalysisContext` with the calls ScadBuddy already makes.

Nothing here writes. In particular nothing uploads: Bambuddy's plate slots and its
eligibility report are read only for an output that already has a library file, which
the print dialog's own eligibility check or a send has made. An output with none is
judged on what can be read without one, and the context says what is missing.

Every Bambuddy read goes through :class:`~scadbuddy.bambuddy.client.BambuddyClient`,
so a missing API-key scope is reported by name (``bambuddy/errors.py``), as the reason
an input is unavailable rather than as a failed run.
"""

from __future__ import annotations

import asyncio
import logging
import zipfile

from scadbuddy.analyzers.context import (
    AnalysisContext,
    AnalysisRequest,
    FilamentSlot,
    InputName,
)
from scadbuddy.bambuddy.client import BambuddyClient, client_for
from scadbuddy.bambuddy.filaments import gather_options
from scadbuddy.bambuddy.models import EligibilityRequest, Printer, Spool
from scadbuddy.bambuddy.pipelines import pipeline_view
from scadbuddy.core.problems import ApiError
from scadbuddy.library.outputs import OutputMeta, OutputStore
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.render.plate import plate_for
from scadbuddy.render.schema import ParamValue

logger = logging.getLogger(__name__)

NO_BAMBUDDY = "Bambuddy is not configured"
BAMBUDDY_INPUTS: tuple[InputName, ...] = (
    "pipeline",
    "printer",
    "filaments",
    "inventory",
    "eligibility",
)


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
        await _read_geometry(context, outputs, meta)

    if not settings.bambuddy_url:
        for name in BAMBUDDY_INPUTS:
            context.unavailable[name] = NO_BAMBUDDY
    else:
        async with client_for(settings) as client:
            await _read_bambuddy(context, client, settings)

    model = context.printer.model if context.printer and context.printer.model else None
    if model is None and context.pipeline is not None:
        model = context.pipeline.target_model_class
    plate = plate_for(model)
    # As the customizer does: an unknown or unchosen printer is the configured default.
    context.plate = plate if plate.model is not None else plate_for(settings.default_plate)
    return context


async def _read_geometry(context: AnalysisContext, outputs: OutputStore, meta: OutputMeta) -> None:
    try:
        context.geometry = await asyncio.to_thread(outputs.geometry, meta.id)
    except FileNotFoundError:
        context.unavailable["geometry"] = "this output has no 3MF to analyse"
    except (ValueError, zipfile.BadZipFile) as error:
        context.unavailable["geometry"] = f"the 3MF cannot be analysed: {error}"


async def _read_bambuddy(
    context: AnalysisContext, client: BambuddyClient, settings: StoredSettings
) -> None:
    request = context.request
    meta = context.output
    pipeline_id = request.pipeline_id or settings.pipeline_for(context.slug)

    printers: list[Printer] = []
    try:
        printers = await client.printers()
    except ApiError as error:
        context.unavailable["printer"] = error.detail

    if pipeline_id is None:
        context.unavailable["pipeline"] = "no slicer pipeline is chosen for this model"
    else:
        try:
            context.pipeline = await pipeline_view(client, pipeline_id, printers)
        except ApiError as error:
            context.unavailable["pipeline"] = error.detail

    printer_id = request.printer_id
    if printer_id is None and context.pipeline is not None:
        ids = context.pipeline.printer_ids
        # A class target with several printers has no one printer until the picker asks.
        printer_id = ids[0] if len(ids) == 1 else None
    if printer_id is None:
        printer_id = settings.printer_id
    if "printer" not in context.unavailable:
        context.printer = next((row for row in printers if row.id == printer_id), None)
        if context.printer is None:
            context.unavailable["printer"] = (
                "no printer is chosen" if printer_id is None else f"no printer {printer_id}"
            )

    await _read_filaments(context, client)

    library_file_id = meta.library_file_id if meta is not None else None
    if library_file_id is None:
        reason = "this output has not been uploaded to Bambuddy yet"
        context.unavailable["eligibility"] = reason
        context.unavailable["inventory"] = reason
        return

    if context.pipeline is not None:
        try:
            context.eligibility = await client.check_eligibility(
                context.pipeline.id, EligibilityRequest(source_library_file_id=library_file_id)
            )
        except ApiError as error:
            context.unavailable["eligibility"] = error.detail
    else:
        context.unavailable["eligibility"] = "no pipeline to check against"

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


async def _read_filaments(context: AnalysisContext, client: BambuddyClient) -> None:
    """Each slot's material: the plan's spools where it names them, else the pipeline's
    filament preset for that slot (a preset has a name, and no material field)."""
    plan = context.request.filament_plan
    spools: dict[int, Spool] = {}
    if plan is not None and plan.slots:
        try:
            spools = {spool.id: spool for spool in await client.spools()}
        except ApiError as error:
            context.unavailable["filaments"] = error.detail
            return
    names = context.pipeline.filament_preset_names if context.pipeline else []
    slot_ids = sorted(
        {choice.slot_id for choice in (plan.slots if plan else [])} | set(range(1, len(names) + 1))
    )
    for slot_id in slot_ids:
        spool_id = plan.spool_for(slot_id) if plan else None
        spool = spools.get(spool_id) if spool_id is not None else None
        pipeline_name = names[slot_id - 1] if slot_id <= len(names) else None
        if spool is not None:
            context.filaments.append(
                FilamentSlot(
                    slot_id=slot_id,
                    spool_id=spool.id,
                    material=spool.material,
                    subtype=spool.subtype,
                    brand=spool.brand,
                    preset_name=spool.slicer_filament_name or pipeline_name,
                    origin="spool",
                )
            )
        elif pipeline_name is not None:
            context.filaments.append(
                FilamentSlot(slot_id=slot_id, preset_name=pipeline_name, origin="pipeline")
            )
    if not context.filaments and "filaments" not in context.unavailable:
        context.unavailable["filaments"] = "neither a filament plan nor the pipeline names one"
