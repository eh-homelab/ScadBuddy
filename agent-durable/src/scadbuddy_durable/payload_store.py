"""A session's large payloads, kept in Postgres instead of Temporal's history (#2243).

Temporal's External Storage moves every payload at or over ``OFFLOAD_BYTES`` out of
history: workflow and activity inputs and results, query results, signals, updates and
the continue-as-new input. What history keeps is a reference, the payload's subject and
digest. This is the driver: ``ai_payload_blobs`` holds the sealed bytes (the codec seals
before External Storage stores), by subject and the SHA-256 of those bytes.

Only a sealed payload of a session is stored (``select``): the backend's codec, which
reads the flows' payloads, has no store. The agent service reads and writes the same
rows (agent/src/temporal/payloadCodec.ts, ``PgPayloadStore``), for the tool calls it runs
on ``agent-tools`` and the session's queries and updates it reads.

The plugin reads ``external_storage`` off the worker's client: with it, it lets the
conversation outgrow one payload instead of failing the turn at 2 MB.

Rows go with their subject's key (``ON DELETE CASCADE`` on ``ai_payload_keys``), so
forgetSubject removes them with every other copy of the subject's payloads.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any

import psycopg
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

Connect = Callable[[], AbstractAsyncContextManager[psycopg.AsyncConnection[Any]]]

DRIVER = "scadbuddy-pg"
# Sealed bytes from which a payload leaves history: under Temporal's 512 KiB warning,
# over every message, answer and ordinary tool call. The plugin sizes its conversation
# pages to this before the codec, which grows them by a third, so pages are stored too:
# the codec seals a large payload to the same bytes every time (codec.dedup_iv), so a
# page read again is not stored again.
OFFLOAD_BYTES = 128 * 1024
_SESSION = re.compile(r"^session-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def stored_subject(payload: Payload) -> str | None:
    """The session a sealed payload belongs to; None for one this store does not keep."""
    subject = sealed_subject(payload)
    return subject if subject is not None and _SESSION.match(subject) else None


class PgStorageDriver(StorageDriver):
    def __init__(self, connect: Connect) -> None:
        self._connect = connect

    def name(self) -> str:
        return DRIVER

    async def store(
        self, context: StorageDriverStoreContext, payloads: Sequence[Payload]
    ) -> list[StorageDriverClaim]:
        claims: list[StorageDriverClaim] = []
        async with self._connect() as conn:
            for p in payloads:
                subject = stored_subject(p)
                if subject is None:  # select() lets no other payload through
                    raise ValueError("only a sealed session payload is stored")
                digest = hashlib.sha256(p.data).hexdigest()
                cur = await conn.execute(
                    "SELECT 1 FROM ai_payload_blobs WHERE subject = %s AND digest = %s",
                    (subject, digest),
                )
                if await cur.fetchone() is None:  # stored already: not sent again
                    await conn.execute(
                        "INSERT INTO ai_payload_blobs (subject, digest, data) VALUES (%s, %s, %s)"
                        " ON CONFLICT (subject, digest) DO NOTHING",
                        (subject, digest, p.data),
                    )
                claims.append(StorageDriverClaim({"subject": subject, "digest": digest}))
        return claims

    async def retrieve(
        self, context: StorageDriverRetrieveContext, claims: Sequence[StorageDriverClaim]
    ) -> list[Payload]:
        out: list[Payload] = []
        async with self._connect() as conn:
            for claim in claims:
                subject = claim.claim_data.get("subject", "")
                digest = claim.claim_data.get("digest", "")
                if not _SESSION.match(subject) or not _DIGEST.match(digest):
                    raise ValueError("a stored payload's reference names no session or digest")
                cur = await conn.execute(
                    "SELECT data FROM ai_payload_blobs WHERE subject = %s AND digest = %s",
                    (subject, digest),
                )
                row = await cur.fetchone()
                if row is None:
                    raise SubjectForgottenError(
                        f"the stored payload {digest[:12]} of {subject} is gone: forgotten?"
                    )
                out.append(
                    Payload(
                        metadata={"encoding": SUBJECT_ENCODING, SUBJECT_METADATA: subject.encode()},
                        data=bytes(row[0]),
                    )
                )
        return out


def external_storage(connect: Connect, threshold: int = OFFLOAD_BYTES) -> ExternalStorage:
    driver = PgStorageDriver(connect)

    def select(_: StorageDriverSelectContext, payload: Payload) -> StorageDriver | None:
        return driver if stored_subject(payload) is not None else None

    return ExternalStorage(
        drivers=[driver], driver_selector=select, payload_size_threshold=threshold
    )
