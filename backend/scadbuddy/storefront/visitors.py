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
from http.cookies import CookieError, SimpleCookie
from typing import Final

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

VISITOR_COOKIE: Final = "sb_visitor"
VISITOR_MAX_AGE: Final = 400 * 24 * 3600
VERSION: Final = "v1"
_VISITOR_ID: Final = re.compile(r"[A-Za-z0-9_-]{43}")


def _mac(visitor_id: str, key: bytes) -> str:
    digest = hmac.new(key, f"{VERSION}.{visitor_id}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def sign(visitor_id: str, key: bytes) -> str:
    return f"{VERSION}.{visitor_id}.{_mac(visitor_id, key)}"


def verify(cookie: str, key: bytes) -> str | None:
    """The visitor id the cookie carries, or None when it is not one this key signed."""
    version, _, rest = cookie.partition(".")
    visitor_id, _, mac = rest.partition(".")
    if version != VERSION or _VISITOR_ID.fullmatch(visitor_id) is None:
        return None
    return visitor_id if hmac.compare_digest(mac, _mac(visitor_id, key)) else None


def _cookie(headers: list[tuple[bytes, bytes]]) -> str | None:
    for name, value in headers:
        if name == b"cookie":
            jar: SimpleCookie = SimpleCookie()
            try:
                jar.load(value.decode("latin-1"))
            except CookieError:
                continue
            if VISITOR_COOKIE in jar:
                return jar[VISITOR_COOKIE].value
    return None


class VisitorCookie:
    """ASGI middleware for the paths under ``prefix``: puts the verified visitor id on
    ``request.state.visitor_id``, and sets a new signed cookie when the request had none
    that verifies."""

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
                MutableHeaders(scope=message).append("set-cookie", value)
            await send(message)

        await self.app(scope, receive, send_with_cookie)
