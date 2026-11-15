"""The visitor cookie (#5282, spec 2026-11-02 §5.2, plan "Trust model").

``sb_visitor=v1.<visitor id>.<mac>``: the visitor id is 32 random bytes as URL-safe
base64 (43 characters), the mac HMAC-SHA256 over ``v1.<visitor id>`` under the edge's own
cookie key, URL-safe base64. The CRUD side does not hold the key: the edge drops a cookie
whose mac fails and gives that request a new visitor, before any CRUD call.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from typing import Final

from starlette.datastructures import MutableHeaders
from starlette.requests import cookie_parser
from starlette.types import ASGIApp, Message, Receive, Scope, Send

VISITOR_COOKIE: Final = "sb_visitor"
VISITOR_MAX_AGE: Final = 400 * 24 * 3600
VERSION: Final = "v1"
#: A visitor id and a mac alike: 32 bytes as URL-safe base64, unpadded.
_B64_32: Final = re.compile(r"[A-Za-z0-9_-]{43}")


def cookie_mac(version: str, value: str, key: bytes) -> str:
    """HMAC-SHA256 over ``<version>.<value>`` under the edge's cookie key, URL-safe base64
    unpadded: the mac of the visitor cookie and of the session cookie."""
    digest = hmac.new(key, f"{version}.{value}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _mac(visitor_id: str, key: bytes) -> str:
    return cookie_mac(VERSION, visitor_id, key)


def sign(visitor_id: str, key: bytes) -> str:
    return f"{VERSION}.{visitor_id}.{_mac(visitor_id, key)}"


def verify(cookie: str, key: bytes) -> str | None:
    """The visitor id the cookie carries, or None when it is not one this key signed. Both
    parts are checked to be URL-safe base64 first, so the compare sees ASCII only."""
    version, _, rest = cookie.partition(".")
    visitor_id, _, mac = rest.partition(".")
    if version != VERSION or _B64_32.fullmatch(visitor_id) is None:
        return None
    if _B64_32.fullmatch(mac) is None:
        return None
    expected = _mac(visitor_id, key).encode()
    return visitor_id if hmac.compare_digest(mac.encode(), expected) else None


def _cookie(headers: list[tuple[bytes, bytes]]) -> str | None:
    """The visitor cookie, read as Starlette reads ``request.cookies``: a cookie it cannot
    parse costs only itself, never the others in the header."""
    for name, value in headers:
        if name == b"cookie":
            found = cookie_parser(value.decode("latin-1")).get(VISITOR_COOKIE)
            if found is not None:
                return found
    return None


class VisitorCookie:
    """ASGI middleware for the paths under ``prefix``: puts the verified visitor id on
    ``request.state.visitor_id``, and sets a new signed cookie when the request had none
    that verifies, unless the route's response sets one itself."""

    def __init__(self, app: ASGIApp, *, key: bytes, prefix: str) -> None:
        self.app = app
        self._key = key
        self._prefix = prefix

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not (
            path == self._prefix or path.startswith(self._prefix + "/")
        ):
            await self.app(scope, receive, send)
            return
        cookie = _cookie(scope["headers"])
        known = verify(cookie, self._key) if cookie is not None else None
        visitor_id = known or secrets.token_urlsafe(32)
        scope.setdefault("state", {})["visitor_id"] = visitor_id
        if known is not None:
            await self.app(scope, receive, send)
            return
        value = (
            f"{VISITOR_COOKIE}={sign(visitor_id, self._key)}; Max-Age={VISITOR_MAX_AGE};"
            " Path=/; Secure; HttpOnly; SameSite=Lax"
        )

        async def send_with_cookie(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                # A route that gives the browser a visitor itself (sign-out) sets the one cookie.
                own = f"{VISITOR_COOKIE}="
                if not any(c.startswith(own) for c in headers.getlist("set-cookie")):
                    headers.append("set-cookie", value)
            await send(message)

        await self.app(scope, receive, send_with_cookie)
