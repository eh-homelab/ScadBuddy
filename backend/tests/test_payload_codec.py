"""The backend's subject payload codec (spec 2026-10-01 §6.5, plan 6 Ruling 10): the
agent's agent/test/fixtures/payload-vectors.json opened and sealed again byte for byte,
as agent-durable's tests/test_codec.py does, so the three implementations cannot drift."""

import base64
import json
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from temporalio import workflow
from temporalio.api.common.v1 import Payload
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.converter import ActivitySerializationContext, WorkflowSerializationContext
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from scadbuddy.core.secrets import SealError, kek_from_base64, open_bytes
from scadbuddy.workflows.flows_client import connect_flows
from scadbuddy.workflows.payload_codec import (
    SUBJECT_ENCODING,
    PgPayloadKeys,
    SubjectForgottenError,
    SubjectPayloadCodec,
    open_payload,
    payload_key_context,
    seal_payload,
    subject_of,
)
from tests.support.temporal import temporal_available, temporal_server

ROOT = Path(__file__).resolve().parents[2]
VECTORS = ROOT / "agent" / "test" / "fixtures" / "payload-vectors.json"
MIGRATIONS = ROOT / "agent" / "src" / "db" / "migrations"
SUBJECT = "session-0b6c1e4e-7d3a-4f5e-9a51-3f1c2d4e5f60"
FLOW = "flow-0b0e2a0c-1111-4222-8333-444455556666"


def _payload(view: dict[str, Any]) -> Payload:
    return Payload(
        metadata={k: base64.b64decode(v) for k, v in view["metadata"].items()},
        data=base64.b64decode(view["data"]),
    )


@pytest.fixture(scope="module")
def payload_vectors() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(VECTORS.read_text())
    return data


def test_every_vector_opens_and_seals_again_to_the_same_bytes(
    payload_vectors: dict[str, Any],
) -> None:
    kek = kek_from_base64(payload_vectors["kek_b64"])
    assert kek.id == payload_vectors["kek_id"]
    subject = payload_vectors["subject"]
    dek = open_bytes(
        kek.key, base64.b64decode(payload_vectors["dek_sealed_b64"]), payload_key_context(subject)
    )
    assert payload_vectors["payloads"]
    for v in payload_vectors["payloads"]:
        encoded = _payload(v["encoded"])
        assert open_payload(dek, subject, encoded) == _payload(v["plain"])
        again = seal_payload(dek, subject, _payload(v["plain"]), base64.b64decode(v["iv_b64"]))
        assert again == encoded, v["name"]


def test_the_subject_is_a_session_or_flow_uuid() -> None:
    assert subject_of(FLOW) == FLOW
    assert subject_of(SUBJECT) == SUBJECT
    for other in (None, "print-abc", "flow-not-a-uuid", f"{FLOW}-x", "render-" + FLOW[5:]):
        assert subject_of(other) is None


class _Keys:
    def __init__(self) -> None:
        self.keys: dict[str, bytes] = {}

    async def key_for(self, subject: str, create: bool) -> bytes:
        if subject not in self.keys:
            if not create:
                raise SubjectForgottenError(subject)
            self.keys[subject] = bytes([len(self.keys) + 1]) * 32
        return self.keys[subject]


PLAIN = Payload(metadata={"encoding": b"json/plain"}, data=b'"the flow\'s script"')


async def test_a_flows_payloads_are_sealed_and_others_pass_through() -> None:
    codec = SubjectPayloadCodec(_Keys())
    for context in (
        WorkflowSerializationContext(namespace="default", workflow_id=FLOW),
        ActivitySerializationContext(
            namespace="default",
            activity_id="flow_record",
            activity_type=None,
            activity_task_queue=None,
            workflow_id=FLOW,
            workflow_type="ProjectWorkflow",
            is_local=False,
        ),
    ):
        [sealed] = await codec.with_context(context).encode([PLAIN])
        assert sealed.metadata["encoding"] == SUBJECT_ENCODING
        assert b"the flow" not in sealed.data
        assert await codec.decode([sealed]) == [PLAIN]
    for other in ("render-abc", "flow-nope", f"{FLOW}-x"):
        ctx = WorkflowSerializationContext(namespace="default", workflow_id=other)
        assert await codec.with_context(ctx).encode([PLAIN]) == [PLAIN]
    assert await codec.encode([PLAIN]) == [PLAIN]
    assert await codec.decode([PLAIN]) == [PLAIN]


async def test_a_forgotten_subject_does_not_decode() -> None:
    keys = _Keys()
    ctx = WorkflowSerializationContext(namespace="default", workflow_id=FLOW)
    [sealed] = await SubjectPayloadCodec(keys).with_context(ctx).encode([PLAIN])
    keys.keys.clear()
    with pytest.raises(SubjectForgottenError):
        await SubjectPayloadCodec(keys).decode([sealed])


