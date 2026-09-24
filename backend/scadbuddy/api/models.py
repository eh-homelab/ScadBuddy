from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, File, Form, Response, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel

from scadbuddy.api.deps import CatalogueDep, ConfigDep, PathsDep, SlugPath
from scadbuddy.core.problems import ApiError
from scadbuddy.library.catalogue import (
    Catalogue,
    ModelExistsError,
    ModelMeta,
    ModelNotFoundError,
    ModelPatch,
    ModelRecord,
)
from scadbuddy.library.scad import NotOpenSCADError, decode_source, verify_parses
from scadbuddy.library.slugs import InvalidSlugError, slug_from_filename
from scadbuddy.render.runner import cached_schema
from scadbuddy.render.schema import CustomizerSchema

router = APIRouter(tags=["models"])

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def require_model(catalogue: Catalogue, slug: str) -> ModelRecord:
    try:
        return catalogue.record(slug)
    except ModelNotFoundError:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no model named {slug!r}") from None


def _parse_tags(raw: str | None) -> list[str] | None:
    """Tags arrive as a JSON array or a comma-separated list, whichever the form sends."""
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            raise ApiError(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "tags is not valid JSON"
            ) from None
        if not isinstance(decoded, list):
            raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, "tags must be a list")
        return [str(tag) for tag in decoded]
    return [tag.strip() for tag in text.split(",") if tag.strip()]


@router.get("/models", response_model=list[ModelRecord], summary="List models")
def list_models(catalogue: CatalogueDep) -> list[ModelRecord]:
    return catalogue.list_models()


@router.post(
    "/models",
    response_model=ModelRecord,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a model",
)
async def create_model(
    catalogue: CatalogueDep,
    config: ConfigDep,
    file: Annotated[UploadFile, File(description="The .scad source")],
    thumbnail: Annotated[UploadFile | None, File(description="Optional PNG")] = None,
    readme: Annotated[UploadFile | None, File(description="Optional README.md")] = None,
    name: Annotated[str | None, Form()] = None,
    description: Annotated[str | None, Form()] = None,
    tags: Annotated[str | None, Form(description="JSON array or comma-separated")] = None,
) -> ModelRecord:
    try:
        slug = slug_from_filename(file.filename or "")
    except InvalidSlugError:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "the upload needs a filename that yields a slug",
        ) from None
    if catalogue.exists(slug):
        raise ApiError(status.HTTP_409_CONFLICT, f"a model named {slug!r} already exists")

    try:
        source = decode_source(await file.read())
        await verify_parses(source, config=config)
    except NotOpenSCADError as error:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT, str(error), log_tail=error.log_tail
        ) from None

    thumbnail_bytes: bytes | None = None
    if thumbnail is not None:
        thumbnail_bytes = await thumbnail.read()
        if not thumbnail_bytes.startswith(PNG_MAGIC):
            raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, "the thumbnail is not a PNG")

    readme_text: str | None = None
    if readme is not None:
        try:
            readme_text = decode_source(await readme.read())
        except NotOpenSCADError:
            raise ApiError(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "the README is not UTF-8 text"
            ) from None

    meta = ModelMeta(
        name=name or slug,
        description=description or "",
        tags=_parse_tags(tags) or [],
    )
    try:
        return catalogue.create(slug, source, meta, thumbnail=thumbnail_bytes, readme=readme_text)
    except ModelExistsError:
        raise ApiError(status.HTTP_409_CONFLICT, f"a model named {slug!r} already exists") from None


@router.get("/models/{slug}", response_model=ModelRecord, summary="Model metadata")
def get_model(slug: SlugPath, catalogue: CatalogueDep) -> ModelRecord:
    return require_model(catalogue, slug)


@router.patch("/models/{slug}", response_model=ModelRecord, summary="Edit model metadata")
def patch_model(slug: SlugPath, patch: ModelPatch, catalogue: CatalogueDep) -> ModelRecord:
    require_model(catalogue, slug)
    return catalogue.update(slug, patch)


@router.delete("/models/{slug}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a model")
def delete_model(slug: SlugPath, catalogue: CatalogueDep) -> Response:
    require_model(catalogue, slug)
    catalogue.delete(slug)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


class SourceUpdate(BaseModel):
    source: str
    # What the revision is called in the history. The paste/edit path (#92) passes
    # its own; anything else gets the default.
    message: str | None = None


@router.put(
    "/models/{slug}/source",
    response_model=ModelRecord,
    summary="Replace a model's source as one revision",
)
async def put_source(
    slug: SlugPath,
    body: SourceUpdate,
    catalogue: CatalogueDep,
    config: ConfigDep,
) -> ModelRecord:
    require_model(catalogue, slug)
    try:
        await verify_parses(body.source, config=config)
    except NotOpenSCADError as error:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT, str(error), log_tail=error.log_tail
        ) from None
    return catalogue.write_source(slug, body.source, message=body.message)


@router.get(
    "/models/{slug}/source",
    response_class=FileResponse,
    responses={200: {"content": {"text/plain": {}}}},
    summary="Raw OpenSCAD source",
)
def get_source(slug: SlugPath, catalogue: CatalogueDep, paths: PathsDep) -> FileResponse:
    require_model(catalogue, slug)
    return FileResponse(paths.model_source(slug), media_type="text/plain; charset=utf-8")


@router.get("/models/{slug}/schema", response_model=CustomizerSchema, summary="Customizer schema")
async def get_schema(
    slug: SlugPath, catalogue: CatalogueDep, paths: PathsDep, config: ConfigDep
) -> CustomizerSchema:
    require_model(catalogue, slug)
    try:
        return await cached_schema(paths.model_source(slug), paths.model_meta(slug), config=config)
    except FileNotFoundError:
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE, "openscad is not available to build the schema"
        ) from None


@router.get(
    "/models/{slug}/thumbnail",
    response_class=FileResponse,
    responses={200: {"content": {"image/png": {}}}},
    summary="Model thumbnail",
)
def get_thumbnail(slug: SlugPath, catalogue: CatalogueDep) -> FileResponse:
    require_model(catalogue, slug)
    path = catalogue.thumbnail_path(slug)
    if not path.is_file():
        raise ApiError(status.HTTP_404_NOT_FOUND, f"{slug!r} has no thumbnail")
    return FileResponse(path, media_type="image/png")
