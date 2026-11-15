"""The agent's Web Push records and notification channels behind its CRUD routes (#5299
step 5, area ``notify``, `api/crud_agent_notify.py`): the API seals and opens them with the
KEK under the agent's own AAD, so the agent needs none, and a row either side writes opens
on the other."""

from __future__ import annotations

import base64
import json
import os
import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from scadbuddy.agent_data.rotation import rotate
from scadbuddy.core.secrets import Envelope, kek_from_base64, open_secret, seal_secret
from scadbuddy.core.settings import Settings
from tests.api.test_crud_agent_cost import token

pytestmark = [pytest.mark.requires_postgres, pytest.mark.agent_migrations]

BASE = "/api/v1/internal/crud/agent"
KEY_TEXT = base64.b64encode(b"\x42" * 32).decode()
KEK = kek_from_base64(KEY_TEXT)
POINT = "04" + "ab" * 64
P256DH = "B" + "A" * 86
AUTH = "A" * 22


@pytest.fixture
def settings(settings: Settings, tmp_path: Path) -> Settings:
    key = tmp_path / "kek"
    key.write_text(KEY_TEXT)
    return settings.model_copy(update={"secret_key_file": key})


@pytest.fixture
def agent(client: TestClient) -> dict[str, str]:
    return token(client)


@pytest.fixture
def db(pg_conninfo: str) -> Any:
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        yield conn


def _envelope(row: Any) -> Envelope:
    return Envelope(secret_sealed=bytes(row[0]), dek_sealed=bytes(row[1]), kek_id=row[2])


def _owner(who: str = "browser") -> dict[str, str]:
    return {"kind": "browser", "id": who, "label": "You"}


def test_the_vapid_pair_is_stored_once_and_sealed_as_the_agent_seals_it(
    client: TestClient, agent: dict[str, str], db: Any
) -> None:
    assert client.get(f"{BASE}/push/vapid", headers=agent).json() == {"vapid": None}
    first = {"public_key": POINT, "private_key": "p" * 43}
    stored = client.post(f"{BASE}/push/vapid", json=first, headers=agent)
    assert stored.status_code == 200, stored.text
    assert stored.json() == {"vapid": first}
    # A second replica's pair loses: both read the first back.
    second = {"public_key": "04" + "cd" * 64, "private_key": "q" * 43}
    assert client.post(f"{BASE}/push/vapid", json=second, headers=agent).json() == {"vapid": first}
    row = db.execute("SELECT secret_sealed, dek_sealed, kek_id FROM ai_push_vapid").fetchone()
    assert open_secret(KEK, _envelope(row), "push-vapid:1") == "p" * 43


def test_a_subscription_is_its_owners_and_capped(
    client: TestClient, agent: dict[str, str], db: Any
) -> None:
    def subscribe(endpoint: str, who: str = "browser") -> Any:
        body = {"owner": _owner(who), "endpoint": endpoint, "p256dh": P256DH, "auth": AUTH}
        return client.post(f"{BASE}/push/subscriptions", json=body, headers=agent)

    first = subscribe("https://push.example/1")
    assert first.status_code == 200, first.text
    sub_id = first.json()["id"]
    # The same browser again keeps its id.
    assert subscribe("https://push.example/1").json() == {"id": sub_id}
    taken = subscribe("https://push.example/1", "someone-else")
    assert (taken.status_code, taken.json()["reason"]) == (409, "subscription-taken")

    row = db.execute(
        "SELECT secret_sealed, dek_sealed, kek_id FROM ai_push_subscriptions WHERE id = %s",
        (sub_id,),
    ).fetchone()
    opened = open_secret(KEK, _envelope(row), f"push-subscription:{sub_id}:https://push.example/1")
    assert json.loads(opened) == {"p256dh": P256DH, "auth": AUTH}

    for n in range(2, 8):
        subscribe(f"https://push.example/{n}")
    listed = client.get(f"{BASE}/push/subscriptions", headers=agent).json()["subscriptions"]
    assert len(listed) == 5
    assert all((s["p256dh"], s["auth"]) == (P256DH, AUTH) for s in listed)

    # A row sealed by another key is skipped, never an error.
    other = kek_from_base64(base64.b64encode(os.urandom(32)).decode())
    sealed = seal_secret(other, "{}", "x")
    db.execute(
        "UPDATE ai_push_subscriptions SET secret_sealed = %s, dek_sealed = %s, kek_id = %s"
        " WHERE endpoint = 'https://push.example/7'",
        (sealed.secret_sealed, sealed.dek_sealed, sealed.kek_id),
    )
    listed = client.get(f"{BASE}/push/subscriptions", headers=agent).json()["subscriptions"]
    assert len(listed) == 4

    gone = {"endpoint": "https://push.example/7", "owner": _owner("someone-else")}
    assert client.post(f"{BASE}/push/subscriptions/remove", json=gone, headers=agent).json() == {
        "done": False
    }
    gone = {"endpoint": "https://push.example/7"}
    assert client.post(f"{BASE}/push/subscriptions/remove", json=gone, headers=agent).json() == {
        "done": True
    }


