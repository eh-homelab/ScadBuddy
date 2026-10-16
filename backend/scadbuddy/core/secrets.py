"""Envelope encryption for secrets at rest (#602), in the agent's format.

A Python reading of ``agent/src/secrets.ts``, whose header documents the key file, the
sealed format and its versions; ``agent-durable/src/scadbuddy_durable/secrets.py`` opens
the same envelopes. In short: the key file holds 32 random bytes, base64-encoded
(``openssl rand -base64 32``); a secret is sealed with AES-256-GCM under a random data
key, and the data key under the KEK, each as ``version(1) | IV(12) | tag(16) |
ciphertext``, with the AAD ``v2|<context>`` (and ``dek:<context>`` for the data key). The
KEK's id is the first 16 hex characters of SHA-256 over it. v2 is written; v1 (the
context alone as AAD) is still opened.

No exception, log record or ``repr`` here carries a plaintext, a data key or the KEK.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEK_BYTES = 32
_IV_BYTES = 12
_TAG_BYTES = 16
SEAL_V1 = 0x01
SEAL_VERSION = 0x02
_KNOWN_VERSIONS = frozenset({SEAL_V1, SEAL_VERSION})
_BASE64 = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
VARIABLE = "SCADBUDDY_SECRET_KEY_FILE"

#: Where a seal takes its IVs and data key: ``os.urandom``, except in the vector test.
#: A repeated IV under one key breaks GCM, so never pass anything else.
RandomSource = Callable[[int], bytes]


class SecretKeyError(Exception):
    """The key file is unreadable or not a key. Never carries its path or contents."""


class SealError(Exception):
    """Opening failed: another key, or altered bytes. Never carries plaintext."""


@dataclass(frozen=True)
class Kek:
    id: str
    key: bytes = field(repr=False)


@dataclass(frozen=True)
class Envelope:
    secret_sealed: bytes = field(repr=False)
    dek_sealed: bytes = field(repr=False)
    kek_id: str


def kek_from_base64(text: str) -> Kek:
    # JS trim() also strips U+FEFF, so a BOM before the key loads in the agent.
    trimmed = text.strip().strip("﻿").strip()
    if not _BASE64.match(trimmed) or len(trimmed) % 4 != 0:
        raise SecretKeyError(
            f"{VARIABLE} is not base64; it must hold 32 random bytes, base64-encoded"
            " (openssl rand -base64 32)"
        )
    try:
        key = base64.b64decode(trimmed, validate=True)
    except binascii.Error:
        raise SecretKeyError(f"{VARIABLE} is not base64") from None
    if len(key) != KEK_BYTES:
        raise SecretKeyError(
            f"{VARIABLE} decodes to {len(key)} bytes, not {KEK_BYTES} (openssl rand -base64 32)"
        )
    return Kek(id=hashlib.sha256(key).hexdigest()[:16], key=key)


def load_kek(path: Path) -> Kek:
    try:
        raw = path.read_bytes()
    except OSError as err:
        raise SecretKeyError(f"{VARIABLE} cannot be read (errno {err.errno})") from None
    # As Node's readFile(..., 'utf8'): invalid bytes become U+FFFD, so a raw key file is
    # refused as "not base64" and none of its bytes reach the message.
    return kek_from_base64(raw.decode("utf-8", errors="replace"))


def _aad(version: int, context: str) -> bytes:
    # v1 did not authenticate its version byte (secrets.ts `aadFor`).
    return (context if version == SEAL_V1 else f"v{version}|{context}").encode()


def _seal(key: bytes, plaintext: bytes, context: str, random: RandomSource) -> bytes:
    iv = random(_IV_BYTES)
    sealed = AESGCM(key).encrypt(iv, plaintext, _aad(SEAL_VERSION, context))
    # `cryptography` appends the tag; the format puts it before the ciphertext.
    ciphertext, tag = sealed[:-_TAG_BYTES], sealed[-_TAG_BYTES:]
    return bytes([SEAL_VERSION]) + iv + tag + ciphertext


def _open(key: bytes, sealed: bytes, context: str) -> bytes:
    if len(sealed) < 1 + _IV_BYTES + _TAG_BYTES or sealed[0] not in _KNOWN_VERSIONS:
        raise SealError("sealed value is malformed or of an unknown version")
    version = sealed[0]
    iv = sealed[1 : 1 + _IV_BYTES]
    tag = sealed[1 + _IV_BYTES : 1 + _IV_BYTES + _TAG_BYTES]
    ciphertext = sealed[1 + _IV_BYTES + _TAG_BYTES :]
    try:
        return AESGCM(key).decrypt(iv, ciphertext + tag, _aad(version, context))
    except InvalidTag:
        raise SealError("sealed value failed authentication (wrong key, or altered)") from None


def open_bytes(key: bytes, sealed: bytes, context: str) -> bytes:
    """secrets.ts ``openBytes``: what ``seal_bytes`` (or the agent) sealed under ``key``."""
    return _open(key, sealed, context)


def seal_bytes(key: bytes, data: bytes, context: str, iv: bytes | None = None) -> bytes:
    """secrets.ts ``sealBytes``, the current version: the payload codec's data keys and
    payloads (``workflows/payload_codec.py``). ``iv`` is for the vectors only: a
    repeated IV under one key breaks GCM."""
    if iv is None:
        return _seal(key, data, context, os.urandom)
    fixed = iv
    return _seal(key, data, context, lambda _n: fixed)


def seal_secret(kek: Kek, plaintext: str, aad: str, random: RandomSource = os.urandom) -> Envelope:
    dek = random(KEK_BYTES)
    return Envelope(
        secret_sealed=_seal(dek, plaintext.encode(), aad, random),
        dek_sealed=_seal(kek.key, dek, f"dek:{aad}", random),
        kek_id=kek.id,
    )


def open_secret(kek: Kek, envelope: Envelope, aad: str) -> str:
    if envelope.kek_id != kek.id:
        raise SealError(
            f"secret was sealed with key {envelope.kek_id}, but {VARIABLE} holds key {kek.id}"
        )
    dek = _open(kek.key, envelope.dek_sealed, f"dek:{aad}")
    return _open(dek, envelope.secret_sealed, aad).decode()
