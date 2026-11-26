"""A streamed response that releases what it holds however it ends: the MJPEG camera route
(`api/printing.py`) and the live camera proxy (`api/camera.py`)."""

from __future__ import annotations

from contextlib import AsyncExitStack
from typing import Any

from fastapi.responses import StreamingResponse
from starlette.types import Receive, Scope, Send


class ClosingStreamingResponse(StreamingResponse):
    """A stream that closes ``closing`` however the response ends (#2752 review).

    A viewer gone before the first byte fails the response's start, so the body
    generator never runs and Starlette skips ``background``: neither would release
    the stream. Closing here does, whether the response finished, failed or was
    cancelled; a stack the body already closed closes again as a no-op."""

    def __init__(self, *args: Any, closing: AsyncExitStack, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._closing = closing

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self._closing.aclose()
