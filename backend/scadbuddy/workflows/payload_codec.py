"""The payload codec of durable subjects (spec 2026-10-01 §6.5, plan 5c Ruling 11),
in the backend for flow runs (plan 2026-10-09-durable-phase-6-flows.md Ruling 10).

Every payload of a workflow ``session-<uuid>`` or ``flow-<uuid>`` is sealed under that
subject's own data key, a row of ``ai_payload_keys``. Deleting the row
(``forgetSubject``, in the agent service) makes every copy of the subject's payloads
undecryptable: history, Visibility, Archival. Other workflows' payloads pass through.

The subject comes from the serialization context on encode (``with_context``, which the
SDK calls with the workflow every payload belongs to) and from the payload's own
metadata on decode, so a payload decodes with or without context. This is
agent/src/temporal/payloadCodec.ts in Python, a copy of
agent-durable/src/scadbuddy_durable/codec.py; agent/test/fixtures/payload-vectors.json
pins the three together (tests/test_payload_codec.py). Change them together.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import os
import re
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol, Self

import psycopg
from temporalio.api.common.v1 import Payload
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.converter import (
    ActivitySerializationContext,
    DataConverter,
    DefaultFailureConverterWithEncodedAttributes,
    PayloadCodec,
    SerializationContext,
    WithSerializationContext,
    WorkflowSerializationContext,
)

from scadbuddy.core.secrets import KEK_BYTES, Kek, SealError, open_bytes, seal_bytes

SUBJECT_ENCODING = b"binary/scadbuddy-subject"
SUBJECT_METADATA = "scadbuddy-subject"
_SUBJECT = re.compile(
    r"^(session|flow)-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

Connect = Callable[[], AbstractAsyncContextManager[psycopg.AsyncConnection[Any]]]


class SubjectForgottenError(Exception):
    """The subject's payloads cannot be decoded: its key was deleted (forgetSubject)."""


def subject_of(workflow_id: str | None) -> str | None:
    return workflow_id if workflow_id is not None and _SUBJECT.match(workflow_id) else None


def payload_key_context(subject: str) -> str:
    return f"dek:ai_payload_keys:{subject}"


def payload_key_lock(subject: str) -> str:
    """The advisory lock key creation and forgetSubject take (payloadCodec.ts payloadKeyLock)."""
    return f"scadbuddy:ai_payload_keys:{subject}"


def payload_context(subject: str) -> str:
    return f"payload:{subject}"


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def payload_bytes(payload: Payload) -> bytes:
    """The original payload as the bytes that are sealed: sorted keys, as the agent's."""
    m = {key: _b64(payload.metadata[key]) for key in sorted(payload.metadata)}
    return json.dumps({"d": _b64(payload.data), "m": m}, separators=(",", ":")).encode()


def _payload_from(data: bytes) -> Payload:
    parsed = json.loads(data)
    return Payload(
        metadata={k: base64.b64decode(v) for k, v in parsed["m"].items()},
        data=base64.b64decode(parsed["d"]),
    )


def seal_payload(dek: bytes, subject: str, payload: Payload, iv: bytes | None = None) -> Payload:
    return Payload(
        metadata={"encoding": SUBJECT_ENCODING, SUBJECT_METADATA: subject.encode()},
        data=seal_bytes(dek, payload_bytes(payload), payload_context(subject), iv),
    )


def sealed_subject(payload: Payload) -> str | None:
    """The subject a sealed payload belongs to; None for any other payload."""
    if payload.metadata.get("encoding") != SUBJECT_ENCODING:
        return None
    return payload.metadata.get(SUBJECT_METADATA, b"").decode()


def open_payload(dek: bytes, subject: str, payload: Payload) -> Payload:
    return _payload_from(open_bytes(dek, payload.data, payload_context(subject)))


class PayloadKeys(Protocol):
    async def key_for(self, subject: str, create: bool) -> bytes: ...


