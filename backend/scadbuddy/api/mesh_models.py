"""Mesh models (#2146, spec 2026-10-11): open a 3MF or STL as a model whose scene
compiles to its source, read the scene, edit it, eject it. Every write is an operation
(`meshmodel/operations.py`)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator

from scadbuddy.api.deps import CatalogueDep, SettingsStoreDep, SlugPath
from scadbuddy.api.media import receive_upload
from scadbuddy.api.models import _require_new, _slug_from_name, require_mine, require_model_exists
from scadbuddy.api.operations import (
    KEY_REQUIRED_DETAIL,
    KEY_REQUIRED_PROBLEM,
    OPERATION_RESPONSES,
    Claimed,
    IdempotencyKey,
    answer_recorded,
    operation_answer,
    recorded,
    run_operation,
)
from scadbuddy.bambuddy.client import client_for
from scadbuddy.bambuddy.print_import import (
    SourceChoice,
    SourceFile,
    archive_detail,
    chosen,
    find_sources,
    read_source,
)
from scadbuddy.core.problems import ApiError
from scadbuddy.library.catalogue import Catalogue, ModelRecord
from scadbuddy.library.outputs import OUTPUT_ID_PATTERN
from scadbuddy.meshmodel.edits import Edit
from scadbuddy.meshmodel.operations import hand_edited, read_scene
from scadbuddy.meshmodel.paint import scene_notes
from scadbuddy.meshmodel.scene import Scene
from scadbuddy.operations.claims import ClaimStore, Held
from scadbuddy.operations.component import OperationCommands, OperationsDep
from scadbuddy.operations.store import Operation

router = APIRouter(tags=["mesh models"])

ACCEPTED_SUFFIXES = (".3mf", ".stl")
MAX_NAME_CHARS = 200
#: The upload's ``name`` part: at most `MAX_NAME_CHARS` characters, 4 bytes each.
MAX_NAME_BYTES = MAX_NAME_CHARS * 4
MAX_DESCRIPTION_CHARS = 10_000


class MeshModelCreate(BaseModel):
    name: str = Field(min_length=1, max_length=MAX_NAME_CHARS)
    description: str = Field(default="", max_length=MAX_DESCRIPTION_CHARS)
    library_file_id: int | None = Field(default=None, ge=1)
    output_id: str | None = Field(default=None, pattern=OUTPUT_ID_PATTERN)
    archive_id: int | None = Field(
        default=None,
        ge=1,
        description="A Bambuddy archive whose file opens (#6916); `archive_source` names which",
    )
    archive_source: SourceChoice | None = Field(
        default=None,
        description=(
            "Where the archive's 3MF is read from, one that `GET /print-imports/{archive_id}"
            "/sources` lists as available: the archive's file, its slicer project, a library "
            "file, or a file on the printer's storage"
        ),
    )

    @model_validator(mode="after")
    def _one_source(self) -> MeshModelCreate:
        if (self.archive_id is None) != (self.archive_source is None):
            raise ValueError("archive_id and archive_source go together")
        named = [self.library_file_id, self.output_id, self.archive_id]
        if sum(source is not None for source in named) != 1:
            raise ValueError("name exactly one of library_file_id, output_id and archive_id")
        return self


class MeshUpload(BaseModel):
    """The multipart body of ``POST /models/from-mesh/upload``."""

    file: bytes = Field(description="The mesh: a .3mf or an .stl, by its file name")
    name: str | None = Field(
        default=None,
        max_length=MAX_NAME_CHARS,
        description="The model's name; the file's name without its extension when omitted",
    )
    description: str | None = Field(default=None, max_length=MAX_DESCRIPTION_CHARS)


class SceneEdits(BaseModel):
    edits: list[Edit] = Field(min_length=1, max_length=50)
    drop_from_presets: bool = False
    message: str | None = Field(default=None, max_length=200)


class SceneView(BaseModel):
    scene: Scene
    #: What reading the file dropped, and where a painted object's painting is not
    #: carried whole (meshmodel/paint.py `scene_notes`).
    notes: list[str]
    #: model.scad no longer matches what the scene compiled; edits are refused.
    hand_edited: bool


async def create_mesh_model(
    ops: OperationCommands,
    response: Response,
    catalogue: Catalogue,
    idempotency_key: str | None,
    *,
    name: str,
    description: str,
    source: dict[str, Any],
    claim: Callable[[ClaimStore], Awaitable[Held]] | None = None,
    changed: Callable[[Operation, bool, bool], str] | None = None,
) -> ModelRecord | JSONResponse:
    """The ``mesh_model_create`` operation. ``claim`` holds an upload's bytes by claim,
    called only when no record answers the request: a repeat holds nothing. With
    ``changed``, a key on record for another body of this model is a 409 whose text is
    ``changed(first, made, same_file)``: ``first`` the operation that key started (it may
    have succeeded, failed or still be running), ``made`` whether the model exists now (it
    may have been deleted since), ``same_file`` whether that request named this print file,
    name and description, so only the file's bytes moved (#5979; #7019). Otherwise the key
    was first sent for another body, or before the request recorded its print file."""
    slug = _slug_from_name(name)
    request = {"slug": slug, "name": name, "description": description, "source": source}
    kind = ops.kinds["mesh_model_create"]
    claims = ClaimStore(catalogue.paths.claims)
    held: list[Held] = []
    if (
        await recorded(
            ops, kind=kind, subject=slug, request=request, idempotency_key=idempotency_key
        )
        is None
    ):
        first = (
            await ops.store.keyed_operation(kind.name, slug, idempotency_key)
            if changed is not None and idempotency_key is not None
            else None
        )
        if changed is not None and first is not None:
            assert idempotency_key is not None
            same_file = "origin" in source and (
                await ops.store.keyed_operation(
                    kind.name,
                    slug,
                    idempotency_key,
                    sent={
                        "name": name,
                        "description": description,
                        "source": {"origin": source["origin"]},
                    },
                )
                is not None
            )
            raise ApiError(
                status.HTTP_409_CONFLICT,
                changed(first, catalogue.exists(slug), same_file),
                operation_id=first.id,
            )
        _require_new(catalogue, slug)
        if claim is not None:
            held.append(await claim(claims))
    result = await run_operation(
        ops,
        response,
        kind=kind,
        subject=slug,
        request=request,
        idempotency_key=idempotency_key,
        claimed=Claimed(claims, held),
    )
    return operation_answer(result, ModelRecord)


def print_file_origin(archive_id: int, choice: SourceChoice) -> dict[str, Any]:
    """Which print's file a model is made from (#6984): kept in the operation's request,
    so a resend that names another print or source is not answered the first model. One
    shape for both routes that read a print's file (from-mesh and Import print's new
    model), so a key sent to both with one name, description and source is one resend."""
    return {"archive_id": archive_id, "source": choice.model_dump(mode="json")}


async def made_before(
    ops: OperationCommands,
    response: Response,
    idempotency_key: str,
    *,
    name: str,
    description: str,
    origin: dict[str, Any],
) -> ModelRecord | None:
    """The model a finished ``mesh_model_create`` sent with ``idempotency_key`` made for
    ``name``, or its problem raised (#6969). A route that reads a print's file asks this
    first: a resend after a lost answer then gets the first send's answer, the printer is
    not read again, and a file deleted since is no 404. None when no such operation
    finished, or when it was sent with another name, description or ``origin``
    (#6984): the route reads the file, and `create_mesh_model` answers the rest."""
    first = await ops.store.keyed_operation(
        ops.kinds["mesh_model_create"].name,
        _slug_from_name(name),
        idempotency_key,
        sent={"name": name, "description": description, "source": {"origin": origin}},
    )
    if first is None or first.status == "running":
        return None
    made = operation_answer(answer_recorded(first, response), ModelRecord)
    assert isinstance(made, ModelRecord)  # a finished operation is never a 202
    return made


async def create_from_read(
    ops: OperationCommands,
    response: Response,
    catalogue: Catalogue,
    idempotency_key: str | None,
    *,
    name: str,
    description: str,
    read: SourceFile,
    origin: dict[str, Any],
    changed: Callable[[Operation, bool, bool], str],
) -> ModelRecord | JSONResponse:
    """`create_mesh_model` from a print's file the route read (Import print's new model,
    #4698, and a print's file here, #6916): its bytes held by claim, as an upload's are.
    ``origin`` (`print_file_origin`) names the print and source in the request."""

    async def claim(claims: ClaimStore) -> Held:
        return await asyncio.to_thread(claims.hold, read.data)

    return await create_mesh_model(
        ops,
        response,
        catalogue,
        idempotency_key,
        name=name,
        description=description,
        source={"kind": "upload", "claim": read.sha256, "name": read.name, "origin": origin},
        claim=claim,
        changed=changed,
    )


def key_sent_before(name: str, same_file: bool) -> str:
    """Why a key on record answers another body (#5979, #6984, #7019): the print file's
    bytes changed since (a printer overwrites its files), or the key was first sent for
    another body, or before the request recorded which print file it names."""
    if same_file:
        return f"{name} changed since this request was first sent"
    return (
        "this Idempotency-Key was first sent for another print file, name or description,"
        " or before this server recorded which print file a request names"
    )


def _print_file_changed(name: str, slug: str, first: Operation, same_file: bool) -> str:
    """The 409 of a key on record for another body of this model (#5979, #6984)."""
    why = key_sent_before(name, same_file)
    if first.status == "running":
        return (
            f"{why}; the model {slug!r} that request started is still being made"
            f" (operation {first.id}, `GET /api/v1/operations/{first.id}`)"
        )
    return f"{why} (operation {first.id}): send the request with a new Idempotency-Key"


async def _read_print_file(
    store: SettingsStoreDep, archive_id: int, choice: SourceChoice
) -> SourceFile:
    """The bytes of the print's file at ``choice``, read as Import print reads them
    (`find_sources`, `chosen`, `read_source`). A sliced file is a 422 before it is read."""
    async with client_for(store.load()) as client:
        archive = await archive_detail(client, archive_id)
        sources = await find_sources(client, archive, printer=choice.kind == "printer")
        source = chosen(sources, choice)
        if source.sliced:
            raise ApiError(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"{source.name} is a sliced file, which has no meshes to open as a model:"
                " choose the archive's slicer project or an unsliced 3MF",
            )
        return await read_source(client, archive, source)


@router.post(
    "/models/from-mesh",
    response_model=ModelRecord,
    status_code=status.HTTP_201_CREATED,
    responses=OPERATION_RESPONSES,
    summary="Open a library file, an output or a print's file as an editable mesh model",
    description=(
        "Reads the 3MF (or STL) of a Bambuddy library file, an output, or a print's file "
        "(`archive_id` with `archive_source`, #6916: the archive's file, its slicer project, "
        "a library file or the printer's storage, as Import print reads it; the printer is "
        "reached only for a `printer` source), writes one mesh per object and colour into "
        "the new model's `meshes/`, and a `scene.json` that compiles to its `model.scad` "
        "(spec 2026-10-11). Nothing is written to Bambuddy, and the model is no print of "
        "the archive (Import print links one). A sliced file is a 422. A print's file needs "
        "an `Idempotency-Key` (428 without one); a resend of the same body under "
        "the key of a finished create answers what that create answered, and reads nothing "
        "again. The key sent with another description or print file, or a name of the "
        "same slug, is a 409; a name of another slug makes another model."
    ),
)
async def create_from_mesh(
    body: MeshModelCreate,
    response: Response,
    ops: OperationsDep,
    catalogue: CatalogueDep,
    store: SettingsStoreDep,
    idempotency_key: IdempotencyKey = None,
) -> ModelRecord | JSONResponse:
    if body.archive_id is not None and body.archive_source is not None:
        if idempotency_key is None:
            raise ApiError(
                status.HTTP_428_PRECONDITION_REQUIRED,
                KEY_REQUIRED_DETAIL,
                type_=KEY_REQUIRED_PROBLEM,
            )
        origin = print_file_origin(body.archive_id, body.archive_source)
        made = await made_before(
            ops,
            response,
            idempotency_key,
            name=body.name,
            description=body.description,
            origin=origin,
        )
        if made is not None:
            return made
        read = await _read_print_file(store, body.archive_id, body.archive_source)
        slug = _slug_from_name(body.name)
        return await create_from_read(
            ops,
            response,
            catalogue,
            idempotency_key,
            name=body.name,
            description=body.description,
            read=read,
            origin=origin,
            # A retry reads the file again: other bytes are named, never opened.
            changed=lambda first, _made, same_file: _print_file_changed(
                read.name, slug, first, same_file
            ),
        )
    source: dict[str, Any] = (
        {"kind": "bambuddy_library", "file_id": body.library_file_id}
        if body.library_file_id is not None
        else {"kind": "output", "output_id": body.output_id}
    )
    return await create_mesh_model(
        ops,
        response,
        catalogue,
        idempotency_key,
        name=body.name,
        description=body.description,
        source=source,
    )


@router.post(
    "/models/from-mesh/upload",
    response_model=ModelRecord,
    status_code=status.HTTP_201_CREATED,
    responses={415: {"description": "Not a .3mf or an .stl"}, **OPERATION_RESPONSES},
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {"schema": {"$ref": "#/components/schemas/MeshUpload"}}
            },
        }
    },
    summary="Open an uploaded 3MF or STL as an editable mesh model",
    description=(
        "As `POST /models/from-mesh`, from an uploaded file: its type is its name's "
        "extension. The body may be as large as the library download cap (256 MiB)."
    ),
)
async def create_from_upload(
    request: Request,
    response: Response,
    ops: OperationsDep,
    catalogue: CatalogueDep,
    idempotency_key: IdempotencyKey = None,
) -> ModelRecord | JSONResponse:
    received = await receive_upload(
        request,
        catalogue.paths.cache,
        fields={"name": MAX_NAME_BYTES, "description": MAX_DESCRIPTION_CHARS * 4},
    )
    try:
        path = received.files.get("file")
        if path is None or received.sizes["file"] == 0:
            raise ApiError(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "send the mesh as the `file` part"
            )
        filename = received.filenames["file"]
        if not filename.lower().endswith(ACCEPTED_SUFFIXES):
            raise ApiError(
                status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "a mesh model opens a .3mf or an .stl"
            )
        try:
            name = received.fields.get("name", bytearray()).decode("utf-8").strip()
            description = received.fields.get("description", bytearray()).decode("utf-8")
        except UnicodeDecodeError:
            raise ApiError(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "the name or description is not UTF-8"
            ) from None
        name = name or filename.rsplit(".", 1)[0]
        if len(name) > MAX_NAME_CHARS:
            raise ApiError(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"a name is at most {MAX_NAME_CHARS} characters",
            )
        if len(description) > MAX_DESCRIPTION_CHARS:
            raise ApiError(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"a description is at most {MAX_DESCRIPTION_CHARS:,} characters",
            )
        digest = received.hashes["file"].hexdigest()

        async def claim(claims: ClaimStore) -> Held:
            return await asyncio.to_thread(claims.hold_file, path, digest)

        return await create_mesh_model(
            ops,
            response,
            catalogue,
            idempotency_key,
            name=name,
            description=description,
            source={"kind": "upload", "claim": digest, "name": filename},
            claim=claim,
        )
    finally:
        # What was not claimed: all of it for a repeat or a refusal.
        received.discard()


