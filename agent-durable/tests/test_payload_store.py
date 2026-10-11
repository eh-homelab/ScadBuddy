"""A session's large payloads on the data share, history holding a reference (#2243)."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest
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
    SubjectForgottenError,
    SubjectPayloadCodec,
    data_converter,
)
from scadbuddy_durable.payload_store import (
    OFFLOAD_BYTES,
    FileStorageDriver,
    external_storage,
)

SESSION = "session-0b6c1e4e-7d3a-4f5e-9a51-3f1c2d4e5f60"
FLOW = "flow-0b6c1e4e-7d3a-4f5e-9a51-3f1c2d4e5f61"


class _Keys:
    def __init__(self) -> None:
        self.keys: dict[str, bytes] = {}

    async def key_for(self, subject: str, create: bool) -> bytes:
        if subject not in self.keys:
            if not create:
                raise SubjectForgottenError(subject)
            self.keys[subject] = bytes([len(self.keys) + 1]) * 32
        return self.keys[subject]


def _json(size: int) -> Payload:
    return Payload(metadata={"encoding": b"json/plain"}, data=b'"' + b"x" * size + b'"')


def _codec(keys: _Keys, subject: str) -> SubjectPayloadCodec:
    ctx = WorkflowSerializationContext(namespace="default", workflow_id=subject)
    return SubjectPayloadCodec(keys).with_context(ctx)


def _files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file())


async def test_a_large_payload_seals_to_the_same_bytes_and_a_small_one_never() -> None:
    codec = _codec(_Keys(), SESSION)
    big, small = _json(DEDUP_BYTES), _json(10)
    assert (await codec.encode([big])) == (await codec.encode([big]))
    assert (await codec.encode([small])) != (await codec.encode([small]))
    [sealed] = await codec.encode([big])
    assert await codec.decode([sealed]) == [big]


async def test_a_sessions_large_payload_is_one_ciphertext_file(tmp_path: Path) -> None:
    converter = data_converter(_Keys(), external_storage(tmp_path)).with_context(
        WorkflowSerializationContext(namespace="default", workflow_id=SESSION)
    )
    value = "y" * (OFFLOAD_BYTES * 3)
    first = await converter.encode([value])
    second = await converter.encode([value])
    for [p] in (first, second):
        assert p.ByteSize() < 1024  # a reference, not the bytes
        assert b"yyyy" not in p.SerializeToString()
    assert first == second
    [stored] = _files(tmp_path)
    assert stored.parent == tmp_path / SESSION
    assert stored.name == hashlib.sha256(stored.read_bytes()).hexdigest()
    assert b"yyyy" not in stored.read_bytes()  # sealed before it is stored
    assert await converter.decode(first, [str]) == [value]
    # A small one stays inline.
    [inline] = await converter.encode(["hello"])
    assert inline.metadata["encoding"] == b"binary/scadbuddy-subject"
    assert len(_files(tmp_path)) == 1


async def test_only_sealed_session_payloads_are_selected(tmp_path: Path) -> None:
    storage = external_storage(tmp_path)
    select = storage.driver_selector
    assert select is not None
    ctx = StorageDriverSelectContext()
    keys = _Keys()
    sealed_session = (await _codec(keys, SESSION).encode([_json(10)]))[0]
    sealed_flow = (await _codec(keys, FLOW).encode([_json(10)]))[0]
    assert select(ctx, sealed_session) is storage.drivers[0]
    assert select(ctx, sealed_flow) is None  # the backend's codec has no store
    assert select(ctx, _json(10)) is None  # not ours: never sealed, never stored


async def test_a_removed_subject_directory_is_a_forgotten_payload(tmp_path: Path) -> None:
    driver = FileStorageDriver(tmp_path)
    sealed = Payload(
        metadata={"encoding": b"binary/scadbuddy-subject", "scadbuddy-subject": SESSION.encode()},
        data=b"\x02sealed bytes",
    )
    [claim] = await driver.store(StorageDriverStoreContext(), [sealed])
    digest = hashlib.sha256(sealed.data).hexdigest()
    assert claim.claim_data == {"subject": SESSION, "digest": digest}
    assert await driver.retrieve(StorageDriverRetrieveContext(), [claim]) == [sealed]
    shutil.rmtree(tmp_path / SESSION)  # what forgetSubject does
    with pytest.raises(SubjectForgottenError):
        await driver.retrieve(StorageDriverRetrieveContext(), [claim])
    for bad in ({"subject": FLOW, "digest": "0" * 64}, {"subject": SESSION, "digest": "../x"}):
        with pytest.raises(ValueError, match="names no session"):
            await driver.retrieve(StorageDriverRetrieveContext(), [StorageDriverClaim(bad)])


async def test_a_reference_the_agent_writes_is_read_back(tmp_path: Path) -> None:
    """agent/test/payloadCodec.test.ts writes this same reference for a tool's large
    result (referencePayload); External Storage here must read it."""
    keys = _Keys()
    [sealed] = await _codec(keys, SESSION).encode([_json(10)])
    digest = hashlib.sha256(sealed.data).hexdigest()
    (tmp_path / SESSION).mkdir()
    (tmp_path / SESSION / digest).write_bytes(sealed.data)  # as FilePayloadStore.put
    agent_ref = Payload(
        metadata={
            "messageType": b"temporal.api.sdk.v1.ExternalStorageReference",
            "encoding": b"json/protobuf",
        },
        data=(
            f'{{"claimData":{{"digest":"{digest}","subject":"{SESSION}"}},'
            '"driverName":"scadbuddy-file"}'
        ).encode(),
    )
    converter = data_converter(keys, external_storage(tmp_path))
    assert await converter.decode([agent_ref], [str]) == ["x" * 10]
