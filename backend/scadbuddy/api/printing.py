"""``/api/v1/print/…`` — choosing, creating and running a Bambuddy slicer pipeline.

Kept out of ``outputs.py`` because these routes are about Bambuddy's pipelines rather
than about an output, and only two of the five are output-scoped at all. ``POST
/outputs/{id}/send`` stays where it was: it is the send bar's one-click path, and it
now resolves the same per-model default this router sets.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel

from scadbuddy.api.deps import OutputIdPath, OutputsDep, SettingsStoreDep, SlugPath
from scadbuddy.api.outputs import require_output
from scadbuddy.bambuddy.client import client_for
from scadbuddy.bambuddy.filaments import FilamentOptions
from scadbuddy.bambuddy.models import PipelineCreate, PresetRef, PresetSource
from scadbuddy.bambuddy.pipelines import (
    EligibilityOverview,
    PipelineChoices,
    PipelineDefault,
    PipelineView,
    PresetOptions,
    PrintRunRequest,
    PrintRunResult,
    check_pipelines,
    create_pipeline,
    describe_pipelines,
    filament_options_for_output,
    preset_options,
    run_for_output,
)
from scadbuddy.bambuddy.progress import PrintProgress, progress_for

router = APIRouter(prefix="/print", tags=["print"])


class EligibilityCheck(BaseModel):
    """``pipeline_ids`` omitted means every pipeline Bambuddy has."""

    pipeline_ids: list[int] | None = None


class PipelineDefaultPatch(BaseModel):
    """``null`` clears this model's default, falling back to the global one."""

    pipeline_id: int | None = None


@router.get(
    "/presets",
    response_model=PresetOptions,
    summary="Presets a new pipeline can be built from",
)
async def get_presets(
    store: SettingsStoreDep,
    printer_preset_source: Annotated[PresetSource | None, Query()] = None,
    printer_preset_id: Annotated[str | None, Query()] = None,
) -> PresetOptions:
    """Printer presets and bed types, plus — once a printer preset is named — the
    process and filament presets compatible with it.

    The filter is server-side on purpose: the live instance holds ~4000 process and
    filament presets across the cloud and standard tiers, which is not a payload to
    hand a browser so it can filter them itself. Nozzle diameter is not a field
    anywhere; it lives in the process preset's *name* ("… H2C 0.2 nozzle"), which is
    why the form picks a process preset rather than a diameter.
    """
    chosen = (
        PresetRef(source=printer_preset_source, id=printer_preset_id)
        if printer_preset_source is not None and printer_preset_id is not None
        else None
    )
    async with client_for(store.load()) as client:
        return await preset_options(client, printer_preset=chosen)


@router.post(
    "/pipelines",
    response_model=PipelineView,
    summary="Create a pipeline from presets",
)
async def post_pipeline(body: PipelineCreate, store: SettingsStoreDep) -> PipelineView:
    """``POST /api/v1/slicer-pipelines/`` verbatim.

    ``SlicerPipelineCreate`` carries no target or fanout fields, so the new pipeline
    cannot be created pre-aimed at a printer — Bambuddy targets it and the response
    reports what it chose. Re-targeting is a ``PUT`` ScadBuddy does not make.
    """
    async with client_for(store.load()) as client:
        return await create_pipeline(client, body)


@router.get(
    "/models/{slug}/pipelines",
    response_model=PipelineChoices,
    summary="Pipelines, with this model's default",
)
async def get_model_pipelines(slug: SlugPath, store: SettingsStoreDep) -> PipelineChoices:
    settings = store.load()
    async with client_for(settings) as client:
        return await describe_pipelines(client, settings, slug)


@router.put(
    "/models/{slug}/pipeline",
    response_model=PipelineDefault,
    summary="Remember this model's pipeline",
)
def put_model_pipeline(
    slug: SlugPath, body: PipelineDefaultPatch, store: SettingsStoreDep
) -> PipelineDefault:
    """Needs no Bambuddy: this is ScadBuddy's own preference, stored per slug."""
    settings = store.set_model_pipeline(slug, body.pipeline_id)
    return PipelineDefault(
        slug=slug,
        pipeline_id=settings.model_pipelines.get(slug),
        global_pipeline_id=settings.pipeline_id,
    )


