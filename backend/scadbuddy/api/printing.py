"""``/api/v1/print/…`` — the print dialog's choices, and running a spool-first print.

Kept out of ``outputs.py`` because these routes are about the print dialog rather
than about an output, and only some of them are output-scoped at all. ``POST
/outputs/{id}/send`` stays in ``outputs.py``: it only uploads (#312).
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import Annotated

from fastapi import APIRouter, Query, Response, status
from pydantic import BaseModel, Field
from temporalio.common import WorkflowIDReusePolicy
from temporalio.service import RPCError

from scadbuddy.api.deps import (
    OutputIdPath,
    OutputsDep,
    PrintCommands,
    PrintLinksDep,
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
from scadbuddy.bambuddy.linking import owned_queue_items
from scadbuddy.bambuddy.models import RackAlgorithm
from scadbuddy.bambuddy.print_run import (
    PrintCheck,
    PrintRunRequest,
    check_for_output,
    chosen_project,
    filament_options_for_output,
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
from scadbuddy.rack.component import RackUsageDep
from scadbuddy.workflows.commands import (
    RETRY_AFTER_SECONDS,
    AlreadyClosedError,
    CommandStillAcceptingError,
    TemporalUnavailableError,
    start_command,
)
from scadbuddy.workflows.print_models import (
    ACCEPTED_UPDATE,
    PRINT_RUN_WORKFLOW,
    AcceptAnswer,
    PrintRunInput,
    SourceSpec,
)

logger = logging.getLogger(__name__)

#: Problem ``type``s for a print the route could not hand to Temporal (#1052).
STILL_ACCEPTING_PROBLEM = "https://scadbuddy.dev/problems/command-still-accepting"
TEMPORAL_UNAVAILABLE_PROBLEM = "https://scadbuddy.dev/problems/temporal-unavailable"

router = APIRouter(prefix="/print", tags=["print"])


class PrinterBedTypePut(BaseModel):
    """The plate to remember on the printer the path names (#83); ``null`` forgets it."""

    bed_type: str | None = Field(default=None, max_length=64)


class PrinterBedType(BaseModel):
    """The plate remembered on one printer (#83)."""

    printer_id: int
    bed_type: str | None = None


class PrinterRackAlgorithmPut(BaseModel):
    """How to pick this printer's rack nozzle (#836); ``null`` forgets it."""

    algorithm: RackAlgorithm | None = None


class PrinterRackAlgorithm(BaseModel):
    """The rack algorithm in force on one printer (#836)."""

    printer_id: int
    algorithm: RackAlgorithm


class ProjectAttach(BaseModel):
    """Which of this output's queue entries to file under the project.

    The ids come from the progress read (#89): a plate's queue item only exists once it
    has sliced, so the caller learns them by polling.
    """

    #: Omitted means the remembered project; an explicit ``null`` is "No project" (#317).
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


@router.put(
    "/printers/{printer_id}/rack-algorithm",
    response_model=PrinterRackAlgorithm,
    summary="Remember how this printer's rack nozzle is picked",
)
def put_printer_rack_algorithm(
    printer_id: int, body: PrinterRackAlgorithmPut, store: SettingsStoreDep
) -> PrinterRackAlgorithm:
    """The print dialog's Advanced rack algorithm (#836, spec §4), per printer. Needs no
    Bambuddy, like the printer's remembered plate."""
    settings = store.set_printer_rack_algorithm(printer_id, body.algorithm)
    return PrinterRackAlgorithm(
        printer_id=printer_id, algorithm=settings.rack_algorithm(printer_id)
    )


@router.get(
    "/printers/{printer_id}/camera",
    response_class=Response,
    responses={200: {"content": {"image/jpeg": {}}, "description": "The current frame"}},
    summary="The printer's current camera frame",
)
async def get_printer_camera(printer_id: int, store: SettingsStoreDep) -> Response:
    """One JPEG from the printer's camera, through Bambuddy (#796): what is on the bed
    now, whoever started the print. Bambuddy's stream token never leaves the server."""
    async with client_for(store.load()) as client:
        frame = await client.camera_snapshot(printer_id)
    return Response(
        frame,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.post(
    "/outputs/{output_id}/run",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=PrintRun,
    responses={
        status.HTTP_200_OK: {
            "model": PrintRun,
            "description": "A repeat of a run in flight, or one that succeeded (or failed "
            "after it tried to queue) within the last ten minutes: that run, and no new print.",
        },
    },
    summary="Slice this output with the dialog's choices and queue it, in the background",
)
async def post_run(
    output_id: OutputIdPath,
    body: PrintRunRequest,
    response: Response,
    outputs: OutputsDep,
    runs: PrintRunsDep,
) -> PrintRun:
    """Derive every slicer preset from the chosen spools, nozzles, quality and plate
    (spec 2026-09-27 §4), slice, then queue on one printer. No pipeline is run.

    Answers **202** with a ``running`` run once the request is accepted, and uploads,
    slices and queues in the background (#470): the slices alone can take minutes,
    longer than the proxies in front wait. Follow ``GET /print/runs/{id}`` (or the
    ``print.run`` event on the ``print:<output id>`` topic, or ``print:library:<file id>``
    for a library file's) to ``succeeded``, whose ``result`` is what this route used to
    answer, or ``failed``, whose ``error`` is the problem it used to answer with.

    Refused before any run starts, with nothing uploaded: an output with no plates, no
    printer, a printer the resolver cannot serve, and choices the resolver refuses —
    mixed nozzle sizes, no printer or process preset (422). A slot with no filament
    preset for the nozzle needs the plate's slots, which only the uploaded file
    answers, so that one is the run's ``failed`` with the same 422 and message.

    The same request for the same output again is the same run: while it is in flight,
    or for ten minutes after it succeeded or failed once it had tried to queue
    (``may_have_queued``: a queue call that timed out, or a later plate that failed
    after an earlier one was queued), this answers **200** with that run and starts
    nothing (``repeated`` is true), so a retry after a proxy timeout cannot queue the
    print twice. "The same request" includes ``request_id``: a client that makes a new
    one per deliberate Print gets a new print each time, and a retry of one press
    (same id) its run.
    """
    meta = require_output(outputs, output_id)
    return await accept_run(
        runs,
        response,
        subject=meta.id,
        slug=meta.slug,
        request=body,
        source=SourceSpec(kind="output", output_id=meta.id),
    )


#: How long a request waits for an execution past its repeat window to close.
CLOSING_WAIT = 5.0


async def accept_run(
    runs: PrintCommands,
    response: Response,
    *,
    subject: str,
    slug: str,
    request: PrintRunRequest,
    source: SourceSpec,
) -> PrintRun:
    """The 202-and-follow model every print run shares (#470, #742), on Temporal
    (#1052, spec 2026-10-01 §5.1).

    ``subject`` is what the run is keyed and recorded under: an output's id, or
    ``library:<file id>``. Our record is read first: a repeat answers 200 with its run
    and touches nothing else. Otherwise ``PrintRun`` is started (or attached to) with
    update-with-start, and its ``accepted`` Update answers with the new row (202), the
    run it repeats (200) or the refusal, raised as the problem it carries.
    """
    key = run_key(subject, request)
    has_request_id = request.request_id is not None
    repeated = await runs.store.find(key, has_request_id=has_request_id)
    if repeated is not None:
        response.status_code = status.HTTP_200_OK
        return repeated.model_copy(update={"repeated": True})
    arg = PrintRunInput(
        subject=subject,
        slug=slug,
        key=key,
        source=source,
        request=request,
        # The store's window, so a repeat the record no longer matches starts anew.
        repeat_window_s=runs.store.repeat_window.total_seconds(),
        search_attributes=runs.search_attributes,
    )
    workflow_id = f"print-{key}"

    async def start() -> AcceptAnswer:
        return await start_command(
            runs.client,
            PRINT_RUN_WORKFLOW,
            arg,
            id=workflow_id,
            task_queue=runs.task_queue,
            update=ACCEPTED_UPDATE,
            result_type=AcceptAnswer,
            reuse=(
                WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY
                if has_request_id
                else WorkflowIDReusePolicy.ALLOW_DUPLICATE
            ),
        )

    try:
        answer = await start()
        if not has_request_id and answer.repeated and answer.run is not None:
            # The record is the truth: the workflow's copy of the row may not have
            # caught up with the run's end yet.
            stored = await runs.store.get(answer.run.id)
            ended = stored is not None and stored.status != "running"
        else:
            ended = False
        if ended:
            # Our record no longer repeats this ended run: its window is over and the
            # execution is closing. Let it close, then this request starts its own.
            with suppress(Exception):
                await asyncio.wait_for(
                    runs.client.get_workflow_handle(workflow_id).result(), CLOSING_WAIT
                )
            answer = await start()
    except AlreadyClosedError:
        # The press's execution closed after recording its run (§4.2): that run.
        closed = await runs.store.find(key, has_request_id=True)
        if closed is None:
            raise ApiError(
                status.HTTP_409_CONFLICT,
                "This print's run has ended and left no record. Print again to retry.",
            ) from None
        response.status_code = status.HTTP_200_OK
        return closed.model_copy(update={"repeated": True})
    except CommandStillAcceptingError:
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "ScadBuddy is still checking this print. Send the same request again to follow it.",
            type_=STILL_ACCEPTING_PROBLEM,
            headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
        ) from None
    except (RPCError, TemporalUnavailableError):
        logger.warning("could not start a print run on Temporal", exc_info=True)
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "ScadBuddy cannot reach Temporal, where print runs run. Nothing was queued; try"
            " again shortly.",
            type_=TEMPORAL_UNAVAILABLE_PROBLEM,
            headers={"Retry-After": "5"},
        ) from None
    if answer.refusal is not None:
        refusal = answer.refusal
        raise ApiError(
            refusal.status,
            refusal.detail,
            title=refusal.title,
            type_=refusal.type,
            **refusal.extensions,
        )
    assert answer.run is not None  # the Update answers one or the other
    if answer.repeated:
        response.status_code = status.HTTP_200_OK
    return answer.run.model_copy(update={"repeated": answer.repeated})


