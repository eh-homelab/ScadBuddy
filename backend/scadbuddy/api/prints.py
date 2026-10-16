"""The media of a print, proxied from Bambuddy (#307, epic #305).

Every byte comes through ScadBuddy's server, so Bambuddy's API key never reaches the
browser and nothing depends on Bambuddy being reachable from it (the iframe). A video
seek is a ``Range`` request: it is passed through, and Bambuddy's ``FileResponse``
answers it with a ``206`` and only those bytes, streamed back without buffering.

Strictly read-only: ``GET`` and ``HEAD`` only. Of the browser's request only ``Range``
and ``If-Range`` go to Bambuddy; of Bambuddy's answer only the media headers come back
(`FORWARDED_HEADERS`), so neither its key nor its address reaches the browser. And only
for an archive one of ScadBuddy's outputs printed (#306).
"""

from __future__ import annotations

import asyncio
import io
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Path, Query, Request, Response, status
from fastapi.responses import StreamingResponse

from scadbuddy.api.deps import PrintLinksDep, SettingsStoreDep
from scadbuddy.api.media import (
    CARD_SIDE,
    IMMUTABLE_CACHE_CONTROL,
    LEGACY_CACHE_CONTROL,
    MAX_CONCURRENT_THUMBNAILS,
    THUMBNAIL_VERSION,
    _shrink,
)
from scadbuddy.attachments.component import PrintAttachmentsDep
from scadbuddy.bambuddy.attachments import file_path as attachment_file_path
from scadbuddy.bambuddy.client import Operation, client_for
from scadbuddy.core.problems import ApiError
from scadbuddy.library.media import MAX_IMAGE_BYTES, content_type_of
from scadbuddy.library.settings_store import SettingsStore

#: What the proxy answers: Bambuddy's bytes, whole (200) or a range of them (206).
MEDIA_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {"content": {"application/octet-stream": {}}, "description": "The file"},
    206: {"content": {"application/octet-stream": {}}, "description": "The requested range"},
    416: {"description": "The range is past the end; `Content-Range` gives the length"},
}


#: What the browser may see of Bambuddy's answer. Not its cookies, server or anything
#: else: only what a media element and a download need.
FORWARDED_HEADERS = (
    "content-type",
    "content-length",
    "content-range",
    "content-encoding",
    "accept-ranges",
    "etag",
    "last-modified",
    "content-disposition",
)

#: Bambuddy's photo names: ``finish_<timestamp>_<hex>.jpg`` or ``<hex>.<ext>``.
PHOTO_NAME = r"^[A-Za-z0-9_-]+\.(jpg|jpeg|png|webp)$"

ArchiveIdPath = Annotated[int, Path(ge=1)]

#: How many card-size photos are kept once made (#2355): a WebP of `CARD_SIDE` is some
#: 50 kB, so this is some 13 MB, a few pages of the history's covers.
CARD_PHOTO_CACHE_ITEMS = 256


async def require_linked_archive(archive_id: ArchiveIdPath, links: PrintLinksDep) -> None:
    """Refuse an archive ScadBuddy did not print, with a 404, so the proxy is not
    a window onto all of Bambuddy's history (#305 plan §2.3). The check is the print
    links (#306): an output's archive is linked once a progress read or a project
    attach has seen it, a library file's once the prints list has (#976)."""
    if await links.linked(archive_id) is None:
        raise ApiError(
            status.HTTP_404_NOT_FOUND,
            f"archive {archive_id} is not a print of any ScadBuddy output",
        )


router = APIRouter(
    prefix="/prints/{archive_id}",
    tags=["prints"],
    default_response_class=StreamingResponse,
    responses=MEDIA_RESPONSES,
    dependencies=[Depends(require_linked_archive)],
)


