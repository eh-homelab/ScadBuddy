"""The media of a print, proxied from Bambuddy (#307, epic #305).

Every byte comes through ScadBuddy's server, so Bambuddy's API key never reaches the
browser and nothing depends on Bambuddy being reachable from it (the iframe). A video
seek is a ``Range`` request: it is passed through, and Bambuddy's ``FileResponse``
answers it with a ``206`` and only those bytes, streamed back without buffering.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from typing import Annotated, Any

from fastapi import APIRouter, Path, Request
from fastapi.responses import StreamingResponse

from scadbuddy.api.deps import SettingsStoreDep
from scadbuddy.bambuddy.client import client_for
from scadbuddy.library.settings_store import SettingsStore

#: What the proxy answers: Bambuddy's bytes, whole (200) or a range of them (206).
MEDIA_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {"content": {"application/octet-stream": {}}, "description": "The file"},
    206: {"content": {"application/octet-stream": {}}, "description": "The requested range"},
}

router = APIRouter(
    prefix="/prints/{archive_id}",
    tags=["prints"],
    default_response_class=StreamingResponse,
    responses=MEDIA_RESPONSES,
)

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


def require_linked_archive(archive_id: int) -> None:
    """Refuse an archive no ScadBuddy output printed, so the proxy is not a window onto
    all of Bambuddy's history (#305 plan §2.3).

    A no-op until #306 records the output → archive links: until then ScadBuddy has no
    list to check against, and it already exposes Bambuddy's sends to whoever reaches
    it. #306 replaces the body with the Postgres lookup.
    """
    del archive_id


async def _proxy(
    store: SettingsStore, request: Request, path: str, *, what: str
) -> StreamingResponse:
    """Stream ``path`` from Bambuddy, with the browser's ``Range`` passed through.

    The client and the upstream response stay open until the body has been sent, so
    both live on an exit stack that the body's own generator closes.
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

    async def body() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await stack.aclose()

    headers = {
        name: upstream.headers[name] for name in FORWARDED_HEADERS if name in upstream.headers
    }
    return StreamingResponse(body(), status_code=upstream.status_code, headers=headers)


@router.get("/timelapse", summary="The print's timelapse video (Range supported)")
async def get_timelapse(
    archive_id: ArchiveIdPath, request: Request, store: SettingsStoreDep
) -> StreamingResponse:
    require_linked_archive(archive_id)
    return await _proxy(
        store, request, f"/archives/{archive_id}/timelapse", what="play the timelapse"
    )


@router.get("/photos/{filename}", summary="A photo of the print")
async def get_photo(
    archive_id: ArchiveIdPath,
    filename: Annotated[str, Path(pattern=PHOTO_NAME)],
    request: Request,
    store: SettingsStoreDep,
) -> StreamingResponse:
    require_linked_archive(archive_id)
    return await _proxy(
        store, request, f"/archives/{archive_id}/photos/{filename}", what="show the photo"
    )


@router.get("/thumbnail", summary="The print's thumbnail")
async def get_thumbnail(
    archive_id: ArchiveIdPath, request: Request, store: SettingsStoreDep
) -> StreamingResponse:
    require_linked_archive(archive_id)
    return await _proxy(
        store, request, f"/archives/{archive_id}/thumbnail", what="show the thumbnail"
    )


@router.get("/plates/{index}/thumbnail", summary="One plate's image from the slicer")
async def get_plate_thumbnail(
    archive_id: ArchiveIdPath,
    index: Annotated[int, Path(ge=1)],
    request: Request,
    store: SettingsStoreDep,
) -> StreamingResponse:
    require_linked_archive(archive_id)
    return await _proxy(
        store,
        request,
        f"/archives/{archive_id}/plate-thumbnail/{index}",
        what="show the plate image",
    )


@router.get("/files/sliced", summary="The sliced file that was printed")
async def get_sliced_file(
    archive_id: ArchiveIdPath, request: Request, store: SettingsStoreDep
) -> StreamingResponse:
    require_linked_archive(archive_id)
    return await _proxy(
        store, request, f"/archives/{archive_id}/download", what="download the sliced file"
    )


@router.get("/files/source", summary="The slicer project 3MF, when there is one")
async def get_source_file(
    archive_id: ArchiveIdPath, request: Request, store: SettingsStoreDep
) -> StreamingResponse:
    require_linked_archive(archive_id)
    return await _proxy(
        store, request, f"/archives/{archive_id}/source", what="download the source 3MF"
    )


@router.get(
    "/attachments/{library_file_id}",
    summary="A photo or video of the print kept in Bambuddy's library (Range supported)",
)
async def get_attachment(
    archive_id: ArchiveIdPath,
    library_file_id: Annotated[int, Path(ge=1)],
    request: Request,
    store: SettingsStoreDep,
) -> StreamingResponse:
    """#309 records which library files are a print's attachments and checks it here;
    until then this is gated like the rest."""
    require_linked_archive(archive_id)
    return await _proxy(
        store,
        request,
        f"/library/files/{library_file_id}/download",
        what="play the attachment",
    )
