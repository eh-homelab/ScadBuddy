from __future__ import annotations

import asyncio
import zipfile
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, File, Query, Response, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from scadbuddy.api.deps import (
    CatalogueDep,
    ConfigDep,
    OutputIdPath,
    OutputsDep,
    PathsDep,
    SettingsStoreDep,
    SlugPath,
    UploadsDep,
)
from scadbuddy.api.jobs import GLB_MEDIA_TYPE, PNG_MEDIA_TYPE, ViewSize, preview_view
from scadbuddy.api.models import PNG_MAGIC, require_model
from scadbuddy.api.operations import (
    OPERATION_RESPONSES,
    Claimed,
    IdempotencyKey,
    operation_answer,
    run_operation,
)
from scadbuddy.bambuddy.download import download_3mf
from scadbuddy.bambuddy.project_file import (
    ProjectFile,
    ProjectFileRequest,
)
from scadbuddy.bambuddy.send import SendRequest, SendResult
from scadbuddy.bambuddy.uploads import BambuddyUploadStore, LibraryCopy
from scadbuddy.core.problems import ApiError
from scadbuddy.library.outputs import (
    MODEL_NAME,
    PREVIEW_NAME,
    THUMBNAIL_NAME,
    OutputMeta,
    OutputNotFoundError,
    OutputStore,
    require_output,
)
from scadbuddy.operations.claims import ClaimStore
from scadbuddy.operations.component import OperationsDep
from scadbuddy.operations.store import Operation
from scadbuddy.render.bambu3mf import plates_of
from scadbuddy.render.geometry import GeometryAnalysis, NoSuchPlateError
from scadbuddy.render.inputs import (
    legacy_inputs,
)
from scadbuddy.render.schema import ParamValue
from scadbuddy.render.thumbnail import PLATE_PNG_SIZE, ViewName

router = APIRouter(tags=["outputs"])

THREE_MF_MEDIA_TYPE = "model/3mf"


class OutputSummary(OutputMeta):
    has_thumbnail: bool
    #: Every copy of ``model.3mf`` ScadBuddy has put in Bambuddy's file library (#316),
    #: in upload order, read from `BambuddyUploadStore` (#455). One per (folder,
    #: target): a project's folder keeps the file each of its prints came from.
    library_files: list[LibraryCopy] = Field(default_factory=list)


class OutputDetail(OutputSummary):
    params: dict[str, ParamValue] = Field(default_factory=dict)
    #: The template inputs this output was saved with (spec §4.3).
    inputs: dict[str, Any] = Field(default_factory=dict)


class OutputPlate(BaseModel):
    """One plate of the output's 3MF (#83): the ``plate_id`` a print of it queues."""

    index: int
    has_thumbnail: bool
    #: What the plate holds, for its label (#929): its own name, else the names of the
    #: objects on it; ``None`` when neither says, and the UI falls back to "Plate N".
    name: str | None = None


class CreateOutputRequest(BaseModel):
    job_id: str
    name: str | None = None
    #: The inputs on screen when Generate was pressed (spec §4.3). Their ``params``
    #: must be the ones the job rendered; left out, the job's own inputs are recorded.
    inputs: dict[str, Any] | None = None


class EditTarget(BaseModel):
    """What ``/edit/{output_id}`` needs to reopen the customizer."""

    # "model_version" trips pydantic's reserved "model_" prefix; see OutputMeta.
    model_config = ConfigDict(protected_namespaces=())

    output_id: str
    slug: str
    name: str | None
    params: dict[str, ParamValue] = Field(default_factory=dict)
    inputs: dict[str, Any] = Field(default_factory=dict)
    model_version: str | None = None
    #: ``record`` when the output is still saved, ``3mf`` when only the file survives.
    source: Literal["record", "3mf"]


def detail(store: OutputStore, meta: OutputMeta, library_files: list[LibraryCopy]) -> OutputDetail:
    params = store.params(meta.id)
    return OutputDetail(
        **meta.model_dump(),
        has_thumbnail=store.thumbnail_path(meta.id).is_file(),
        params=params,
        inputs=store.inputs(meta.id, params),
        library_files=library_files,
    )


