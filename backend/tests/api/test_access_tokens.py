"""Personal access tokens and delegated sessions (#4975 phase 6, spec 2026-11-01 §7.4,
§7.5): minted by a person or the agent's credential, each refused where only a person
may go."""

from __future__ import annotations

import base64
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR
from scadbuddy.auth.component import AUTH
from scadbuddy.auth.gate import SESSION_COOKIE
from scadbuddy.auth.service_credentials import TokenReader
from scadbuddy.core.settings import Settings
from tests.api.test_customers import create as create_customer

PUBLIC = "https://scadbuddy.test"
HEADER = {"X-ScadBuddy-Request": "1"}


@pytest.fixture
def settings(settings: Settings, tmp_path: Path) -> Settings:
    key = tmp_path / "kek"
    key.write_text(base64.b64encode(os.urandom(32)).decode())
    return settings.model_copy(
        update={"secret_key_file": key, "public_url": PUBLIC, "auth_mode": "observe"}
    )


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, base_url=PUBLIC, headers=HEADER) as test_client:
        yield test_client


def _count(app: FastAPI, route_cls: str, reason: str) -> float:
    registry = getattr(app.state, STATE_ATTR).metrics.registry
    value = registry.get_sample_value(
        "scadbuddy_auth_would_refuse_total", {"class": route_cls, "reason": reason}
    )
    return value or 0.0


def _sign_in(client: TestClient, app: FastAPI) -> str:
    auth = getattr(app.state, STATE_ATTR).components.get(AUTH)
    token, session = auth.sessions.create(issuer="https://idp.test", subject="alice", kind="login")
    client.cookies.set(SESSION_COOKIE, token)
    return str(session.principal)


def _mint(client: TestClient, **body: Any) -> dict[str, Any]:
    response = client.post("/api/v1/auth/access-tokens", json={"name": "fs", **body})
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


def test_a_token_is_shown_once_and_acts_as_its_person(client: TestClient, app: FastAPI) -> None:
    principal = _sign_in(client, app)
    created = _mint(client, scope="write")
    assert created["token"].startswith("sbpat_")
    listed = client.get("/api/v1/auth/access-tokens").json()
    assert [row["id"] for row in listed] == [created["access_token"]["id"]]
    assert all("token" not in row for row in listed)
    customer = create_customer(client)
    client.cookies.clear()
    bearer = {"Authorization": f"Bearer {created['token']}"}
    response = client.post("/api/v1/orders", json={"customer_id": customer["id"]}, headers=bearer)
    assert response.status_code == 201, response.text
    expected = {"kind": "browser", "principal": principal, "session": None, "run_id": None}
    assert response.json()["created_by"] == expected
    assert _count(app, "api", "signed_out") == 0
    # Never a person's route: no token mints another.
    client.get("/api/v1/auth/access-tokens", headers=bearer)
    assert _count(app, "person", "not_a_person") == 1


def test_a_read_token_does_not_write(client: TestClient, app: FastAPI) -> None:
    _sign_in(client, app)
    token = _mint(client)["token"]
    client.cookies.clear()
    bearer = {"Authorization": f"Bearer {token}"}
    client.get("/api/v1/models", headers=bearer)
    assert _count(app, "api", "out_of_scope") == 0
    client.post("/api/v1/customers", json={"name": "Ada Lovelace"}, headers=bearer)
    assert _count(app, "api", "out_of_scope") == 1


def test_a_revoked_token_is_nobody(client: TestClient, app: FastAPI) -> None:
    _sign_in(client, app)
    created = _mint(client)
    token_id = created["access_token"]["id"]
    assert client.delete(f"/api/v1/auth/access-tokens/{token_id}").status_code == 204
    assert client.delete(f"/api/v1/auth/access-tokens/{token_id}").status_code == 404
    client.cookies.clear()
    client.get("/api/v1/models", headers={"Authorization": f"Bearer {created['token']}"})
    assert _count(app, "api", "signed_out") == 1


def test_minting_needs_a_signed_in_person(client: TestClient) -> None:
    assert client.post("/api/v1/auth/access-tokens", json={"name": "fs"}).status_code == 401


def _agent(app: FastAPI, settings: Settings) -> dict[str, str]:
    kek = getattr(app.state, STATE_ATTR).settings_store.kek
    token = TokenReader("agent", settings.database_url, kek).token()
    assert token is not None
    return {"Authorization": f"Bearer {token}"}


def test_a_delegated_session(client: TestClient, app: FastAPI, settings: Settings) -> None:
    body = {"principal": "token:abc", "ttl_seconds": 600}
    # Only the agent's credential delegates, signed in or not.
    _sign_in(client, app)
    assert client.post("/api/v1/auth/delegated-sessions", json=body).status_code == 403
    client.cookies.clear()
    response = client.post(
        "/api/v1/auth/delegated-sessions", json=body, headers=_agent(app, settings)
    )
    assert response.status_code == 201, response.text
    url = response.json()["url"]
    assert url.startswith(f"{PUBLIC}/api/v1/auth/delegated?code=")
    code = parse_qs(urlsplit(url).query)["code"][0]
    page = client.get("/api/v1/auth/delegated", params={"code": code})
    assert page.status_code == 200
    assert "__Host-scadbuddy_session=" in page.headers["set-cookie"]
    assert "Max-Age=600" in page.headers["set-cookie"]
    # Once only.
    assert client.get("/api/v1/auth/delegated", params={"code": code}).status_code == 400
    session = client.get("/api/v1/auth/session").json()
    assert session["kind"] == "delegated" and session["user"]["principal"] == "token:abc"
    # It is not a person: a flow's answer is refused, and so is a person's route.
    answer = client.post(
        "/api/v1/workflow-runs/some-run/answer", json={"call_id": "c", "answer": "yes"}
    )
    assert answer.status_code == 403
    client.get("/api/v1/auth/config")
    assert _count(app, "person", "not_a_person") == 2
