"""Session tokens (spec 2026-11-01 §5.2): 32 random bytes, base64url without padding.
Only the SHA-256 of the token is stored; the token itself goes to the browser once."""

from __future__ import annotations

import hashlib
import re
import secrets

TOKEN_BYTES = 32
#: 32 bytes in base64url without padding are 43 characters.
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}")


def new_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def token_hash(token: str) -> bytes | None:
    """The row key for ``token``, or None for a value no token could be, which is then
    never looked up."""
    if _TOKEN_RE.fullmatch(token) is None:
        return None
    return hashlib.sha256(token.encode("ascii")).digest()
