from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, File, Response, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from scadbuddy.api.deps import (
    CatalogueDep,
    OutputIdPath,
    OutputsDep,
    QueueDep,
    SettingsStoreDep,
    SlugPath,
)
from scadbuddy.api.jobs import require_job
from scadbuddy.api.models import PNG_MAGIC, require_model
from scadbuddy.bambuddy.client import client_for
from scadbuddy.bambuddy.send import SendRequest, SendResult, send_output
from scadbuddy.core.problems import ApiError
from scadbuddy.library.outputs import (
    MODEL_NAME,
    THUMBNAIL_NAME,
    OutputMeta,
    OutputNotFoundError,
    OutputStore,
    download_filename,
)
from scadbuddy.render.schema import ParamValue

router = APIRouter(tags=["outputs"])

THREE_MF_MEDIA_TYPE = "model/3mf"


class OutputSummary(OutputMeta):
    has_thumbnail: bool


class OutputDetail(OutputSummary):
    params: dict[str, ParamValue] = Field(default_factory=dict)


class CreateOutputRequest(BaseModel):
    job_id: str
    name: str | None = None


class EditTarget(BaseModel):
    """What ``/edit/{output_id}`` needs to reopen the customizer."""

    # "model_version" trips pydantic's reserved "model_" prefix; see OutputMeta.
    model_config = ConfigDict(protected_namespaces=())

    output_id: str
    slug: str
    name: str | None
    params: dict[str, ParamValue] = Field(default_factory=dict)
    model_version: str | None = None
    #: ``record`` when the output is still saved, ``3mf`` when only the file survives.
    source: Literal["record", "3mf"]


def _detail(store: OutputStore, meta: OutputMeta) -> OutputDetail:
    return OutputDetail(
        **meta.model_dump(),
        has_thumbnail=store.thumbnail_path(meta.id).is_file(),
        params=store.params(meta.id),
    )


def require_output(store: OutputStore, output_id: str) -> OutputMeta:
    try:
        return store.get(output_id)
    except OutputNotFoundError:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no output with id {output_id!r}") from None


@router.post(
    "/models/{slug}/outputs",
    response_model=OutputDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Persist a finished render",
)
def create_output(
    slug: SlugPath,
    body: CreateOutputRequest,
    catalogue: CatalogueDep,
    outputs: OutputsDep,
    queue: QueueDep,
    store: SettingsStoreDep,
) -> OutputDetail:
    require_model(catalogue, slug)
    job = require_job(queue, body.job_id)
    if job.slug != slug:
        raise ApiError(
            status.HTTP_409_CONFLICT, f"job {job.id!r} rendered {job.slug!r}, not {slug!r}"
        )
    if job.state != "done" or job.result is None:
        raise ApiError(
            status.HTTP_409_CONFLICT, f"job {job.id!r} is {job.state}, so there is nothing to save"
        )
    return _detail(outputs, outputs.create(job, name=body.name, public_url=store.load().public_url))


@router.get("/models/{slug}/outputs", response_model=list[OutputDetail], summary="Output history")
def list_outputs(
    slug: SlugPath, catalogue: CatalogueDep, outputs: OutputsDep
) -> list[OutputDetail]:
    """Details, not summaries: the history page shows each output's parameter diff, and
    a summary list would make it fetch every row again one at a time."""
    require_model(catalogue, slug)
    return [_detail(outputs, meta) for meta in outputs.list_for(slug)]


@router.get("/outputs/{output_id}", response_model=OutputDetail, summary="Output detail")
def get_output(output_id: OutputIdPath, outputs: OutputsDep) -> OutputDetail:
    return _detail(outputs, require_output(outputs, output_id))


@router.get(
    "/outputs/{output_id}/edit",
    response_model=EditTarget,
    summary="Resolve an edit deep link",
)
def get_edit_target(output_id: OutputIdPath, outputs: OutputsDep) -> EditTarget:
    """Where ``/edit/{output_id}`` should land, and with which values.

    The record answers first. When it is gone — the directory restored without its
    sidecars, or hand-pruned — the 3MF still carries the same provenance, so the
    link keeps working from the file alone.
    """
    try:
        meta = outputs.get(output_id)
    except OutputNotFoundError:
        stamped = outputs.provenance(output_id)
        if stamped is None:
            raise ApiError(
                status.HTTP_404_NOT_FOUND,
                f"no output with id {output_id!r}, and no 3MF left to read it from",
            ) from None
        return EditTarget(
            output_id=output_id,
            slug=stamped.model,
            name=None,
            params=stamped.params,
            model_version=stamped.version,
            source="3mf",
        )
    return EditTarget(
        output_id=meta.id,
        slug=meta.slug,
        name=meta.name,
        params=outputs.params(output_id),
        model_version=meta.model_version,
        source="record",
    )


@router.delete(
    "/outputs/{output_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete an output"
)
def delete_output(output_id: OutputIdPath, outputs: OutputsDep) -> Response:
    require_output(outputs, output_id)
    outputs.delete(output_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/outputs/{output_id}/model.3mf",
    response_class=FileResponse,
    responses={200: {"content": {THREE_MF_MEDIA_TYPE: {}}}},
    summary="Download the 3MF",
)
def download_output(output_id: OutputIdPath, outputs: OutputsDep) -> FileResponse:
    meta = require_output(outputs, output_id)
    path = outputs.directory(output_id) / MODEL_NAME
    if not path.is_file():
        raise ApiError(status.HTTP_404_NOT_FOUND, f"output {output_id!r} has no 3MF")
    return FileResponse(path, media_type=THREE_MF_MEDIA_TYPE, filename=download_filename(meta))


@router.get(
    "/outputs/{output_id}/thumbnail",
    response_class=FileResponse,
    responses={200: {"content": {"image/png": {}}}},
    summary="Output thumbnail",
)
def get_output_thumbnail(output_id: OutputIdPath, outputs: OutputsDep) -> FileResponse:
    require_output(outputs, output_id)
    path = outputs.directory(output_id) / THUMBNAIL_NAME
    if not path.is_file():
        raise ApiError(status.HTTP_404_NOT_FOUND, f"output {output_id!r} has no thumbnail")
    return FileResponse(path, media_type="image/png")


@router.put(
    "/outputs/{output_id}/thumbnail",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Upload the canvas capture",
)
async def put_output_thumbnail(
    output_id: OutputIdPath,
    outputs: OutputsDep,
    file: Annotated[UploadFile, File(description="PNG captured by the viewer")],
) -> Response:
    require_output(outputs, output_id)
    png = await file.read()
    if not png.startswith(PNG_MAGIC):
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, "the thumbnail is not a PNG")
    outputs.write_thumbnail(output_id, png)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/outputs/{output_id}/send",
    response_model=SendResult,
    summary="Send the 3MF to Bambuddy",
)
async def send_output_to_bambuddy(
    output_id: OutputIdPath,
    body: SendRequest,
    outputs: OutputsDep,
    store: SettingsStoreDep,
) -> SendResult:
    """Upload ``model.3mf`` to the configured library folder and, in ``queue`` mode,
    slice and queue it.

    The file is read from the PVC and pushed by the server, so the API key never
    reaches the browser. A re-send replaces the file Bambuddy already holds rather
    than adding a second copy.
    """
    meta = require_output(outputs, output_id)
    settings = store.load()
    async with client_for(settings) as client:
        return await send_output(client, outputs, meta, settings, body)
