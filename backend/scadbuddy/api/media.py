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
import io
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path as FilePath
from typing import IO, Annotated, Any

from fastapi import APIRouter, Path, Query, Request, status
from fastapi.responses import FileResponse, Response
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel, Field, StringConstraints
from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import MultipartParser, parse_options_header

from scadbuddy.api.deps import CatalogueDep, EventsDep, SlugPath
from scadbuddy.api.models import require_model_exists
from scadbuddy.core.events import ModelEvent, emit
from scadbuddy.core.problems import ApiError
from scadbuddy.library.catalogue import (
    Catalogue,
    MediaNotFoundError,
    MediaOrderError,
    MediaReadOnlyError,
    ModelNotFoundError,
    ModelRecord,
    TooManyMediaError,
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
            self._out = path.open("xb")
        self._part = name

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        chunk = data[start:end]
        if self._out is not None and self._part is not None:
            self._out.write(chunk)
            self.received.sizes[self._part] += len(chunk)
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


def _no_model(slug: str) -> ApiError:
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


def _read_only(slug: str, item_id: str) -> ApiError:
    return ApiError(
        status.HTTP_403_FORBIDDEN,
        f"{item_id!r} is shipped with the built-in template {slug!r} and is read-only; "
        "only the media added to it can change",
    )


def _require_media_store(catalogue: Catalogue) -> None:
    """503 before anything is read or written, so an upload is refused on its
    headers rather than after a gigabyte of body."""
    if catalogue.media_store is None:
        raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, NO_DATABASE)


def _no_item(slug: str, item_id: str) -> ApiError:
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
        raise _no_model(slug) from None
    except MediaNotFoundError:
        raise _no_item(slug, item_id) from None
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
        raise _no_model(slug) from None
    except MediaNotFoundError:
        raise ApiError(
            status.HTTP_404_NOT_FOUND, f"{slug!r} has no poster for {item_id!r}"
        ) from None
    return FileResponse(
        path,
        media_type=content_type_of(path.name),
        headers={"Cache-Control": IMMUTABLE_CACHE_CONTROL},
    )


#: The longest side of a thumbnail: the gallery strip's tile at 2x, with room to spare.
THUMBNAIL_SIDE = 192
#: The most pixels a thumbnail is decoded from: a 10 MB PNG can declare far more than
#: a photo has, and every request would decode it again. Larger is served as it is.
MAX_THUMBNAIL_SOURCE_PIXELS = 50_000_000
#: The decoders a thumbnail may use: only the image types an item can be (`ACCEPTED`),
#: so a mislabeled legacy file never reaches any other Pillow plugin.
THUMBNAIL_FORMATS = ("PNG", "JPEG", "WEBP")
#: The version of what `_thumbnail_of` makes. The client asks for ``?v=`` this, and a
#: thumbnail is cached as ``immutable`` only when it does: bump it whenever the
#: output changes for the same source (side, quality, resampler, format), and the
#: frontend's ``MEDIA_THUMBNAIL_VERSION`` with it, so a browser holding an old copy
#: asks again (#1424). ``client.test.ts`` fails when the two differ (#1691).
THUMBNAIL_VERSION = 1
#: How many thumbnails are decoded at once: a 50 MP PNG costs some 200 MB, and a
#: gallery strip asks for every item's thumbnail together (#1420).
MAX_CONCURRENT_THUMBNAILS = 2
#: Square, so an EXIF orientation of 5 to 8, which swaps width and height, fits it
#: either way round: the image is shrunk before it is turned upright, and the
#: full-size copy `exif_transpose` would make is never made (#1426).
_THUMBNAIL_BOX = (THUMBNAIL_SIDE, THUMBNAIL_SIDE)


def _shrink(file: IO[bytes]) -> bytes | None:
    """``file`` shrunk to a WebP no larger than `THUMBNAIL_SIDE`, or None when Pillow
    cannot read it as one of `THUMBNAIL_FORMATS`, or it is over
    `MAX_THUMBNAIL_SOURCE_PIXELS` once ``draft`` has had its say (checked from the
    header, before decoding). ``draft`` lets a JPEG decode at a fraction of its size,
    so a large photo is still cheap enough to shrink. Any error decoding it -- a
    malformed EXIF block raises ``ValueError`` or ``SyntaxError`` -- is a None too,
    since serving the file as it is is always safe."""
    try:
        with Image.open(file, formats=THUMBNAIL_FORMATS) as image:
            image.draft("RGB", _THUMBNAIL_BOX)
            if image.width * image.height > MAX_THUMBNAIL_SOURCE_PIXELS:
                return None
            image.thumbnail(_THUMBNAIL_BOX, Image.Resampling.LANCZOS)
            # Upright, as a browser shows the original: the WebP carries no EXIF.
            with ImageOps.exif_transpose(image) as upright:
                out = io.BytesIO()
                upright.save(out, "WEBP", quality=80)
                return out.getvalue()
    except (
        UnidentifiedImageError,
        OSError,
        Image.DecompressionBombError,
        ValueError,
        SyntaxError,
    ):
        return None


