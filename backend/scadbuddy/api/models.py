from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, Form, Header, Query, Request, Response, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, ValidationError

from scadbuddy.api.deps import CatalogueDep, ChecksDep, ConfigDep, PathsDep, SlugPath
from scadbuddy.api.limits import ClientGoneError, unless_the_client_leaves
from scadbuddy.core.config import Config
from scadbuddy.core.problems import ApiError
from scadbuddy.library.catalogue import (
    Catalogue,
    ModelExistsError,
    ModelMeta,
    ModelNotFoundError,
    ModelPatch,
    ModelRecord,
)
from scadbuddy.library.scad import (
    CheckedSource,
    NotOpenSCADError,
    SourceCheck,
    check_source,
    decode_source,
    inspect_source,
    parse_diagnostics,
)
from scadbuddy.library.slugs import SLUG_PATTERN, InvalidSlugError, slug_from_filename, slugify
from scadbuddy.render.runner import OpenSCADError, cached_schema
from scadbuddy.render.schema import CustomizerSchema, store_cached_schema

logger = logging.getLogger(__name__)

router = APIRouter(tags=["models"])

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

#: What the multipart branch will read a body from. `text/*` at large is NOT accepted:
#: the route documents `text/plain`, and silently treating `text/html` as OpenSCAD
#: source is a wider contract than anything here promises.
FORM_CONTENT_TYPES = frozenset({"multipart/form-data", "application/x-www-form-urlencoded"})

#: The longest source any of the paste routes will look at. A check spends the one
#: `SCADBUDDY_CHECK_CONCURRENCY` permit for as long as OpenSCAD takes to read what it
#: is given, so an unbounded body is a way to hold that permit against everyone else.
#: 1M characters is far past anything hand-written or generated for this tool, so the
#: cap only ever refuses something that was not going to be a model.
MAX_SOURCE_CHARS = 1_000_000


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


class PastedSource(BaseModel):
    """A model pasted as source rather than uploaded as a file."""

    name: str = Field(description="Display name; its slug is derived from it")
    source: str = Field(max_length=MAX_SOURCE_CHARS, description="The OpenSCAD source")
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    force: bool = Field(default=False, description="Save even when the parse check fails")


class SourceReplacement(BaseModel):
    source: str = Field(max_length=MAX_SOURCE_CHARS, description="The replacement OpenSCAD source")
    force: bool = Field(default=False, description="Save even when the parse check fails")


class CheckRequest(BaseModel):
    source: str = Field(
        max_length=MAX_SOURCE_CHARS, description="The OpenSCAD source to parse-check"
    )
    slug: str | None = Field(
        default=None,
        pattern=SLUG_PATTERN,
        max_length=100,
        description=(
            "An existing model whose directory the source is checked against, so its "
            "`include`/`use` of sibling files resolve as they will on render"
        ),
    )


def _malformed_body(error: Exception) -> ApiError:
    """What FastAPI would have produced had this body gone through a typed parameter.

    The three content types share one route, so the JSON body is parsed by hand — which
    also opts out of `RequestValidationError`, and with it the 422 problem document
    every other body on this API answers with. This puts that back.
    """
    if isinstance(error, ValidationError):
        return ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "the request did not match the expected shape",
            errors=[
                {"loc": ["body", *(str(part) for part in detail["loc"])], "msg": detail["msg"]}
                for detail in error.errors()
            ],
        )
    return ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, "the request body is not valid JSON")


def _rejected(error: NotOpenSCADError, check: SourceCheck | None = None) -> ApiError:
    return ApiError(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        str(error),
        log_tail=error.log_tail,
        diagnostics=[d.model_dump() for d in check.diagnostics] if check else [],
        # Round-tripped so a refusal reads the same as the live check did: a timeout
        # says the source may be slow, not that its syntax is wrong.
        timed_out=check.timed_out if check else False,
    )


async def _guard_source(
    source: str,
    *,
    config: Config,
    force: bool,
    limit: asyncio.Semaphore,
    context: Path | None = None,
) -> CheckedSource | None:
    """Parse-check the source, unless the caller insisted on saving it regardless.

    Returns what the check derived, so the caller can store the schema instead of
    running OpenSCAD a second time to rebuild it.
    """
    if force:
        return None
    checked = await inspect_source(source, config=config, limit=limit, context=context)
    if not checked.check.ok:
        why = (
            "the parse check timed out"
            if checked.check.timed_out
            else "OpenSCAD could not parse the source"
        )
        raise _rejected(NotOpenSCADError(why, checked.check.log_tail), checked.check)
    return checked


