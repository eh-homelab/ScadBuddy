"""A template's images and videos (#274): serve, upload, caption, reorder, remove.

The upload is parsed here rather than by FastAPI, which would spool every file part
to the system temp directory and only then call the route. A video runs to a
gigabyte, so its bytes go straight to ``cache/`` on the data volume, from where the
catalogue renames them into ``media/``. `api.limits.BodySizeGate` caps the body at
``media_upload_max_bytes`` before any of it is read.

A built-in takes media too (#722), as an overlay: what it ships is listed first and
``readonly`` (a write to it is a 403), what is added to it is kept outside the models
repository and changes like a template of mine's, without a revision. Its cover is
chosen with ``PUT .../media/cover``, since its shipped items keep their place.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from dataclasses import dataclass, field
from pathlib import Path as FilePath
from typing import IO, Annotated, Any

from fastapi import APIRouter, Path, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, StringConstraints
from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import MultipartParser, parse_options_header

from scadbuddy.api.deps import CatalogueDep, SlugPath
from scadbuddy.api.models import require_model_exists
from scadbuddy.api.operations import (
    OPERATION_RESPONSES,
    Claimed,
    IdempotencyKey,
    operation_answer,
    recorded,
    run_operation,
)
from scadbuddy.core.problems import ApiError
from scadbuddy.library.catalogue import (
    Catalogue,
    MediaNotFoundError,
    ModelNotFoundError,
    ModelRecord,
)
from scadbuddy.library.media import (
    LEGACY_ID,
    MAX_CAPTION_CHARS,
    MAX_IMAGE_BYTES,
    MAX_MEDIA_ITEMS,
    MEDIA_ID_PATTERN,
    MEDIA_UPLOAD_PREFIX,
    SNIFF_BYTES,
    StagedMedia,
    content_type_of,
    sniff_kind,
)
from scadbuddy.operations.claims import ClaimStore, Held
from scadbuddy.operations.component import OperationCommands, OperationsDep

router = APIRouter(tags=["media"])

MediaIdPath = Annotated[str, Path(pattern=MEDIA_ID_PATTERN)]
MediaId = Annotated[str, StringConstraints(pattern=MEDIA_ID_PATTERN)]

#: A stored item's id names its contents for good: a replaced cover gets a new id.
IMMUTABLE_CACHE_CONTROL = "private, max-age=31536000, immutable"
#: The legacy item is ``thumbnail.png``, which a thumbnail PUT replaces in place.
LEGACY_CACHE_CONTROL = "no-cache"

#: The file parts the upload reads; any other part is skipped.
FILE_PARTS = ("file", "poster")
#: A caption is at most `MAX_CAPTION_CHARS` characters, so at most 4 bytes each.
MAX_CAPTION_BYTES = MAX_CAPTION_CHARS * 4

ACCEPTED = "a PNG, JPEG or WebP image, or an MP4 or WebM video"


class MediaUpload(BaseModel):
    """The multipart body of ``POST /models/{slug}/media``."""

    file: bytes = Field(description=f"The image or video: {ACCEPTED}")
    caption: str | None = Field(default=None, max_length=MAX_CAPTION_CHARS)
    poster: bytes | None = Field(
        default=None, description="A video's poster image (PNG, JPEG or WebP)"
    )


class MediaCaption(BaseModel):
    caption: str = Field(max_length=MAX_CAPTION_CHARS)


class MediaOrder(BaseModel):
    """Every item's id, once each, in the new order. On a built-in, every added
    item's: a shipped item keeps its place, and its id may be left out."""

    ids: list[MediaId] = Field(max_length=MAX_MEDIA_ITEMS)


class MediaCover(BaseModel):
    """The item to make the cover."""

    id: MediaId | None = Field(
        description=(
            "The item's id. None goes back to a built-in's shipped cover; a template "
            "of mine's cover is always one of its items."
        ),
    )


# ── the streamed multipart body ──────────────────────────────────────────────


