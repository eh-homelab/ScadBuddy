from __future__ import annotations

import asyncio
import time
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Query, Request, Response, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from scadbuddy.api.deps import (
    JOB_ID_PATTERN,
    AssetsDep,
    CatalogueDep,
    ConfigDep,
    FetcherDep,
    HistoryDep,
    JobIdPath,
    PathsDep,
    QueueDep,
    SlugPath,
)
from scadbuddy.api.models import require_model_exists
from scadbuddy.api.params import require_valid_params, schema_of
from scadbuddy.api.versions import require_history
from scadbuddy.core.config import Config
from scadbuddy.core.problems import ApiError
from scadbuddy.library.assets import file_assets
from scadbuddy.library.history import (
    COMMIT_ID_PATTERN,
    GitError,
    RevisionNotFoundError,
)
from scadbuddy.render.diagnostics import Diagnostic
from scadbuddy.render.glb import BoundingBox, read_glb
from scadbuddy.render.jobs import (
    Job,
    JobNotFoundError,
    JobState,
    PartInfo,
    PlateInfo,
    QueueFullError,
    RenderQueue,
)
from scadbuddy.render.schema import ParamValue
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

router = APIRouter(tags=["jobs"])

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
    params: dict[str, ParamValue] = Field(default_factory=dict)
    # #90's "Customize this version": render an old revision without restoring it.
    # Omitted means the revision the model is currently at.
    version: str | None = Field(default=None, pattern=COMMIT_ID_PATTERN)
    # The job this render replaces -- the preview's previous submit. Dropped unrendered
    # if no worker has taken it yet, so a slider drag does not queue every stop on
    # the way. Harmless when it has already started or finished.
    supersedes: str | None = Field(default=None, pattern=JOB_ID_PATTERN)


class RenderAccepted(BaseModel):
    job_id: str
    status_url: str


class JobStatus(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    id: str
    slug: str
    status: JobState
    model_version: str | None = None
    params: dict[str, ParamValue] = Field(default_factory=dict)
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
    )


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


def require_job(queue: RenderQueue, job_id: str) -> Job:
    try:
        return queue.store.read(job_id)
    except JobNotFoundError:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no job with id {job_id!r}") from None


@router.post(
    "/models/{slug}/render",
    response_model=RenderAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Queue a render",
    responses={
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": (
                "SCADBUDDY_RENDER_QUEUE_MAX renders are already waiting (only when that "
                "limit is set); retry after `Retry-After` seconds"
            )
        }
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
    queue: QueueDep,
    assets: AssetsDep,
    fetcher: FetcherDep,
) -> RenderAccepted:
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
    require_valid_params(schema, body.params)
    try:
        # A `file` parameter's value must name an upload or one of the revision's
        # own sample files (#204): checked here, so a bad one is a 422 rather than a
        # job that fails later or renders without it.
        await asyncio.to_thread(file_assets, schema, body.params, assets, source.scad.parent)
    except ValueError as error:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None

    # Refused only when SCADBUDDY_RENDER_QUEUE_MAX is set and reached; by default
    # the queue accepts every render and works through them.
    try:
        job = await queue.submit(
            slug, body.params, model_version=source.version, supersedes=body.supersedes
        )
    except QueueFullError as error:
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            str(error),
            headers={"Retry-After": str(error.retry_after)},
            retry_after=error.retry_after,
        ) from None
    return RenderAccepted(job_id=job.id, status_url=request.url_for("get_job", job_id=job.id).path)


@router.get("/jobs/{job_id}", response_model=JobStatus, summary="Render job state")
def get_job(job_id: JobIdPath, request: Request, queue: QueueDep) -> JobStatus:
    job = require_job(queue, job_id)
    return _job_status(job, request.url_for("get_job_preview", job_id=job_id).path)


@router.get(
    "/jobs/{job_id}/preview.glb",
    response_class=FileResponse,
    responses={200: {"content": {GLB_MEDIA_TYPE: {}}}},
    summary="Render job preview mesh",
)
def get_job_preview(job_id: JobIdPath, queue: QueueDep, paths: PathsDep) -> FileResponse:
    job = require_job(queue, job_id)
    if job.result is None:
        raise ApiError(
            status.HTTP_404_NOT_FOUND, f"job {job_id!r} is {job.state} and has no preview"
        )
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
    slug: SlugPath, catalogue: CatalogueDep, queue: QueueDep
) -> ModelDiagnostics:
    require_model_exists(catalogue, slug)
    # Reads every job file on the PVC; off the loop.
    job = await asyncio.to_thread(queue.store.latest_finished, slug)
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
    queue: QueueDep,
    paths: PathsDep,
    config: ConfigDep,
    size: ViewSize = PLATE_PNG_SIZE,
) -> Response:
    job = require_job(queue, job_id)
    if job.result is None:
        raise ApiError(
            status.HTTP_404_NOT_FOUND, f"job {job_id!r} is {job.state} and has no preview"
        )
    return await preview_view(
        paths.root / job.result.preview_glb, view, size, config=config, owner=f"job {job_id!r}"
    )


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
    queue: QueueDep,
    paths: PathsDep,
    config: ConfigDep,
    view: ViewName = "iso",
    size: Annotated[
        int,
        Query(ge=MIN_VIEW_SIZE, le=MAX_BREAKDOWN_TILE_SIZE, description="Edge of each tile"),
    ] = BREAKDOWN_TILE_SIZE,
) -> Response:
    job = require_job(queue, job_id)
    if job.result is None:
        raise ApiError(
            status.HTTP_404_NOT_FOUND, f"job {job_id!r} is {job.state} and has no preview"
        )
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
