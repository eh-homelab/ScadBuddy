from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request, Response, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from temporalio.client import WorkflowUpdateFailedError

from scadbuddy.api.deps import (
    JOB_ID_PATTERN,
    AssetsDep,
    CatalogueDep,
    ConfigDep,
    FetcherDep,
    FontsDep,
    HistoryDep,
    JobIdPath,
    PathsDep,
    RenderDep,
    SlugPath,
    StateDep,
)
from scadbuddy.api.models import require_model, require_model_exists
from scadbuddy.api.operations import (
    TEMPORAL_UNAVAILABLE_PROBLEM,
    IdempotencyKey,
    still_accepting,
)
from scadbuddy.api.params import require_installed_fonts, require_valid_params, schema_of
from scadbuddy.api.versions import require_history
from scadbuddy.core.config import Config
from scadbuddy.core.paths import MODEL_META_NAME
from scadbuddy.core.problems import ApiError
from scadbuddy.library.assets import file_assets
from scadbuddy.library.catalogue import meta_from_raw
from scadbuddy.library.history import (
    COMMIT_ID_PATTERN,
    GitError,
    RevisionNotFoundError,
)
from scadbuddy.render.diagnostics import Diagnostic
from scadbuddy.render.glb import BoundingBox, read_glb
from scadbuddy.render.inputs import InputsError, legacy_inputs, normalize_inputs
from scadbuddy.render.job_models import (
    BomEntry,
    Job,
    JobNotFoundError,
    JobState,
    PartInfo,
    PlateInfo,
    QueueFullError,
)
from scadbuddy.render.jobs import SnapshotPendingError, SnapshotUnavailableError
from scadbuddy.render.schema import ParamValue
from scadbuddy.render.submit import RenderService
from scadbuddy.render.thumbnail import (
    BREAKDOWN_TILE_SIZE,
    MAX_BREAKDOWN_TILE_SIZE,
    MAX_VIEW_SIZE,
    MIN_VIEW_SIZE,
    PLATE_PNG_SIZE,
    ColourBreakdown,
    ViewName,
    render_colour_breakdown,
    render_view,
)
from scadbuddy.store.cache import materialize_result
from scadbuddy.store.content import StoreFullError
from scadbuddy.workflows.commands import (
    CommandClosedError,
    CommandStillAcceptingError,
    TemporalBusyError,
    TemporalRefusedError,
    TemporalUnavailableError,
)
from scadbuddy.workflows.models import MigrateResult

logger = logging.getLogger(__name__)

router = APIRouter(tags=["jobs"])

#: A render whose `accepted` Update failed, or whose start Temporal refused, for a
#: reason a re-send would not change.
RENDER_UNSTARTABLE_PROBLEM = "https://scadbuddy.dev/problems/render-unstartable"

GLB_MEDIA_TYPE = "model/gltf-binary"
PNG_MEDIA_TYPE = "image/png"

ViewSize = Annotated[
    int,
    Query(
        ge=MIN_VIEW_SIZE,
        le=MAX_VIEW_SIZE,
        description="Edge of the square PNG, in pixels",
    ),
]


class RenderRequest(BaseModel):
    #: Template inputs (spec 2026-09-27 §4.3). Their `params` are what is rendered.
    inputs: dict[str, Any] | None = None
    #: The body before inputs: still accepted, and read as `{"params": …, "v": 0}`.
    params: dict[str, ParamValue] | None = None
    # #90's "Customize this version": render an old revision without restoring it.
    # Omitted means the revision the model is currently at.
    version: str | None = Field(default=None, pattern=COMMIT_ID_PATTERN)
    # The job this render replaces -- the preview's previous submit. Dropped unrendered
    # if no worker has taken it yet, so a slider drag does not queue every stop on
    # the way. Harmless when it has already started or finished. Needs an
    # `Idempotency-Key`: a re-send without one would release the job again (#1053).
    supersedes: str | None = Field(
        default=None,
        pattern=JOB_ID_PATTERN,
        description="The job this render replaces; needs an `Idempotency-Key` header (422 without)",
    )


class RenderAccepted(BaseModel):
    job_id: str
    status_url: str
    #: The caller's own inputs, normalised. A submit that joined a waiting job (the
    #: same `params`) shares that job, whose status keeps its creator's inputs.
    inputs: dict[str, Any] = Field(default_factory=dict)