@dataclass
class _Received:
    """The parts of an upload: the file parts on disk, the caption in memory."""

    files: dict[str, FilePath] = field(default_factory=dict)
    sizes: dict[str, int] = field(default_factory=dict)
    #: Each file part's sha256, computed while it streamed: its claim's name (#1054).
    hashes: dict[str, Any] = field(default_factory=dict)
    caption: bytearray = field(default_factory=bytearray)

    def discard(self) -> None:
        """Remove whatever was not moved into ``media/``."""
        for path in self.files.values():
            path.unlink(missing_ok=True)


class _Receiver:
    """`MultipartParser` callbacks writing ``file`` and ``poster`` to ``directory``."""

    def __init__(self, directory: FilePath, received: _Received) -> None:
        self.directory = directory
        self.received = received
        self._field = bytearray()
        self._value = bytearray()
        self._disposition = b""
        self._part: str | None = None
        self._out: IO[bytes] | None = None

    def on_part_begin(self) -> None:
        self._disposition = b""
        self._part = None

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._field += data[start:end]

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._value += data[start:end]

    def on_header_end(self) -> None:
        if bytes(self._field).lower() == b"content-disposition":
            self._disposition = bytes(self._value)
        self._field.clear()
        self._value.clear()

    def on_headers_finished(self) -> None:
        _, options = parse_options_header(self._disposition)
        name = options.get(b"name", b"").decode("latin-1")
        if name in FILE_PARTS:
            if name in self.received.files:
                raise ApiError(
                    status.HTTP_422_UNPROCESSABLE_CONTENT, f"more than one {name!r} part"
                )
            path = self.directory / f"{MEDIA_UPLOAD_PREFIX}{uuid.uuid4().hex}"
            self.received.files[name] = path
            self.received.sizes[name] = 0
            self.received.hashes[name] = hashlib.sha256()
            self._out = path.open("xb")
        self._part = name

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        chunk = data[start:end]
        if self._out is not None and self._part is not None:
            self._out.write(chunk)
            self.received.sizes[self._part] += len(chunk)
            self.received.hashes[self._part].update(chunk)
        elif self._part == "caption":
            self.received.caption += chunk
            if len(self.received.caption) > MAX_CAPTION_BYTES:
                raise ApiError(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    f"a caption is at most {MAX_CAPTION_CHARS} characters",
                )

    def on_part_end(self) -> None:
        self.close()

    def close(self) -> None:
        if self._out is not None:
            self._out.close()
            self._out = None


def _boundary(request: Request) -> bytes:
    content_type, options = parse_options_header(request.headers.get("content-type", ""))
    boundary = options.get(b"boundary")
    if content_type != b"multipart/form-data" or not boundary:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "the upload is multipart/form-data, with the media in a `file` part",
        )
    return boundary


async def _receive(request: Request, directory: FilePath) -> _Received:
    """Stream the body's parts to ``directory``. On any failure the files written
    so far are removed and the error raised -- including the gate's, when the
    body passes the limit part-way."""
    boundary = _boundary(request)
    directory.mkdir(parents=True, exist_ok=True)
    received = _Received()
    receiver = _Receiver(directory, received)
    parser = MultipartParser(
        boundary,
        callbacks={
            "on_part_begin": receiver.on_part_begin,
            "on_header_field": receiver.on_header_field,
            "on_header_value": receiver.on_header_value,
            "on_header_end": receiver.on_header_end,
            "on_headers_finished": receiver.on_headers_finished,
            "on_part_data": receiver.on_part_data,
            "on_part_end": receiver.on_part_end,
        },
    )
    try:
        async for chunk in request.stream():
            if chunk:
                # Off the event loop: a gigabyte of small writes would stall it.
                await asyncio.to_thread(parser.write, chunk)
        parser.finalize()
    except MultipartParseError as error:
        receiver.close()
        received.discard()
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"the multipart body is malformed: {error}"
        ) from None
    except BaseException:
        receiver.close()
        received.discard()
        raise
    receiver.close()
    return received