@router.post(
    "/models",
    response_model=ModelRecord,
    status_code=status.HTTP_201_CREATED,
    summary="Add a model",
    description=(
        "Three request bodies, one code path. `multipart/form-data` uploads a `.scad` "
        "file (plus an optional thumbnail and README); `application/json` posts "
        "`{name, source}` pasted straight in; `text/plain` posts the bare source and "
        "takes its name from the `X-Model-Name` header. All three derive the slug, "
        "parse-check the source and build the customizer schema identically."
    ),
    openapi_extra={
        "requestBody": {
            "content": {
                "application/json": {"schema": PastedSource.model_json_schema()},
                "text/plain": {"schema": {"type": "string"}},
            }
        }
    },
)
async def create_model(
    request: Request,
    catalogue: CatalogueDep,
    config: ConfigDep,
    checks: ChecksDep,
    file: Annotated[UploadFile | None, File(description="The .scad source")] = None,
    thumbnail: Annotated[UploadFile | None, File(description="Optional PNG")] = None,
    readme: Annotated[UploadFile | None, File(description="Optional README.md")] = None,
    name: Annotated[str | None, Form()] = None,
    description: Annotated[str | None, Form()] = None,
    tags: Annotated[str | None, Form(description="JSON array or comma-separated")] = None,
    model_name: Annotated[
        str | None, Header(alias="X-Model-Name", description="Name for a text/plain paste")
    ] = None,
    force: Annotated[bool, Query(description="Save even when the parse check fails")] = False,
) -> ModelRecord:
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()

    if content_type == "application/json":
        try:
            pasted = PastedSource.model_validate(await request.json())
        except (json.JSONDecodeError, UnicodeDecodeError, ValidationError) as error:
            raise _malformed_body(error) from None
        return await _create(
            catalogue,
            config,
            checks,
            slug=_slug_from_name(pasted.name),
            source=pasted.source,
            meta=ModelMeta(
                name=pasted.name, description=pasted.description, tags=list(pasted.tags)
            ),
            force=pasted.force,
        )

    if content_type == "text/plain":
        if not model_name:
            raise ApiError(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "a text/plain paste needs an X-Model-Name header",
            )
        try:
            pasted_text = decode_source(await request.body())
        except NotOpenSCADError as error:
            raise _rejected(error) from None
        # This branch never reaches `PastedSource`, so the cap the other two get from
        # pydantic has to be applied here by hand.
        if len(pasted_text) > MAX_SOURCE_CHARS:
            raise ApiError(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"the paste is too large: {len(pasted_text)} characters, "
                f"and this route reads at most {MAX_SOURCE_CHARS}",
            )
        return await _create(
            catalogue,
            config,
            checks,
            slug=_slug_from_name(model_name),
            source=pasted_text,
            meta=ModelMeta(name=model_name),
            force=force,
        )

    if content_type not in FORM_CONTENT_TYPES:
        raise ApiError(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            f"{content_type or 'an empty content type'} is not one this route accepts: "
            "multipart/form-data, application/json or text/plain",
        )
    if file is None:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, "the upload needs a file part")
    try:
        slug = slug_from_filename(file.filename or "")
    except InvalidSlugError:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "the upload needs a filename that yields a slug",
        ) from None

    try:
        source = decode_source(await file.read())
    except NotOpenSCADError as error:
        raise _rejected(error) from None

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

    return await _create(
        catalogue,
        config,
        checks,
        slug=slug,
        source=source,
        meta=ModelMeta(
            name=name or slug,
            description=description or "",
            tags=_parse_tags(tags) or [],
        ),
        force=force,
        thumbnail=thumbnail_bytes,
        readme=readme_text,
    )


def _slug_from_name(name: str) -> str:
    try:
        return slugify(name)
    except InvalidSlugError:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"{name!r} does not yield a usable slug"
        ) from None


