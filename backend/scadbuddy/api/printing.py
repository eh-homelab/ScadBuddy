"""``/api/v1/print/…`` — the print dialog's choices, and running a spool-first print.

Kept out of ``outputs.py`` because these routes are about the print dialog rather
than about an output, and only some of them are output-scoped at all. ``POST
/outputs/{id}/send`` stays where it was: it is the send bar's one-click path and
still runs a Bambuddy slicer pipeline, unrelated to the dialog's own run.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Response, status
from pydantic import BaseModel, Field

from scadbuddy.api.deps import (
    OutputIdPath,
    OutputsDep,
    PrintProgressDep,
    PrintRunsDep,
    PrintWatcherDep,
    RunIdPath,
    SettingsStoreDep,
    SlugPath,
    UploadsDep,
)
from scadbuddy.api.outputs import require_output
from scadbuddy.bambuddy.choices import ChoicesView, choices_for_output
from scadbuddy.bambuddy.client import client_for
from scadbuddy.bambuddy.filaments import FilamentOptions
from scadbuddy.bambuddy.pipelines import (
    PrintRunRequest,
    PrintRunResult,
    execute_run,
    filament_options_for_output,
    prepare_run,
)
from scadbuddy.bambuddy.progress import PrintProgress, progress_for
from scadbuddy.bambuddy.projects import (
    AttachResult,
    ProjectChoices,
    ProjectRequest,
    ProjectView,
    attach_results,
    describe_projects,
    ensure_project,
)
from scadbuddy.bambuddy.runs import PrintRun, run_key
from scadbuddy.core.problems import ApiError
from scadbuddy.library.settings_store import ModelPrintChoices

router = APIRouter(prefix="/print", tags=["print"])


class PrinterBedTypePut(BaseModel):
    """The plate to remember on the printer the path names (#83); ``null`` forgets it."""

    bed_type: str | None = Field(default=None, max_length=64)


class PrinterBedType(BaseModel):
    """The plate remembered on one printer (#83)."""

    printer_id: int
    bed_type: str | None = None


class ProjectAttach(BaseModel):
    """Which of this output's queue entries to file under the project.

    The ids come from the progress read (#89): a pipeline run's
    ``jobs[].queue_entry_id`` is null when the run answers 202, so the caller is the
    only one that knows them, and only once it has polled.
    """

    project_id: int | None = None
    queue_item_ids: list[int] = Field(default_factory=list)


@router.put(
    "/models/{slug}/choices",
    response_model=ModelPrintChoices,
    summary="Remember this model's printer and spools",
)
def put_model_choices(
    slug: SlugPath, body: ModelPrintChoices, store: SettingsStoreDep
) -> ModelPrintChoices:
    """The printer and spools the picker last chose for this model (#78).

    Replaces this model's entry whole; an empty body forgets it, so the picker opens on
    the auto-match again. Needs no Bambuddy: this is ScadBuddy's own preference, stored
    per slug.
    """
    settings = store.set_model_choices(slug, body)
    return settings.model_print_choices.get(slug, ModelPrintChoices())


@router.put(
    "/printers/{printer_id}/bed-type",
    response_model=PrinterBedType,
    summary="Remember the plate on this printer",
)
def put_printer_bed_type(
    printer_id: int, body: PrinterBedTypePut, store: SettingsStoreDep
) -> PrinterBedType:
    """What the picker last printed on this printer with (#83), which it opens on next.

    ScadBuddy's own memory, because Bambuddy's printer status reports no plate type.
    Needs no Bambuddy, like the model's other remembered choices.
    """
    settings = store.set_printer_bed_type(printer_id, body.bed_type)
    return PrinterBedType(
        printer_id=printer_id, bed_type=settings.printer_bed_types.get(str(printer_id))
    )


@router.post(
    "/outputs/{output_id}/run",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=PrintRun,
    responses={
        status.HTTP_200_OK: {
            "model": PrintRun,
            "description": "A repeat of a run in flight, or one that succeeded within "
            "the last ten minutes: that run, and no new print.",
        },
    },
    summary="Slice this output with the dialog's choices and queue it, in the background",
)
async def post_run(
    output_id: OutputIdPath,
    body: PrintRunRequest,
    response: Response,
    outputs: OutputsDep,
    uploads: UploadsDep,
    store: SettingsStoreDep,
    observer: PrintProgressDep,
    watcher: PrintWatcherDep,
    runs: PrintRunsDep,
) -> PrintRun:
    """Derive every slicer preset from the chosen spools, nozzles, quality and plate
    (spec 2026-09-27 §4), slice, then queue on one printer. No pipeline is run.

    Answers **202** with a ``running`` run once the request is accepted, and uploads,
    slices and queues in the background (#470): the slices alone can take minutes,
    longer than the proxies in front wait. Follow ``GET /print/runs/{id}`` (or the
    ``print.run`` event on the ``print:<output id>`` topic) to ``succeeded``, whose
    ``result`` is what this route used to answer, or ``failed``, whose ``error`` is
    the problem it used to answer with.

    Refused before any run starts, with nothing uploaded: an output with no plates, no
    printer, a printer the resolver cannot serve, and choices the resolver refuses —
    mixed nozzle sizes, no printer or process preset (422). A slot with no filament
    preset for the nozzle needs the plate's slots, which only the uploaded file
    answers, so that one is the run's ``failed`` with the same 422 and message.

    The same request for the same output again is the same run: while it is in flight,
    or for ten minutes after it succeeded, this answers **200** with that run and
    starts nothing, so a retry after a proxy timeout cannot queue the print twice.
    """
    meta = require_output(outputs, output_id)
    key = run_key(meta.id, body)
    repeated = await runs.store.find(key)
    if repeated is not None:
        response.status_code = status.HTTP_200_OK
        return repeated
    settings = store.load()
    async with client_for(settings) as client:
        prepared = await prepare_run(client, outputs, meta, settings, body)
    run, created = await runs.store.claim(meta.id, key)
    if not created:
        # Another request for the same print claimed it while this one was checking.
        response.status_code = status.HTTP_200_OK
        return run

    async def work() -> PrintRunResult:
        async with client_for(settings) as client:
            result = await execute_run(client, outputs, uploads, meta, settings, body, prepared)
        observer.started(meta)
        await watcher.started(meta.id)
        return result

    runs.start(run, meta.slug, work)
    runs.announce(run, meta.slug)
    return run


@router.get(
    "/runs/{run_id}",
    response_model=PrintRun,
    summary="How a print run is going",
)
async def get_run(run_id: RunIdPath, runs: PrintRunsDep) -> PrintRun:
    """A run ``POST /print/outputs/{id}/run`` accepted, from any replica (#470).

    ``running`` until it ends as ``succeeded`` (with ``result``) or ``failed`` (with
    ``error``). A run whose process went away before it ended reads as ``failed``: it
    may have queued the print, and its message says to check Bambuddy's queue.
    Once ``succeeded``, the print itself is followed by ``/outputs/{id}/progress``.
    """
    run = await runs.store.get(run_id)
    if run is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"there is no print run {run_id}")
    return run


