from __future__ import annotations

import asyncio
import logging
import zipfile
from pathlib import Path
from typing import Annotated, Any, Literal

import psycopg
from fastapi import APIRouter, File, Query, Response, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from scadbuddy.api.deps import (
    CatalogueDep,
    ConfigDep,
    EventsDep,
    OutputIdPath,
    OutputsDep,
    PrintLinksDep,
    RenderDep,
    SettingsStoreDep,
    SlugPath,
    StateDep,
    UploadsDep,
)
from scadbuddy.api.jobs import GLB_MEDIA_TYPE, PNG_MEDIA_TYPE, ViewSize, preview_view, require_job
from scadbuddy.api.models import PNG_MAGIC, require_model
from scadbuddy.bambuddy.client import client_for
from scadbuddy.bambuddy.download import download_3mf
from scadbuddy.bambuddy.project_file import (
    ProjectFile,
    ProjectFileRequest,
    file_into_project,
    project_stem,
)
from scadbuddy.bambuddy.send import SendRequest, SendResult, send_output
from scadbuddy.bambuddy.send import delete_inbox_copies as remove_inbox_copies
from scadbuddy.bambuddy.uploads import BambuddyUploadStore, DatabaseRequiredError, LibraryCopy
from scadbuddy.core.events import OutputEvent, emit
from scadbuddy.core.paths import DataPaths
from scadbuddy.core.problems import ApiError
from scadbuddy.library.catalogue import Catalogue, ModelNotFoundError
from scadbuddy.library.libraries import LibraryError, model_search_path
from scadbuddy.library.outputs import (
    MODEL_NAME,
    PREVIEW_NAME,
    THUMBNAIL_NAME,
    OutputMeta,
    OutputNotFoundError,
    OutputStore,
)
from scadbuddy.render.bambu3mf import plates_of
from scadbuddy.render.geometry import GeometryAnalysis, NoSuchPlateError
from scadbuddy.render.inputs import InputsError, legacy_inputs, normalize_inputs
from scadbuddy.render.schema import ParamValue, load_cached_schema, source_sha256
from scadbuddy.render.thumbnail import PLATE_PNG_SIZE, ViewName
from scadbuddy.store.cache import materialize_result

logger = logging.getLogger(__name__)

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


def _detail(store: OutputStore, meta: OutputMeta, library_files: list[LibraryCopy]) -> OutputDetail:
    return OutputDetail(
        **meta.model_dump(),
        has_thumbnail=store.thumbnail_path(meta.id).is_file(),
        params=store.params(meta.id),
        inputs=store.inputs(meta.id),
        library_files=library_files,
    )


async def _details(
    store: OutputStore, uploads: BambuddyUploadStore, metas: list[OutputMeta]
) -> list[OutputDetail]:
    copies = await uploads.for_outputs(meta.id for meta in metas)
    # The thumbnail check and params read are file IO: off the event loop.
    return await asyncio.to_thread(
        lambda: [_detail(store, meta, copies[meta.id]) for meta in metas]
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
async def create_output(
    slug: SlugPath,
    body: CreateOutputRequest,
    catalogue: CatalogueDep,
    outputs: OutputsDep,
    render: RenderDep,
    store: SettingsStoreDep,
    events: EventsDep,
    state: StateDep,
) -> OutputDetail:
    await asyncio.to_thread(require_model, catalogue, slug)
    job = await asyncio.to_thread(require_job, render, body.job_id)
    if job.slug != slug:
        raise ApiError(
            status.HTTP_409_CONFLICT, f"job {job.id!r} rendered {job.slug!r}, not {slug!r}"
        )
    if job.state != "done" or job.result is None:
        raise ApiError(
            status.HTTP_409_CONFLICT, f"job {job.id!r} is {job.state}, so there is nothing to save"
        )
    inputs = None
    if body.inputs is not None:
        try:
            inputs = normalize_inputs(body.inputs, None)
        except InputsError as error:
            raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
        rendered = f"inputs.params are not the parameters job {job.id} rendered"
        # Two checks, both needed. The equality is Python's, where 12.0 == 12 and
        # True == 1, so it is only a cheap first answer (and the one that covers a job
        # with no params, which the store's rule skips). The store's rule below
        # compares type as well as value; it is the guarantee that nothing reaching
        # the copy is refused there.
        if inputs["params"] != job.params:
            raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, rendered)
        try:
            normalize_inputs(inputs, job.params)
        except InputsError:
            raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, rendered) from None
    # The copy reads the job's files, which on the bambuddy backend come through the cache.
    await materialize_result(state.store.blobs, job.result)
    # A piece the store no longer has (aged out, or the Bambuddy store unreachable) is not
    # fetched, and the copy would fail with a server path in its message: say so instead.
    files = (job.result.model_3mf, job.result.preview_glb)
    if not all((outputs.paths.root / name).is_file() for name in files):
        raise ApiError(status.HTTP_404_NOT_FOUND, f"the result of job {job.id!r} is gone")
    public_url = (await asyncio.to_thread(store.load)).public_url
    try:
        meta = await asyncio.to_thread(
            outputs.create, job, name=body.name, public_url=public_url, inputs=inputs
        )
    except OSError:
        # Evicted or swept after the check above, or unreadable: the same answer, not
        # the copy's path.
        raise ApiError(status.HTTP_404_NOT_FOUND, f"the result of job {job.id!r} is gone") from None
    emit(events, OutputEvent(kind="output.created", output_id=meta.id, slug=meta.slug))
    # A new output has no uploads yet: no read to make.
    return _detail(outputs, meta, [])


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
    return EditTarget(
        output_id=meta.id,
        slug=meta.slug,
        name=meta.name,
        params=outputs.params(output_id),
        inputs=outputs.inputs(output_id),
        model_version=meta.model_version,
        source="record",
    )