def _staged(received: _Received, part: str) -> StagedMedia | None:
    """The part as typed media, or None when it was not sent (or sent empty, as a
    form with no file chosen sends it). 415 when its bytes are not media."""
    path = received.files.get(part)
    if path is None or received.sizes[part] == 0:
        return None
    with path.open("rb") as handle:
        sniffed = sniff_kind(handle.read(SNIFF_BYTES))
    if sniffed is None:
        what = "the poster" if part == "poster" else "the upload"
        raise ApiError(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, f"{what} is not {ACCEPTED}")
    kind, extension, _ = sniffed
    if kind == "image" and received.sizes[part] > MAX_IMAGE_BYTES:
        raise ApiError(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"an image is at most {MAX_IMAGE_BYTES // (1024 * 1024)} MB: images are "
            "kept in the template's history",
        )
    return StagedMedia(path=path, kind=kind, extension=extension)


def _upload_parts(received: _Received) -> tuple[StagedMedia, str, StagedMedia | None]:
    upload = _staged(received, "file")
    if upload is None:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, "the upload has no `file` part")
    poster = _staged(received, "poster")
    if poster is not None:
        if upload.kind != "video":
            raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, "only a video takes a poster")
        if poster.kind != "image":
            raise ApiError(
                status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                "the poster is not a PNG, JPEG or WebP image",
            )
    try:
        caption = received.caption.decode("utf-8")
    except UnicodeDecodeError:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, "the caption is not UTF-8") from None
    if len(caption) > MAX_CAPTION_CHARS:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"a caption is at most {MAX_CAPTION_CHARS} characters",
        )
    return upload, caption, poster


# ── routes ────────────────────────────────────────────────────────────────────


def no_model(slug: str) -> ApiError:
    return ApiError(status.HTTP_404_NOT_FOUND, f"no model named {slug!r}")


#: Until #401 makes the database required, a deployment may run without one.
NO_DATABASE = (
    "template media needs a database: set SCADBUDDY_DATABASE_URL. Without one, only "
    "the thumbnail is shown"
)


#: Every media write's answer when there is no database.
NO_DATABASE_RESPONSE: dict[int | str, dict[str, Any]] = {
    503: {"description": "No database: SCADBUDDY_DATABASE_URL is unset"}
}

#: A write to an item a built-in ships.
READ_ONLY_RESPONSE: dict[int | str, dict[str, Any]] = {
    403: {"description": "The item is one a built-in template ships, and is read-only"}
}


def read_only(slug: str, item_id: str) -> ApiError:
    return ApiError(
        status.HTTP_403_FORBIDDEN,
        f"{item_id!r} is shipped with the built-in template {slug!r} and is read-only; "
        "only the media added to it can change",
    )


def require_media_store(catalogue: Catalogue) -> None:
    """503 before anything is read or written, so an upload is refused on its
    headers rather than after a gigabyte of body."""
    if catalogue.media_store is None:
        raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, NO_DATABASE)


def no_item(slug: str, item_id: str) -> ApiError:
    return ApiError(status.HTTP_404_NOT_FOUND, f"{slug!r} has no media item {item_id!r}")


@router.get(
    "/models/{slug}/media/{item_id}",
    response_class=FileResponse,
    responses={
        200: {"content": {"image/*": {}, "video/*": {}}},
        206: {"description": "The byte range asked for with `Range`"},
    },
    summary="One media file",
    description=(
        "Serves one image or video of the template. Honours `Range`, so a video can "
        "seek. An item's id never changes its contents, so it is cached as "
        "`immutable` -- except `thumbnail`, the legacy item, which is `no-cache`. "
        "404 for an unknown id and for an entry whose file is missing."
    ),
)
def get_media(slug: SlugPath, item_id: MediaIdPath, catalogue: CatalogueDep) -> FileResponse:
    require_model_exists(catalogue, slug)
    try:
        item, path = catalogue.media_item(slug, item_id)
    except ModelNotFoundError:
        raise no_model(slug) from None
    except MediaNotFoundError:
        raise no_item(slug, item_id) from None
    cache = LEGACY_CACHE_CONTROL if item.id == LEGACY_ID else IMMUTABLE_CACHE_CONTROL
    return FileResponse(path, media_type=item.content_type, headers={"Cache-Control": cache})


