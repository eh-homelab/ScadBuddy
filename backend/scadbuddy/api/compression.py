"""Responses go out gzipped when the client accepts it (#1033).

Nothing in front of the app compresses: the Envoy Gateway route passes the bytes
through as they are, so without this the JS bundle and every JSON answer cross the
wire at about 3.5 times their gzipped size.

Starlette's middleware already leaves alone a ``206``, a response that carries its own
``Content-Encoding`` (the Bambuddy proxies forward upstream's), ``text/event-stream``,
and images, audio, video and fonts. Zip containers are added here: a 3MF is a zip, so
gzipping one spends CPU to save nothing. So is ``application/octet-stream``, the type a
pipeline output's extra files go out as (#1855): those are often a zip or a 3MF already.
That gives up on the few that would shrink (an ASCII STL or a CSV extra, a media file of
an unknown type) on purpose: the bytes are unknown, and most are compressed already.
A GLB is not excluded: the previews carry no
Draco or meshopt compression, so their float buffers do shrink.
"""

from __future__ import annotations

from starlette.datastructures import MutableHeaders
from starlette.middleware.gzip import DEFAULT_EXCLUDED_CONTENT_TYPES, GZipMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

#: Below this the gzip framing costs about what it saves.
MINIMUM_SIZE = 1000
#: zlib's own default. Level 9 takes about twice as long for about 1% less.
COMPRESS_LEVEL = 6
EXCLUDED_CONTENT_TYPES = (
    *DEFAULT_EXCLUDED_CONTENT_TYPES,
    "model/3mf",
    "application/octet-stream",
)


class Compression:
    """Gzip the response, and weaken a strong ``ETag`` on any encoded one.

    The ETag is weakened because the gzipped bytes are not the bytes that strong tag
    names (RFC 9110 §8.8.3). A conditional ``If-None-Match`` still gets its ``304``,
    since every comparator here compares weakly. An ``If-Range`` naming the weak tag gets
    the whole file rather than a range of bytes that would not fit the client's copy.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = GZipMiddleware(
            app,
            minimum_size=MINIMUM_SIZE,
            compresslevel=COMPRESS_LEVEL,
            exclude_content_types=EXCLUDED_CONTENT_TYPES,
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def weaken(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(raw=message["headers"])
                etag = headers.get("etag")
                if "content-encoding" in headers and etag and not etag.startswith("W/"):
                    headers["etag"] = f"W/{etag}"
            await send(message)

        await self.app(scope, receive, weaken)