@router.delete(
    "/outputs/{output_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete an output"
)
async def delete_output(
    output_id: OutputIdPath,
    outputs: OutputsDep,
    uploads: UploadsDep,
    links: PrintLinksDep,
    events: EventsDep,
    store: SettingsStoreDep,
    delete_inbox_copies: Annotated[bool, Query()] = False,
) -> Response:
    """Delete the output, and with ``delete_inbox_copies`` its copies in Bambuddy's
    inbox folder (#316).

    Copies in a project's folder are never deleted: they are that project's record of
    what it printed, listed in ``library_files`` so the UI can say they stay. Nor are
    sliced files, which a queued print may still reference. A Bambuddy delete that
    fails stops here, before the record goes — it is the only pointer to the file.
    """
    meta = require_output(outputs, output_id)
    if delete_inbox_copies and await uploads.for_output(meta.id):
        settings = store.load()
        async with client_for(settings) as client:
            await remove_inbox_copies(client, uploads, meta, settings)
    outputs.delete(output_id)
    # After the files: a failed delete keeps the output, and so must keep its records.
    # Best effort once the files are gone, as for a deleted model: the output is. Each
    # on its own, so a failed upload cleanup cannot leave links serving its archives.
    try:
        await uploads.delete_outputs([output_id])
    except (DatabaseRequiredError, psycopg.Error):
        logger.exception(
            "could not forget a deleted output's Bambuddy uploads", extra={"id": output_id}
        )
    try:
        await links.delete_outputs([output_id])
    except (DatabaseRequiredError, psycopg.Error):
        logger.exception("could not forget a deleted output's print links", extra={"id": output_id})
    emit(events, OutputEvent(kind="output.deleted", output_id=meta.id, slug=meta.slug))
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
        OutputPlate(index=plate.index, has_thumbnail=plate.thumbnail is not None)
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
    summary="Upload the 3MF to the Bambuddy library",
)
async def send_output_to_bambuddy(
    output_id: OutputIdPath,
    body: SendRequest,
    outputs: OutputsDep,
    uploads: UploadsDep,
    store: SettingsStoreDep,
) -> SendResult:
    """Upload ``model.3mf`` to the configured library folder, laid out for the printer
    set in Settings, and note the "Edit in ScadBuddy" link on it.

    Nothing is sliced or queued (#312): printing is ``POST /print/outputs/{id}/run``.
    ``mode`` accepts only ``"library"``.

    The file is read from the PVC and pushed by the server, so the API key never
    reaches the browser. A re-send reuses the copy already in the inbox while it was
    laid out for the same printer, and replaces it otherwise (#316).
    """
    del body  # validated for its ``mode`` alone
    meta = require_output(outputs, output_id)
    settings = store.load()
    async with client_for(settings) as client:
        return await send_output(client, outputs, uploads, meta, settings)


def _cached_defaults(paths: DataPaths, slug: str) -> dict[str, ParamValue | None]:
    """The model's param defaults from its cached schema, or ``{}`` when the renderer
    would not use that cache entry (:func:`load_cached_schema`: another source, cache
    version, schema format or set of library pins).

    Only read: a name hangs on them, so neither openscad nor a library fetch (which may
    clone) is run for them; a pinned checkout missing from the volume means no defaults.
    Every render of the live model caches the schema.
    """
    try:
        source = paths.model_source(slug).read_text(encoding="utf-8")
        schema = load_cached_schema(
            paths.model_schema_cache(slug),
            source_sha256(source),
            library_path=model_search_path(paths, slug),
        )
    except (OSError, ValueError, LibraryError):
        return {}
    if schema is None:
        return {}
    return {param.name: param.initial for param in schema.parameters}


def _output_stem(meta: OutputMeta, outputs: OutputStore, catalogue: Catalogue) -> str:
    params = outputs.params(meta.id)
    try:
        template = catalogue.record(meta.slug).name
    except ModelNotFoundError:
        return project_stem(meta.slug, params, {}, name=meta.name)
    defaults = _cached_defaults(outputs.paths, meta.slug)
    return project_stem(template, params, defaults, name=meta.name)


async def output_stem(meta: OutputMeta, outputs: OutputStore, catalogue: Catalogue) -> str:
    """The name a project file of this output goes by (#317): the template's name and
    the params that differ from its defaults (`project_stem`).

    Naming never fails what it names: when the model, its params or its cached schema
    cannot be read, the name falls back to the slug and the output's own name.
    """
    try:
        return await asyncio.to_thread(_output_stem, meta, outputs, catalogue)
    except Exception:
        logger.warning("could not name the project file; using a plain name", exc_info=True)
        return project_stem(meta.slug, {}, {}, name=meta.name)


@router.post(
    "/outputs/{output_id}/project-file",
    response_model=ProjectFile,
    summary="File this output's 3MF in a project's Bambuddy folder",
)
async def post_project_file(
    output_id: OutputIdPath,
    body: ProjectFileRequest,
    outputs: OutputsDep,
    uploads: UploadsDep,
    store: SettingsStoreDep,
    catalogue: CatalogueDep,
) -> ProjectFile:
    """Upload the editable project 3MF into the project's folder (#317), as Generate does
    when a project is chosen.

    Idempotent per (folder, target): the same project chosen again answers with the file
    already there (``created: false``), and a later print on the same printer reuses it
    (#316). The folder is created and linked if the project has none. Every Bambuddy
    call is made here, so the API key never reaches the browser.
    """
    meta = require_output(outputs, output_id)
    settings = store.load()
    stem = await output_stem(meta, outputs, catalogue)
    async with client_for(settings) as client:
        return await file_into_project(
            client, outputs, uploads, meta, settings, body.project_id, stem=stem
        )