async def _proxy(
    store: SettingsStore,
    request: Request,
    path: str,
    *,
    operation: Operation,
    what: str,
    cache_control: str | None = None,
) -> Response:
    """Stream ``path`` from Bambuddy, with the browser's ``Range`` passed through.

    The client and the upstream response stay open until the body has been sent, so
    both live on an exit stack that the body's own generator closes. A ``HEAD`` gets
    the same status and headers with no body: Bambuddy's media routes answer only
    ``GET``, so the upstream request is a ``GET`` whose body is never read.
    """
    stack = AsyncExitStack()
    try:
        client = await stack.enter_async_context(client_for(store.load()))
        upstream = await stack.enter_async_context(
            client.stream(
                path,
                operation=operation,
                what=what,
                range_header=request.headers.get("range"),
                if_range=request.headers.get("if-range"),
            )
        )
    except BaseException:
        await stack.aclose()
        raise

    headers = {
        name: upstream.headers[name] for name in FORWARDED_HEADERS if name in upstream.headers
    }
    # Bambuddy's bytes are served from ScadBuddy's origin, so a mislabeled response
    # must never be sniffed into HTML here (#608).
    headers["x-content-type-options"] = "nosniff"
    if cache_control is not None and upstream.status_code in (200, 206):
        headers["cache-control"] = cache_control
    if request.method == "HEAD":
        await stack.aclose()
        return Response(status_code=upstream.status_code, headers=headers)

    async def body() -> AsyncIterator[bytes]:
        # The stack is closed with the failure, so `BambuddyClient.stream` sees a read
        # that failed midway and fails its span (#1192).
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        except BaseException as error:
            await stack.__aexit__(type(error), error, error.__traceback__)
            raise
        await stack.aclose()

    return StreamingResponse(body(), status_code=upstream.status_code, headers=headers)


_Handler = Callable[..., Awaitable[Response]]


def _media_route(path: str, summary: str) -> Callable[[_Handler], _Handler]:
    """A GET, and a HEAD with the same handler left out of the schema. One route with
    both methods takes its ``operationId`` from whichever method its set yields first,
    which changes with the hash seed, and gives both operations that one id. Register
    every route here that answers HEAD through this, not ``api_route``."""

    def register(handler: _Handler) -> _Handler:
        router.head(path, include_in_schema=False)(handler)
        return router.get(path, summary=summary)(handler)

    return register


@_media_route("/timelapse", "The print's timelapse video (Range supported)")
async def get_timelapse(
    archive_id: ArchiveIdPath, request: Request, store: SettingsStoreDep
) -> Response:
    return await _proxy(
        store,
        request,
        f"/archives/{archive_id}/timelapse",
        operation="media.timelapse",
        what="play the timelapse",
    )


class _CardPhotos:
    """The card-size photos made most recently, by Bambuddy, archive and name; only
    touched on the loop. A photo's name is Bambuddy's for one capture, so what is kept
    under it never goes stale."""

    def __init__(self, size: int = CARD_PHOTO_CACHE_ITEMS) -> None:
        self.size = size
        self._items: OrderedDict[tuple[str, int, str], tuple[bytes, str]] = OrderedDict()

    def get(self, key: tuple[str, int, str]) -> tuple[bytes, str] | None:
        kept = self._items.get(key)
        if kept is not None:
            self._items.move_to_end(key)
        return kept

    def put(self, key: tuple[str, int, str], photo: tuple[bytes, str]) -> None:
        self._items[key] = photo
        self._items.move_to_end(key)
        while len(self._items) > self.size:
            self._items.popitem(last=False)


async def _card_photo(
    store: SettingsStore, request: Request, archive_id: int, filename: str
) -> tuple[bytes, str] | None:
    """The photo shrunk to a card's cover, as a WebP no larger than `CARD_SIDE` (#2355),
    made once per photo; the photo as it is when it cannot be decoded, and None when it is
    over `MAX_IMAGE_BYTES`. Decodes share
    the catalogue thumbnails' limit (`MAX_CONCURRENT_THUMBNAILS`)."""
    state = request.app.state
    cache: _CardPhotos | None = getattr(state, "print_card_photos", None)
    if cache is None:
        cache = _CardPhotos()
        state.print_card_photos = cache
    decodes: asyncio.Semaphore | None = getattr(state, "thumbnail_decodes", None)
    if decodes is None:
        decodes = asyncio.Semaphore(MAX_CONCURRENT_THUMBNAILS)
        state.thumbnail_decodes = decodes
    async with client_for(store.load()) as client:
        key = (client.config.base_url, archive_id, filename)
        if (kept := cache.get(key)) is not None:
            return kept
        async with client.stream(
            f"/archives/{archive_id}/photos/{filename}",
            operation="media.photo",
            what="show the photo",
        ) as upstream:
            # Read at most `MAX_IMAGE_BYTES`: a larger body is not shrunk here, it is
            # streamed whole by the caller, so no photo is ever held in memory past it.
            chunks: list[bytes] = []
            read = 0
            async for chunk in upstream.aiter_bytes():
                read += len(chunk)
                if read > MAX_IMAGE_BYTES:
                    return None
                chunks.append(chunk)
            original = b"".join(chunks)
            content_type = upstream.headers.get("content-type", "application/octet-stream")
    async with decodes:
        if (kept := cache.get(key)) is not None:
            return kept
        small = await asyncio.to_thread(_shrink, io.BytesIO(original), CARD_SIDE)
    if small is None:
        return original, content_type
    cache.put(key, (small, "image/webp"))
    return small, "image/webp"


