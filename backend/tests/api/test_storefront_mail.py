"""`/api/v1/storefront/mail` (#5283, spec 2026-11-02 §5.7): the SMTP password is sealed
under the KEK, bound to the host, port and user it was saved for, and reported only as
set. The connection's settings and the password are a person's to change (ruling R2)."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scadbuddy.core.settings import Settings
from scadbuddy.main import create_app

pytestmark = [pytest.mark.requires_postgres, pytest.mark.requires_temporal]

PROBLEMS = "https://scadbuddy.dev/problems/storefront/"
KEK_MISSING = PROBLEMS + "kek_missing"
AGENT = {"X-ScadBuddy-Agent-Author": "token:p1", "X-ScadBuddy-Agent-Author-Session": "s1"}
CONNECTION = {
    "storefront_mail_host": "smtp.example.com",
    "storefront_mail_user": "shop",
    "storefront_mail_sender": "shop@example.com",
}


@pytest.fixture
def settings(settings: Settings, tmp_path: Path) -> Settings:
    key = tmp_path / "kek"
    key.write_text(base64.b64encode(b"\x07" * 32).decode())
    return settings.model_copy(update={"secret_key_file": key})


def connect(client: TestClient) -> None:
    assert client.put("/api/v1/settings", json=CONNECTION).status_code == 200


def test_the_storefront_mail_password_is_write_only(client: TestClient) -> None:
    assert client.get("/api/v1/storefront/mail/password").json() == {"set": False}
    connect(client)
    put = client.put("/api/v1/storefront/mail/password", json={"password": "hunter2"})
    assert put.status_code == 200 and put.json() == {"set": True}
    assert "hunter2" not in client.get("/api/v1/settings").text
    assert "hunter2" not in client.get("/api/v1/storefront/mail").text
    assert client.get("/api/v1/storefront/mail/password").json() == {"set": True}
    shown = client.get("/api/v1/storefront/mail").json()
    # The sender may have tried once between `connect` and the password (and noted "no
    # SMTP password is set"), so the server's error fields are not asserted here.
    assert set(shown) == {"password_set", "password_for", "server_error", "server_error_at"}
    assert shown["password_set"] is True
    assert shown["password_for"] == "shop@smtp.example.com:587 (starttls)"
    gone = client.delete("/api/v1/storefront/mail/password")
    assert gone.status_code == 200 and gone.json() == {"set": False}
    assert client.get("/api/v1/storefront/mail/password").json() == {"set": False}


def test_the_password_needs_a_host_and_a_user(client: TestClient) -> None:
    put = client.put("/api/v1/storefront/mail/password", json={"password": "pw"})
    assert put.status_code == 409 and put.json()["type"] == PROBLEMS + "mail_connection_unset"


def test_an_empty_storefront_mail_password_is_refused(client: TestClient) -> None:
    put = client.put("/api/v1/storefront/mail/password", json={"password": ""})
    assert put.status_code == 422


def test_the_password_needs_the_key_file(settings: Settings) -> None:
    app = create_app(settings.model_copy(update={"secret_key_file": None}))
    with TestClient(app) as client:
        connect(client)
        put = client.put("/api/v1/storefront/mail/password", json={"password": "pw"})
    assert put.status_code == 409 and put.json()["type"] == KEK_MISSING


@pytest.mark.parametrize(
    "body",
    [
        {"storefront_mail_host": "evil.example.com"},
        {"storefront_mail_port": 2525},
        {"storefront_mail_user": "attacker"},
        {"storefront_mail_sender": "x@evil.example.com"},
        {"storefront_mail_tls": "none"},
        {"storefront_mail_tls": "implicit"},
        {"reset": ["storefront_mail_host"]},
    ],
)
def test_only_a_person_changes_the_mail_connection(
    client: TestClient, body: dict[str, object]
) -> None:
    connect(client)
    refused = client.put("/api/v1/settings", json=body, headers=AGENT)
    assert refused.status_code == 403 and refused.json()["type"] == PROBLEMS + "person_only"
    assert client.get("/api/v1/settings").json()["storefront_mail_host"] == "smtp.example.com"


def test_an_agent_still_changes_other_settings(client: TestClient) -> None:
    put = client.put("/api/v1/settings", json={"storefront_renders_per_minute": 5}, headers=AGENT)
    assert put.status_code == 200


def test_only_a_person_sets_or_forgets_the_password(client: TestClient) -> None:
    connect(client)
    for method in ("put", "delete"):
        kwargs: dict[str, object] = {"headers": AGENT}
        if method == "put":
            kwargs["json"] = {"password": "pw"}
        refused = getattr(client, method)("/api/v1/storefront/mail/password", **kwargs)
        assert refused.status_code == 403 and refused.json()["type"] == PROBLEMS + "person_only"


def test_the_tls_mode_is_starttls_implicit_or_none(client: TestClient) -> None:
    assert client.get("/api/v1/settings").json()["storefront_mail_tls"] == "starttls"
    ok = client.put("/api/v1/settings", json={"storefront_mail_tls": "implicit"})
    assert ok.status_code == 200 and ok.json()["storefront_mail_tls"] == "implicit"
    assert client.put("/api/v1/settings", json={"storefront_mail_tls": "plain"}).status_code == 422
    assert "storefront_mail_starttls" not in client.get("/api/v1/settings").json()


def test_no_tls_is_only_for_a_relay_with_no_login(client: TestClient) -> None:
    relay = client.put(
        "/api/v1/settings",
        json={"storefront_mail_host": "relay.lan", "storefront_mail_tls": "none"},
    )
    assert relay.status_code == 200 and relay.json()["storefront_mail_tls"] == "none"
    login = client.put("/api/v1/settings", json={"storefront_mail_user": "shop"})
    assert login.status_code == 422
    assert client.get("/api/v1/settings").json()["storefront_mail_user"] is None
    both = client.put(
        "/api/v1/settings",
        json={"storefront_mail_user": "shop", "storefront_mail_tls": "starttls"},
    )
    assert both.status_code == 200
    back = client.put("/api/v1/settings", json={"storefront_mail_tls": "none"})
    assert back.status_code == 422