@pytest.fixture
async def agent_db() -> AsyncIterator[psycopg.AsyncConnection[Any]]:
    """A throwaway schema holding the agent's tables, from its own migration files."""
    url = os.environ.get("SCADBUDDY_TEST_DATABASE_URL")
    if not url:
        pytest.skip("SCADBUDDY_TEST_DATABASE_URL is not set")
    schema = f"codec_{uuid.uuid4().hex[:12]}"
    conn = await psycopg.AsyncConnection.connect(url, autocommit=True)
    try:
        await conn.execute(f'CREATE SCHEMA "{schema}"'.encode())
        await conn.execute(f'SET search_path TO "{schema}"'.encode())
        for sql in sorted(MIGRATIONS.glob("*.sql")):
            await conn.execute(sql.read_text().encode())
        yield conn
    finally:
        await conn.execute(f'DROP SCHEMA "{schema}" CASCADE'.encode())
        await conn.close()


@asynccontextmanager
async def _same(conn: psycopg.AsyncConnection[Any]) -> AsyncIterator[psycopg.AsyncConnection[Any]]:
    yield conn


@pytest.mark.requires_postgres
async def test_keys_are_made_once_kept_sealed_and_opened_with_the_previous_key(
    agent_db: psycopg.AsyncConnection[Any],
) -> None:
    kek = kek_from_base64(base64.b64encode(b"\x11" * 32).decode())
    newer = kek_from_base64(base64.b64encode(b"\x22" * 32).decode())
    one = PgPayloadKeys(lambda: _same(agent_db), kek)
    two = PgPayloadKeys(lambda: _same(agent_db), kek, cache_s=0)
    made = await one.key_for(FLOW, True)
    assert await two.key_for(FLOW, True) == made
    cur = await agent_db.execute(
        "SELECT dek_sealed, kek_id FROM ai_payload_keys WHERE subject = %s", (FLOW,)
    )
    row = await cur.fetchone()
    assert row is not None and made not in bytes(row[0]) and row[1] == kek.id
    # The agent re-wraps at start after a rotation; meanwhile the previous key opens it.
    assert (
        await PgPayloadKeys(lambda: _same(agent_db), newer, previous=kek).key_for(FLOW, False)
        == made
    )
    with pytest.raises(SealError):
        await PgPayloadKeys(lambda: _same(agent_db), newer).key_for(FLOW, False)
    # forgetSubject: the tombstone and the deletion. No encoder makes the key again.
    await agent_db.execute("INSERT INTO ai_forgotten_subjects (subject) VALUES (%s)", (FLOW,))
    await agent_db.execute("DELETE FROM ai_payload_keys")
    with pytest.raises(SubjectForgottenError):
        await two.key_for(FLOW, False)
    with pytest.raises(SubjectForgottenError):
        await two.key_for(FLOW, True)
    cur = await agent_db.execute("SELECT count(*) FROM ai_payload_keys")
    assert await cur.fetchone() == (0,)
    assert "\\x" not in repr(one) and str(made) not in repr(one)


@workflow.defn(name="CodecProbe")
class CodecProbe:
    @workflow.run
    async def run(self, words: str) -> str:
        await workflow.sleep(timedelta(milliseconds=1))
        return words.upper()


@pytest.fixture(scope="module")
def temporal_address() -> Iterator[str]:
    if not temporal_available():
        pytest.skip("no Temporal")
    with temporal_server() as address:
        yield address


@pytest.mark.requires_temporal
async def test_a_flows_history_holds_no_plaintext(temporal_address: str) -> None:
    keys = _Keys()
    client = await connect_flows(temporal_address, "default", keys)
    queue = f"codec-{uuid.uuid4().hex[:8]}"
    wf_id = f"flow-{uuid.uuid4()}"
    # Unsandboxed: the sandbox would re-import this module, and cryptography with it.
    async with Worker(
        client,
        task_queue=queue,
        workflows=[CodecProbe],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        result = await client.execute_workflow(
            CodecProbe.run, "pink spool", id=wf_id, task_queue=queue
        )
    assert result == "PINK SPOOL"
    plain = await Client.connect(
        temporal_address, namespace="default", data_converter=pydantic_data_converter
    )
    raw = (await plain.get_workflow_handle(wf_id).fetch_history()).to_json()
    assert "pink spool" not in raw and "PINK SPOOL" not in raw
    for words in (b'"pink spool"', b'"PINK SPOOL"'):
        assert base64.b64encode(words).decode() not in raw