def test_a_sent_claim_is_won_once(client: TestClient, agent: dict[str, str], db: Any) -> None:
    body = {"request_id": "question:1", "target": "push:x"}
    assert client.post(f"{BASE}/push/sent", json=body, headers=agent).json() == {"claimed": True}
    assert client.post(f"{BASE}/push/sent", json=body, headers=agent).json() == {"claimed": False}
    db.execute("UPDATE ai_notifications_sent SET sent_at = now() - interval '3 days'")
    assert client.post(f"{BASE}/push/sent", json=body, headers=agent).json() == {"claimed": True}


def _channel(client: TestClient, agent: dict[str, str], **extra: Any) -> str:
    body = {"kind": "ntfy", "name": "Phone", "url": "https://ntfy.sh/topic", **extra}
    answer = client.post(f"{BASE}/notify-channels", json=body, headers=agent)
    assert answer.status_code == 201, answer.text
    return str(answer.json()["id"])


def test_a_channels_token_is_sealed_to_its_kind_and_url(
    client: TestClient, agent: dict[str, str], db: Any
) -> None:
    channel_id = _channel(client, agent, token="tk_0123456789abcd")
    [view] = client.get(f"{BASE}/notify-channels", headers=agent).json()["channels"]
    assert (view["has_token"], view["token_last4"]) == (True, "abcd")
    assert "tk_0123456789abcd" not in json.dumps(view)
    row = db.execute("SELECT secret_sealed, dek_sealed, kek_id FROM ai_notify_channels").fetchone()
    aad = f"notify-channel:{channel_id}:ntfy:https://ntfy.sh/topic"
    assert open_secret(KEK, _envelope(row), aad) == "tk_0123456789abcd"

    opened = client.post(f"{BASE}/notify-channels/{channel_id}/open", headers=agent).json()
    assert opened["channel"]["token"] == "tk_0123456789abcd"

    # Keeping the token holds only while the kind and URL are the row's.
    keep = {"kind": "ntfy", "name": "Phone 2", "url": "https://ntfy.sh/topic", "keep_token": True}
    path = f"{BASE}/notify-channels/{channel_id}/update"
    assert client.post(path, json=keep, headers=agent).json() == {"done": True}
    moved = client.post(path, json={**keep, "url": "https://ntfy.sh/other"}, headers=agent)
    assert (moved.status_code, moved.json()["reason"]) == (409, "channel-moved")
    dropped = {"kind": "ntfy", "name": "Phone", "url": "https://ntfy.sh/other"}
    assert client.post(path, json=dropped, headers=agent).json() == {"done": True}
    [view] = client.get(f"{BASE}/notify-channels", headers=agent).json()["channels"]
    assert (view["name"], view["has_token"], view["url"]) == (
        "Phone",
        False,
        "https://ntfy.sh/other",
    )


def test_the_enabled_channels_open_and_a_send_is_recorded(
    client: TestClient, agent: dict[str, str]
) -> None:
    on = _channel(client, agent)
    _channel(client, agent, enabled=False)
    enabled = client.post(f"{BASE}/notify-channels/open-enabled", headers=agent).json()
    assert [c["id"] for c in enabled["channels"]] == [on]
    record = f"{BASE}/notify-channels/{on}/record"
    assert client.post(record, json={"error": "HTTP 500" * 200}, headers=agent).json() == {
        "done": True
    }
    one = client.get(f"{BASE}/notify-channels/{on}", headers=agent).json()["channel"]
    assert len(one["last_error"]) == 500
    client.post(record, json={"error": None}, headers=agent)
    one = client.get(f"{BASE}/notify-channels/{on}", headers=agent).json()["channel"]
    assert (one["last_error"], one["last_sent_at"] is not None) == (None, True)
    assert client.post(f"{BASE}/notify-channels/{on}/delete", headers=agent).json() == {
        "done": True
    }


def test_home_assistant_needs_a_service_and_a_token(
    client: TestClient, agent: dict[str, str]
) -> None:
    body = {
        "kind": "home_assistant",
        "name": "HA",
        "url": "https://ha.local",
        "target": "mobile_app_x",
    }
    assert client.post(f"{BASE}/notify-channels", json=body, headers=agent).status_code == 422
    no_target = {**body, "target": None, "token": "t" * 20}
    assert client.post(f"{BASE}/notify-channels", json=no_target, headers=agent).status_code == 422
    assert (
        client.post(
            f"{BASE}/notify-channels", json={**body, "token": "t" * 20}, headers=agent
        ).status_code
        == 201
    )


def test_without_a_kek_the_api_refuses_what_it_must_seal(
    client: TestClient, agent: dict[str, str], settings: Settings
) -> None:
    app: Any = client.app
    app.state.scadbuddy.settings = settings.model_copy(update={"secret_key_file": None})
    refused = client.get(f"{BASE}/push/vapid", headers=agent)
    assert (refused.status_code, refused.json()["reason"]) == (503, "kek-missing")
    # A channel with no token needs no key.
    assert client.post(f"{BASE}/notify-channels/open-enabled", headers=agent).status_code == 200


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