class PgPayloadKeys:
    """``ai_payload_keys``, sealed under the KEK; a key stays in memory 30 s at most.

    30 s is how long another process can still open a subject after forgetSubject.
    """

    def __init__(
        self,
        connect: Connect,
        kek: Kek,
        *,
        previous: Kek | None = None,
        cache_s: float = 30.0,
        cache_max: int = 1000,
    ) -> None:
        self._connect = connect
        self._kek = kek
        self._previous = previous
        self._cache_s = cache_s
        self._cache_max = cache_max
        self._cache: OrderedDict[str, tuple[bytes, float]] = OrderedDict()

    def __repr__(self) -> str:
        return f"PgPayloadKeys(kek={self._kek.id}, cached={len(self._cache)})"

    async def _row(self, conn: psycopg.AsyncConnection[Any], subject: str) -> Any:
        cur = await conn.execute(
            "SELECT dek_sealed, kek_id FROM ai_payload_keys WHERE subject = %s", (subject,)
        )
        return await cur.fetchone()

    async def key_for(self, subject: str, create: bool) -> bytes:
        cached = self._cache.get(subject)
        if cached is not None and cached[1] > time.monotonic():
            return cached[0]
        self._cache.pop(subject, None)
        async with self._connect() as conn:
            row = await self._row(conn, subject)
            if row is None and create:
                sealed = seal_bytes(
                    self._kek.key, os.urandom(KEK_BYTES), payload_key_context(subject)
                )
                # Never for a forgotten subject: its payloads stay unreadable, new ones too.
                # Under the subject's lock, which forgetSubject also takes, so the
                # tombstone check never reads from before a forget that is committing.
                async with conn.transaction():
                    await conn.execute(
                        "SELECT pg_advisory_xact_lock(hashtext(%s))", (payload_key_lock(subject),)
                    )
                    await conn.execute(
                        "INSERT INTO ai_payload_keys (subject, dek_sealed, kek_id)"
                        " SELECT %s, %s, %s WHERE NOT EXISTS"
                        " (SELECT 1 FROM ai_forgotten_subjects WHERE subject = %s)"
                        " ON CONFLICT (subject) DO NOTHING",
                        (subject, sealed, self._kek.id, subject),
                    )
                # Whoever inserted first, this is the key every encoder of the subject uses.
                row = await self._row(conn, subject)
        if row is None:
            raise SubjectForgottenError(
                f"the payloads of {subject} cannot be decoded: its key was deleted"
            )
        dek_sealed, kek_id = bytes(row[0]), row[1]
        if kek_id == self._kek.id:
            kek = self._kek
        elif self._previous is not None and kek_id == self._previous.id:
            kek = self._previous
        else:
            raise SealError(f"the payload key of {subject} was sealed with key {kek_id}")
        key = open_bytes(kek.key, dek_sealed, payload_key_context(subject))
        # Expired keys go now, not when their subject is next asked for.
        now = time.monotonic()
        for held in [s for s, (_, until) in self._cache.items() if until <= now]:
            del self._cache[held]
        if len(self._cache) >= self._cache_max:
            self._cache.popitem(last=False)
        self._cache[subject] = (key, time.monotonic() + self._cache_s)
        return key


class SubjectPayloadCodec(PayloadCodec, WithSerializationContext):
    def __init__(self, keys: PayloadKeys, subject: str | None = None) -> None:
        self._keys = keys
        self._subject = subject

    def with_context(self, context: SerializationContext) -> Self:
        workflow_id = (
            context.workflow_id
            if isinstance(context, WorkflowSerializationContext | ActivitySerializationContext)
            else None
        )
        return type(self)(self._keys, subject_of(workflow_id))

    async def encode(self, payloads: Sequence[Payload]) -> list[Payload]:
        if self._subject is None:
            return list(payloads)
        key = await self._keys.key_for(self._subject, True)
        return [
            p if sealed_subject(p) is not None else seal_payload(key, self._subject, p)
            for p in payloads
        ]

    async def decode(self, payloads: Sequence[Payload]) -> list[Payload]:
        out: list[Payload] = []
        for p in payloads:
            subject = sealed_subject(p)
            if subject is None:
                out.append(p)
                continue
            if not _SUBJECT.match(subject):
                raise SealError("a sealed payload names no subject")
            out.append(open_payload(await self._keys.key_for(subject, False), subject, p))
        return out


def flows_converter(keys: PayloadKeys) -> DataConverter:
    """The converter of the flows client and the `projects` worker (plan 6 Ruling 9).

    The backend's pydantic converter, which the harness needs, with the codec. Failure
    messages and stack traces are moved into an encoded payload, so the codec seals them
    too: a host call's error text is a run's content as much as its result.
    """
    return dataclasses.replace(
        pydantic_data_converter,
        payload_codec=SubjectPayloadCodec(keys),
        failure_converter_class=DefaultFailureConverterWithEncodedAttributes,
    )
