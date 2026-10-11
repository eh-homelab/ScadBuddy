"""A session's large payloads in ai_payload_blobs, history holding a reference (#2243)."""

from __future__ import annotations

import hashlib
from typing import Any

import pytest
from session_support import Conn, Connect
from temporalio.api.common.v1 import Payload
from temporalio.converter import (
    StorageDriverClaim,
    StorageDriverRetrieveContext,
    StorageDriverSelectContext,
    StorageDriverStoreContext,
    WorkflowSerializationContext,
)

from scadbuddy_durable.codec import (
    DEDUP_BYTES,
    PgPayloadKeys,
    SubjectForgottenError,
    SubjectPayloadCodec,
    data_converter,
)
from scadbuddy_durable.payload_store import OFFLOAD_BYTES, PgStorageDriver, external_storage
from scadbuddy_durable.secrets import kek_from_base64

pytestmark = [pytest.mark.requires_postgres]

KEK = kek_from_base64("WlpaWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlo=")
SESSION = "session-0b6c1e4e-7d3a-4f5e-9a51-3f1c2d4e5f60"
FLOW = "flow-0b6c1e4e-7d3a-4f5e-9a51-3f1c2d4e5f61"


def _json(size: int) -> Payload:
    return Payload(metadata={"encoding": b"json/plain"}, data=b'"' + b"x" * size + b'"')


def _codec(connect: Connect, subject: str) -> SubjectPayloadCodec:
    ctx = WorkflowSerializationContext(namespace="default", workflow_id=subject)
    return SubjectPayloadCodec(PgPayloadKeys(connect, KEK)).with_context(ctx)


async def _rows(conn: Conn) -> list[tuple[Any, ...]]:
    cur = await conn.execute("SELECT subject, digest FROM ai_payload_blobs")
    return list(await cur.fetchall())


async def test_a_large_payload_seals_to_the_same_bytes_and_a_small_one_never(
    agent_db: Conn, connect: Connect
) -> None:
    codec = _codec(connect, SESSION)
    big, small = _json(DEDUP_BYTES), _json(10)
    assert (await codec.encode([big])) == (await codec.encode([big]))
    assert (await codec.encode([small])) != (await codec.encode([small]))
    [sealed] = await codec.encode([big])
    assert await codec.decode([sealed]) == [big]


async def test_a_sessions_large_payload_is_stored_once_and_read_back(
    agent_db: Conn, connect: Connect
) -> None:
    converter = data_converter(PgPayloadKeys(connect, KEK), external_storage(connect)).with_context(
        WorkflowSerializationContext(namespace="default", workflow_id=SESSION)
    )
    value = "y" * (OFFLOAD_BYTES * 3)
    first = await converter.encode([value])
    second = await converter.encode([value])
    for [p] in (first, second):
        assert p.ByteSize() < 1024  # a reference, not the bytes
        assert b"yyyy" not in p.SerializeToString()
    assert first == second
    [(subject, digest)] = await _rows(agent_db)
    assert subject == SESSION and len(digest) == 64
    assert await converter.decode(first, [str]) == [value]
    # A small one stays inline.
    [inline] = await converter.encode(["hello"])
    assert inline.metadata["encoding"] == b"binary/scadbuddy-subject"
    assert len(await _rows(agent_db)) == 1


async def test_only_sealed_session_payloads_are_selected(connect: Connect) -> None:
    storage = external_storage(connect)
    select = storage.driver_selector
    assert select is not None
    ctx = StorageDriverSelectContext()
    sealed_session = (await _codec(connect, SESSION).encode([_json(10)]))[0]
    sealed_flow = (await _codec(connect, FLOW).encode([_json(10)]))[0]
    assert select(ctx, sealed_session) is storage.drivers[0]
    assert select(ctx, sealed_flow) is None  # the backend's codec has no store
    assert select(ctx, _json(10)) is None  # not ours: never sealed, never stored


@pytest.fixture
async def connect_with_key(agent_db: Conn, connect: Connect) -> Connect:
    await PgPayloadKeys(connect, KEK).key_for(SESSION, True)
    return connect


async def test_forgetting_the_subject_removes_its_stored_payloads(
    agent_db: Conn, connect_with_key: Connect
) -> None:
    driver = PgStorageDriver(connect_with_key)
    sealed = Payload(
        metadata={"encoding": b"binary/scadbuddy-subject", "scadbuddy-subject": SESSION.encode()},
        data=b"\x02sealed bytes",
    )
    [claim] = await driver.store(StorageDriverStoreContext(), [sealed])
    assert claim.claim_data == {
        "subject": SESSION,
        "digest": hashlib.sha256(sealed.data).hexdigest(),
    }
    assert await driver.retrieve(StorageDriverRetrieveContext(), [claim]) == [sealed]
    await agent_db.execute("DELETE FROM ai_payload_keys WHERE subject = %s", (SESSION,))
    assert await _rows(agent_db) == []
    with pytest.raises(SubjectForgottenError):
        await driver.retrieve(StorageDriverRetrieveContext(), [claim])
    with pytest.raises(ValueError, match="names no session"):
        await driver.retrieve(
            StorageDriverRetrieveContext(),
            [StorageDriverClaim({"subject": FLOW, "digest": "0" * 64})],
        )


async def test_a_reference_the_agent_writes_is_read_back(
    agent_db: Conn, connect_with_key: Connect
) -> None:
    """agent/test/payloadCodec.test.ts writes this same reference for a tool's large
    result (referencePayload); External Storage here must read it."""
    codec = _codec(connect_with_key, SESSION)
    [sealed] = await codec.encode([_json(10)])
    digest = hashlib.sha256(sealed.data).hexdigest()
    [claim] = await PgStorageDriver(connect_with_key).store(StorageDriverStoreContext(), [sealed])
    assert claim.claim_data["digest"] == digest
    agent_ref = Payload(
        metadata={
            "messageType": b"temporal.api.sdk.v1.ExternalStorageReference",
            "encoding": b"json/protobuf",
        },
        data=(
            f'{{"claimData":{{"digest":"{digest}","subject":"{SESSION}"}},'
            '"driverName":"scadbuddy-pg"}'
        ).encode(),
    )
    converter = data_converter(
        PgPayloadKeys(connect_with_key, KEK), external_storage(connect_with_key)
    )
    assert await converter.decode([agent_ref], [str]) == ["x" * 10]