async def _details(
    store: OutputStore, uploads: BambuddyUploadStore, metas: list[OutputMeta]
) -> list[OutputDetail]:
    copies = await uploads.for_outputs(meta.id for meta in metas)
    # The thumbnail check and params read are file IO: off the event loop.
    return await asyncio.to_thread(lambda: [detail(store, meta, copies[meta.id]) for meta in metas])


@router.post(
    "/models/{slug}/outputs",
    response_model=OutputDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Persist a finished render",
    responses=OPERATION_RESPONSES,
)
async def create_output(
    slug: SlugPath,
    body: CreateOutputRequest,
    response: Response,
    ops: OperationsDep,
    idempotency_key: IdempotencyKey = None,
) -> OutputDetail | JSONResponse:
    """The ``output_create`` operation (#1054): its check makes the 404 for the model
    or the job and the 409 for a job of another model or not done; its run fetches the
    result, compares ``inputs`` with what the job rendered (422) and copies it."""
    result = await run_operation(
        ops,
        response,
        kind=ops.kinds["output_create"],
        subject=slug,
        request={"slug": slug, **body.model_dump(mode="json")},
        idempotency_key=idempotency_key,
    )
    return operation_answer(result, OutputDetail)


@router.get("/models/{slug}/outputs", response_model=list[OutputDetail], summary="Output history")
async def list_outputs(
    slug: SlugPath, catalogue: CatalogueDep, outputs: OutputsDep, uploads: UploadsDep
) -> list[OutputDetail]:
    """Details, not summaries: the history page shows each output's parameter diff, and
    a summary list would make it fetch every row again one at a time."""
    await asyncio.to_thread(require_model, catalogue, slug)
    metas = await asyncio.to_thread(outputs.list_for, slug)
    return await _details(outputs, uploads, metas)


@router.get("/outputs/{output_id}", response_model=OutputDetail, summary="Output detail")
async def get_output(
    output_id: OutputIdPath, outputs: OutputsDep, uploads: UploadsDep
) -> OutputDetail:
    meta = await asyncio.to_thread(require_output, outputs, output_id)
    [detail] = await _details(outputs, uploads, [meta])
    return detail


@router.get(
    "/outputs/{output_id}/edit",
    response_model=EditTarget,
    summary="Resolve an edit deep link",
)
def get_edit_target(
    output_id: OutputIdPath, catalogue: CatalogueDep, outputs: OutputsDep
) -> EditTarget:
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
        require_model(catalogue, stamped.model)
        return EditTarget(
            output_id=output_id,
            slug=stamped.model,
            name=None,
            params=stamped.params,
            inputs=legacy_inputs(stamped.params),
            model_version=stamped.version,
            source="3mf",
        )
    # Every other route through a slug asks the catalogue first. A link is allowed to
    # outlive its output — that is the point of the 3MF fallback — but not its model:
    # answering 200 would send the customizer somewhere it cannot load.
    require_model(catalogue, meta.slug)
    params = outputs.params(output_id)
    return EditTarget(
        output_id=meta.id,
        slug=meta.slug,
        name=meta.name,
        params=params,
        inputs=outputs.inputs(output_id, params),
        model_version=meta.model_version,
        source="record",
    )


@router.delete(
    "/outputs/{output_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an output",
    responses=OPERATION_RESPONSES,
)
async def delete_output(
    output_id: OutputIdPath,
    response: Response,
    ops: OperationsDep,
    delete_inbox_copies: Annotated[bool, Query()] = False,
    idempotency_key: IdempotencyKey = None,
) -> Response:
    """Delete the output, and with ``delete_inbox_copies`` its copies in Bambuddy's
    inbox folder (#316).

    Copies in a project's folder are never deleted: they are that project's record of
    what it printed, listed in ``library_files`` so the UI can say they stay. Nor are
    sliced files, which a queued print may still reference. A Bambuddy delete that
    fails stops here, before the record goes — it is the only pointer to the file.
    """
    result = await run_operation(
        ops,
        response,
        kind=ops.kinds["output_delete"],
        subject=output_id,
        request={"output_id": output_id, "delete_inbox_copies": delete_inbox_copies},
        idempotency_key=idempotency_key,
    )
    return _no_content(result)


