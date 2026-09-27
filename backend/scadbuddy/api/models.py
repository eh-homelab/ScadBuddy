from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import replace
from pathlib import Path
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    FastAPI,
    File,
    Form,
    Header,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, ValidationError

from scadbuddy.api.deps import (
    CatalogueDep,
    ChecksDep,
    ConfigDep,
    HistoryDep,
    PathsDep,
    PresetsDep,
    QueueDep,
    SlugPath,
)
from scadbuddy.api.limits import MAX_TEXT_BODY_BYTES, ClientGoneError, unless_the_client_leaves
from scadbuddy.core.config import Config
from scadbuddy.core.paths import is_builtin
from scadbuddy.core.problems import ApiError, problem_response
from scadbuddy.library.catalogue import (
    Catalogue,
    InvalidModelMetaError,
    ModelExistsError,
    ModelMeta,
    ModelNotFoundError,
    ModelPatch,
    ModelRecord,
    SidecarNotFoundError,
    meta_from_raw,
)
from scadbuddy.library.history import (
    COMMIT_ID_PATTERN,
    MAX_SUBJECT,
    GitError,
    GitUnavailableError,
)
from scadbuddy.library.libraries import (
    LibraryDeclarationError,
    ModelLibrary,
    model_search_path,
    parse_declaration,
    search_path,
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
from scadbuddy.library.slugs import (
    MAX_MODEL_ID_LENGTH,
    MODEL_ID_PATTERN,
    InvalidSlugError,
    slug_from_filename,
    slugify,
)
from scadbuddy.library.upstream import (
    InvalidMergeBaseError,
    NoUpstreamError,
    has_conflict_markers,
)
from scadbuddy.library.url_import import (
    IMPORT_TIMEOUT,
    ImportRefusedError,
    fetch_model,
)
from scadbuddy.render.jobs import resolve_source
from scadbuddy.render.runner import OpenSCADError, cached_schema
from scadbuddy.render.schema import CustomizerSchema, store_cached_schema

logger = logging.getLogger(__name__)

router = APIRouter(tags=["models"])

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

#: The largest thumbnail a model takes (#179). Every set is a commit in the models
#: repository, which keeps each one for good, so this is what stops that history
#: growing without bound -- the multipart cap (32 MiB) alone would not. 10 MiB leaves
#: room for a high-fidelity image of the user's own: on a self-hosted volume, space
#: and bandwidth are not the constraint, only an unbounded history is. (The bundled
#: thumbnails are 30 KB at most, and a plate image this server renders is 512x512.)
MAX_THUMBNAIL_BYTES = 10 * 1024 * 1024


def _mib(size: int) -> str:
    """``size`` bytes in MiB, for a message: `2 MiB`, `1.5 MiB`."""
    return f"{size / (1024 * 1024):g} MiB"


#: The thumbnail limit as people read it, derived so no message can drift from it.
MAX_THUMBNAIL_SIZE = _mib(MAX_THUMBNAIL_BYTES)

#: The cap on a multipart create's `meta` part, the model's model.json. It is small
#: metadata -- name, description, tags, libraries, attribution -- and the largest
#: bundled one is about 1 KiB, so 64 KiB leaves room for a long description while
#: refusing a part large enough to make decoding and parsing it cost real time.
MAX_META_BYTES = 64 * 1024


def _kib(size: int) -> str:
    """``size`` bytes in KiB, for a message: `64 KiB`."""
    return f"{size / 1024:g} KiB"


#: The model.json limit as people read it, derived like the thumbnail's.
MAX_META_SIZE = _kib(MAX_META_BYTES)

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


def require_model_exists(catalogue: Catalogue, slug: str) -> None:
    """Existence without building a record -- which costs a `git log` for the
    model's revision. The render path runs on every debounced keystroke and only
    needs the 404."""
    if not catalogue.exists(slug):
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no model named {slug!r}")


def require_mine(slug: str) -> None:
    """A built-in is the image's, and only the boot sync writes it (#155)."""
    if is_builtin(slug):
        raise ApiError(
            status.HTTP_403_FORBIDDEN, f"{slug!r} is a built-in template and is read-only"
        )


#: What a hand-parsed client JSON can raise. `RecursionError` too: `json.loads`
#: recurses per level of nesting, so `[` * 100000 is a stack overflow rather than
#: a decode error -- a bare 500 unless it is caught like any other bad JSON.
_BAD_JSON = (UnicodeDecodeError, ValueError, RecursionError)


def _client_json(text: str | bytes, refusal: str) -> Any:
    """JSON a client sent in a form field or part, or a 422 saying ``refusal``.

    `ValueError` covers `json.JSONDecodeError`, which subclasses it.
    """
    try:
        return json.loads(text)
    except _BAD_JSON:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, refusal) from None