class JobOutputSummary(BaseModel):
    """One `ctx.output` of a pipeline job (spec 2026-09-27 §5.2): what Generate saves by
    its ``index``."""

    index: int
    name: str | None
    bom: list[BomEntry] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)


class JobStatus(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    id: str
    slug: str
    status: JobState
    model_version: str | None = None
    params: dict[str, ParamValue] = Field(default_factory=dict)
    #: The inputs of the submission that created the job (spec §4.3); a caller whose
    #: submit coalesced keeps the inputs its own response returned. `{"params": …}` for
    #: a row written before inputs existed.
    inputs: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
    log_tail: list[str] = Field(default_factory=list)
    preview_url: str | None = None
    bbox_mm: BoundingBox | None = None
    colors: list[str] | None = None
    #: ScadBuddy's own warnings: a done job's result's, or what a failed one could
    #: still say (#408), say a file parameter's asset OpenSCAD could not open.
    warnings: list[str] | None = None
    #: What the template echoed as `NOTE:`/`WARNING:` on a successful render (#285).
    notes: list[str] | None = None
    parts: list[PartInfo] | None = None
    #: Every plate of a multi-plate render (spec §6.4); empty for a one-plate one.
    plates: list[PlateInfo] | None = None
    #: OpenSCAD's ERROR/WARNING lines, parsed, on a failed job as well as a done one.
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    #: How many more OpenSCAD printed past the cap; 0 when ``diagnostics`` is all.
    #: A factory default, as the lists have, so the generated client reads it as
    #: optional: an older mock or cached response without it still type-checks.
    diagnostics_dropped: int = Field(default_factory=int)
    #: A pipeline job's outputs, in order; empty for a job that wrote none.
    outputs: list[JobOutputSummary] = Field(default_factory=list)


class ModelDiagnostics(BaseModel):
    """What the latest settled render of a model reported (#252)."""

    model_config = ConfigDict(protected_namespaces=())

    job_id: str
    status: JobState
    model_version: str | None = None
    finished_at: datetime | None = None
    error: str | None = None
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    #: How many more OpenSCAD printed past the cap; 0 when ``diagnostics`` is all.
    diagnostics_dropped: int = Field(default_factory=int)


def _job_status(job: Job, preview_url: str | None) -> JobStatus:
    result = job.result
    return JobStatus(
        id=job.id,
        slug=job.slug,
        status=job.state,
        model_version=job.model_version,
        params=job.params,
        inputs=job.inputs or legacy_inputs(job.params),
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        error=job.error,
        log_tail=job.log_tail,
        preview_url=preview_url if result is not None else None,
        bbox_mm=result.bbox_mm if result else None,
        colors=result.colors if result else None,
        warnings=result.warnings if result else job.warnings or None,
        notes=result.notes if result else None,
        parts=result.parts if result else None,
        plates=result.plates if result else None,
        diagnostics=job.diagnostics,
        diagnostics_dropped=job.diagnostics_dropped,
        outputs=[
            JobOutputSummary(index=i, name=o.name, bom=o.bom, files=o.files)
            for i, o in enumerate(job.outputs)
        ],
    )


def _declares_pipeline(directory: Path, slug: str) -> bool:
    """Whether the template's model.json at this revision declares a pipeline (§5.1),
    read as the catalogue reads it. A malformed declaration counts: the job then reaches
    the worker, whose `load_pipeline` names what is wrong with it. An unreadable
    model.json declares none, and the job renders `model.scad`'s parameters."""
    try:
        raw = json.loads((directory / MODEL_META_NAME).read_text(encoding="utf-8"))
        meta = meta_from_raw(raw if isinstance(raw, dict) else {}, slug)
    except (OSError, ValueError, ValidationError):
        return False
    return meta.pipeline is not None or meta.pipeline_raw is not None


async def _resolve_version(history: HistoryDep, slug: str, version: str | None) -> str | None:
    """Validate an explicitly requested revision. ``None`` leaves the caller to fall
    back to whatever the model is currently at."""
    if version is None:
        return None
    require_history(history)
    try:
        # `git rev-parse` is a subprocess, and this runs from an `async def`.
        return await asyncio.to_thread(history.resolve, version)
    except RevisionNotFoundError:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no revision {version!r}") from None
    except GitError as error:
        raise ApiError(status.HTTP_500_INTERNAL_SERVER_ERROR, str(error)) from None


def require_job(render: RenderService, job_id: str) -> Job:
    try:
        return render.store.read(job_id)
    except JobNotFoundError:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no job with id {job_id!r}") from None


@router.post(
    "/models/{slug}/render",
    response_model=RenderAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Queue a render",
    responses={
        status.HTTP_409_CONFLICT: {
            "description": (
                "the Bambuddy blob store has no commit of the template to snapshot for the render"
            )
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": (
                "SCADBUDDY_RENDER_QUEUE_MAX renders are already waiting (only when that "
                "limit is set); or Temporal, where renders run, is unreachable or could not "
                "take the start now (`temporal-unavailable`; `may_have_started` is false "
                "only when nothing reached Temporal, and true means send the same request "
                "again with the same `Idempotency-Key` to follow a start it may hold); or "
                "the render is "
                "still being accepted (`command-still-accepting`: send the same request "
                "again, with the same `Idempotency-Key`, to follow it as one request; "
                "without a key, each send is one more claim on the job); or, on the "
                "Bambuddy blob store, the revision's first snapshot is still uploading "
                "(problem `code` `snapshot_pending`). Retry after `Retry-After` seconds"
            )
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "description": (
                "The render's execution, or Temporal, refused it (`render-unstartable`: "
                "a configuration error, see the logs). With `may_have_started` true "
                "(Temporal refused, after a start it may have persisted), send the same "
                "request again with the same `Idempotency-Key` to follow it; false, the "
                "render's first step failed and no job exists"
            )
        },
        status.HTTP_507_INSUFFICIENT_STORAGE: {
            "description": "the blob store has no room for the template's source snapshot"
        },
    },
)
async def render_model(
    slug: SlugPath,
    body: RenderRequest,
    request: Request,
    catalogue: CatalogueDep,
    history: HistoryDep,
    paths: PathsDep,
    config: ConfigDep,
    render: RenderDep,
    assets: AssetsDep,
    fetcher: FetcherDep,
    fonts: FontsDep,
    idempotency_key: IdempotencyKey = None,
) -> RenderAccepted:
    if body.supersedes is not None and idempotency_key is None:
        # A re-send without a key would release the job once more, and with it another
        # request's claim (review #1066 2.1).
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "a render that supersedes another needs an Idempotency-Key header",
        )
    require_model_exists(catalogue, slug)
    requested = await _resolve_version(history, slug, body.version)
    source, schema = await schema_of(
        slug,
        requested,
        paths=paths,
        history=history,
        config=config,
        version=body.version,
        fetcher=fetcher,
    )
    try:
        normalized = normalize_inputs(body.inputs, body.params)
    except InputsError as error:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
    params, inputs = normalized.params, normalized.data
    # A pipeline passes params to the pieces it renders, each of which checks its own
    # (§5.2); only the built-in pipeline renders `model.scad` with them.
    pipeline = await asyncio.to_thread(_declares_pipeline, source.scad.parent, slug)
    if not pipeline:
        require_valid_params(schema, params)
        # A family that is not installed is a 422 here, not a render in the default font.
        await require_installed_fonts(schema, params, fonts)
    try:
        # A `file` parameter's value must name an upload or one of the revision's
        # own sample files (#204): checked here, so a bad one is a 422 rather than a
        # job that fails later or renders without it.
        await asyncio.to_thread(file_assets, schema, params, assets, source.scad.parent)
    except ValueError as error:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None

    # Refused only when SCADBUDDY_RENDER_QUEUE_MAX is set and reached; by default
    # the queue accepts every render and works through them.
    with submit_problems():
        job = await render.submit(
            slug,
            params,
            inputs=inputs,
            model_version=source.version,
            supersedes=body.supersedes,
            whole_inputs=pipeline,
            request_id=idempotency_key,
        )
    return RenderAccepted(
        job_id=job.id,
        status_url=request.url_for("get_job", job_id=job.id).path,
        inputs=inputs,
    )


