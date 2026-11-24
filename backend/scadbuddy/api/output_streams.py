"""An output's bytes as a response (#7256): `OutputStore.open_role` and `open_file` give
an `OutputStream`, which is the blob or the NFS file, and this sends it.

It lives apart from `api/outputs.py` because `bambuddy/download.py` uses it too, and
`api/outputs.py` imports that module; `api/outputs.py` re-exports it.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping
from urllib.parse import quote

import anyio
from fastapi.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from scadbuddy.library.output_bytes import OutputStream

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


class _OutputStreamResponse(StreamingResponse):
    """A response that closes its stream however it ends: sent, failed, cancelled by a
    client that went away, or never sent at all. The close is shielded, so a cancel
    does not stop the backend's read from closing."""

    def __init__(
        self, stream: OutputStream, *, media_type: str, headers: Mapping[str, str]
    ) -> None:
        super().__init__(_body(stream), media_type=media_type, headers=headers)
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
    stream: OutputStream, *, media_type: str, headers: Mapping[str, str]
) -> StreamingResponse:
    """``stream`` as the body, with ``Content-Length`` its size, a strong ``ETag`` when
    its sha256 is known (a blob; a file has none), and no ranges. ``headers`` are added.
    The response closes the stream. A route that does more work between the open and
    this call closes the stream itself when that work raises."""
    sent = {
        **headers,
        "content-length": str(stream.size),
        "accept-ranges": "none",
    }
    if stream.sha256 is not None:
        sent["etag"] = f'"{stream.sha256}"'
    return _OutputStreamResponse(stream, media_type=media_type, headers=sent)