@router.get(
    "/models/{slug}/media/{item_id}/poster",
    response_class=FileResponse,
    responses={200: {"content": {"image/*": {}}}},
    summary="A video's poster",
    description="The poster image of one video. 404 when it has none.",
)
def get_media_poster(slug: SlugPath, item_id: MediaIdPath, catalogue: CatalogueDep) -> FileResponse:
    require_model_exists(catalogue, slug)
    try:
        path = catalogue.media_poster(slug, item_id)
    except ModelNotFoundError:
        raise no_model(slug) from None
    except MediaNotFoundError:
        raise ApiError(
            status.HTTP_404_NOT_FOUND, f"{slug!r} has no poster for {item_id!r}"
        ) from None
    return FileResponse(
        path,
        media_type=content_type_of(path.name),
        headers={"Cache-Control": IMMUTABLE_CACHE_CONTROL},
    )


@router.post(
    "/models/{slug}/media",
    response_model=ModelRecord,
    responses={
        **NO_DATABASE_RESPONSE,
        409: {"description": f"The template already holds {MAX_MEDIA_ITEMS} items"},
        413: {
            "description": "Larger than `SCADBUDDY_MEDIA_UPLOAD_MAX_BYTES`, or an image over 10 MB"
        },
        415: {"description": f"Not {ACCEPTED}"},
        **OPERATION_RESPONSES,
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {"schema": {"$ref": "#/components/schemas/MediaUpload"}}
            },
        }
    },
    summary="Add an image or video",
    description=(
        f"Adds {ACCEPTED} as the template's last media item, as one revision. The "
        "type comes from the bytes, not the name. A video may carry a `poster` image "
        "and any item a `caption`. The body may be as large as "
        "`SCADBUDDY_MEDIA_UPLOAD_MAX_BYTES` (reported by `GET /settings` as "
        "`media_upload_max_bytes`); an image is at most 10 MB, since it is committed to "
        "the template's history, while a video is not. The first write turns a legacy "
        "`thumbnail.png` into an ordinary item. On a built-in the item is added after "
        "what it ships, kept outside its history: the built-in's revision does not move."
    ),
)
async def upload_media(
    slug: SlugPath,
    request: Request,
    response: Response,
    catalogue: CatalogueDep,
    ops: OperationsDep,
    idempotency_key: IdempotencyKey = None,
) -> ModelRecord | JSONResponse:
    require_model_exists(catalogue, slug)
    require_media_store(catalogue)
    received = await _receive(request, catalogue.paths.cache)
    claims = ClaimStore(catalogue.paths.claims)
    held: list[Held] = []
    try:
        upload, caption, poster = _upload_parts(received)
        # By claim, as streamed: a video runs to a gigabyte (#1054).
        named = {part: received.hashes[part].hexdigest() for part in received.files}
        body = {
            "slug": slug,
            "file": named["file"],
            "kind": upload.kind,
            "extension": upload.extension,
            "poster": None if poster is None else named["poster"],
            "poster_extension": None if poster is None else poster.extension,
            "caption": caption,
        }
        kind = ops.kinds["model_media_upload"]
        if (
            await recorded(
                ops, kind=kind, subject=slug, request=body, idempotency_key=idempotency_key
            )
            is None
        ):
            held.append(await asyncio.to_thread(claims.hold_file, upload.path, named["file"]))
            if poster is not None:
                held.append(await asyncio.to_thread(claims.hold_file, poster.path, named["poster"]))
    finally:
        # What was not claimed: a part not sent, or all of it for a repeat.
        received.discard()
    result = await run_operation(
        ops,
        response,
        kind=kind,
        subject=slug,
        request=body,
        idempotency_key=idempotency_key,
        claimed=Claimed(claims, held),
    )
    return operation_answer(result, ModelRecord)