@contextmanager
def submit_problems() -> Iterator[None]:
    """What a route answers when `RenderService` does not accept its job: a full queue,
    a start still accepting, Temporal out of reach, or the store or a snapshot
    unready. The render route and Arrange share it."""
    try:
        yield
    except QueueFullError as error:
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            str(error),
            headers={"Retry-After": str(error.retry_after)},
            retry_after=error.retry_after,
        ) from None
    except (CommandStillAcceptingError, CommandClosedError):
        # The render's first activity has not answered yet, or its execution ended before
        # it did (review #1061); the same request joins it or starts it again.
        raise still_accepting() from None
    except (TemporalUnavailableError, TemporalRefusedError) as error:
        raise _temporal_problem(error) from None
    except WorkflowUpdateFailedError as error:
        # The worker's failure text is for the log, not the client (review #1066 5.1).
        logger.error("the render could not be started: %s", error.cause, exc_info=True)
        # The run answered (`RENDER_UNSTARTABLE`, review #1066 (10) 1): no job exists.
        raise ApiError(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "the render could not be started",
            type_=RENDER_UNSTARTABLE_PROBLEM,
            may_have_started=False,
        ) from None
    except StoreFullError as error:
        # `submit` pins the template's snapshot in the blob store before the job exists.
        raise ApiError(
            status.HTTP_507_INSUFFICIENT_STORAGE,
            f"the blob store has no room for this template's source: {error}",
        ) from None
    except SnapshotUnavailableError as error:
        # The bambuddy store renders from a snapshot of a commit, and there is none.
        raise ApiError(status.HTTP_409_CONFLICT, str(error)) from None
    except SnapshotPendingError as error:
        # The revision's first snapshot is still uploading (#686); it carries on.
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            str(error),
            headers={"Retry-After": str(error.retry_after)},
            retry_after=error.retry_after,
            code="snapshot_pending",
        ) from None