@router.get(
    "/outputs/{output_id}/filaments",
    response_model=FilamentOptions,
    summary="Spools that can print this output, and what the plate needs",
)
async def get_filaments(
    output_id: OutputIdPath,
    outputs: OutputsDep,
    uploads: UploadsDep,
    store: SettingsStoreDep,
    printer_id: Annotated[int | None, Query()] = None,
    plate_id: Annotated[int, Query(ge=1)] = 1,
    all_plates: Annotated[bool, Query()] = False,
) -> FilamentOptions:
    """Bambuddy's whole spool inventory, joined to where each spool is loaded (#87).

    One route rather than three calls from the browser, because the join is the part
    with the traps in it: ``/inventory/assignments`` covers every printer while
    ``inventory-remain`` covers one, ``remain: -1`` means unknown and so does
    ``used_grams: 0``.

    ``printer_id`` is what turns "the inventory" into "the inventory, and where it is on
    this printer": without one the spools are still listed, with their last known
    assignment, but the reconciled remaining weights are not. It is also what reads the
    mounted nozzles (#78).

    ``all_plates`` answers for an all-plates print: one row per slot any plate uses, in
    place of ``plate_id``'s, so a slot only a later plate uses still gets a spool.
    """
    meta = require_output(outputs, output_id)
    settings = store.load()
    async with client_for(settings) as client:
        return await filament_options_for_output(
            client,
            outputs,
            uploads,
            meta,
            settings,
            printer_id=printer_id,
            plate_id=plate_id,
            all_plates=all_plates,
        )


