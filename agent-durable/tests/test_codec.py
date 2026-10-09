"""The payload codec (spec 2026-10-01 §6.5): agent/test/fixtures/payload-vectors.json,
written by the agent's payloadCodec.test.ts, opened and sealed again byte for byte."""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import psycopg
import pytest
from temporalio.api.common.v1 import Payload
from temporalio.converter import ActivitySerializationContext, WorkflowSerializationContext

from scadbuddy_durable.codec import (
    SUBJECT_ENCODING,
    PgPayloadKeys,
    SubjectForgottenError,
    SubjectPayloadCodec,
    open_payload,
    payload_key_context,
    seal_payload,
)
from scadbuddy_durable.secrets import SealError, kek_from_base64, open_bytes

VECTORS = (
    Path(__file__).resolve().parents[2] / "agent" / "test" / "fixtures" / "payload-vectors.json"
)
SUBJECT = "session-0b6c1e4e-7d3a-4f5e-9a51-3f1c2d4e5f60"


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
    dek = open_bytes(
        kek.key, base64.b64decode(payload_vectors["dek_sealed_b64"]), payload_key_context(SUBJECT)
    )
    assert payload_vectors["payloads"]
    for v in payload_vectors["payloads"]:
        encoded = _payload(v["encoded"])
        assert open_payload(dek, SUBJECT, encoded) == _payload(v["plain"])
        again = seal_payload(dek, SUBJECT, _payload(v["plain"]), base64.b64decode(v["iv_b64"]))
        assert again == encoded, v["name"]


class _Keys:
    def __init__(self) -> None:
        self.keys: dict[str, bytes] = {}

    async def key_for(self, subject: str, create: bool) -> bytes:
        if subject not in self.keys:
            if not create:
                raise SubjectForgottenError(subject)
            self.keys[subject] = bytes([len(self.keys) + 1]) * 32
        return self.keys[subject]


PLAIN = Payload(metadata={"encoding": b"json/plain"}, data=b'"the user\'s words"')


async def test_a_sessions_payloads_are_sealed_and_others_pass_through() -> None:
    keys = _Keys()
    codec = SubjectPayloadCodec(keys)
    for context in (
        WorkflowSerializationContext(namespace="default", workflow_id=SUBJECT),
        ActivitySerializationContext(
            namespace="default",
            activity_id="tool-1",
            activity_type=None,
            activity_task_queue=None,
            workflow_id=SUBJECT,
            workflow_type="DurableSession",
            is_local=False,
        ),
    ):
        [sealed] = await codec.with_context(context).encode([PLAIN])
        assert sealed.metadata["encoding"] == SUBJECT_ENCODING
        assert b"the user" not in sealed.data
        assert await codec.decode([sealed]) == [PLAIN]
    for other in ("render-abc", "session-nope", f"{SUBJECT}-x"):
        ctx = WorkflowSerializationContext(namespace="default", workflow_id=other)
        assert await codec.with_context(ctx).encode([PLAIN]) == [PLAIN]
    assert await codec.encode([PLAIN]) == [PLAIN]
    assert await codec.decode([PLAIN]) == [PLAIN]


async def test_a_forgotten_subject_does_not_decode() -> None:
    keys = _Keys()
    ctx = WorkflowSerializationContext(namespace="default", workflow_id=SUBJECT)
    [sealed] = await SubjectPayloadCodec(keys).with_context(ctx).encode([PLAIN])
    keys.keys.clear()
    with pytest.raises(SubjectForgottenError):
        await SubjectPayloadCodec(keys).decode([sealed])


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
    made = await one.key_for(SUBJECT, True)
    assert await two.key_for(SUBJECT, True) == made
    cur = await agent_db.execute(
        "SELECT dek_sealed, kek_id FROM ai_payload_keys WHERE subject = %s", (SUBJECT,)
    )
    row = await cur.fetchone()
    assert row is not None and made not in bytes(row[0]) and row[1] == kek.id
    # After a rotation the agent re-wraps at start; meanwhile the previous key opens it.
    assert (
        await PgPayloadKeys(lambda: _same(agent_db), newer, previous=kek).key_for(SUBJECT, False)
        == made
    )
    with pytest.raises(SealError):
        await PgPayloadKeys(lambda: _same(agent_db), newer).key_for(SUBJECT, False)
    # forgetSubject: the tombstone and the deletion. No encoder makes the key again.
    await agent_db.execute("INSERT INTO ai_forgotten_subjects (subject) VALUES (%s)", (SUBJECT,))
    await agent_db.execute("DELETE FROM ai_payload_keys")
    with pytest.raises(SubjectForgottenError):
        await two.key_for(SUBJECT, False)
    with pytest.raises(SubjectForgottenError):
        await two.key_for(SUBJECT, True)
    cur = await agent_db.execute("SELECT count(*) FROM ai_payload_keys")
    assert await cur.fetchone() == (0,)
    assert PgPayloadKeys(lambda: _same(agent_db), kek)._cache_s == 30.0
    assert "\\x" not in repr(one) and str(made) not in repr(one)
