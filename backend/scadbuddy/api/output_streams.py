"""An output's bytes as a response (#7256): `OutputStore.open_role` and `open_file` give
an `OutputStream` of the blob (#7257), and this sends it.

It lives apart from `api/outputs.py` because `bambuddy/download.py` uses it too, and
`api/outputs.py` imports that module; `api/outputs.py` re-exports it.

A conditional GET gets its ``304`` (#7256): a blob's strong ``ETag`` is its sha256,
which `blob_not_modified` compares from the index before any byte of the blob is read,
once the backend says it holds the object (`OutputBytes.held_sha`: a lost blob's file
serves, with its own tag); a
file's weak ``ETag`` is from its size and mtime, read on the descriptor that serves it
(`stream_response`). A request whose ``v`` names the bytes' current version
(`OutputStream.version`) is cached for good (`IMMUTABLE_CACHE_CONTROL`).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping
from urllib.parse import quote

import anyio
from fastapi import Response, status
from fastapi.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from scadbuddy.api.media import IMMUTABLE_CACHE_CONTROL
from scadbuddy.api.models import etag_matches
from scadbuddy.library.output_bytes import OutputStream, blob_version

logger = logging.getLogger(__name__)


async def _body(stream: OutputStream) -> AsyncIterator[bytes]:
    """The stream's chunks. An error past the first byte (a corrupt blob over
    `output_bytes._PREFETCH`, a transport error) is logged and raised again, so the
    response ends short of its ``Content-Length`` and never ends clean."""
    try:
        async for chunk in stream.chunks:
            yield chunk
    except Exception:
        logger.error("an output's bytes broke off in the middle of a response", exc_info=True)
        raise


async def _nothing() -> AsyncIterator[bytes]:
    return
    yield b""


def _matches(if_none_match: str | None, etag: str) -> bool:
    """`api/models.py` `etag_matches`, weakly: the tag may be weak itself."""
    return etag_matches(if_none_match, etag.removeprefix("W/"))


def _cache(current: str | None, asked: str | None) -> dict[str, str]:
    """``Cache-Control`` for good when the request's ``v`` names the current version."""
    if asked is not None and asked == current:
        return {"cache-control": IMMUTABLE_CACHE_CONTROL}
    return {}


def blob_not_modified(
    sha256: str | None, *, if_none_match: str | None, v: str | None = None
) -> Response | None:
    """The ``304`` for a blob whose sha256 (from the index) the request's
    ``If-None-Match`` names, before the blob is read; ``None`` otherwise."""
    if sha256 is None:
        return None
    etag = f'"{sha256}"'
    if not _matches(if_none_match, etag):
        return None
    return Response(
        status_code=status.HTTP_304_NOT_MODIFIED,
        headers={"etag": etag, **_cache(blob_version(sha256), v)},
    )


class _OutputStreamResponse(StreamingResponse):
    """A response that closes its stream however it ends: sent, failed, cancelled by a
    client that went away, or never sent at all. The close is shielded, so a cancel
    does not stop the backend's read from closing."""

    def __init__(
        self,
        stream: OutputStream,
        *,
        media_type: str | None,
        headers: Mapping[str, str],
        status_code: int = status.HTTP_200_OK,
    ) -> None:
        body = _body(stream) if status_code == status.HTTP_200_OK else _nothing()
        super().__init__(body, status_code=status_code, media_type=media_type, headers=headers)
        self._stream = stream

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await self._stream.aclose()


def attachment(filename: str) -> str:
    """A ``Content-Disposition`` that downloads as ``filename``, as Starlette's
    `FileResponse` writes it."""
    quoted = quote(filename)
    if quoted != filename:
        return f"attachment; filename*=utf-8''{quoted}"
    return f'attachment; filename="{filename}"'


def stream_response(
    stream: OutputStream,
    *,
    media_type: str,
    headers: Mapping[str, str],
    if_none_match: str | None = None,
    v: str | None = None,
) -> StreamingResponse:
    """``stream`` as the body, with ``Content-Length`` its size, its ``ETag``
    (`OutputStream.etag`: strong for a blob, weak for a file), and no ranges. ``headers``
    are added. A ``v`` that names the stream's version adds `IMMUTABLE_CACHE_CONTROL`. An
    ``If-None-Match`` that names the tag gets a ``304`` with no body. The response closes
    the stream. A route that does more work between the open and this call closes the
    stream itself when that work raises."""
    etag = stream.etag
    cache = _cache(stream.version, v)
    if etag is not None and _matches(if_none_match, etag):
        return _OutputStreamResponse(
            stream,
            media_type=None,
            headers={"etag": etag, **cache},
            status_code=status.HTTP_304_NOT_MODIFIED,
        )
    sent = {
        **headers,
        **cache,
        "content-length": str(stream.size),
        "accept-ranges": "none",
    }
    if etag is not None:
        sent["etag"] = etag
    return _OutputStreamResponse(stream, media_type=media_type, headers=sent)