def _temporal_problem(
    error: TemporalUnavailableError | TemporalRefusedError,
) -> ApiError:
    """What the route answers when Temporal did not take the render's start, as the
    print route does: only a failed connect wrote nothing; any other may follow a start
    Temporal persisted, which the same request sent again follows (review #1066 (8) 2).
    ``may_have_started`` says which, as #1316's routes do: the browser re-sends the same
    `Idempotency-Key` when it is true, so a detail tells a client to send again only
    then (review #1066 (10) 3, 4)."""
    if isinstance(error, TemporalRefusedError):
        # A wrong namespace or a denied permission: configuration. Temporal's message
        # stays in the log. The refusal may still follow a start it persisted.
        logger.error("Temporal refused to start a render", exc_info=error)
        return ApiError(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Temporal refused to start this render; see ScadBuddy's logs. Send the same"
            " request again to follow it if it started.",
            type_=RENDER_UNSTARTABLE_PROBLEM,
            may_have_started=True,
        )
    logger.warning("could not start a render on Temporal", exc_info=error)
    started = not isinstance(error.__cause__, RuntimeError)
    if not started:
        # The lazy client's first connect failed (`start_command`): nothing was sent.
        detail = (
            "ScadBuddy cannot reach Temporal, where renders run. Nothing was queued; try"
            " again shortly."
        )
    elif isinstance(error, TemporalBusyError):
        # Temporal answered, but could not take the start now (`temporal_failure`).
        detail = (
            "Temporal could not start this render right now. Send the same request again"
            " shortly to follow it if it started."
        )
    else:
        detail = (
            "ScadBuddy cannot reach Temporal, where renders run. Send the same request"
            " again shortly to follow it if it started."
        )
    return ApiError(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        detail,
        type_=TEMPORAL_UNAVAILABLE_PROBLEM,
        headers={"Retry-After": "5"},
        may_have_started=started,
    )


@router.get("/jobs/{job_id}", response_model=JobStatus, summary="Render job state")
def get_job(job_id: JobIdPath, request: Request, render: RenderDep) -> JobStatus:
    job = require_job(render, job_id)
    return _job_status(job, request.url_for("get_job_preview", job_id=job_id).path)


@router.get(
    "/jobs/{job_id}/preview.glb",
    response_class=FileResponse,
    responses={200: {"content": {GLB_MEDIA_TYPE: {}}}},
    summary="Render job preview mesh",
)
async def get_job_preview(
    job_id: JobIdPath, render: RenderDep, paths: PathsDep, state: StateDep
) -> FileResponse:
    job = await asyncio.to_thread(require_job, render, job_id)
    if job.result is None:
        raise ApiError(
            status.HTTP_404_NOT_FOUND, f"job {job_id!r} is {job.state} and has no preview"
        )
    await materialize_result(state.store.blobs, job.result)
    preview = paths.root / job.result.preview_glb
    if not preview.is_file():
        raise ApiError(status.HTTP_404_NOT_FOUND, f"the preview for job {job_id!r} is gone")
    return FileResponse(preview, media_type=GLB_MEDIA_TYPE)