@router.post(
    "/outputs/{output_id}/eligibility",
    response_model=EligibilityOverview,
    summary="Which pipelines would accept this output",
)
async def post_eligibility(
    output_id: OutputIdPath,
    body: EligibilityCheck,
    outputs: OutputsDep,
    store: SettingsStoreDep,
) -> EligibilityOverview:
    """Uploads the 3MF if Bambuddy does not have it yet, then asks each pipeline.

    Bambuddy judges a *library file*, so there is no eligibility answer before an
    upload. The upload happens once per output: an output is immutable, so a recorded
    ``library_file_id`` still describes this 3MF.

    Every report comes back as Bambuddy sent it, including ``printer_reports`` — under
    ``target_kind="printer_class"`` that is where the per-printer reasons are, and the
    top-level ``ok`` means only that *some* printer passes.
    """
    meta = require_output(outputs, output_id)
    settings = store.load()
    async with client_for(settings) as client:
        return await check_pipelines(
            client, outputs, meta, settings, pipeline_ids=body.pipeline_ids
        )


@router.post(
    "/outputs/{output_id}/run",
    response_model=PrintRunResult,
    summary="Run a pipeline for this output",
)
async def post_run(
    output_id: OutputIdPath,
    body: PrintRunRequest,
    outputs: OutputsDep,
    store: SettingsStoreDep,
) -> PrintRunResult:
    """``POST /api/v1/slicer-pipelines/{id}/run`` with ``copies`` and an explicit
    ``force``.

    Without ``pipeline_id`` the model's own default is used, then the global one. A
    blocking eligibility issue is Bambuddy's 409, whose body this passes through as the
    ``bambuddy_body`` problem extension; ``force: true`` runs anyway and Bambuddy records
    ``eligibility_overridden``.

    There is deliberately no printer here. ``PipelineRunRequest`` carries none, so a
    class-targeted pipeline fans out by its own ``fanout_strategy`` and reports the
    printer per copy in ``run.jobs[]``.
    """
    meta = require_output(outputs, output_id)
    settings = store.load()
    async with client_for(settings) as client:
        return await run_for_output(client, outputs, meta, settings, body)


@router.get(
    "/outputs/{output_id}/filaments",
    response_model=FilamentOptions,
    summary="Spools that can print this output, and what the plate needs",
)
async def get_filaments(
    output_id: OutputIdPath,
    outputs: OutputsDep,
    store: SettingsStoreDep,
    printer_id: Annotated[int | None, Query()] = None,
    pipeline_id: Annotated[int | None, Query()] = None,
    plate_id: Annotated[int, Query(ge=1)] = 1,
) -> FilamentOptions:
    """Bambuddy's whole spool inventory, joined to where each spool is loaded (#87).

    One route rather than six calls from the browser, because the join is the part with
    the traps in it: an AMS id is the printer's numbering and not a list index, the flat
    tray id ``ams_mapping`` carries is ``inventory-remain``'s own ``global_tray_id``,
    ``remain: -1`` means unknown and so does ``used_grams: 0``, and the nozzle
    temperature window is on the AMS tray rather than on the spool row.

    ``printer_id`` is what turns "the inventory" into "the inventory, and where it is on
    this printer": without one the spools are still listed, with their last known
    assignment, but nothing that depends on live AMS state is reported. ``pipeline_id``
    is read only for its process preset's *name*, which is the one place a nozzle
    diameter is written down.
    """
    meta = require_output(outputs, output_id)
    settings = store.load()
    async with client_for(settings) as client:
        return await filament_options_for_output(
            client,
            outputs,
            meta,
            settings,
            printer_id=printer_id,
            pipeline_id=pipeline_id,
            plate_id=plate_id,
        )


@router.get(
    "/outputs/{output_id}/progress",
    response_model=PrintProgress | None,
    summary="How the last print of this output is going",
)
async def get_progress(
    output_id: OutputIdPath,
    outputs: OutputsDep,
    store: SettingsStoreDep,
) -> PrintProgress | None:
    """Follow whichever of Bambuddy's two routes this output last took (#89).

    ``null`` means this output has never been printed — that is an answer, not an
    error, and the send bar shows nothing rather than a failure.

    The poll is needed rather than optional on the pipeline route: ``run`` answers 202
    and creates the queue entries in a background task, so ``jobs[].queue_entry_id`` is
    still null when the run response arrives. ``settled`` is what says the polling can
    stop; it is computed from the copies, because a run can report a terminal status
    while a copy is still being dispatched.
    """
    meta = require_output(outputs, output_id)
    async with client_for(store.load()) as client:
        return await progress_for(client, meta)