@router.patch(
    "/models/{slug}/media/{item_id}",
    response_model=ModelRecord,
    responses={**NO_DATABASE_RESPONSE, **READ_ONLY_RESPONSE, **OPERATION_RESPONSES},
    summary="Caption a media item",
)
async def patch_media(
    slug: SlugPath,
    item_id: MediaIdPath,
    body: MediaCaption,
    response: Response,
    ops: OperationsDep,
    idempotency_key: IdempotencyKey = None,
) -> ModelRecord | JSONResponse:
    return await _edit(
        ops,
        response,
        "model_media_patch",
        {"slug": slug, "item_id": item_id, "caption": body.caption},
        idempotency_key,
    )


async def _edit(
    ops: OperationCommands,
    response: Response,
    kind: str,
    request: dict[str, Any],
    idempotency_key: str | None,
) -> ModelRecord | JSONResponse:
    """A media edit as its operation (#1054); the check makes the 404 and the 503, so a
    re-send gets its recorded answer whatever has changed since (review 3e final M1)."""
    result = await run_operation(
        ops,
        response,
        kind=ops.kinds[kind],
        subject=request["slug"],
        request=request,
        idempotency_key=idempotency_key,
    )
    return operation_answer(result, ModelRecord)


@router.put(
    "/models/{slug}/media/order",
    response_model=ModelRecord,
    responses={**NO_DATABASE_RESPONSE, **OPERATION_RESPONSES},
    summary="Reorder the media",
    description=(
        "Puts the items in the order given, which must name every item exactly once "
        "(422 otherwise). The first is the cover. A built-in's shipped items keep "
        "their place: the order names every item added to it, a shipped id in it is "
        "passed over, and its cover is set with `PUT .../media/cover`."
    ),
)
async def reorder_media(
    slug: SlugPath,
    body: MediaOrder,
    response: Response,
    ops: OperationsDep,
    idempotency_key: IdempotencyKey = None,
) -> ModelRecord | JSONResponse:
    return await _edit(
        ops,
        response,
        "model_media_order",
        {"slug": slug, "ids": body.ids},
        idempotency_key,
    )


@router.put(
    "/models/{slug}/media/cover",
    response_model=ModelRecord,
    responses={**NO_DATABASE_RESPONSE, **OPERATION_RESPONSES},
    summary="Choose the cover",
    description=(
        "Makes one item the cover. A template of mine's cover is its first item, so "
        "the item is moved to the front, as one revision (an `id` of null is a 422). "
        "A built-in lists what it ships first, and that order stays: its cover is a "
        "choice of its own (`media_cover` on the record), any shipped or added item, "
        "listed first; null goes back to the shipped cover. 404 for an id the "
        "template does not list."
    ),
)
async def put_media_cover(
    slug: SlugPath,
    body: MediaCover,
    response: Response,
    ops: OperationsDep,
    idempotency_key: IdempotencyKey = None,
) -> ModelRecord | JSONResponse:
    return await _edit(
        ops,
        response,
        "model_media_cover",
        {"slug": slug, "id": body.id},
        idempotency_key,
    )


@router.delete(
    "/models/{slug}/media/{item_id}",
    response_model=ModelRecord,
    responses={**NO_DATABASE_RESPONSE, **READ_ONLY_RESPONSE, **OPERATION_RESPONSES},
    summary="Remove a media item",
    description=(
        "Removes one item and its files, as one revision. An entry whose file is "
        "missing is removed all the same. What a built-in ships is not removed (403); "
        "what was added to it is."
    ),
)
async def delete_media(
    slug: SlugPath,
    item_id: MediaIdPath,
    response: Response,
    ops: OperationsDep,
    idempotency_key: IdempotencyKey = None,
) -> ModelRecord | JSONResponse:
    return await _edit(
        ops,
        response,
        "model_media_delete",
        {"slug": slug, "item_id": item_id},
        idempotency_key,
    )