@router.get(
    "/models/{slug}/diagnostics",
    response_model=ModelDiagnostics,
    summary="Diagnostics of the latest render",
    description=(
        "OpenSCAD's warnings and errors, with the file and line each names, from the "
        "model's most recently settled render (done or failed). A 404 when no render of "
        "it is on record: jobs are kept for `SCADBUDDY_JOB_TTL`."
    ),
)
async def get_model_diagnostics(
    slug: SlugPath, catalogue: CatalogueDep, render: RenderDep
) -> ModelDiagnostics:
    require_model_exists(catalogue, slug)
    # Reads every job file on the PVC; off the loop.
    job = await asyncio.to_thread(render.store.latest_finished, slug)
    if job is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no finished render of {slug!r} is on record")
    return ModelDiagnostics(
        job_id=job.id,
        status=job.state,
        model_version=job.model_version,
        finished_at=job.finished_at,
        error=job.error,
        diagnostics=job.diagnostics,
        diagnostics_dropped=job.diagnostics_dropped,
    )


def _draw_view(glb: Path, view: ViewName, size: int) -> bytes | None:
    parts = read_glb(glb)
    return render_view(parts, view, size) if parts else None


async def preview_view(
    glb: Path, view: ViewName, size: int, *, config: Config, owner: str
) -> Response:
    """``glb`` drawn from ``view`` as a PNG, with the plate cover's rasteriser.

    Off the loop and under the render budget, for the reasons `plate_thumbnails`
    gives: it is seconds of numpy on a large mesh, and has no child to kill.
    """
    if not glb.is_file():
        raise ApiError(status.HTTP_404_NOT_FOUND, f"the preview for {owner} is gone")
    try:
        png = await asyncio.wait_for(
            asyncio.to_thread(_draw_view, glb, view, size), timeout=config.render_timeout
        )
    except TimeoutError:
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"drawing the {view} view took longer than {config.render_timeout:g}s",
        ) from None
    if png is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"the preview for {owner} has no geometry")
    return Response(png, media_type=PNG_MEDIA_TYPE, headers={"Cache-Control": "no-store"})


@router.get(
    "/jobs/{job_id}/views/{view}.png",
    response_class=Response,
    responses={200: {"content": {PNG_MEDIA_TYPE: {}}}},
    summary="Render job preview from a named view",
    description=(
        "The job's preview mesh drawn from `view` (iso, front, back, left, right, top, "
        "bottom) as a shaded PNG, so the geometry can be checked without a 3D viewer."
    ),
)
async def get_job_view(
    job_id: JobIdPath,
    view: ViewName,
    render: RenderDep,
    paths: PathsDep,
    config: ConfigDep,
    state: StateDep,
    size: ViewSize = PLATE_PNG_SIZE,
) -> Response:
    job = await asyncio.to_thread(require_job, render, job_id)
    if job.result is None:
        raise ApiError(
            status.HTTP_404_NOT_FOUND, f"job {job_id!r} is {job.state} and has no preview"
        )
    await materialize_result(state.store.blobs, job.result)
    return await preview_view(
        paths.root / job.result.preview_glb, view, size, config=config, owner=f"job {job_id!r}"
    )


class MigrateInputsRequest(BaseModel):
    inputs: dict[str, Any]
    #: The template revision to migrate for, as the render route takes it; the live
    #: template by default.
    version: str | None = Field(default=None, pattern=COMMIT_ID_PATTERN)


