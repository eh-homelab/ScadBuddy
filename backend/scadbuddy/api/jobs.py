from __future__ import annotations

import asyncio
from datetime import datetime

from fastapi import APIRouter, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from scadbuddy.api.deps import (
    CatalogueDep,
    ConfigDep,
    HistoryDep,
    JobIdPath,
    PathsDep,
    QueueDep,
    SlugPath,
)
from scadbuddy.api.models import require_model_exists
from scadbuddy.api.versions import require_history
from scadbuddy.core.problems import ApiError
from scadbuddy.library.history import COMMIT_ID_PATTERN, RevisionNotFoundError
from scadbuddy.render.glb import BoundingBox
from scadbuddy.render.jobs import Job, JobState, PartInfo, RenderQueue, resolve_source
from scadbuddy.render.runner import UnknownParameterError, build_defines, cached_schema
from scadbuddy.render.schema import ParamValue

router = APIRouter(tags=["jobs"])

GLB_MEDIA_TYPE = "model/gltf-binary"


class RenderRequest(BaseModel):
    params: dict[str, ParamValue] = Field(default_factory=dict)
    # #90's "Customize this version": render an old revision without restoring it.
    # Omitted means the revision the model is currently at.
    version: str | None = Field(default=None, pattern=COMMIT_ID_PATTERN)


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
    warnings: list[str] | None = None
    parts: list[PartInfo] | None = None


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
        warnings=result.warnings if result else None,
        parts=result.parts if result else None,
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


def require_job(queue: RenderQueue, job_id: str) -> Job:
    try:
        return queue.store.read(job_id)
    except FileNotFoundError:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no job with id {job_id!r}") from None


@router.post(
    "/models/{slug}/render",
    response_model=RenderAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Queue a render",
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
) -> RenderAccepted:
    require_model_exists(catalogue, slug)
    requested = await _resolve_version(history, slug, body.version)
    try:
        # The schema the parameters are validated against has to be the schema of
        # the revision being rendered, not the one the model is currently at.
        # `resolve_source` also hands back which revision that is, so the job can
        # be stamped without asking git a second time.
        source = await resolve_source(slug, requested, paths=paths, history=history)
        schema = await cached_schema(source.scad, source.schema_cache, config=config)
    except RevisionNotFoundError:
        raise ApiError(
            status.HTTP_404_NOT_FOUND, f"{slug!r} does not exist at {body.version}"
        ) from None
    except FileNotFoundError:
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE, "openscad is not available to build the schema"
        ) from None

    unknown = sorted(set(body.params) - {p.name for p in schema.parameters})
    if unknown:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"unknown parameters: {', '.join(unknown)}",
            parameters=unknown,
        )
    try:
        build_defines(schema, body.params)
    except UnknownParameterError as error:  # pragma: no cover - covered by the check above
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
    except ValueError as error:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None

    job = await queue.submit(slug, body.params, model_version=source.version)
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