async def _create(
    catalogue: Catalogue,
    config: Config,
    limit: asyncio.Semaphore,
    *,
    slug: str,
    source: str,
    meta: ModelMeta,
    force: bool,
    thumbnail: bytes | None = None,
    readme: str | None = None,
) -> ModelRecord:
    """The one path every create takes, whatever carried the source in."""
    if catalogue.exists(slug):
        raise ApiError(status.HTTP_409_CONFLICT, f"a model named {slug!r} already exists")
    checked = await _guard_source(source, config=config, force=force, limit=limit)
    try:
        record = catalogue.create(slug, source, meta, thumbnail=thumbnail, readme=readme)
    except ModelExistsError:
        raise ApiError(status.HTTP_409_CONFLICT, f"a model named {slug!r} already exists") from None
    if checked is not None and checked.schema is not None:
        # The check already derived it; storing it here is what stops the first
        # customizer open paying for the same subprocess again.
        store_cached_schema(catalogue.paths.model_meta(slug), checked.schema)
    return record


@router.post(
    "/models/check",
    response_model=SourceCheck,
    summary="Parse-check OpenSCAD source",
    description=(
        "Runs OpenSCAD's customizer-parameter export — the same one the schema is "
        "built from, so a source that passes here is one the customizer can open — "
        "and returns its diagnostics with line numbers. No geometry is rendered and "
        "nothing is saved. `checked` is false when no openscad binary is available, "
        "in which case `ok` says nothing."
    ),
)
async def check_model_source(
    request: Request,
    body: CheckRequest,
    config: ConfigDep,
    checks: ChecksDep,
    catalogue: CatalogueDep,
    paths: PathsDep,
) -> SourceCheck:
    context = paths.model_dir(body.slug) if body.slug and catalogue.exists(body.slug) else None
    try:
        return await unless_the_client_leaves(
            request, check_source(body.source, config=config, limit=checks, context=context)
        )
    except ClientGoneError as error:
        # 499, as nginx writes it: the request was not answered because there was no
        # longer anyone to answer. Nothing reads this body; what matters is that the
        # check was cancelled and gave its permit back.
        raise ApiError(499, str(error)) from None


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


@router.get(
    "/models/{slug}/source",
    response_class=FileResponse,
    responses={200: {"content": {"text/plain": {}}}},
    summary="Raw OpenSCAD source",
)
def get_source(slug: SlugPath, catalogue: CatalogueDep, paths: PathsDep) -> FileResponse:
    require_model(catalogue, slug)
    return FileResponse(paths.model_source(slug), media_type="text/plain; charset=utf-8")


@router.put(
    "/models/{slug}/source",
    response_model=ModelRecord,
    summary="Replace the source",
    description=(
        "Overwrites the source in place and re-derives the customizer schema. The "
        "schema cache is keyed by the source's SHA-256, so the replacement invalidates "
        "it; it is rebuilt here so the next customizer open does not pay for it."
    ),
)
async def put_source(
    slug: SlugPath,
    body: SourceReplacement,
    catalogue: CatalogueDep,
    paths: PathsDep,
    config: ConfigDep,
    checks: ChecksDep,
) -> ModelRecord:
    require_model(catalogue, slug)
    checked = await _guard_source(
        body.source,
        config=config,
        force=body.force,
        limit=checks,
        # The model's own directory, so a replacement that includes a sibling file is
        # checked — and has its schema derived — against the files it will really see.
        context=paths.model_dir(slug),
    )
    await asyncio.to_thread(catalogue.replace_source, slug, body.source)
    if checked is not None and checked.schema is not None:
        store_cached_schema(paths.model_meta(slug), checked.schema)
    else:
        # A forced save, or no openscad at all: nothing was derived to store. The cache
        # is keyed by the source's SHA-256, so the stale entry is already invalid, and
        # GET /schema is where the failure surfaces.
        logger.warning("stored source without a schema", extra={"slug": slug})
    return catalogue.record(slug)


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
    except OpenSCADError as error:
        # Reachable since `force`: a saved-anyway model has source OpenSCAD cannot get
        # through, and the customizer opens straight onto this route. It is the model's
        # problem, not the server's, so it reads like every other rejection here.
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "OpenSCAD could not build a customizer schema from this model's source",
            log_tail=error.log_tail,
            diagnostics=[d.model_dump() for d in parse_diagnostics(error.log_tail)],
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
