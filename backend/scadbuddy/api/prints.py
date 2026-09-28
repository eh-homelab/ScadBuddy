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

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Request, Response, status
from fastapi.responses import StreamingResponse

from scadbuddy.api.deps import PrintLinksDep, SettingsStoreDep
from scadbuddy.bambuddy.client import client_for
from scadbuddy.core.problems import ApiError
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


async def require_linked_archive(archive_id: ArchiveIdPath, links: PrintLinksDep) -> None:
    """Refuse an archive no ScadBuddy output printed, with a 404, so the proxy is not
    a window onto all of Bambuddy's history (#305 plan §2.3). The check is
    ``output_bambuddy_prints`` (#306): an archive is linked once a progress read or a
    project attach has seen it."""
    # Without a database nothing is linked, so nothing is served (#522 review).
    if not links.available or await links.output_for(archive_id) is None:
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


async def _proxy(store: SettingsStore, request: Request, path: str, *, what: str) -> Response:
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
    if request.method == "HEAD":
        await stack.aclose()
        return Response(status_code=upstream.status_code, headers=headers)

    async def body() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await stack.aclose()

    return StreamingResponse(body(), status_code=upstream.status_code, headers=headers)


_Handler = Callable[..., Awaitable[Response]]


def _media_route(path: str, summary: str) -> Callable[[_Handler], _Handler]:
    """A GET, and a HEAD with the same handler left out of the schema. One route with
    both methods takes its ``operationId`` from whichever method its set yields first,
    which changes with the hash seed, and gives both operations that one id."""

    def register(handler: _Handler) -> _Handler:
        router.head(path, include_in_schema=False)(handler)
        return router.get(path, summary=summary)(handler)

    return register


@_media_route("/timelapse", "The print's timelapse video (Range supported)")
async def get_timelapse(
    archive_id: ArchiveIdPath, request: Request, store: SettingsStoreDep
) -> Response:
    return await _proxy(
        store, request, f"/archives/{archive_id}/timelapse", what="play the timelapse"
    )


@_media_route("/photos/{filename}", "A photo of the print")
async def get_photo(
    archive_id: ArchiveIdPath,
    filename: Annotated[str, Path(pattern=PHOTO_NAME)],
    request: Request,
    store: SettingsStoreDep,
) -> Response:
    return await _proxy(
        store, request, f"/archives/{archive_id}/photos/{filename}", what="show the photo"
    )


@_media_route("/thumbnail", "The print's thumbnail")
async def get_thumbnail(
    archive_id: ArchiveIdPath, request: Request, store: SettingsStoreDep
) -> Response:
    return await _proxy(
        store, request, f"/archives/{archive_id}/thumbnail", what="show the thumbnail"
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
        what="show the plate image",
    )


@_media_route("/files/sliced", "The sliced file that was printed")
async def get_sliced_file(
    archive_id: ArchiveIdPath, request: Request, store: SettingsStoreDep
) -> Response:
    return await _proxy(
        store, request, f"/archives/{archive_id}/download", what="download the sliced file"
    )


@_media_route("/files/source", "The slicer project 3MF, when there is one")
async def get_source_file(
    archive_id: ArchiveIdPath, request: Request, store: SettingsStoreDep
) -> Response:
    return await _proxy(
        store, request, f"/archives/{archive_id}/source", what="download the source 3MF"
    )
