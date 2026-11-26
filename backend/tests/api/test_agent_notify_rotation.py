"""The re-wrap of the agent's Web Push keys and notification channels' tokens at a key
rotation (#5299 step 5, `agent_data/rotation.py`). The routes of area ``notify`` that seal
and open them are Go's (#7667), tested in ``tools/go/core/internal/notify``."""

from __future__ import annotations

import base64
import uuid
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from scadbuddy.agent_data.rotation import rotate
from scadbuddy.core.secrets import Envelope, kek_from_base64, open_secret, seal_secret

pytestmark = [pytest.mark.requires_postgres, pytest.mark.agent_migrations]

KEY_TEXT = base64.b64encode(b"\x42" * 32).decode()
KEK = kek_from_base64(KEY_TEXT)
POINT = "04" + "ab" * 64


@pytest.fixture
def db(pg_conninfo: str) -> Any:
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        yield conn


def _envelope(row: Any) -> Envelope:
    return Envelope(secret_sealed=bytes(row[0]), dek_sealed=bytes(row[1]), kek_id=row[2])


def test_a_key_rotation_rewraps_the_push_keys_and_channel_tokens(
    client: TestClient, db: Any
) -> None:
    """At the API's start (`agent_data/rotation.py`), as the agent did at its own."""
    old = kek_from_base64(base64.b64encode(b"\x07" * 32).decode())
    vapid = seal_secret(old, "p" * 43, "push-vapid:1")
    db.execute(
        "INSERT INTO ai_push_vapid (id, public_key, secret_sealed, dek_sealed, kek_id)"
        " VALUES (1, %s, %s, %s, %s)",
        (bytes.fromhex(POINT), vapid.secret_sealed, vapid.dek_sealed, vapid.kek_id),
    )
    sub_id = str(uuid.uuid4())
    endpoint = "https://push.example/r"
    sub = seal_secret(old, "{}", f"push-subscription:{sub_id}:{endpoint}")
    db.execute(
        "INSERT INTO ai_push_subscriptions (id, owner_kind, owner_id, owner_label, endpoint,"
        " secret_sealed, dek_sealed, kek_id) VALUES (%s, 'browser', 'b', 'You', %s, %s, %s, %s)",
        (sub_id, endpoint, sub.secret_sealed, sub.dek_sealed, sub.kek_id),
    )
    channel_id = str(uuid.uuid4())
    url = "https://ntfy.sh/t"
    tok = seal_secret(old, "tk", f"notify-channel:{channel_id}:ntfy:{url}")
    db.execute(
        "INSERT INTO ai_notify_channels (id, kind, name, url, secret_sealed, dek_sealed, kek_id)"
        " VALUES (%s, 'ntfy', 'n', %s, %s, %s, %s)",
        (channel_id, url, tok.secret_sealed, tok.dek_sealed, tok.kek_id),
    )
    assert rotate(db, old, KEK)["notify"] == (3, 0)
    assert rotate(db, old, KEK)["notify"] == (0, 0)
    row = db.execute("SELECT secret_sealed, dek_sealed, kek_id FROM ai_push_vapid").fetchone()
    assert open_secret(KEK, _envelope(row), "push-vapid:1") == "p" * 43
    row = db.execute("SELECT secret_sealed, dek_sealed, kek_id FROM ai_notify_channels").fetchone()
    assert open_secret(KEK, _envelope(row), f"notify-channel:{channel_id}:ntfy:{url}") == "tk"