@dataclass(frozen=True)
class _Thumbnail:
    body: bytes
    media_type: str
    #: Of the file ``body`` was read from, through the same handle.
    stat: os.stat_result


def _thumbnail_of(path: FilePath, content_type: str) -> _Thumbnail:
    """``path`` shrunk by `_shrink`, or as it is (typed ``content_type``) when it
    cannot be. The stat and the bytes come from one open handle, so the legacy
    item's ETag describes what is served even if a thumbnail PUT replaces the file
    meanwhile (#1689). ``FileNotFoundError`` when the file is gone. The fallback is
    read whole, not streamed: an image is capped at `MAX_IMAGE_BYTES`, and at most
    `MAX_CONCURRENT_THUMBNAILS` are held at once."""
    with path.open("rb") as file:
        stat = os.fstat(file.fileno())
        small = _shrink(file)
        if small is not None:
            return _Thumbnail(small, "image/webp", stat)
        file.seek(0)
        return _Thumbnail(file.read(), content_type, stat)


async def _bounded_thumbnail_of(request: Request, path: FilePath, content_type: str) -> _Thumbnail:
    """`_thumbnail_of`, at most `MAX_CONCURRENT_THUMBNAILS` at a time. A request
    waiting its turn holds no thread. The semaphore is the app's, made on first use,
    so it belongs to the loop that serves the app."""
    decodes: asyncio.Semaphore | None = getattr(request.app.state, "thumbnail_decodes", None)
    if decodes is None:
        decodes = asyncio.Semaphore(MAX_CONCURRENT_THUMBNAILS)
        request.app.state.thumbnail_decodes = decodes
    async with decodes:
        return await asyncio.to_thread(_thumbnail_of, path, content_type)


def _legacy_etag(stat: os.stat_result) -> str:
    """A validator for the legacy item's thumbnail, which a thumbnail PUT replaces
    in place: from the source's mtime and size, and `THUMBNAIL_VERSION`."""
    return f'W/"{stat.st_mtime_ns:x}-{stat.st_size:x}-{THUMBNAIL_VERSION}"'


def _matches(if_none_match: str | None, etag: str) -> bool:
    if if_none_match is None:
        return False
    tags = [tag.strip() for tag in if_none_match.split(",")]
    weak = etag.removeprefix("W/")
    return "*" in tags or any(tag.removeprefix("W/") == weak for tag in tags)


@dataclass(frozen=True)
class _ThumbnailSource:
    path: FilePath
    content_type: str
    legacy: bool


def _thumbnail_source(catalogue: Catalogue, slug: str, item_id: str) -> _ThumbnailSource:
    """The file a thumbnail is made from: the image, or a video's poster. 404 for an
    unknown id, a missing file and a video with no poster (or a poster whose file
    is gone)."""
    require_model_exists(catalogue, slug)
    try:
        item, path = catalogue.media_item(slug, item_id)
    except ModelNotFoundError:
        raise _no_model(slug) from None
    except MediaNotFoundError:
        raise _no_item(slug, item_id) from None
    if item.kind != "video":
        return _ThumbnailSource(path, item.content_type, item.id == LEGACY_ID)
    try:
        poster = catalogue.media_poster(slug, item_id)
    except MediaNotFoundError:
        poster = None
    if poster is None or not poster.is_file():
        raise ApiError(status.HTTP_404_NOT_FOUND, f"{slug!r} has no poster for {item_id!r}")
    return _ThumbnailSource(poster, content_type_of(poster.name), False)


