from __future__ import annotations

import base64
import json
import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from psycopg_pool import AsyncConnectionPool
from temporalio.api.common.v1 import Payload
from temporalio.converter import ActivitySerializationContext, WorkflowSerializationContext

from scadbuddy_durable.codec import SubjectPayloadCodec, data_converter, is_subject
from scadbuddy_durable.payload_keys import PayloadKeys
from scadbuddy_durable.secrets import Kek, SealError, kek_from_base64, open_bytes, seal_bytes

VECTORS = json.loads((Path(__file__).parents[2] / "agent/test/fixtures/secret-vectors.json").read_text())
VECTOR_KEK = kek_from_base64(VECTORS["kek_base64"])
SESSION = f"session-{uuid.uuid4()}"


class MemoryKeys:
    """Data keys in memory, one per subject."""

    def __init__(self) -> None:
        self.keys: dict[str, bytes] = {}

    async def data_key(self, subject: str) -> bytes | None:
        return self.keys.get(subject)


def payload(text: str) -> Payload:
    return Payload(
        metadata={"encoding": b"json/plain", "extra": b"x"},
        data=json.dumps({"text": text}).encode(),
    )


def activity_context(workflow_id: str) -> ActivitySerializationContext:
    return ActivitySerializationContext(
        namespace="n",
        activity_id="a",
        activity_type="t",
        activity_task_queue="q",
        workflow_id=workflow_id,
        workflow_type="w",
        is_local=False,
    )


def test_is_subject() -> None:
    assert is_subject(SESSION)
    assert is_subject(f"flow-{uuid.uuid4()}")
    assert not is_subject("render-x")
    assert not is_subject("print-run-y")


@pytest.mark.parametrize(
    "context",
    [WorkflowSerializationContext(namespace="n", workflow_id=SESSION), activity_context(SESSION)],
    ids=["workflow", "activity"],
)
async def test_round_trip(context: WorkflowSerializationContext | ActivitySerializationContext) -> None:
    keys = MemoryKeys()
    keys.keys[SESSION] = os.urandom(32)
    codec = SubjectPayloadCodec(keys).with_context(context)
    original = [payload("hello"), payload("again")]
    encoded = await codec.encode(original)
    for p in encoded:
        assert p.metadata["encoding"] == b"binary/encrypted"
        assert p.metadata["encryption-key-id"] == SESSION.encode()
        assert b"hello" not in p.data
    # Decode trusts encryption-key-id: a codec with no context opens it too.
    assert await SubjectPayloadCodec(keys).decode(encoded) == original


@pytest.mark.parametrize(
    "context",
    [
        WorkflowSerializationContext(namespace="n", workflow_id="render-x"),
        activity_context("print-run-y"),
        None,
    ],
    ids=["render-x", "print-run-y", "no-context"],
)
async def test_passes_other_workflows_through(
    context: WorkflowSerializationContext | ActivitySerializationContext | None,
) -> None:
    codec = SubjectPayloadCodec(MemoryKeys())
    if context is not None:
        codec = codec.with_context(context)
    original = [payload("plain")]
    assert await codec.encode(original) == original
    assert await codec.decode(original) == original


async def test_fails_closed_without_a_key() -> None:
    codec = SubjectPayloadCodec(MemoryKeys()).with_context(
        WorkflowSerializationContext(namespace="n", workflow_id=SESSION)
    )
    with pytest.raises(SealError):
        await codec.encode([payload("x")])


async def test_gone_key() -> None:
    keys = MemoryKeys()
    keys.keys[SESSION] = os.urandom(32)
    codec = SubjectPayloadCodec(keys).with_context(
        WorkflowSerializationContext(namespace="n", workflow_id=SESSION)
    )
    encoded = await codec.encode([payload("x")])
    del keys.keys[SESSION]
    with pytest.raises(SealError, match=f"payload key for {SESSION} is gone"):
        await codec.decode(encoded)


def test_data_converter_carries_the_codec() -> None:
    assert isinstance(data_converter(MemoryKeys()).payload_codec, SubjectPayloadCodec)


@pytest.mark.parametrize("vector", VECTORS["payload"], ids=lambda v: v["subject"])
def test_typescript_payload_vectors_open(vector: dict[str, str]) -> None:
    subject = vector["subject"]
    dek = open_bytes(
        VECTOR_KEK.key, base64.b64decode(vector["dek_sealed_b64"]), f"dek:ai_payload_keys:{subject}"
    )
    assert dek == base64.b64decode(vector["dek_b64"])
    plaintext = open_bytes(dek, base64.b64decode(vector["sealed_b64"]), f"ai_payload:{subject}")
    assert plaintext == base64.b64decode(vector["plaintext_b64"])


@pytest_asyncio.fixture
async def pool(pg_conninfo: str) -> AsyncIterator[AsyncConnectionPool]:
    async with AsyncConnectionPool(pg_conninfo, open=False) as p:
        yield p


async def insert_key(pool: AsyncConnectionPool, subject: str, dek_sealed: bytes, kek: Kek) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO ai_payload_keys (subject, dek_sealed, kek_id) VALUES (%s, %s, %s)",
            (subject, dek_sealed, kek.id),
        )


@pytest.mark.requires_postgres
async def test_opens_a_typescript_shaped_payload(pool: AsyncConnectionPool) -> None:
    vector = VECTORS["payload"][0]
    subject = vector["subject"]
    await insert_key(pool, subject, base64.b64decode(vector["dek_sealed_b64"]), VECTOR_KEK)
    inner = payload("from typescript")
    # As agent/src/temporal/codec.ts writes it: the serialized Payload, sealed under the data key.
    sealed = seal_bytes(
        base64.b64decode(vector["dek_b64"]), inner.SerializeToString(), f"ai_payload:{subject}"
    )
    outer = Payload(
        metadata={"encoding": b"binary/encrypted", "encryption-key-id": subject.encode()}, data=sealed
    )
    codec = SubjectPayloadCodec(PayloadKeys(pool, [VECTOR_KEK]))
    assert await codec.decode([outer]) == [inner]


@pytest.mark.requires_postgres
async def test_keys_by_kek_id(pool: AsyncConnectionPool) -> None:
    previous = Kek(id="prev0000prev0000", key=os.urandom(32))
    dek = os.urandom(32)
    await insert_key(pool, SESSION, seal_bytes(previous.key, dek, f"dek:ai_payload_keys:{SESSION}"), previous)
    assert await PayloadKeys(pool, [VECTOR_KEK, previous]).data_key(SESSION) == dek
    assert await PayloadKeys(pool, [VECTOR_KEK]).data_key(f"flow-{uuid.uuid4()}") is None
    with pytest.raises(SealError):
        await PayloadKeys(pool, [VECTOR_KEK]).data_key(SESSION)


@pytest.mark.requires_postgres
async def test_caches_a_key_for_a_minute(pool: AsyncConnectionPool) -> None:
    now = 0.0
    keys = PayloadKeys(pool, [VECTOR_KEK], clock=lambda: now)
    dek = os.urandom(32)
    await insert_key(
        pool, SESSION, seal_bytes(VECTOR_KEK.key, dek, f"dek:ai_payload_keys:{SESSION}"), VECTOR_KEK
    )
    assert await keys.data_key(SESSION) == dek
    async with pool.connection() as conn:
        await conn.execute("DELETE FROM ai_payload_keys WHERE subject = %s", (SESSION,))
    now += 59
    assert await keys.data_key(SESSION) == dek
    now += 2
    assert await keys.data_key(SESSION) is None