def _parse_tags(raw: str | None) -> list[str] | None:
    """Tags arrive as a JSON array or a comma-separated list, whichever the form sends.

    None when the field is missing or blank, so it falls through to the model.json
    (#179); an explicit `[]` is still an empty list.
    """
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    if text.startswith("["):
        decoded = _client_json(text, "tags is not valid JSON")
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


class SourceUpdate(BaseModel):
    source: str = Field(max_length=MAX_SOURCE_CHARS, description="The replacement OpenSCAD source")
    force: bool = Field(default=False, description="Save even when the parse check fails")
    # What the revision is called in the history. Flattened to one printable line
    # before it reaches git -- see `history.subject_line`.
    message: str | None = Field(
        default=None,
        max_length=MAX_SUBJECT,
        description="What the revision is called in the history; a default when omitted",
    )


class ReadmeUpdate(BaseModel):
    content: str = Field(max_length=MAX_SOURCE_CHARS, description="The README, as Markdown text")


class CheckRequest(BaseModel):
    source: str = Field(
        max_length=MAX_SOURCE_CHARS, description="The OpenSCAD source to parse-check"
    )
    slug: str | None = Field(
        default=None,
        pattern=MODEL_ID_PATTERN,
        max_length=MAX_MODEL_ID_LENGTH,
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

    A NUL is refused first, and `force` does not bypass it: the multipart branch's
    `decode_source` already rejects binary, and pasted text must not be the way a
    binary blob gets into the models repository, where it breaks the diff route.
    """
    if "\x00" in source:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "the source contains a NUL byte, so it is binary, not OpenSCAD text",
        )
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
        "file (plus an optional thumbnail, README and `model.json`, the layout of a "
        "bundled model's directory); `application/json` posts "
        "`{name, source}` pasted straight in; `text/plain` posts the bare source and "
        "takes its name from the `X-Model-Name` header. All three derive the slug, "
        "parse-check the source and build the customizer schema identically."
    ),
    openapi_extra={
        "requestBody": {
            "content": {
                # Named in `components` by `create_app`, so the client gets a type for it.
                "application/json": {"schema": {"$ref": "#/components/schemas/PastedSource"}},
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
    file: Annotated[
        UploadFile | None,
        File(description=f"The .scad source, at most {MAX_SOURCE_CHARS:,} characters"),
    ] = None,
    thumbnail: Annotated[
        UploadFile | None, File(description=f"Optional PNG, at most {MAX_THUMBNAIL_SIZE}")
    ] = None,
    readme: Annotated[UploadFile | None, File(description="Optional README.md")] = None,
    meta: Annotated[
        UploadFile | None,
        File(
            description=(
                f"Optional model.json, at most {MAX_META_SIZE}. A non-blank name, "
                "description or tags form field wins over it; a missing or blank one "
                "falls through to it"
            )
        ),
    ] = None,
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
        except (*_BAD_JSON, ValidationError) as error:
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
            # Either spelling forces, as the design and the OpenAPI both promise.
            force=force or pasted.force,
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
        _require_within_cap(pasted_text, "the paste")
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
    except InvalidSlugError as exc:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"the upload needs a filename that yields a slug: {exc}",
        ) from None

    try:
        source = decode_source(await file.read())
    except NotOpenSCADError as error:
        raise _rejected(error) from None
    # The cap the JSON and text/plain branches hold a source to, before the parse
    # check spends an openscad run -- and a check permit -- on it.
    _require_within_cap(source, "the source")

    thumbnail_bytes: bytes | None = None
    if thumbnail is not None:
        thumbnail_bytes = _require_png(await thumbnail.read())

    readme_text: str | None = None
    if readme is not None:
        readme_text = _readme_text(await readme.read())
        # The cap `PUT /readme` holds it to, so what is created can be saved again.
        _require_within_cap(readme_text, "the README")

    # What a bundled model's `model.json` says, so a dropped `models/<slug>/`
    # directory lands with the same metadata its bundled built-in has.
    base = await _read_meta_part(meta, slug) if meta is not None else ModelMeta(name=slug)
    parsed_tags = _parse_tags(tags)

    return await _create(
        catalogue,
        config,
        checks,
        slug=slug,
        source=source,
        # The model.json's `source` attribution carries over. Its `origin_url` never
        # does: that is set only by `POST /models/import`, which fetched the URL over
        # https itself, and the catalogue renders it as a link -- taken from an
        # uploaded file it would be a stored `javascript:` link waiting for a click.
        meta=base.model_copy(
            update={
                "name": _first_name(name, base.name, slug),
                # Blank is absent, as for the name; a non-blank one is kept as given.
                "description": description
                if description is not None and description.strip()
                else base.description,
                "tags": parsed_tags if parsed_tags is not None else base.tags,
                "origin_url": None,
                # Nor `upstream` (#156): only `POST /models/{slug}/duplicate` records
                # which template this one came from.
                "upstream": None,
            }
        ),
        force=force,
        thumbnail=thumbnail_bytes,
        readme=readme_text,
    )


def _first_name(*candidates: str | None) -> str:
    """The first candidate that is not blank, stripped. The last one is the slug,
    which never is, so a blank form field or model.json name can never name a model."""
    for candidate in candidates:
        if candidate is not None and candidate.strip():
            return candidate.strip()
    raise ValueError("every name candidate is blank")


def _require_pins(libraries: Any) -> None:
    """A dropped model.json's ``libraries`` (#93, #179): pins, as ScadBuddy writes
    them, or nothing. Checked here because the metadata itself reads them leniently
    -- a model on disk must still list -- and an upload must not lose one quietly:
    OpenSCAD only WARNs on a missing ``use``. A bare name, from before pins moved
    into each model, has nothing left to resolve it against."""
    try:
        declared = parse_declaration({"libraries": libraries})
    except LibraryDeclarationError as error:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
    names = [entry for entry in declared if isinstance(entry, str)]
    if names:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"the model.json names libraries without a pin: {', '.join(names)}; "
            "pin them to the model once it is created",
            libraries=names,
        )


def _require_png(payload: bytes) -> bytes:
    """A model thumbnail, on create and on PUT alike: a PNG, and no larger than
    `MAX_THUMBNAIL_BYTES`. Checked before anything is written."""
    if not payload.startswith(PNG_MAGIC):
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, "the thumbnail is not a PNG")
    if len(payload) > MAX_THUMBNAIL_BYTES:
        # A 422 with the limit, as the source and README caps answer.
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"the thumbnail is too large: {len(payload)} bytes, "
            f"and a thumbnail is at most {MAX_THUMBNAIL_BYTES} bytes ({MAX_THUMBNAIL_SIZE})",
        )
    return payload


def _readme_text(payload: bytes) -> str:
    try:
        return decode_source(payload)
    except NotOpenSCADError:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "the README is not UTF-8 text"
        ) from None


async def _read_meta_part(meta: UploadFile, slug: str) -> ModelMeta:
    """A ``model.json`` part, held to `MAX_META_BYTES` before any of it is decoded.

    Reads at most one byte past the cap, so an oversized part is never pulled into
    memory whole; the decode and parse then run off the event loop, as JSON of any
    size is CPU work this handler must not stall every other request behind.
    """
    payload = await meta.read(MAX_META_BYTES + 1)
    if len(payload) > MAX_META_BYTES:
        size = meta.size if meta.size is not None else len(payload)
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"the model.json is too large: {size} bytes, "
            f"and a model.json is at most {MAX_META_BYTES} bytes ({MAX_META_SIZE})",
        )
    return await asyncio.to_thread(_read_meta_file, payload, slug)


def _read_meta_file(payload: bytes, slug: str) -> ModelMeta:
    """A ``model.json`` part, read the way the catalogue reads one from disk."""
    refusal = "the model.json is not valid JSON"
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, refusal) from None
    raw = _client_json(text, refusal)
    if not isinstance(raw, dict):
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, "the model.json is not an object")
    if raw.get("libraries") is not None:
        _require_pins(raw["libraries"])
    # Read as the catalogue reads one on disk: a `null` for a defaulted field is
    # the field left out, so the name falls through to the slug.
    try:
        return meta_from_raw(raw, slug)
    except ValidationError as error:
        raise _malformed_body(error) from None
    except RecursionError:
        # Nesting shallow enough to parse can still be too deep to validate.
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, refusal) from None


def _require_within_cap(source: str, what: str) -> None:
    if len(source) > MAX_SOURCE_CHARS:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"{what} is too large: {len(source)} characters, "
            f"and this route reads at most {MAX_SOURCE_CHARS}",
        )


def _slug_from_name(name: str) -> str:
    try:
        return slugify(name)
    except InvalidSlugError as exc:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None


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
    # The pins a dropped model.json carries (#93) are on the parse check's
    # OPENSCADPATH, as they will be on every render; none for any other create. A
    # pin whose checkout is not on this volume is the 409 every render would be.
    # One entry per name: the first, as `use <NAME/...>` can only mean one.
    pins: list[ModelLibrary] = []
    for library in meta.libraries:
        if all(pin.name != library.name for pin in pins):
            pins.append(library)
    meta = meta.model_copy(update={"libraries": pins})
    library_path: tuple[Path, ...] = ()
    if pins:
        # A directory check per pin: off the event loop.
        library_path = await asyncio.to_thread(search_path, catalogue.paths, pins)
    checked = await _guard_source(
        source, config=replace(config, library_path=library_path), force=force, limit=limit
    )
    try:
        # `to_thread`, because a create is a `git add` + `git commit` against the
        # PVC and this handler is `async def` -- FastAPI only offloads plain `def`
        # ones. On the event loop it would stall every render poll and /healthz
        # for the length of the commit. Same at every git-touching call below.
        record = await asyncio.to_thread(
            catalogue.create, slug, source, meta, thumbnail=thumbnail, readme=readme
        )
    except ModelExistsError:
        raise ApiError(status.HTTP_409_CONFLICT, f"a model named {slug!r} already exists") from None
    if checked is not None and checked.schema is not None:
        # The check already derived it; storing it here is what stops the first
        # customizer open paying for the same subprocess again.
        store_cached_schema(
            catalogue.paths.model_schema_cache(slug), checked.schema, library_path=library_path
        )
    return record


class UrlImport(BaseModel):
    url: str = Field(max_length=2048, description="An https URL to the model's source")
    name: str | None = Field(
        default=None, description="Display name; taken from the URL's file name when omitted"
    )
    force: bool = Field(default=False, description="Save even when the parse check fails")


@router.post(
    "/models/import",
    response_model=ModelRecord,
    status_code=status.HTTP_201_CREATED,
    summary="Import a model from a URL",
    description=(
        "Fetches the source on the server -- https only, from public internet addresses "
        f"only, at most {MAX_TEXT_BODY_BYTES} bytes, within {IMPORT_TIMEOUT:.0f} seconds "
        "-- then creates the model exactly as a paste does, recording the URL as "
        "`origin_url`. A direct link to the file works; a MakerWorld model page is "
        "refused, because MakerWorld only serves files to a signed-in account. Every "
        "refusal is a 422, and an address that is not public reads the same as one that "
        "did not answer."
    ),
)
async def import_model(
    body: UrlImport,
    catalogue: CatalogueDep,
    config: ConfigDep,
    checks: ChecksDep,
) -> ModelRecord:
    try:
        imported = await fetch_model(body.url, limit=MAX_TEXT_BODY_BYTES)
    except ImportRefusedError as error:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
    _require_within_cap(imported.source, "the imported file")
    name = body.name or imported.name
    return await _create(
        catalogue,
        config,
        checks,
        slug=_slug_from_name(name),
        source=imported.source,
        meta=ModelMeta(name=name, origin_url=imported.origin_url),
        force=body.force,
    )


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
    if body.slug and context is not None:
        # The model's own libraries, as its render will see them (#93).
        # Off the loop, like every other read of the PVC from an `async def`.
        library_path = await asyncio.to_thread(model_search_path, paths, body.slug)
        config = replace(config, library_path=library_path)
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
    require_mine(slug)
    require_model(catalogue, slug)
    try:
        return catalogue.update(slug, patch)
    except ModelNotFoundError:
        # A concurrent delete of the same slug got there first.
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no model named {slug!r}") from None


class DuplicateRequest(BaseModel):
    name: str = Field(description="Display name of the duplicate; its slug is derived from it")


@router.post(
    "/models/{slug}/duplicate",
    response_model=ModelRecord,
    status_code=status.HTTP_201_CREATED,
    summary="Duplicate a template",
    description=(
        "Copies any template, built-in or mine, to a new template of mine whose slug is "
        "derived from `name` as `POST /models` derives it, and records the template it "
        "came from as `upstream`, with `base` the upstream's current revision. One "
        "revision: `Duplicate <id> as <new slug>`. Derived files (schema cache, "
        "outputs, revisions) are not copied; the presets saved on it are."
    ),
)
def duplicate_model(
    slug: SlugPath, body: DuplicateRequest, catalogue: CatalogueDep, presets: PresetsDep
) -> ModelRecord:
    require_model_exists(catalogue, slug)
    new_slug = _slug_from_name(body.name)
    try:
        record = catalogue.duplicate(slug, new_slug, body.name)
    except ModelExistsError:
        raise ApiError(
            status.HTTP_409_CONFLICT, f"a model named {new_slug!r} already exists"
        ) from None
    except ModelNotFoundError:
        # A concurrent delete of the upstream got there first.
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no model named {slug!r}") from None
    except GitError as error:
        # Reading the upstream at `base` failed; as every other route that reads
        # the history maps it. Nothing of the duplicate is left behind.
        raise ApiError(status.HTTP_500_INTERNAL_SERVER_ERROR, str(error)) from None
    # The presets saved on the upstream come along. Best effort: the duplicate
    # exists by now, and failing it over its presets would report a copy that was
    # made as one that was not.
    try:
        presets.copy(slug, new_slug)
    except (OSError, ValueError):
        logger.exception("could not copy presets to a duplicate", extra={"slug": new_slug})
    return record


@router.delete(
    "/models/{slug}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a model",
    description=(
        "409 while templates of mine are duplicates of it, with how many as "
        "`duplicates` (and which, as `slugs`); `?force=true` deletes it anyway, and "
        "they report their upstream as `gone`."
    ),
)
def delete_model(
    slug: SlugPath,
    catalogue: CatalogueDep,
    queue: QueueDep,
    force: Annotated[
        bool, Query(description="Delete even when duplicates track this template")
    ] = False,
) -> Response:
    require_mine(slug)
    require_model_exists(catalogue, slug)
    if not force:
        duplicates = catalogue.duplicates_of(slug)
        if duplicates:
            raise ApiError(
                status.HTTP_409_CONFLICT,
                f"{len(duplicates)} template(s) are duplicates of {slug!r} and would lose "
                "their upstream; delete with ?force=true to go ahead",
                duplicates=len(duplicates),
                slugs=duplicates,
            )
    # Best effort, not a lock: a render submitted after this check reads a model
    # that is gone and fails as an ordinary job error, which is harmless.
    if queue.store.has_unfinished(slug):
        raise ApiError(
            status.HTTP_409_CONFLICT, f"{slug!r} has a render in progress; try again when it ends"
        )
    try:
        catalogue.delete(slug)
    except ModelNotFoundError:
        # A concurrent delete of the same slug got there first.
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no model named {slug!r}") from None
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
    summary="Replace a model's source as one revision",
    description=(
        "Parse-checks the replacement against the model's own directory (skipped with "
        "`force`), then writes it as one revision in the model's history, named by "
        "`message` when given, and re-derives the customizer schema so the next "
        "customizer open does not pay for it. `merge_base` saves the resolution of a "
        "conflicted upstream merge: the source must carry no conflict markers (`force` "
        "does not skip that), `merge_base` must be a revision of the upstream (422 "
        "otherwise, writing nothing), and the duplicate's `base` becomes it in the same "
        "commit, `Merge <upstream id> into <slug>` unless `message` names it."
    ),
)
async def put_source(
    slug: SlugPath,
    body: SourceUpdate,
    catalogue: CatalogueDep,
    paths: PathsDep,
    config: ConfigDep,
    checks: ChecksDep,
    force: Annotated[bool, Query(description="Save even when the parse check fails")] = False,
    merge_base: Annotated[
        str | None,
        Query(
            pattern=COMMIT_ID_PATTERN,
            description="The upstream revision this resolves a merge of: the 409's `merge_base`",
        ),
    ] = None,
) -> ModelRecord:
    # `require_model_exists`, not `require_model`: building a record costs a
    # `git log` for the model's revision, and this handler is `async def`. The
    # record `write_source` returns carries the new revision anyway.
    require_mine(slug)
    require_model_exists(catalogue, slug)
    if merge_base is not None and has_conflict_markers(body.source):
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "the source still has conflict markers; resolve every conflict first",
        )
    library_path = await asyncio.to_thread(model_search_path, paths, slug)
    checked = await _guard_source(
        body.source,
        config=replace(config, library_path=library_path),
        # Either spelling forces, as on `POST /models`.
        force=force or body.force,
        limit=checks,
        # The model's own directory, so a replacement that includes a sibling file is
        # checked — and has its schema derived — against the files it will really see.
        context=paths.model_dir(slug),
    )
    try:
        record = await asyncio.to_thread(
            catalogue.write_source, slug, body.source, message=body.message, merge_base=merge_base
        )
    except ModelNotFoundError:
        # A concurrent delete of the same slug got there first.
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no model named {slug!r}") from None
    except NoUpstreamError:
        raise ApiError(
            status.HTTP_409_CONFLICT, f"{slug!r} is not a duplicate, so it has no merge to resolve"
        ) from None
    except InvalidMergeBaseError as error:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
    except GitUnavailableError:
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "model history is unavailable: no git repository under the models directory",
        ) from None
    except GitError as error:
        raise ApiError(status.HTTP_500_INTERNAL_SERVER_ERROR, str(error)) from None
    if checked is not None and checked.schema is not None:
        # After the write: `write_source` drops the old cache entry.
        store_cached_schema(
            paths.model_schema_cache(slug), checked.schema, library_path=library_path
        )
    else:
        # A forced save, or no openscad at all: nothing was derived to store. GET
        # /schema is where the failure surfaces.
        logger.warning("stored source without a schema", extra={"slug": slug})
    return record


@router.get("/models/{slug}/schema", response_model=CustomizerSchema, summary="Customizer schema")
async def get_schema(
    slug: SlugPath,
    catalogue: CatalogueDep,
    history: HistoryDep,
    paths: PathsDep,
    config: ConfigDep,
) -> CustomizerSchema:
    require_model_exists(catalogue, slug)
    source = await resolve_source(slug, None, paths=paths, history=history)
    try:
        return await cached_schema(
            source.scad, source.schema_cache, config=source.configure(config)
        )
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


#: How a model thumbnail may be cached: kept, but revalidated on every use.
#:
#: Not `immutable`, although the catalogue's URL carries a `?v=` key (the model's
#: version, the thumbnail's source and its covering output): that key is not proven
#: to change with the bytes. `version` is None wherever git is not available, so
#: there a replaced thumbnail keeps the key it had; and the model directory and the
#: outputs are plain files on a volume anyone with access can change. A strong
#: ETag over the bytes served costs one hash and saves the whole download instead,
#: without ever serving a stale image.
THUMBNAIL_CACHE_CONTROL = "no-cache"


def _etag_matches(if_none_match: str | None, etag: str) -> bool:
    """RFC 9110 §13.1.2: `*`, or any listed tag, compared weakly."""
    if if_none_match is None:
        return False
    candidates = [candidate.strip() for candidate in if_none_match.split(",")]
    return "*" in candidates or etag in (c.removeprefix("W/") for c in candidates)


@router.get(
    "/models/{slug}/thumbnail",
    response_class=Response,
    responses={
        200: {"content": {"image/png": {}}},
        304: {"description": "The copy named by `If-None-Match` is still current"},
    },
    summary="Model thumbnail",
    description=(
        "The thumbnail set on the model or, when it has none, the plate image of its "
        "first generated output. 404 when there is neither. Carries a strong `ETag` "
        "over the image and `Cache-Control: no-cache`; a matching `If-None-Match` is "
        "answered 304 with no body."
    ),
)
def get_thumbnail(
    slug: SlugPath,
    catalogue: CatalogueDep,
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
) -> Response:
    require_model_exists(catalogue, slug)
    try:
        png = catalogue.thumbnail(slug)
    except ModelNotFoundError:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no model named {slug!r}") from None
    if png is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"{slug!r} has no thumbnail")
    # Over the bytes themselves, so it changes exactly when the image does, from
    # whichever source -- a set, a removal, or the fallback moving to another output.
    etag = f'"{hashlib.sha256(png).hexdigest()}"'
    headers = {"ETag": etag, "Cache-Control": THUMBNAIL_CACHE_CONTROL}
    if _etag_matches(if_none_match, etag):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    return Response(png, media_type="image/png", headers=headers)


@router.put(
    "/models/{slug}/thumbnail",
    response_model=ModelRecord,
    summary="Set a model's thumbnail",
    description=(
        "Sets or replaces the catalogue thumbnail with an uploaded PNG, as one revision "
        "in the model's history."
    ),
)
async def put_thumbnail(
    slug: SlugPath,
    catalogue: CatalogueDep,
    file: Annotated[
        UploadFile, File(description=f"The thumbnail, a PNG of at most {MAX_THUMBNAIL_SIZE}")
    ],
) -> ModelRecord:
    require_mine(slug)
    require_model_exists(catalogue, slug)
    png = _require_png(await file.read())
    try:
        # `to_thread`: a git commit, from an `async def` handler. See `_create`.
        return await asyncio.to_thread(catalogue.write_thumbnail, slug, png)
    except ModelNotFoundError:
        # A concurrent delete of the same slug got there first.
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no model named {slug!r}") from None


@router.delete(
    "/models/{slug}/thumbnail",
    response_model=ModelRecord,
    summary="Remove a model's thumbnail",
    description=(
        "Removes the thumbnail set on the model, as one revision in its history. The "
        "record that comes back can still have one: a generated model falls back to "
        "its first output's plate image (`thumbnail_source` is then `output`)."
    ),
)
def delete_thumbnail(slug: SlugPath, catalogue: CatalogueDep) -> ModelRecord:
    require_mine(slug)
    require_model_exists(catalogue, slug)
    try:
        return catalogue.delete_thumbnail(slug)
    except ModelNotFoundError:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no model named {slug!r}") from None
    except SidecarNotFoundError:
        raise ApiError(
            status.HTTP_404_NOT_FOUND, f"{slug!r} has no thumbnail of its own to remove"
        ) from None


@router.get(
    "/models/{slug}/readme",
    # `Response`, not `PlainTextResponse`: a response class with a media type of its
    # own adds it to the documented 200 beside `text/markdown`, which is all this
    # route ever answers.
    response_class=Response,
    responses={200: {"content": {"text/markdown": {"schema": {"type": "string"}}}}},
    summary="Model README",
)
def get_readme(slug: SlugPath, catalogue: CatalogueDep) -> Response:
    require_model_exists(catalogue, slug)
    try:
        text = catalogue.read_readme(slug)
    except ModelNotFoundError:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no model named {slug!r}") from None
    except SidecarNotFoundError:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"{slug!r} has no README") from None
    return Response(text, media_type="text/markdown; charset=utf-8")


@router.put(
    "/models/{slug}/readme",
    response_model=ModelRecord,
    summary="Set a model's README",
    description="Sets or replaces the README, as one revision in the model's history.",
)
async def put_readme(slug: SlugPath, body: ReadmeUpdate, catalogue: CatalogueDep) -> ModelRecord:
    require_mine(slug)
    require_model_exists(catalogue, slug)
    if "\x00" in body.content:
        # The same line `_guard_source` draws: the models repository holds text.
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "the README contains a NUL byte, so it is binary, not text",
        )
    try:
        return await asyncio.to_thread(catalogue.write_readme, slug, body.content)
    except ModelNotFoundError:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no model named {slug!r}") from None


@router.delete(
    "/models/{slug}/readme",
    response_model=ModelRecord,
    summary="Remove a model's README",
    description="Removes the README, as one revision in the model's history.",
)
def delete_readme(slug: SlugPath, catalogue: CatalogueDep) -> ModelRecord:
    require_mine(slug)
    require_model_exists(catalogue, slug)
    try:
        return catalogue.delete_readme(slug)
    except ModelNotFoundError:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no model named {slug!r}") from None
    except SidecarNotFoundError:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"{slug!r} has no README to remove") from None


def install_model_handlers(app: FastAPI) -> None:
    """A model whose model.json on disk cannot be read is a 409 from every route that
    reads it, as a hand-broken `libraries.lock` is: the model is in a state only an
    edit of that file fixes, and naming the file beats the bare 500 it would be."""

    @app.exception_handler(InvalidModelMetaError)
    async def _bad_meta(request: Request, exc: InvalidModelMetaError) -> JSONResponse:
        return problem_response(
            request, status.HTTP_409_CONFLICT, str(exc), title="Invalid Model Metadata"
        )
