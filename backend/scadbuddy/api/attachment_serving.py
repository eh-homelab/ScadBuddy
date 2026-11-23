"""How a kept attachment's file is served (#4251, #6950), shared by an order's routes
(`api/order_attachments.py`) and any entity's (`api/entity_attachments.py`).

Only the types a browser shows and never runs (``orders/models.py`` ``INLINE``) are
served as themselves; every other file is ``application/octet-stream`` and a download,
under a sandboxing policy, so an uploaded page never runs on ScadBuddy's origin.

No ``from __future__ import annotations``: FastAPI reads the dependency annotations at
runtime (``api/components.py``)."""

import asyncio

from fastapi import Request, Response, status
from fastapi.responses import FileResponse

from scadbuddy.api import media as media_api
from scadbuddy.core.problems import ApiError
from scadbuddy.orders import attachments
from scadbuddy.orders.models import INLINE, MAX_ATTACHMENT_CAPTION, THUMBNAILED
from scadbuddy.store import BlobStore

#: An attachment's id names its bytes for good: a changed file is a new attachment.
IMMUTABLE = "private, max-age=31536000, immutable"
#: Every file served: no sniffing it into something it is not.
NOSNIFF = {"X-Content-Type-Options": "nosniff"}
#: An image, a video or a download opened as a page: inert, as `api/assets.py`'s
#: user-supplied images are. Not a PDF's: a browser's PDF viewer will not run under it.
INERT = {
    "Content-Security-Policy": (
        "default-src 'none'; img-src 'self'; media-src 'self'; style-src 'unsafe-inline'; sandbox"
    ),
}


def caption_of(received: media_api.ReceivedUpload) -> str:
    """An upload's ``caption`` part: UTF-8, at most `MAX_ATTACHMENT_CAPTION` characters."""
    try:
        caption = received.caption.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, "the caption is not UTF-8") from None
    if len(caption) > MAX_ATTACHMENT_CAPTION:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"a caption is at most {MAX_ATTACHMENT_CAPTION} characters",
        )
    return caption


async def serve_content(
    blobs: BlobStore,
    key: str,
    *,
    attachment_id: str,
    media_type: str,
    filename: str,
    download: bool,
) -> FileResponse:
    """The kept file: inline as itself only for an `INLINE` type (and not ``download``),
    else a download of ``application/octet-stream``; ``Range`` is honoured."""
    path = await attachments.blob_path(blobs, key)
    if path is None:
        raise ApiError(
            status.HTTP_404_NOT_FOUND, f"the file of attachment {attachment_id!r} is gone"
        )
    inline = media_type in INLINE and not download
    headers = {"Cache-Control": IMMUTABLE, **NOSNIFF}
    if media_type != "application/pdf" or not inline:
        headers |= INERT
    return FileResponse(
        path,
        media_type=media_type if inline else "application/octet-stream",
        filename=filename,
        content_disposition_type="inline" if inline else "attachment",
        headers=headers,
    )


async def serve_thumbnail(
    blobs: BlobStore, key: str, *, attachment_id: str, media_type: str, request: Request
) -> Response:
    """A PNG, JPEG or WebP photo shrunk to `media_api.CARD_SIDE` as WebP; 404 otherwise."""
    if media_type not in THUMBNAILED:
        raise ApiError(status.HTTP_404_NOT_FOUND, "only a PNG, JPEG or WebP photo has a thumbnail")
    path = await attachments.blob_path(blobs, key)
    if path is None:
        raise ApiError(
            status.HTTP_404_NOT_FOUND, f"the file of attachment {attachment_id!r} is gone"
        )
    # The media routes' decode budget, shared, so photos here never starve a gallery.
    decodes = media_api.decode_slots(request)

    def shrink() -> bytes | None:
        with path.open("rb") as handle:
            return media_api.shrink(handle, media_api.CARD_SIDE)

    async with decodes:
        small = await asyncio.to_thread(shrink)
    if small is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "this photo cannot be shrunk; show the file")
    return Response(small, media_type="image/webp", headers={"Cache-Control": IMMUTABLE, **NOSNIFF})