@router.get(
    "/runs/{run_id}",
    response_model=PrintRun,
    summary="How a print run is going",
)
async def get_run(run_id: RunIdPath, runs: PrintRunsDep) -> PrintRun:
    """A run ``POST /print/outputs/{id}/run`` or ``/print/library/{file_id}/run`` accepted,
    from any replica (#470, #742).

    ``running`` until it ends as ``succeeded`` (with ``result``) or ``failed`` (with
    ``error``). A ``failed`` run with ``may_have_queued`` had tried to queue the print,
    so it may be on Bambuddy's queue anyway. A run whose process went away reads as
    ``failed``, and its message says whether it could have queued.
    Once ``succeeded``, an output's print is followed by ``/outputs/{id}/progress``; a
    library file's has no ScadBuddy progress, and its result links to Bambuddy's queue.
    """
    run = await runs.store.get(run_id)
    if run is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"there is no print run {run_id}")
    return run


@router.post(
    "/outputs/{output_id}/check",
    response_model=PrintCheck,
    summary="What the run would refuse for the dialog's choices, before Print",
)
async def post_check(
    output_id: OutputIdPath,
    body: PrintRunRequest,
    outputs: OutputsDep,
    uploads: UploadsDep,
    store: SettingsStoreDep,
    rack: RackUsageDep,
) -> PrintCheck:
    """The run's own pre-upload refusals for the body the run would take (#755, #760), so
    the dialog can say before Print what the run would refuse. ``errors`` is exactly the
    run's 422 for the plates, printer, choices or nozzles; ``warnings`` the nozzle
    advisories it would carry back.

    Nothing is uploaded, sliced or queued; the used slots are read from the local 3MF.
    """
    meta = require_output(outputs, output_id)
    settings = store.load()
    async with client_for(settings) as client:
        return await check_for_output(client, outputs, uploads, meta, settings, body, rack=rack)


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
    rack: RackUsageDep,
    printer_id: Annotated[int | None, Query()] = None,
) -> ChoicesView:
    """Printers, installed nozzles, quality tiers and processes, plates with the last one
    used, and the filament step — one read for the whole dialog (spec §3)."""
    meta = require_output(outputs, output_id)
    settings = store.load()
    async with client_for(settings) as client:
        return await choices_for_output(
            client, outputs, uploads, meta, settings, printer_id=printer_id, rack=rack
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
    links: PrintLinksDep,
    store: SettingsStoreDep,
    observer: PrintProgressDep,
    watcher: PrintWatcherDep,
) -> PrintProgress | None:
    """Follow this output's last print, slice then queue (#89).

    ``null`` means this output has never been printed — that is an answer, not an
    error, and the send bar shows nothing rather than a failure.

    ``settled`` is what says the polling can stop.
    """
    meta = require_output(outputs, output_id)
    async with client_for(store.load()) as client:
        progress = await progress_for(
            client, meta, uploads=uploads, links=links if links.available else None
        )
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


