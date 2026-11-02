"""The login cookie (#4975 phase 2, spec 2026-11-01 §5.1 step 1): ``state``, ``nonce``, the
PKCE verifier, ``return_to`` and whether the login runs in a popup, sealed under a key
derived from the KEK (HKDF, as ``slicing/transfer_token.py`` derives its own), so the
callback needs no row and the browser can neither read nor change it."""

from __future__ import annotations

import base64
import binascii
import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import unquote, urlsplit

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pydantic import BaseModel, ValidationError

from scadbuddy.core.secrets import Kek, SealError, open_bytes, seal_bytes

LOGIN_COOKIE = "__Host-scadbuddy_login"
#: The cookie's ``Max-Age`` and the state's own expiry.
LOGIN_LIFETIME = timedelta(minutes=10)
_KEY_INFO = b"scadbuddy:auth:login:v1"
_CONTEXT = "auth-login-state"


def login_key(kek: Kek) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_KEY_INFO).derive(kek.key)


class LoginState(BaseModel):
    state: str
    nonce: str
    verifier: str
    return_to: str
    popup: bool
    expires_at: datetime

    @property
    def challenge(self) -> str:
        """The PKCE ``S256`` challenge (RFC 7636 §4.2)."""
        digest = hashlib.sha256(self.verifier.encode("ascii")).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def new_state(return_to: str, popup: bool, now: datetime | None = None) -> LoginState:
    return LoginState(
        state=secrets.token_urlsafe(32),
        nonce=secrets.token_urlsafe(32),
        # 43 to 128 characters of the unreserved set (RFC 7636 §4.1).
        verifier=secrets.token_urlsafe(48),
        return_to=safe_return_to(return_to),
        popup=popup,
        expires_at=(now or datetime.now(UTC)) + LOGIN_LIFETIME,
    )


def seal_state(kek: Kek, state: LoginState) -> str:
    sealed = seal_bytes(login_key(kek), state.model_dump_json().encode(), _CONTEXT)
    return base64.urlsafe_b64encode(sealed).rstrip(b"=").decode("ascii")


def open_state(kek: Kek, cookie: str, now: datetime | None = None) -> LoginState | None:
    """The state the cookie seals, or None when it is not one of ours or has expired."""
    try:
        sealed = base64.urlsafe_b64decode(cookie + "=" * (-len(cookie) % 4))
        state = LoginState.model_validate_json(open_bytes(login_key(kek), sealed, _CONTEXT))
    except (binascii.Error, ValueError, SealError, ValidationError):
        return None
    return state if state.expires_at > (now or datetime.now(UTC)) else None


def safe_return_to(value: str) -> str:
    """§5.1 step 1: a same-origin path, or ``/``. It must start with one ``/`` followed
    by neither ``/`` nor ``\\``, and carry no scheme or authority once percent-decoded,
    so ``//host``, ``/\\host``, ``/%2F%2Fhost`` and ``https:…`` are all ``/``."""
    decoded = value
    for _ in range(3):
        decoded = unquote(decoded)
    if not value.startswith("/") or any(ch in value for ch in "\r\n\t\0"):
        return "/"
    for candidate in (value, decoded):
        if candidate[1:2] in ("/", "\\") or "\\" in candidate:
            return "/"
        parts = urlsplit(candidate)
        if parts.scheme or parts.netloc:
            return "/"
    return value