@router.post(
    "/models/{slug}/inputs/migrate",
    response_model=MigrateResult,
    summary="Migrate saved inputs",
    responses={
        status.HTTP_413_CONTENT_TOO_LARGE: {"description": "the inputs are too large to carry"},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "the render service is unavailable"},
        status.HTTP_504_GATEWAY_TIMEOUT: {"description": "the migration ran out of time"},
    },
)
async def migrate_inputs(
    slug: SlugPath,
    body: MigrateInputsRequest,
    render: RenderDep,
    catalogue: CatalogueDep,
    history: HistoryDep,
) -> MigrateResult:
    """Bring saved inputs up to the template's `INPUTS_VERSION` (§8.2)."""
    record = require_model(catalogue, slug)  # 404 for an unknown template, as `get_model`
    v = body.inputs.get("v", 0)
    if record.pipeline is None and record.pipeline_raw is None:
        # No pipeline, so no `migrate` to run, at any revision: the inputs are as they are.
        current = v if isinstance(v, int) else 0
        return MigrateResult(inputs=body.inputs, from_version=current, to_version=current)
    if body.version is None and isinstance(v, int) and v == record.inputs_version:
        # Already current: no worker.
        return MigrateResult(inputs=body.inputs, from_version=v, to_version=v)
    version = await _resolve_version(history, slug, body.version)
    try:
        return await render.migrate_inputs(slug, body.inputs, version=version)
    except InputsError as error:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None


#: The header naming a breakdown's tiles, row by row.
COLOURS_HEADER = "X-ScadBuddy-Colours"
#: The header giving the breakdown grid's width in tiles, so a caller reads the
#: layout rather than working it out again (#750 review).
COLUMNS_HEADER = "X-ScadBuddy-Colour-Columns"


def _draw_breakdown(
    glb: Path, view: ViewName, size: int, order: list[str], deadline: float
) -> ColourBreakdown | None:
    parts = read_glb(glb)
    return render_colour_breakdown(parts, view, size, order, deadline=deadline) if parts else None


@router.get(
    "/jobs/{job_id}/colours.png",
    response_class=Response,
    responses={
        200: {
            "content": {PNG_MEDIA_TYPE: {}},
            "headers": {
                COLOURS_HEADER: {
                    "description": "The tiles' colours, comma-separated, row by row",
                    "schema": {"type": "string"},
                },
                COLUMNS_HEADER: {
                    "description": "Tiles per row of the grid",
                    "schema": {"type": "integer"},
                },
            },
        }
    },
    summary="Render job preview, one tile per colour",
    description=(
        "The job's preview mesh drawn once per colour from `view`, in a near-square grid: "
        "on each tile that colour's parts are in their colour and every other part in "
        "light grey, so a vision model can check which colour goes where (#252). "
        f"`{COLOURS_HEADER}` names the tiles, row by row, in the job's `colors` "
        f"(extruder) order, and `{COLUMNS_HEADER}` how many are in a row. At "
        "most 16 colours (422 above)."
    ),
)
async def get_job_colours(
    job_id: JobIdPath,
    render: RenderDep,
    paths: PathsDep,
    config: ConfigDep,
    state: StateDep,
    view: ViewName = "iso",
    size: Annotated[
        int,
        Query(ge=MIN_VIEW_SIZE, le=MAX_BREAKDOWN_TILE_SIZE, description="Edge of each tile"),
    ] = BREAKDOWN_TILE_SIZE,
) -> Response:
    job = require_job(render, job_id)
    if job.result is None:
        raise ApiError(
            status.HTTP_404_NOT_FOUND, f"job {job_id!r} is {job.state} and has no preview"
        )
    await materialize_result(state.store.blobs, job.result)
    glb = paths.root / job.result.preview_glb
    if not glb.is_file():
        raise ApiError(status.HTTP_404_NOT_FOUND, f"the preview for job {job_id!r} is gone")
    # Off the loop and bounded, as `preview_view`: one raster per colour. The wait
    # cannot stop the thread, so the thread stops itself at the same deadline.
    deadline = time.monotonic() + config.render_timeout
    try:
        drawn = await asyncio.wait_for(
            asyncio.to_thread(_draw_breakdown, glb, view, size, job.result.colors, deadline),
            timeout=config.render_timeout,
        )
    except TimeoutError:
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"drawing the breakdown took longer than {config.render_timeout:g}s",
        ) from None
    except ValueError as error:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
    if drawn is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"the preview for job {job_id!r} has no geometry")
    return Response(
        drawn.png,
        media_type=PNG_MEDIA_TYPE,
        headers={
            "Cache-Control": "no-store",
            COLOURS_HEADER: ",".join(drawn.colours),
            COLUMNS_HEADER: str(drawn.columns),
        },
    )
