"""A body-size gate that runs before anything reads the body.

A `max_length` on a pydantic field refuses an oversized source, but only after the
whole body has been buffered — FastAPI reads it before it resolves a single
dependency. For the paste routes that is the wrong order: the point of the cap is
not to reject the value, it is to not read a gigabyte into the pod's memory in the
first place. Content-Length says how much is coming, so the refusal can happen on
the headers alone.

A body sent without Content-Length (chunked) does not carry that promise, so it is
still bounded only by the field cap — the gate closes the declared case, which is
what every ordinary client sends.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from scadbuddy.core.problems import PROBLEM_MEDIA_TYPE

#: Only the text bodies are gated: a multipart upload legitimately carries a
#: thumbnail and a README beside the source, and its own limits live elsewhere.
GATED_CONTENT_TYPES = frozenset({"application/json", "text/plain"})

#: The ceiling for those bodies. Well above `MAX_SOURCE_CHARS` even once the source is
#: JSON-escaped and UTF-8 encoded, so the field cap stays the thing that answers 422
#: for a source that is merely too long; this one only stops a body that was never
#: going to be read to the end.
MAX_TEXT_BODY_BYTES = 8 * 1024 * 1024


class BodySizeGate:
    """Refuse a declared body larger than `limit` bytes with a 413 problem."""

    def __init__(self, app: ASGIApp, *, limit: int) -> None:
        self.app = app
        self.limit = limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = Headers(scope=scope)
            kind = headers.get("content-type", "").split(";")[0].strip().lower()
            declared = headers.get("content-length", "")
            if kind in GATED_CONTENT_TYPES and declared.isdigit() and int(declared) > self.limit:
                response = JSONResponse(
                    {
                        "type": "about:blank",
                        "title": "Content Too Large",
                        "status": 413,
                        "detail": (
                            f"the body declares {declared} bytes and this API reads at "
                            f"most {self.limit}"
                        ),
                        "instance": scope.get("path", ""),
                    },
                    status_code=413,
                    media_type=PROBLEM_MEDIA_TYPE,
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


class ClientGoneError(Exception):
    """The client hung up before the work it asked for finished."""


async def _client_hung_up(request: Request) -> None:
    """Resolve when the client disconnects.

    Starlette does not watch for this: `request_response` awaits the endpoint and
    nothing cancels it, so a handler keeps running for a request nobody is listening
    to. Once the body has been read, the only message left on `receive` is the
    disconnect, so this waits rather than polls.
    """
    while True:
        message = await request.receive()
        if message["type"] == "http.disconnect":
            return


async def unless_the_client_leaves[T](request: Request, work: Coroutine[Any, Any, T]) -> T:
    """Run `work`, cancelling it if the client disconnects first.

    The editor supersedes its own parse check on every keystroke pause, and the
    browser aborts the request it no longer wants. Without this the server finishes
    every one of them anyway, each holding the single check permit in turn, so the
    check the user IS waiting for queues behind work that was abandoned.
    """
    task = asyncio.ensure_future(work)
    watcher = asyncio.ensure_future(_client_hung_up(request))
    try:
        await asyncio.wait({task, watcher}, return_when=asyncio.FIRST_COMPLETED)
        if task.done():
            return task.result()
        task.cancel()
        raise ClientGoneError("the client disconnected before the work finished")
    finally:
        watcher.cancel()
        await asyncio.gather(task, watcher, return_exceptions=True)
