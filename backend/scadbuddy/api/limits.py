"""A body-size gate that runs before anything reads the body.

A `max_length` on a pydantic field refuses an oversized source, but only after the
whole body has been buffered — FastAPI reads it before it resolves a single
dependency. For the paste routes that is the wrong order: the point of the cap is
not to reject the value, it is to not read a gigabyte into the pod's memory in the
first place. Content-Length says how much is coming, so the refusal can happen on
the headers alone.

A body sent without Content-Length (chunked) makes no such promise, so it is counted
as it arrives instead, and refused the moment it passes the same limit. Either way no
more than the limit is ever buffered.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine, Mapping
from typing import Any

from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from scadbuddy.core.problems import PROBLEM_MEDIA_TYPE

#: The ceiling for the text bodies. Well above `MAX_SOURCE_CHARS` even once the source
#: is JSON-escaped and UTF-8 encoded, so the field cap stays the thing that answers 422
#: for a source that is merely too long; this one only stops a body that was never
#: going to be read to the end.
MAX_TEXT_BODY_BYTES = 8 * 1024 * 1024

#: The ceiling for a multipart upload, which carries a thumbnail and a README beside
#: the source. Starlette caps none of that by default: a non-file field is held to
#: 1 MiB, but a file part is spooled to disk without any total, and the routes then
#: `read()` it whole. This leaves room for a source at `MAX_SOURCE_CHARS` fully
#: UTF-8 encoded (4 MiB), a README as large again, and a viewer-sized PNG.
MAX_MULTIPART_BODY_BYTES = 32 * 1024 * 1024

#: The gated content types and what each may carry. A url-encoded form holds no
#: files, so it gets the text ceiling; anything else is not a body any route reads.
BODY_LIMITS: Mapping[str, int] = {
    "application/json": MAX_TEXT_BODY_BYTES,
    "text/plain": MAX_TEXT_BODY_BYTES,
    "application/x-www-form-urlencoded": MAX_TEXT_BODY_BYTES,
    "multipart/form-data": MAX_MULTIPART_BODY_BYTES,
}


class _BodyTooLargeError(Exception):
    """A streamed body passed the limit; raised out of `receive` to stop the read."""


class BodySizeGate:
    """Refuse a gated body larger than its content type's limit with a 413 problem.

    A declared length is judged on the headers alone. An undeclared one is counted
    chunk by chunk, and reading stops at the first chunk that crosses the limit.
    """

    def __init__(self, app: ASGIApp, *, limits: Mapping[str, int]) -> None:
        self.app = app
        self.limits = limits

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        kind = headers.get("content-type", "").split(";")[0].strip().lower()
        limit = self.limits.get(kind)
        if limit is None:
            await self.app(scope, receive, send)
            return
        declared = headers.get("content-length", "")
        if declared.isdigit():
            if int(declared) > limit:
                await self._refuse(
                    scope,
                    receive,
                    send,
                    f"the body declares {declared} bytes and this API reads at most {limit}",
                )
                return
            await self.app(scope, receive, send)
            return

        received = 0
        started = False

        async def counted_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _BodyTooLargeError
            return message

        async def tracked_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counted_receive, tracked_send)
        except _BodyTooLargeError:
            if started:  # pragma: no cover - the body is read before any response
                raise
            await self._refuse(
                scope,
                receive,
                send,
                f"the body passed {limit} bytes, which is the most this API reads",
            )

    @staticmethod
    async def _refuse(scope: Scope, receive: Receive, send: Send, detail: str) -> None:
        response = JSONResponse(
            {
                "type": "about:blank",
                "title": "Content Too Large",
                "status": 413,
                "detail": detail,
                "instance": scope.get("path", ""),
            },
            status_code=413,
            media_type=PROBLEM_MEDIA_TYPE,
        )
        await response(scope, receive, send)


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