def _no_content(result: dict[str, Any] | Operation) -> Response:
    if isinstance(result, Operation):
        return JSONResponse(result.model_dump(mode="json"), status_code=status.HTTP_202_ACCEPTED)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/outputs/{output_id}/model.3mf",
    response_class=Response,
    responses={200: {"content": {THREE_MF_MEDIA_TYPE: {}}}},
    summary="Download the 3MF",
)
async def download_output(
    output_id: OutputIdPath, outputs: OutputsDep, store: SettingsStoreDep, catalogue: CatalogueDep
) -> Response:
    meta = require_output(outputs, output_id)
    path = outputs.directory(output_id) / MODEL_NAME
    if not path.is_file():
        raise ApiError(status.HTTP_404_NOT_FOUND, f"output {output_id!r} has no 3MF")
    # For the default printer, on its real presets (#769), with the template's own
    # print settings (#770).
    return await download_3mf(path, meta, store.load(), catalogue.print_settings(meta.slug))


@router.get(
    "/outputs/{output_id}/preview.glb",
    response_class=FileResponse,
    responses={200: {"content": {GLB_MEDIA_TYPE: {}}}},
    summary="The output's preview mesh",
)
def get_output_preview(output_id: OutputIdPath, outputs: OutputsDep) -> FileResponse:
    require_output(outputs, output_id)
    path = outputs.directory(output_id) / PREVIEW_NAME
    if not path.is_file():
        raise ApiError(status.HTTP_404_NOT_FOUND, f"output {output_id!r} has no preview mesh")
    return FileResponse(path, media_type=GLB_MEDIA_TYPE)


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


@router.get(
    "/outputs/{output_id}/views/{view}.png",
    response_class=Response,
    responses={200: {"content": {PNG_MEDIA_TYPE: {}}}},
    summary="Output preview from a named view",
    description=(
        "The saved output's preview mesh drawn from `view` (iso, front, back, left, "
        "right, top, bottom) as a shaded PNG."
    ),
)
async def get_output_view(
    output_id: OutputIdPath,
    view: ViewName,
    outputs: OutputsDep,
    config: ConfigDep,
    size: ViewSize = PLATE_PNG_SIZE,
) -> Response:
    require_output(outputs, output_id)
    return await preview_view(
        outputs.directory(output_id) / PREVIEW_NAME,
        view,
        size,
        config=config,
        owner=f"output {output_id!r}",
    )


def _model_3mf(outputs: OutputStore, output_id: str) -> Path:
    path = outputs.directory(output_id) / MODEL_NAME
    if not path.is_file():
        raise ApiError(status.HTTP_404_NOT_FOUND, f"output {output_id!r} has no 3MF")
    return path


@router.get(
    "/outputs/{output_id}/plates", response_model=list[OutputPlate], summary="The 3MF's plates"
)
def get_output_plates(output_id: OutputIdPath, outputs: OutputsDep) -> list[OutputPlate]:
    """What the print picker offers as ``plate_id`` (#83). ScadBuddy's own renders are
    one plate unless the template asks for more (#289)."""
    require_output(outputs, output_id)
    return [
        OutputPlate(index=plate.index, has_thumbnail=plate.thumbnail is not None, name=plate.name)
        for plate in plates_of(_model_3mf(outputs, output_id))
    ]


@router.get(
    "/outputs/{output_id}/plates/{index}/thumbnail",
    response_class=Response,
    responses={200: {"content": {"image/png": {}}}},
    summary="A plate's cover image",
)
def get_output_plate_thumbnail(
    output_id: OutputIdPath, index: int, outputs: OutputsDep
) -> Response:
    """The plate's own cover from inside the 3MF, the one Bambu Studio would show."""
    require_output(outputs, output_id)
    path = _model_3mf(outputs, output_id)
    cover = next(
        (plate.thumbnail for plate in plates_of(path) if plate.index == index and plate.thumbnail),
        None,
    )
    if cover is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"plate {index} of {output_id!r} has no cover")
    with zipfile.ZipFile(path) as archive:
        return Response(archive.read(cover), media_type="image/png")