class LastProject(BaseModel):
    """The project the pickers open on (#317); ``null`` is "No project"."""

    project_id: int | None = None


@router.put("/projects/last", response_model=LastProject, summary="Remember the chosen project")
def put_last_project(body: LastProject, store: SettingsStoreDep) -> LastProject:
    """The project chosen on the Customize page or in the print dialog, which both open
    on next (``last_project_id``). ScadBuddy's own preference: it needs no Bambuddy."""
    settings = store.remember_project(body.project_id)
    return LastProject(project_id=settings.last_project_id)


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
    links: PrintLinksDep,
    store: SettingsStoreDep,
) -> AttachResult:
    """``add-queue`` now, and ``add-archives`` for whatever the entries have produced.

    Separate from the run because neither id exists when a print starts: a plate's
    queue item only exists once it has sliced, and an archive only exists once a print
    has finished. Calling this again later is how the archives eventually land on the
    project's page, and attaching the same id twice is Bambuddy's to dedupe.
    """
    meta = require_output(outputs, output_id)
    settings = store.load()
    project_id = chosen_project(body, settings)
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
        # The body's ids are filed under the project as asked, but only the output's own
        # items are linked to it: a caller-named item would open its archive's media.
        linkable = await owned_queue_items(client, meta, links, ids) if links.available else set()
        return await attach_results(
            client,
            project_id,
            queue_item_ids=ids,
            output_id=meta.id,
            links=links if links.available else None,
            linkable=linkable,
        )
