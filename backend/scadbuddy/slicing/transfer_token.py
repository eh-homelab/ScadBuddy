"""The token the internal blob routes take in ``X-ScadBuddy-Transfer`` (plan
2026-10-16-native-slicer-rust-worker Task 10): one method on one key until one time,
MACed under a key derived from the KEK, so the slicer worker needs no credential of
its own. The format, fixed:
``urlsafe_b64(f"{method}\\n{blob_key}\\n{expiry seconds}") + "." + urlsafe_b64(HMAC-SHA256)``.
A token is a bearer secret: never log one, or a URL or header carrying one."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
from datetime import UTC, datetime
from typing import Literal

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pydantic import BaseModel

from scadbuddy.core.secrets import Kek

Method = Literal["GET", "PUT"]

#: The HKDF label of :func:`transfer_key`.
TRANSFER_KEY_INFO = b"scadbuddy:slicing:transfer:v1"


def transfer_key(kek: Kek) -> bytes:
    """The key every transfer token is MACed under, derived from the KEK as
    `bambuddy/catalogue.py` `catalogue_name_key` derives its own."""
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=TRANSFER_KEY_INFO).derive(
        kek.key
    )


class TransferToken(BaseModel):
    method: Method
    blob_key: str
    #: Whole seconds: the format carries no fraction.
    expires_at: datetime


class TokenError(ValueError):
    """The token is missing, malformed, MACed under another key, for another method or
    key, or expired. Its message never carries the token."""


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"), altchars=b"-_", validate=True)


def _mac(secret: bytes, body: bytes) -> bytes:
    return hmac.new(secret, body, hashlib.sha256).digest()


def sign(secret: bytes, token: TransferToken) -> str:
    if "\n" in token.blob_key:
        raise ValueError("a blob key holds no newline")
    body = f"{token.method}\n{token.blob_key}\n{int(token.expires_at.timestamp())}".encode()
    return f"{_b64(body)}.{_b64(_mac(secret, body))}"


def verify(
    secret: bytes, value: str, *, method: str, blob_key: str, now: datetime
) -> TransferToken:
    """The token ``value`` holds, if its MAC is ``secret``'s and it names ``method`` and
    ``blob_key`` and expires after ``now``; else `TokenError`."""
    try:
        encoded_body, encoded_mac = value.split(".")
        body, mac = _unb64(encoded_body), _unb64(encoded_mac)
    except (ValueError, UnicodeEncodeError, binascii.Error):
        raise TokenError("malformed transfer token") from None
    if not hmac.compare_digest(mac, _mac(secret, body)):
        raise TokenError("transfer token failed authentication")
    try:
        signed_method, signed_key, expiry = body.decode().split("\n")
        token = TransferToken.model_validate(
            {
                "method": signed_method,
                "blob_key": signed_key,
                "expires_at": datetime.fromtimestamp(int(expiry), UTC),
            }
        )
    except (ValueError, OverflowError, OSError):
        raise TokenError("malformed transfer token") from None
    if token.method != method or token.blob_key != blob_key:
        raise TokenError("transfer token is for another method or key")
    if now >= token.expires_at:
        raise TokenError("transfer token expired")
    return token
