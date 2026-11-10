"""The session cookie of a signed-in visitor (#5283, spec 2026-11-02 §5.2).

``sb_session=s1.<token>.<mac>``: the token is the CRUD side's session token
(`orders/storefront/accounts.py`, 32 random bytes as URL-safe base64), the mac
HMAC-SHA256 over ``s1.<token>`` under the edge's cookie key, as the visitor cookie's
(`visitors.py`). Its own version tag keeps the two apart: a visitor cookie never verifies
as a session. The CRUD side keeps only the token's sha256 and ends it at sign-out, so the
mac only spares it a lookup of a token the edge never gave.

The cookie is ``HttpOnly``, ``Secure``, ``SameSite=Lax`` and ``Path=/``, and lives as long
as the session (`SESSION_MAX_AGE`). A sign-in replaces it, and a sign-out clears it.
"""

from __future__ import annotations

import hmac
import re
from typing import Final

from fastapi import Request, Response

from scadbuddy.storefront.visitors import cookie_mac

SESSION_COOKIE: Final = "sb_session"
#: The CRUD side's `SESSION_TTL`: 30 days.
SESSION_MAX_AGE: Final = 30 * 24 * 3600
VERSION: Final = "s1"
#: A session token and a mac alike: 32 bytes as URL-safe base64, unpadded.
_B64_32: Final = re.compile(r"[A-Za-z0-9_-]{43}")


def _mac(token: str, key: bytes) -> str:
    return cookie_mac(VERSION, token, key)


def sign(token: str, key: bytes) -> str:
    return f"{VERSION}.{token}.{_mac(token, key)}"


def verify(cookie: str, key: bytes) -> str | None:
    """The session token the cookie carries, or None when this key did not sign it."""
    version, _, rest = cookie.partition(".")
    token, _, mac = rest.partition(".")
    if version != VERSION or _B64_32.fullmatch(token) is None or _B64_32.fullmatch(mac) is None:
        return None
    expected = _mac(token, key).encode()
    return token if hmac.compare_digest(mac.encode(), expected) else None


def _key(request: Request) -> bytes:
    key: bytes = request.app.state.cookie_key
    return key


def session_of(request: Request) -> str | None:
    """The session token of the request's cookie, when the edge signed it."""
    cookie = request.cookies.get(SESSION_COOKIE)
    return verify(cookie, _key(request)) if cookie is not None else None


def set_session(request: Request, response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        sign(token, _key(request)),
        max_age=SESSION_MAX_AGE,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )


def clear_session(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="lax")