@router.get(
    "/models/{slug}/media/{item_id}/thumbnail",
    response_class=Response,
    responses={
        200: {
            "content": {"image/*": {}},
            "description": "Usually `image/webp`; the item's own type when it is served as it is",
        },
        304: {"description": "The legacy item's thumbnail has not changed (`If-None-Match`)"},
    },
    summary="A small copy of one item",
    description=(
        f"The image, or a video's poster, shrunk to at most {THUMBNAIL_SIDE} pixels a "
        "side as WebP, so a strip of thumbnails does not download every original. A "
        "file the server cannot decode is served as it is. 404 for an unknown id, a "
        "missing file and a video with no poster. Cached as the item itself is when "
        f"`v` is {THUMBNAIL_VERSION}, the current thumbnail version, and `no-cache` "
        "otherwise; the legacy item's carries an `ETag`, and answers 304 to a "
        "matching `If-None-Match`."
    ),
)
async def get_media_thumbnail(
    slug: SlugPath,
    item_id: MediaIdPath,
    catalogue: CatalogueDep,
    request: Request,
    v: Annotated[int | None, Query(description="The thumbnail version asked for")] = None,
) -> Response:
    source = await asyncio.to_thread(_thumbnail_source, catalogue, slug, item_id)
    if source.legacy or v != THUMBNAIL_VERSION:
        headers = {"Cache-Control": LEGACY_CACHE_CONTROL}
    else:
        headers = {"Cache-Control": IMMUTABLE_CACHE_CONTROL}
    if source.legacy:
        # A cheap check before the decode; the ETag sent is the decoded file's own.
        try:
            current = _legacy_etag(source.path.stat())
        except FileNotFoundError:
            raise _no_item(slug, item_id) from None
        if _matches(request.headers.get("if-none-match"), current):
            headers["ETag"] = current
            return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    try:
        thumbnail = await _bounded_thumbnail_of(request, source.path, source.content_type)
    except FileNotFoundError:
        raise _no_item(slug, item_id) from None
    if source.legacy:
        headers["ETag"] = _legacy_etag(thumbnail.stat)
    return Response(thumbnail.body, media_type=thumbnail.media_type, headers=headers)


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
    slug: SlugPath, request: Request, catalogue: CatalogueDep, events: EventsDep
) -> ModelRecord:
    require_model_exists(catalogue, slug)
    _require_media_store(catalogue)
    received = await _receive(request, catalogue.paths.cache)
    try:
        upload, caption, poster = _upload_parts(received)
        # `to_thread`: a git commit, from an `async def` handler.
        record = await asyncio.to_thread(catalogue.add_media, slug, upload, caption, poster)
    except ModelNotFoundError:
        raise _no_model(slug) from None
    except TooManyMediaError:
        raise ApiError(
            status.HTTP_409_CONFLICT, f"a template holds at most {MAX_MEDIA_ITEMS} media items"
        ) from None
    finally:
        received.discard()
    emit(events, ModelEvent(kind="model.updated", slug=slug))
    return record


@router.patch(
    "/models/{slug}/media/{item_id}",
    response_model=ModelRecord,
    responses={**NO_DATABASE_RESPONSE, **READ_ONLY_RESPONSE},
    summary="Caption a media item",
)
def patch_media(
    slug: SlugPath,
    item_id: MediaIdPath,
    body: MediaCaption,
    catalogue: CatalogueDep,
    events: EventsDep,
) -> ModelRecord:
    require_model_exists(catalogue, slug)
    _require_media_store(catalogue)
    try:
        record = catalogue.set_caption(slug, item_id, body.caption)
    except ModelNotFoundError:
        raise _no_model(slug) from None
    except MediaNotFoundError:
        raise _no_item(slug, item_id) from None
    except MediaReadOnlyError:
        raise _read_only(slug, item_id) from None
    emit(events, ModelEvent(kind="model.updated", slug=slug))
    return record


@router.put(
    "/models/{slug}/media/order",
    response_model=ModelRecord,
    responses=NO_DATABASE_RESPONSE,
    summary="Reorder the media",
    description=(
        "Puts the items in the order given, which must name every item exactly once "
        "(422 otherwise). The first is the cover. A built-in's shipped items keep "
        "their place: the order names every item added to it, a shipped id in it is "
        "passed over, and its cover is set with `PUT .../media/cover`."
    ),
)
def reorder_media(
    slug: SlugPath, body: MediaOrder, catalogue: CatalogueDep, events: EventsDep
) -> ModelRecord:
    require_model_exists(catalogue, slug)
    _require_media_store(catalogue)
    try:
        record = catalogue.reorder(slug, body.ids)
    except ModelNotFoundError:
        raise _no_model(slug) from None
    except MediaOrderError as error:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
    emit(events, ModelEvent(kind="model.updated", slug=slug))
    return record


@router.put(
    "/models/{slug}/media/cover",
    response_model=ModelRecord,
    responses=NO_DATABASE_RESPONSE,
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
def put_media_cover(
    slug: SlugPath, body: MediaCover, catalogue: CatalogueDep, events: EventsDep
) -> ModelRecord:
    require_model_exists(catalogue, slug)
    _require_media_store(catalogue)
    try:
        record = catalogue.set_cover(slug, body.id)
    except ModelNotFoundError:
        raise _no_model(slug) from None
    except MediaNotFoundError:
        raise _no_item(slug, body.id or "") from None
    except MediaOrderError as error:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
    emit(events, ModelEvent(kind="model.updated", slug=slug))
    return record


@router.delete(
    "/models/{slug}/media/{item_id}",
    response_model=ModelRecord,
    responses={**NO_DATABASE_RESPONSE, **READ_ONLY_RESPONSE},
    summary="Remove a media item",
    description=(
        "Removes one item and its files, as one revision. An entry whose file is "
        "missing is removed all the same. What a built-in ships is not removed (403); "
        "what was added to it is."
    ),
)
def delete_media(
    slug: SlugPath, item_id: MediaIdPath, catalogue: CatalogueDep, events: EventsDep
) -> ModelRecord:
    require_model_exists(catalogue, slug)
    _require_media_store(catalogue)
    try:
        record = catalogue.remove_media(slug, item_id)
    except ModelNotFoundError:
        raise _no_model(slug) from None
    except MediaNotFoundError:
        raise _no_item(slug, item_id) from None
    except MediaReadOnlyError:
        raise _read_only(slug, item_id) from None
    emit(events, ModelEvent(kind="model.updated", slug=slug))
    return record
