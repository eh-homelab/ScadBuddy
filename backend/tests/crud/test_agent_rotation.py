"""The re-wrap of the agent's Claude credentials and payload data keys at the API's start
(`agent_data/rotation.py`, #5299 D5): each row sealed under the previous KEK is sealed under the
current one, and a row the previous KEK cannot open is left and counted. The routes that seal
and open them are Go's (#7667), tested in ``tools/go/core/internal/secrets``."""

from __future__ import annotations

import base64
import uuid
from typing import Any

import pytest

from scadbuddy.agent_data import credentials
from scadbuddy.agent_data.rotation import rotate
from scadbuddy.core.secrets import (
    Envelope,
    Kek,
    kek_from_base64,
    open_bytes,
    open_secret,
    seal_bytes,
    seal_secret,
)
from scadbuddy.render.agent_migrations import migrate_agent
from scadbuddy.workflows.payload_codec import payload_key_context
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_postgres

KEK = kek_from_base64(base64.b64encode(b"\x31" * 32).decode())
OLD = kek_from_base64(base64.b64encode(b"\x32" * 32).decode())
API_KEY = "sk-ant-api03-aaaaaaaaaaaaaaaaaaaaaaaa1234"


@pytest.fixture
def tables(pg_pool: PgPool) -> PgPool:
    # The agent's own migration files, as the API applies them at its start (#5299 D6).
    with pg_pool.connection() as conn:
        migrate_agent(conn)
    return pg_pool


def _sql(pool: PgPool, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with pool.connection() as conn:
        cur = conn.execute(query.encode(), params)
        return list(cur.fetchall()) if cur.description else []


def _seal_credential(kek: Kek, pool: PgPool, priority: int = 0) -> str:
    row_id = str(uuid.uuid4())
    envelope = seal_secret(
        kek, API_KEY, credentials.credential_aad(row_id, "anthropic_api_key", None)
    )
    _sql(
        pool,
        "INSERT INTO ai_credentials (id, priority, kind, secret_sealed, dek_sealed, kek_id, last4)"
        " VALUES (%s, %s, 'anthropic_api_key', %s, %s, %s, '1234')",
        (row_id, priority, envelope.secret_sealed, envelope.dek_sealed, envelope.kek_id),
    )
    return row_id


def test_rotation_rewraps_credentials_and_payload_keys(tables: PgPool) -> None:
    row_id = _seal_credential(OLD, tables)
    # A v1 row (#354) is left as it is, and counted.
    legacy = _seal_credential(OLD, tables, priority=1)
    _sql(
        tables,
        "UPDATE ai_credentials SET secret_sealed = set_byte(secret_sealed, 0, 1) WHERE id = %s",
        (legacy,),
    )
    subject = f"session-{uuid.uuid4()}"
    dek = b"\x55" * 32
    _sql(
        tables,
        "INSERT INTO ai_payload_keys (subject, dek_sealed, kek_id) VALUES (%s, %s, %s)",
        (subject, seal_bytes(OLD.key, dek, payload_key_context(subject)), OLD.id),
    )
    _sql(
        tables,
        "INSERT INTO ai_payload_keys (subject, dek_sealed, kek_id) VALUES (%s, '\\x0200', %s)",
        (f"flow-{uuid.uuid4()}", OLD.id),
    )
    with tables.connection() as conn:
        assert rotate(conn, OLD, KEK) == {
            "credentials": (1, 1),
            "payload_keys": (1, 1),
            "notify": (0, 0),
            "plugins": (0, 0),
            "session_files": (0, 0),
        }
        assert rotate(conn, OLD, KEK) == {
            "credentials": (0, 1),
            "payload_keys": (0, 1),
            "notify": (0, 0),
            "plugins": (0, 0),
            "session_files": (0, 0),
        }
        assert rotate(conn, KEK, KEK) == {}
    row = _sql(
        tables,
        "SELECT secret_sealed, dek_sealed, kek_id FROM ai_credentials WHERE id = %s",
        (row_id,),
    )[0]
    envelope = Envelope(
        secret_sealed=bytes(row["secret_sealed"]),
        dek_sealed=bytes(row["dek_sealed"]),
        kek_id=row["kek_id"],
    )
    aad = credentials.credential_aad(row_id, "anthropic_api_key", None)
    assert open_secret(KEK, envelope, aad) == API_KEY
    key = _sql(tables, "SELECT dek_sealed FROM ai_payload_keys WHERE subject = %s", (subject,))[0]
    assert open_bytes(KEK.key, bytes(key["dek_sealed"]), payload_key_context(subject)) == dek
