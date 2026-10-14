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

from scadbuddy.api.deps import CatalogueDep, SlugPath
from scadbuddy.api.media import receive_upload
from scadbuddy.api.models import _require_new, _slug_from_name, require_mine, require_model_exists
from scadbuddy.api.operations import (
    OPERATION_RESPONSES,
    Claimed,
    IdempotencyKey,
    operation_answer,
    recorded,
    run_operation,
)
from scadbuddy.core.problems import ApiError
from scadbuddy.library.catalogue import Catalogue, ModelRecord
from scadbuddy.library.outputs import OUTPUT_ID_PATTERN
from scadbuddy.meshmodel.edits import Edit
from scadbuddy.meshmodel.operations import hand_edited, read_scene
from scadbuddy.meshmodel.scene import Scene
from scadbuddy.operations.claims import ClaimStore, Held
from scadbuddy.operations.component import OperationCommands, OperationsDep

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

    @model_validator(mode="after")
    def _one_source(self) -> MeshModelCreate:
        if (self.library_file_id is None) == (self.output_id is None):
            raise ValueError("name exactly one of library_file_id and output_id")
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
    notes: list[str]
    #: model.scad no longer matches what the scene compiled; edits are refused.
    hand_edited: bool


async def _create(
    ops: OperationCommands,
    response: Response,
    catalogue: Catalogue,
    idempotency_key: str | None,
    *,
    name: str,
    description: str,
    source: dict[str, Any],
    claim: Callable[[ClaimStore], Awaitable[Held]] | None = None,
) -> ModelRecord | JSONResponse:
    """The ``mesh_model_create`` operation. ``claim`` holds an upload's bytes by claim,
    called only when no record answers the request: a repeat holds nothing."""
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


@router.post(
    "/models/from-mesh",
    response_model=ModelRecord,
    status_code=status.HTTP_201_CREATED,
    responses=OPERATION_RESPONSES,
    summary="Open a library file or an output as an editable mesh model",
    description=(
        "Reads the 3MF (or STL) of a Bambuddy library file or an output, writes one mesh "
        "per object and colour into the new model's `meshes/`, and a `scene.json` that "
        "compiles to its `model.scad` (spec 2026-10-11). Nothing is written to Bambuddy. "
        "A sliced file is a 422."
    ),
)
async def create_from_mesh(
    body: MeshModelCreate,
    response: Response,
    ops: OperationsDep,
    catalogue: CatalogueDep,
    idempotency_key: IdempotencyKey = None,
) -> ModelRecord | JSONResponse:
    source: dict[str, Any] = (
        {"kind": "bambuddy_library", "file_id": body.library_file_id}
        if body.library_file_id is not None
        else {"kind": "output", "output_id": body.output_id}
    )
    return await _create(
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

        return await _create(
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
        return SceneView(scene=scene, notes=scene.notes, hand_edited=hand_edited(directory, scene))

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
