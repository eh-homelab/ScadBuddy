"""A session's large payloads, kept on the data share instead of Temporal's history (#2243).

Temporal's External Storage moves every payload at or over ``OFFLOAD_BYTES`` out of
history: workflow and activity inputs and results, query results, signals, updates and
the continue-as-new input. What history keeps is a reference, the payload's subject and
digest. This is the driver: the bytes go to a file on ScadBuddy's data share (the NAS
volume the backend's blob store lives on), ``<SCADBUDDY_PAYLOAD_DIR>/<subject>/<sha256>``,
content-addressed by the SHA-256 of the stored bytes. Postgres holds none of them.

The stored bytes are the codec's output: External Storage stores after the codec seals,
so every file is ciphertext under the subject's own data key. Deleting that key
(forgetSubject) makes them unreadable at once; forgetSubject then removes the subject's
directory (agent/src/sessions/forget.ts), so nothing of a forgotten session stays.

Only a sealed payload of a session is stored (``select``): the backend's codec, which
reads the flows' payloads, has no store. The agent service reads and writes the same
files (agent/src/temporal/payloadCodec.ts, ``FilePayloadStore``), for the tool calls it
runs on ``agent-tools`` and the session's queries and updates it reads.

The plugin reads ``external_storage`` off the worker's client: with it, it lets the
conversation outgrow one payload instead of failing the turn at 2 MB.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import uuid
from collections.abc import Sequence
from pathlib import Path

from temporalio.api.common.v1 import Payload
from temporalio.converter import (
    ExternalStorage,
    StorageDriver,
    StorageDriverClaim,
    StorageDriverRetrieveContext,
    StorageDriverSelectContext,
    StorageDriverStoreContext,
)

from scadbuddy_durable.codec import (
    SUBJECT_ENCODING,
    SUBJECT_METADATA,
    SubjectForgottenError,
    sealed_subject,
)

DRIVER = "scadbuddy-file"
# Sealed bytes from which a payload leaves history: under Temporal's 512 KiB warning,
# over every message, answer and ordinary tool call. The plugin sizes its conversation
# pages to this before the codec, which grows them by a third, so pages are stored too:
# the codec seals a large payload to the same bytes every time (codec.dedup_iv), so a
# page read again is not written again.
OFFLOAD_BYTES = 128 * 1024
_SESSION = re.compile(r"^session-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def stored_subject(payload: Payload) -> str | None:
    """The session a sealed payload belongs to; None for one this store does not keep."""
    subject = sealed_subject(payload)
    return subject if subject is not None and _SESSION.match(subject) else None


class FileStorageDriver(StorageDriver):
    def __init__(self, root: Path) -> None:
        self._root = root

    def name(self) -> str:
        return DRIVER

    def _path(self, subject: str, digest: str) -> Path:
        if not _SESSION.match(subject) or not _DIGEST.match(digest):
            raise ValueError("a stored payload's reference names no session or digest")
        return self._root / subject / digest

    def _write(self, path: Path, data: bytes) -> None:
        if path.exists():
            return  # content-addressed: the same bytes are there already
        path.parent.mkdir(parents=True, exist_ok=True)
        staging = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
        staging.write_bytes(data)
        os.replace(staging, path)

    async def store(
        self, context: StorageDriverStoreContext, payloads: Sequence[Payload]
    ) -> list[StorageDriverClaim]:
        claims: list[StorageDriverClaim] = []
        for p in payloads:
            subject = stored_subject(p)
            if subject is None:  # select() lets no other payload through
                raise ValueError("only a sealed session payload is stored")
            digest = hashlib.sha256(p.data).hexdigest()
            await asyncio.to_thread(self._write, self._path(subject, digest), p.data)
            claims.append(StorageDriverClaim({"subject": subject, "digest": digest}))
        return claims

    async def retrieve(
        self, context: StorageDriverRetrieveContext, claims: Sequence[StorageDriverClaim]
    ) -> list[Payload]:
        out: list[Payload] = []
        for claim in claims:
            subject = claim.claim_data.get("subject", "")
            digest = claim.claim_data.get("digest", "")
            path = self._path(subject, digest)
            try:
                data = await asyncio.to_thread(path.read_bytes)
            except FileNotFoundError:
                raise SubjectForgottenError(
                    f"the stored payload {digest[:12]} of {subject} is gone: forgotten?"
                ) from None
            out.append(
                Payload(
                    metadata={"encoding": SUBJECT_ENCODING, SUBJECT_METADATA: subject.encode()},
                    data=data,
                )
            )
        return out


def external_storage(root: Path, threshold: int = OFFLOAD_BYTES) -> ExternalStorage:
    driver = FileStorageDriver(root)

    def select(_: StorageDriverSelectContext, payload: Payload) -> StorageDriver | None:
        return driver if stored_subject(payload) is not None else None

    return ExternalStorage(
        drivers=[driver], driver_selector=select, payload_size_threshold=threshold
    )
