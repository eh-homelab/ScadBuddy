"""Opening envelopes sealed by agent/src/secrets.ts (spec 2026-10-01 §6.2).

A port of `openSecret` only: this process never seals a credential. The format and
its versions are documented in secrets.ts; agent/test/fixtures/secret-vectors.json
(written by the agent's tests) is what both sides must open.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from dataclasses import dataclass, field

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEK_BYTES = 32
_IV_BYTES = 12
_TAG_BYTES = 16
SEAL_V1 = 0x01
SEAL_V2 = 0x02
_KNOWN_VERSIONS = frozenset({SEAL_V1, SEAL_V2})
_BASE64 = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")


class SecretKeyError(Exception):
    """The key file is unset, unreadable or not a key. Never carries its contents."""


class SealError(Exception):
    """Decryption failed: wrong key, or altered bytes. Never carries plaintext."""


@dataclass(frozen=True)
class Kek:
    id: str
    key: bytes = field(repr=False)


@dataclass(frozen=True)
class Envelope:
    secret_sealed: bytes = field(repr=False)
    dek_sealed: bytes = field(repr=False)
    kek_id: str


def kek_from_base64(text: str, variable: str = "SCADBUDDY_SECRET_KEY_FILE") -> Kek:
    # JS trim() also strips U+FEFF, so a BOM before the key loads in the agent.
    trimmed = text.strip().strip("\ufeff").strip()
    if not _BASE64.match(trimmed) or len(trimmed) % 4 != 0:
        raise SecretKeyError(
            f"{variable} is not base64; it must hold 32 random bytes, base64-encoded"
            " (openssl rand -base64 32)"
        )
    try:
        key = base64.b64decode(trimmed, validate=True)
    except binascii.Error as err:
        raise SecretKeyError(f"{variable} is not base64") from err
    if len(key) != KEK_BYTES:
        raise SecretKeyError(
            f"{variable} decodes to {len(key)} bytes, not {KEK_BYTES} (openssl rand -base64 32)"
        )
    return Kek(id=hashlib.sha256(key).hexdigest()[:16], key=key)


def load_kek(path: str | None, variable: str = "SCADBUDDY_SECRET_KEY_FILE") -> Kek:
    if path is None:
        raise SecretKeyError(f"{variable} is not set")
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as err:
        raise SecretKeyError(f"{variable} cannot be read ({err.errno})") from None
    # As Node's readFile(..., 'utf8') does: invalid bytes become U+FFFD, so a raw
    # key file is refused as "not base64" and none of its bytes reach the message.
    return kek_from_base64(raw.decode("utf-8", errors="replace"), variable)


def sealed_version(sealed: bytes) -> int | None:
    return sealed[0] if sealed else None


def _aad(version: int, context: str) -> bytes:
    # v1 did not authenticate its version byte (secrets.ts `aadFor`).
    return (context if version == SEAL_V1 else f"v{version}|{context}").encode()


def _open(key: bytes, sealed: bytes, context: str) -> bytes:
    version = sealed_version(sealed)
    if len(sealed) < 1 + _IV_BYTES + _TAG_BYTES or version not in _KNOWN_VERSIONS:
        raise SealError("sealed value is malformed or of an unknown version")
    assert version is not None
    iv = sealed[1 : 1 + _IV_BYTES]
    tag = sealed[1 + _IV_BYTES : 1 + _IV_BYTES + _TAG_BYTES]
    ciphertext = sealed[1 + _IV_BYTES + _TAG_BYTES :]
    try:
        return AESGCM(key).decrypt(iv, ciphertext + tag, _aad(version, context))
    except InvalidTag:
        raise SealError("sealed value failed authentication (wrong key, or altered)") from None


def open_secret(kek: Kek, envelope: Envelope, aad: str) -> str:
    if envelope.kek_id != kek.id:
        raise SealError(
            f"secret was sealed with key {envelope.kek_id}, but SCADBUDDY_SECRET_KEY_FILE"
            f" holds key {kek.id}"
        )
    dek = _open(kek.key, envelope.dek_sealed, f"dek:{aad}")
    return _open(dek, envelope.secret_sealed, aad).decode()