@_media_route("/photos/{filename}", "A photo of the print")
async def get_photo(
    archive_id: ArchiveIdPath,
    filename: Annotated[str, Path(pattern=PHOTO_NAME)],
    request: Request,
    store: SettingsStoreDep,
    size: Annotated[
        Literal["card"] | None,
        Query(description="`card`: a WebP no larger than a card's cover needs (#2355)"),
    ] = None,
    v: Annotated[int | None, Query(description="The thumbnail version asked for")] = None,
) -> Response:
    card = await _card_photo(store, request, archive_id, filename) if size == "card" else None
    if card is not None:
        body, media_type = card
        cache = IMMUTABLE_CACHE_CONTROL if v == THUMBNAIL_VERSION else LEGACY_CACHE_CONTROL
        headers = {"Cache-Control": cache, "x-content-type-options": "nosniff"}
        if request.method == "HEAD":
            return Response(media_type=media_type, headers=headers)
        return Response(body, media_type=media_type, headers=headers)
    # A photo's name is Bambuddy's for one capture: the bytes behind it never change.
    return await _proxy(
        store,
        request,
        f"/archives/{archive_id}/photos/{filename}",
        operation="media.photo",
        what="show the photo",
        cache_control=IMMUTABLE_CACHE_CONTROL,
    )


@_media_route("/thumbnail", "The print's thumbnail")
async def get_thumbnail(
    archive_id: ArchiveIdPath, request: Request, store: SettingsStoreDep
) -> Response:
    return await _proxy(
        store,
        request,
        f"/archives/{archive_id}/thumbnail",
        operation="media.thumbnail",
        what="show the thumbnail",
    )


@_media_route("/plates/{index}/thumbnail", "One plate's image from the slicer")
async def get_plate_thumbnail(
    archive_id: ArchiveIdPath,
    index: Annotated[int, Path(ge=1)],
    request: Request,
    store: SettingsStoreDep,
) -> Response:
    return await _proxy(
        store,
        request,
        f"/archives/{archive_id}/plate-thumbnail/{index}",
        operation="media.plate_thumbnail",
        what="show the plate image",
    )


@_media_route("/files/sliced", "The sliced file that was printed")
async def get_sliced_file(
    archive_id: ArchiveIdPath, request: Request, store: SettingsStoreDep
) -> Response:
    return await _proxy(
        store,
        request,
        f"/archives/{archive_id}/download",
        operation="media.download",
        what="download the sliced file",
    )


@_media_route("/files/source", "The slicer project 3MF, when there is one")
async def get_source_file(
    archive_id: ArchiveIdPath, request: Request, store: SettingsStoreDep
) -> Response:
    return await _proxy(
        store,
        request,
        f"/archives/{archive_id}/source",
        operation="media.source",
        what="download the source 3MF",
    )


@_media_route(
    "/attachments/{attachment_id}/file", "A photo or video added to the print (Range supported)"
)
async def get_attachment_file(
    archive_id: ArchiveIdPath,
    attachment_id: Annotated[int, Path(ge=1)],
    request: Request,
    store: SettingsStoreDep,
    attachments: PrintAttachmentsDep,
) -> Response:
    """#309: only a file recorded as one of this print's attachments, so the proxy is no
    window onto the rest of Bambuddy's library. Its ``Media/`` copy, through Bambuddy's
    library download (Read Status, which serves ``Range``); a photo with no copy
    recorded, from the archive. Served inline, typed by what it is, for an ``<img>``
    or ``<video>``."""
    found = await attachments.get(archive_id, attachment_id)
    if found is None:
        raise ApiError(
            status.HTTP_404_NOT_FOUND, f"print {archive_id} has no attachment {attachment_id}"
        )
    path = attachment_file_path(found)
    response = await _proxy(
        store, request, path, operation="media.attachment", what="show the attachment"
    )
    response.headers["content-type"] = content_type_of(found.filename)
    if "content-disposition" in response.headers:
        del response.headers["content-disposition"]
    return response
