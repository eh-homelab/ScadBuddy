"""The per-subject payload codec (spec 2026-10-01 §6.5, plan ruling 11), the Python twin
of agent/src/temporal/codec.ts. Payloads of session-*/flow-* workflows are sealed, as
a serialized Payload, under the subject's data key with AAD "ai_payload:<subject>",
into a payload with encoding binary/encrypted and encryption-key-id <subject>. Any
other workflow's payloads, and payloads with no context, pass through."""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Sequence
from typing import Self

from temporalio.api.common.v1 import Payload
from temporalio.converter import (
    DataConverter,
    PayloadCodec,
    SerializationContext,
    WithSerializationContext,
)

from scadbuddy_durable.payload_keys import DataKeySource
from scadbuddy_durable.secrets import SealError, open_bytes, seal_bytes

_SUBJECT = re.compile(r"^(session|flow)-[0-9a-f-]{36}$")
_ENCRYPTED = b"binary/encrypted"


def is_subject(workflow_id: str) -> bool:
    return _SUBJECT.match(workflow_id) is not None


class SubjectPayloadCodec(PayloadCodec, WithSerializationContext):
    """Spec §6.5: encrypts the payloads of session-*/flow-* workflows per subject."""

    def __init__(self, keys: DataKeySource, subject: str | None = None) -> None:
        self._keys = keys
        self._subject = subject

    def with_context(self, context: SerializationContext) -> Self:
        workflow_id = getattr(context, "workflow_id", None)
        subject = workflow_id if isinstance(workflow_id, str) and is_subject(workflow_id) else None
        return type(self)(self._keys, subject)

    async def encode(self, payloads: Sequence[Payload]) -> list[Payload]:
        if self._subject is None:
            return list(payloads)
        key = await self._keys.data_key(self._subject)
        if key is None:
            raise SealError(f"no payload key for {self._subject}; refusing to write it unencrypted")
        context = f"ai_payload:{self._subject}"
        return [
            Payload(
                metadata={"encoding": _ENCRYPTED, "encryption-key-id": self._subject.encode()},
                data=seal_bytes(key, p.SerializeToString(), context),
            )
            for p in payloads
        ]

    async def decode(self, payloads: Sequence[Payload]) -> list[Payload]:
        # Trusts encryption-key-id rather than the context, so a payload a workflow
        # carries from another subject still opens.
        out: list[Payload] = []
        for p in payloads:
            if p.metadata.get("encoding") != _ENCRYPTED:
                out.append(p)
                continue
            key_id = p.metadata.get("encryption-key-id")
            if not key_id:
                raise SealError("encrypted payload names no encryption-key-id")
            subject = key_id.decode()
            key = await self._keys.data_key(subject)
            if key is None:
                raise SealError(f"payload key for {subject} is gone")
            decoded = Payload()
            decoded.ParseFromString(open_bytes(key, p.data, f"ai_payload:{subject}"))
            out.append(decoded)
        return out


def data_converter(keys: DataKeySource) -> DataConverter:
    return dataclasses.replace(DataConverter.default, payload_codec=SubjectPayloadCodec(keys))