@router.get(
    "/outputs/{output_id}/geometry",
    response_model=GeometryAnalysis,
    summary="Mesh geometry analysis",
)
def get_output_geometry(
    output_id: OutputIdPath,
    outputs: OutputsDep,
    plate: Annotated[int, Query(ge=1, description="The plate to measure (1-based)")] = 1,
) -> GeometryAnalysis:
    """Printability measurements of the closed per-colour solids on one plate of the
    output (#284): open and non-manifold edges with their locations, bounding box,
    bed contact, height-to-base ratio, overhang area by angle, and estimates of the
    thinnest wall and smallest feature. Coordinates are the model's own (mm, Z up),
    as in the preview. A 3MF with more than one plate (#289) is measured a plate at
    a time; ``plates`` in the result says how many there are. Computed on first ask
    and cached beside the output."""
    require_output(outputs, output_id)
    try:
        return outputs.geometry(output_id, plate)
    except FileNotFoundError:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"output {output_id!r} has no 3MF") from None
    except NoSuchPlateError as error:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"output {output_id!r}: {error}") from None
    except (ValueError, zipfile.BadZipFile) as error:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"the 3MF of output {output_id!r} cannot be analysed: {error}",
        ) from None


@router.put(
    "/outputs/{output_id}/thumbnail",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Upload the canvas capture",
    responses=OPERATION_RESPONSES,
)
async def put_output_thumbnail(
    output_id: OutputIdPath,
    response: Response,
    ops: OperationsDep,
    paths: PathsDep,
    file: Annotated[UploadFile, File(description="PNG captured by the viewer")],
    idempotency_key: IdempotencyKey = None,
) -> Response:
    png = await file.read()
    if not png.startswith(PNG_MAGIC):
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, "the thumbnail is not a PNG")
    # By claim: a canvas capture may be past a workflow payload's limit (#1054).
    claims = ClaimStore(paths.claims)
    held = await asyncio.to_thread(claims.hold, png)
    result = await run_operation(
        ops,
        response,
        kind=ops.kinds["output_thumbnail"],
        subject=output_id,
        request={"output_id": output_id, "png": held.name},
        idempotency_key=idempotency_key,
        claimed=Claimed(claims, [held]),
    )
    return _no_content(result)


@router.post(
    "/outputs/{output_id}/send",
    response_model=SendResult,
    summary="Upload the 3MF to the Bambuddy library",
    responses=OPERATION_RESPONSES,
)
async def send_output_to_bambuddy(
    output_id: OutputIdPath,
    body: SendRequest,
    response: Response,
    outputs: OutputsDep,
    ops: OperationsDep,
    idempotency_key: IdempotencyKey = None,
) -> SendResult | JSONResponse:
    """Upload ``model.3mf`` to the configured library folder, laid out for the printer
    set in Settings, and note the "Edit in ScadBuddy" link on it.

    Nothing is sliced or queued (#312): printing is ``POST /print/outputs/{id}/run``.
    ``mode`` accepts only ``"library"``.

    The file is read from the PVC and pushed by the server, so the API key never
    reaches the browser. A re-send reuses the copy already in the inbox while it was
    laid out for the same printer, and replaces it otherwise (#316).
    """
    del body  # validated for its ``mode`` alone
    require_output(outputs, output_id)
    result = await run_operation(
        ops,
        response,
        kind=ops.kinds["send"],
        subject=output_id,
        request={"output_id": output_id},
        idempotency_key=idempotency_key,
    )
    return operation_answer(result, SendResult)


@router.post(
    "/outputs/{output_id}/project-file",
    response_model=ProjectFile,
    summary="File this output's 3MF in a project's Bambuddy folder",
    responses=OPERATION_RESPONSES,
)
async def post_project_file(
    output_id: OutputIdPath,
    body: ProjectFileRequest,
    response: Response,
    outputs: OutputsDep,
    ops: OperationsDep,
    idempotency_key: IdempotencyKey = None,
) -> ProjectFile | JSONResponse:
    """Upload the editable project 3MF into the project's folder (#317), as Generate does
    when a project is chosen.

    Idempotent per (folder, target): the same project chosen again answers with the file
    already there (``created: false``), and a later print on the same printer reuses it
    (#316). The folder is created and linked if the project has none. Every Bambuddy
    call is made here, so the API key never reaches the browser.
    """
    require_output(outputs, output_id)
    result = await run_operation(
        ops,
        response,
        kind=ops.kinds["project_file"],
        subject=output_id,
        request={"output_id": output_id, "project_id": body.project_id},
        idempotency_key=idempotency_key,
    )
    return operation_answer(result, ProjectFile)
