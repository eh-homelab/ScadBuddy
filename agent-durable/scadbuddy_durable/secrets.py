"""Port of agent/src/secrets.ts: AES-256-GCM envelopes, sealed as
version (1) | IV (12) | tag (16) | ciphertext, v2 AAD = "v2|" + context."""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEK_BYTES = 32
IV_BYTES = 12
TAG_BYTES = 16
SEAL_V1 = 0x01
SEAL_VERSION = 0x02


class SecretKeyError(ValueError):
    pass


class SealError(Exception):
    """Decryption failed: wrong key or altered bytes. Never carries plaintext."""


@dataclass(frozen=True)
class Kek:
    id: str
    key: bytes = b""

    def __repr__(self) -> str:  # never print the key
        return f"Kek(id={self.id!r})"


@dataclass(frozen=True)
class Envelope:
    secret_sealed: bytes
    dek_sealed: bytes
    kek_id: str


def _aad(version: int, context: str) -> bytes:
    return (context if version == SEAL_V1 else f"v{version}|{context}").encode()


def kek_from_base64(text: str) -> Kek:
    trimmed = text.strip()
    if not re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", trimmed) or len(trimmed) % 4:
        raise SecretKeyError("the key file is not base64; it must hold 32 random bytes, base64-encoded")
    try:
        key = base64.b64decode(trimmed, validate=True)
    except binascii.Error as err:
        raise SecretKeyError("the key file is not base64") from err
    if len(key) != KEK_BYTES:
        raise SecretKeyError(f"the key file decodes to {len(key)} bytes, not {KEK_BYTES}")
    return Kek(id=hashlib.sha256(key).hexdigest()[:16], key=key)


def load_kek(path: str) -> Kek:
    return kek_from_base64(Path(path).read_text())


def seal_bytes(key: bytes, plaintext: bytes, context: str) -> bytes:
    iv = os.urandom(IV_BYTES)
    out = AESGCM(key).encrypt(iv, plaintext, _aad(SEAL_VERSION, context))
    ciphertext, tag = out[:-TAG_BYTES], out[-TAG_BYTES:]
    return bytes([SEAL_VERSION]) + iv + tag + ciphertext


def open_bytes(key: bytes, sealed: bytes, context: str) -> bytes:
    if len(sealed) < 1 + IV_BYTES + TAG_BYTES or sealed[0] not in (SEAL_V1, SEAL_VERSION):
        raise SealError("sealed value is malformed or of an unknown version")
    version, iv = sealed[0], sealed[1 : 1 + IV_BYTES]
    tag = sealed[1 + IV_BYTES : 1 + IV_BYTES + TAG_BYTES]
    ciphertext = sealed[1 + IV_BYTES + TAG_BYTES :]
    try:
        return AESGCM(key).decrypt(iv, ciphertext + tag, _aad(version, context))
    except InvalidTag as err:
        raise SealError("sealed value failed authentication (wrong key, or altered)") from err


def open_secret(kek: Kek, envelope: Envelope, aad: str) -> str:
    if envelope.kek_id != kek.id:
        raise SealError(f"secret was sealed with key {envelope.kek_id}, but the mounted key is {kek.id}")
    dek = open_bytes(kek.key, envelope.dek_sealed, f"dek:{aad}")
    return open_bytes(dek, envelope.secret_sealed, aad).decode()
