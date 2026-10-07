"""``/api/v1/print/…`` — the print dialog's choices, and running a spool-first print.

Kept out of ``outputs.py`` because these routes are about the print dialog rather
than about an output, and only some of them are output-scoped at all. ``POST
/outputs/{id}/send`` stays in ``outputs.py``: it only uploads (#312).
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from datetime import timedelta
from typing import Annotated, Any

import psycopg
from fastapi import APIRouter, Query, Response, status
from fastapi.responses import JSONResponse
from psycopg_pool import PoolTimeout
from pydantic import BaseModel, Field
from temporalio.common import WorkflowIDReusePolicy

from scadbuddy.api.deps import (
    OutputIdPath,
    OutputsDep,
    PrintCommands,
    PrintLinksDep,
    PrintProgressDep,
    PrintRunsDep,
    RunIdPath,
    SettingsStoreDep,
    SlugPath,
    UploadsDep,
)
from scadbuddy.api.operations import (
    OPERATION_RESPONSES,
    STILL_ACCEPTING_PROBLEM,
    IdempotencyKey,
    operation_answer,
    run_operation,
    temporal_problems,
    temporal_refused,
    temporal_unavailable,
)
from scadbuddy.bambuddy.choices import ChoicesView, choices_for_output
from scadbuddy.bambuddy.client import client_for
from scadbuddy.bambuddy.filaments import FilamentOptions
from scadbuddy.bambuddy.models import RackAlgorithm
from scadbuddy.bambuddy.print_run import (
    PrintCheck,
    PrintRunRequest,
    check_for_output,
    filament_options_for_output,
)
from scadbuddy.bambuddy.progress import PrintProgress, progress_for
from scadbuddy.bambuddy.projects import (
    AttachResult,
    ProjectAttach,
    ProjectChoices,
    ProjectRequest,
    ProjectView,
    describe_projects,
)
from scadbuddy.bambuddy.runs import UNEXPECTED_DETAIL, PrintRun, run_key
from scadbuddy.core.problems import DATABASE_ERRORS, DATABASE_UNAVAILABLE_PROBLEM, ApiError
from scadbuddy.library.outputs import require_output
from scadbuddy.library.settings_store import ModelPrintChoices
from scadbuddy.operations.component import OperationsDep
from scadbuddy.rack.component import RackUsageDep
from scadbuddy.workflows.commands import (
    COMMAND_ANSWER_DEADLINE,
    CONNECT_MARGIN_SECONDS,
    DESCRIBE_SECONDS,
    RETRY_AFTER_SECONDS,
    AlreadyClosedError,
    CommandClosedError,
    CommandStillAcceptingError,
    TemporalBusyError,
    TemporalRefusedError,
    TemporalUnavailableError,
    TemporalUnreachableError,
    start_command,
)
from scadbuddy.workflows.component import FollowsDep
from scadbuddy.workflows.print_models import (
    ACCEPTED_UPDATE,
    PRINT_RUN_WORKFLOW,
    AcceptAnswer,
    PrintRunInput,
    SourceSpec,
)

logger = logging.getLogger(__name__)

#: ``TemporalRefusedError``. Not "nothing was queued": some refusals come after the start
#: was persisted, and the same request sent again follows it (review #1316 4).
TEMPORAL_REFUSED_DETAIL = (
    "Temporal refused to start this print; see ScadBuddy's logs. Send the same request"
    " again to follow it if it started."
)
#: ``TemporalUnreachableError``: the first connect failed, so no request was written.
TEMPORAL_UNREACHABLE_DETAIL = (
    "ScadBuddy cannot reach Temporal, where print runs run. Nothing was queued; try again shortly."
)
#: ``TemporalUnavailableError``: no answer within the bound, or ``UNAVAILABLE``. Either
#: may follow a persisted start (review #1316 (9) 1a).
TEMPORAL_DOWN_DETAIL = (
    "ScadBuddy cannot reach Temporal, where print runs run. Send the same request again"
    " shortly to follow it if it started."
)
#: ``TemporalBusyError``: Temporal answered, or gRPC ended the call, so it is not "cannot
#: reach" (review #1316 (9) 1b).
TEMPORAL_BUSY_DETAIL = (
    "Temporal could not start this print right now. Send the same request again shortly"
    " to follow it if it started."
)
STILL_CHECKING_DETAIL = (
    "ScadBuddy is still checking this print. Send the same request again to follow it."
)

#: What a print run's routes answer beside the 200 and 202, built from the details the
#: route sends (review #1316 (10) 3, (11) 1, (12) 2).
PRINT_RUN_PROBLEMS: dict[int | str, dict[str, Any]] = temporal_problems(
    refused=TEMPORAL_REFUSED_DETAIL,
    unreachable=TEMPORAL_UNREACHABLE_DETAIL,
    down=TEMPORAL_DOWN_DETAIL,
    busy=TEMPORAL_BUSY_DETAIL,
    still_checking=STILL_CHECKING_DETAIL,
    unexpected=UNEXPECTED_DETAIL,
    other="Any other problem. The check's refusal passes through with its own status and"
    " type (409, 422, or Bambuddy's own, such as 404 for a library file it no longer has,"
    " 502 or 504); the route's own are 404 for an unknown output and 409 for a run whose"
    " record expired.",
)

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
    responses={
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": (
                "The database did not answer within the save's bound, so it was probably not saved "
                "(#1129). Whether resending is safe is #1216."
            )
        }
    },
)
def put_printer_rack_algorithm(
    printer_id: int, body: PrinterRackAlgorithmPut, store: SettingsStoreDep
) -> PrinterRackAlgorithm:
    """The print dialog's Advanced rack algorithm (#836, spec §4), per printer. Needs no
    Bambuddy, like the printer's remembered plate."""
    try:
        algorithm = store.set_printer_rack_algorithm(printer_id, body.algorithm)
    except DATABASE_ERRORS as error:
        # The store gives up on purpose rather than commit after the dialog has (#1129).
        # Only a pool wait or a cancelled statement is known to have saved nothing; any
        # other lost connection may have dropped after the commit.
        logger.warning(
            "rack-algorithm save gave up on the database",
            extra={"printer_id": printer_id, "error": type(error).__name__},
        )
        rolled_back = isinstance(error, PoolTimeout | psycopg.errors.QueryCanceled)
        outcome = (
            "nothing was saved"
            if rolled_back
            else "could not confirm the save; check the setting before resending"
        )
        # Say which failure it was (#1283): a full pool, a statement cut off, or a
        # connection that failed, rather than calling each one a timeout.
        if isinstance(error, PoolTimeout):
            cause = "no database connection came free in time"
        elif isinstance(error, psycopg.errors.QueryCanceled):
            cause = "the database did not answer in time"
        else:
            cause = "the connection to the database failed"
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"{outcome}: {cause} ({type(error).__name__})",
            type_=DATABASE_UNAVAILABLE_PROBLEM,
        ) from None
    return PrinterRackAlgorithm(printer_id=printer_id, algorithm=algorithm)


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
        **PRINT_RUN_PROBLEMS,
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
    (same id) its run, for as long as the run's row is kept
    (``print_run_retention_seconds``).
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
#: All of one accept: a start's deadline, its connect margin and the describe that
#: tells a late Update from an unreachable Temporal. Under Envoy's 15 s, so a repeat
#: that must start again never holds the request past it (review #1061).
ACCEPT_BUDGET = COMMAND_ANSWER_DEADLINE.total_seconds() + CONNECT_MARGIN_SECONDS + DESCRIBE_SECONDS
#: The least deadline a second start is given; below it the request is still accepting.
MIN_RESTART_DEADLINE = 1.0


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

    began = time.monotonic()

    async def start(deadline: timedelta = COMMAND_ANSWER_DEADLINE) -> AcceptAnswer:
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
            deadline=deadline,
        )

    async def ended(answer: AcceptAnswer) -> bool:
        """Whether ``answer`` repeats a run our record no longer repeats: it ended (the
        record is the truth: the workflow's copy of the row may not have caught up
        with the run's end yet) and `runs.store.find` did not answer it."""
        if has_request_id or not answer.repeated or answer.run is None:
            return False
        stored = await runs.store.get(answer.run.id)
        return stored is not None and stored.status != "running"

    try:
        answer = await start()
        if await ended(answer):
            # Our record no longer repeats this ended run: its window is over and the
            # execution is closing. Let it close, then this request starts its own.
            margin = CONNECT_MARGIN_SECONDS + DESCRIBE_SECONDS
            left = ACCEPT_BUDGET - (time.monotonic() - began) - margin
            with suppress(Exception):
                await asyncio.wait_for(
                    runs.client.get_workflow_handle(workflow_id).result(),
                    max(0.0, min(CLOSING_WAIT, left - MIN_RESTART_DEADLINE)),
                )
            left = ACCEPT_BUDGET - (time.monotonic() - began) - margin
            if left < MIN_RESTART_DEADLINE:
                # The client sends it again, and that request starts the new run.
                raise CommandStillAcceptingError(workflow_id)
            answer = await start(timedelta(seconds=left))
            if await ended(answer):
                # Still open past the wait (a loaded worker had not closed it): the
                # start attached to it again. Its old run is no answer to this request;
                # the client sends it again, and that request starts the new run.
                raise CommandStillAcceptingError(workflow_id)
    except AlreadyClosedError:
        # The press's execution closed after recording its run (§4.2): that run. With
        # no row, retention pruned it: the run may well have printed (review #1061 2a).
        closed = await runs.store.find(key, has_request_id=True)
        if closed is None:
            raise ApiError(
                status.HTTP_409_CONFLICT,
                "This print ran before, and its record has expired, so ScadBuddy cannot"
                " tell whether it was queued. Check Bambuddy's queue before printing again.",
            ) from None
        response.status_code = status.HTTP_200_OK
        return closed.model_copy(update={"repeated": True})
    except (CommandStillAcceptingError, CommandClosedError):
        # A closed command recorded nothing, and its execution ended unsuccessfully, so
        # the same request sent again starts a new one.
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            STILL_CHECKING_DETAIL,
            type_=STILL_ACCEPTING_PROBLEM,
            headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
        ) from None
    except TemporalRefusedError:
        # A misconfiguration, not a blip (review #1061 (3) 3).
        raise temporal_refused(
            "to start a print run", TEMPORAL_REFUSED_DETAIL, may_have_started=True
        ) from None
    except TemporalUnavailableError as error:
        if isinstance(error, TemporalUnreachableError):
            detail = TEMPORAL_UNREACHABLE_DETAIL
        elif isinstance(error, TemporalBusyError):
            detail = TEMPORAL_BUSY_DETAIL
        else:
            detail = TEMPORAL_DOWN_DETAIL
        raise temporal_unavailable(
            "a print run",
            detail,
            may_have_started=not isinstance(error, TemporalUnreachableError),
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
    follows: FollowsDep,
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
    # (#268, #1053). Its follow may have given up on a quiet print, or been sent before
    # there was one. In the background: a Temporal that does not answer never holds this
    # read up. It needs only the client and queue, never the print runs' store.
    if progress is not None and not progress.settled:
        follows.ensure(meta.id)
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


@router.post(
    "/projects",
    response_model=ProjectView,
    summary="Create or link a project",
    responses=OPERATION_RESPONSES,
)
async def post_project(
    body: ProjectRequest,
    response: Response,
    ops: OperationsDep,
    idempotency_key: IdempotencyKey = None,
) -> ProjectView | JSONResponse:
    """``POST /api/v1/projects/`` and ``POST /api/v1/library/folders/`` with
    ``project_id``, which is the pairing Bambuddy's own UI makes.

    With ``project_id`` an existing project is linked instead of created, and its folder
    is left alone if it already has one — linking twice must not leave Bambuddy with two
    folders of the same name.
    """
    result = await run_operation(
        ops,
        response,
        kind=ops.kinds["create_project"],
        subject="project",
        request=body,
        idempotency_key=idempotency_key,
    )
    return operation_answer(result, ProjectView)


@router.post(
    "/outputs/{output_id}/project",
    response_model=AttachResult,
    summary="File this output's queue entries under its project",
    responses=OPERATION_RESPONSES,
)
async def post_attach_project(
    output_id: OutputIdPath,
    body: ProjectAttach,
    response: Response,
    outputs: OutputsDep,
    ops: OperationsDep,
    idempotency_key: IdempotencyKey = None,
) -> AttachResult | JSONResponse:
    """``add-queue`` now, and ``add-archives`` for whatever the entries have produced.

    Separate from the run because neither id exists when a print starts: a plate's
    queue item only exists once it has sliced, and an archive only exists once a print
    has finished. Calling this again later is how the archives eventually land on the
    project's page, and attaching the same id twice is Bambuddy's to dedupe.
    """
    require_output(outputs, output_id)
    result = await run_operation(
        ops,
        response,
        kind=ops.kinds["attach_project"],
        subject=output_id,
        # Unset fields stay unset: an omitted project_id is the remembered one (#317).
        request={"output_id": output_id, "body": body.model_dump(mode="json", exclude_unset=True)},
        idempotency_key=idempotency_key,
    )
    return operation_answer(result, AttachResult)