@router.get(
    "/models/{slug}/scene",
    response_model=SceneView,
    responses={
        404: {"description": "No such model, or it is not a mesh model"},
        422: {"description": "Its scene.json is not a valid scene: eject it or restore a version"},
    },
    summary="A mesh model's scene",
)
async def get_scene(slug: SlugPath, catalogue: CatalogueDep) -> SceneView:
    require_model_exists(catalogue, slug)
    directory = catalogue.paths.model_dir(slug)

    def read() -> SceneView:
        scene = read_scene(directory, slug)
        return SceneView(
            scene=scene, notes=scene_notes(scene), hand_edited=hand_edited(directory, scene)
        )

    return await asyncio.to_thread(read)


@router.post(
    "/models/{slug}/scene/edits",
    response_model=ModelRecord,
    responses=OPERATION_RESPONSES,
    summary="Edit a mesh model's scene as one revision",
    description=(
        "Applies the edits in order (spec 2026-10-11 §5). 409 when model.scad was edited "
        "by hand, or when the edits remove a parameter a preset sets (unless "
        "`drop_from_presets`); 422 for an edit the scene refuses."
    ),
)
async def edit_scene(
    slug: SlugPath,
    body: SceneEdits,
    response: Response,
    ops: OperationsDep,
    idempotency_key: IdempotencyKey = None,
) -> ModelRecord | JSONResponse:
    require_mine(slug)
    request = {
        "slug": slug,
        "edits": [edit.model_dump(mode="json") for edit in body.edits],
        "drop_from_presets": body.drop_from_presets,
        "message": body.message or "Edit the scene",
    }
    result = await run_operation(
        ops,
        response,
        kind=ops.kinds["scene_edit"],
        subject=slug,
        request=request,
        idempotency_key=idempotency_key,
    )
    return operation_answer(result, ModelRecord)


@router.post(
    "/models/{slug}/scene/eject",
    response_model=ModelRecord,
    responses=OPERATION_RESPONSES,
    summary="Stop editing a mesh model as a scene",
    description=(
        "Removes scene.json; model.scad and the meshes stay, and the source is edited as "
        "source from then on. The model keeps its `mesh_source`."
    ),
)
async def eject_scene(
    slug: SlugPath,
    response: Response,
    ops: OperationsDep,
    idempotency_key: IdempotencyKey = None,
) -> ModelRecord | JSONResponse:
    require_mine(slug)
    result = await run_operation(
        ops,
        response,
        kind=ops.kinds["scene_eject"],
        subject=slug,
        request={"slug": slug},
        idempotency_key=idempotency_key,
    )
    return operation_answer(result, ModelRecord)