@router.get(
    "/outputs/{output_id}/choices",
    response_model=ChoicesView,
    summary="What the print dialog offers for this output",
)
async def get_choices(
    output_id: OutputIdPath,
    outputs: OutputsDep,
    uploads: UploadsDep,
    store: SettingsStoreDep,
    printer_id: Annotated[int | None, Query()] = None,
) -> ChoicesView:
    """Printers, installed nozzles, quality tiers and processes, plates with the last one
    used, and the filament step — one read for the whole dialog (spec §3)."""
    meta = require_output(outputs, output_id)
    settings = store.load()
    async with client_for(settings) as client:
        return await choices_for_output(
            client, outputs, uploads, meta, settings, printer_id=printer_id
        )


@router.get(
    "/outputs/{output_id}/progress",
    response_model=PrintProgress | None,
    summary="How the last print of this output is going",
)
async def get_progress(
    output_id: OutputIdPath,
    outputs: OutputsDep,
    uploads: UploadsDep,
    store: SettingsStoreDep,
    observer: PrintProgressDep,
    watcher: PrintWatcherDep,
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
        progress = await progress_for(client, meta, uploads=uploads)
    observer.observe(meta, progress)
    # Someone is looking at a print that is still moving: make sure it is followed
    # (#268). The watcher may not be, after a restart without a database, for a print
    # sent before the watcher existed, or once it gave up on a quiet print.
    if progress is not None and not progress.settled:
        watcher.watch(meta.id)
    return progress


@router.get("/projects", response_model=ProjectChoices, summary="Bambuddy's projects")
async def get_projects(store: SettingsStoreDep) -> ProjectChoices:
    """Every Bambuddy project, with the library folder that belongs to it (#79).

    Also the project the last send went to, so the picker opens where it was left.
    ScadBuddy models no relationship between a model and a project: which prints
    belong to a project is on the project's own page, and keeping a second answer
    here would be a copy that goes stale.
    """
    settings = store.load()
    async with client_for(settings) as client:
        return await describe_projects(client, last_project_id=settings.last_project_id)


@router.post("/projects", response_model=ProjectView, summary="Create or link a project")
async def post_project(body: ProjectRequest, store: SettingsStoreDep) -> ProjectView:
    """``POST /api/v1/projects/`` and ``POST /api/v1/library/folders/`` with
    ``project_id``, which is the pairing Bambuddy's own UI makes.

    With ``project_id`` an existing project is linked instead of created, and its folder
    is left alone if it already has one — linking twice must not leave Bambuddy with two
    folders of the same name.
    """
    async with client_for(store.load()) as client:
        return await ensure_project(client, body)


@router.post(
    "/outputs/{output_id}/project",
    response_model=AttachResult,
    summary="File this output's queue entries under its project",
)
async def post_attach_project(
    output_id: OutputIdPath,
    body: ProjectAttach,
    outputs: OutputsDep,
    store: SettingsStoreDep,
) -> AttachResult:
    """``add-queue`` now, and ``add-archives`` for whatever the entries have produced.

    Separate from the run because neither id exists when a print starts: a pipeline
    run's queue entries are created by a background task, and an archive only exists
    once a print has finished. Calling this again later is how the archives eventually
    land on the project's page, and attaching the same id twice is Bambuddy's to dedupe.
    """
    meta = require_output(outputs, output_id)
    settings = store.load()
    project_id = body.project_id or settings.last_project_id
    if project_id is None:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            "this output has no project, so there is nothing to file it under",
        )
    ids = body.queue_item_ids or (
        [plate.queue_item_id for plate in meta.plates]
        or ([meta.queue_item_id] if meta.queue_item_id else [])
    )
    async with client_for(settings) as client:
        return await attach_results(client, project_id, queue_item_ids=ids)
