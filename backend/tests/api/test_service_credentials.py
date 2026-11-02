"""Service credentials through the app (#4975 phase 4, spec 2026-11-01 §7.2): issued at
start, presented as a bearer, and the Settings routes."""

from __future__ import annotations

import base64
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR
from scadbuddy.auth.service_credentials import TokenReader
from scadbuddy.core.settings import Settings

PUBLIC = "https://scadbuddy.test"


@pytest.fixture
def settings(settings: Settings, tmp_path: Path) -> Settings:
    key = tmp_path / "kek"
    key.write_text(base64.b64encode(os.urandom(32)).decode())
    return settings.model_copy(
        update={"secret_key_file": key, "public_url": PUBLIC, "auth_mode": "observe"}
    )


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, base_url=PUBLIC) as test_client:
        yield test_client


def _count(app: FastAPI, route_cls: str, reason: str) -> float:
    registry = getattr(app.state, STATE_ATTR).metrics.registry
    value = registry.get_sample_value(
        "scadbuddy_auth_would_refuse_total", {"class": route_cls, "reason": reason}
    )
    return value or 0.0


def test_each_caller_has_one_at_start(client: TestClient) -> None:
    rows = client.get("/api/v1/auth/service-credentials").json()
    assert sorted(row["caller"] for row in rows) == ["agent", "print", "projects"]
    assert all(row["live"] for row in rows)
    assert all("secret" not in key for row in rows for key in row)


def test_a_bearer_is_the_service(client: TestClient, app: FastAPI, settings: Settings) -> None:
    state = getattr(app.state, STATE_ATTR)
    kek = state.settings_store.kek
    token = TokenReader("print", settings.database_url, kek).token()
    assert token is not None
    client.get(
        "/api/v1/internal/outputs/nope/model.3mf", headers={"Authorization": f"Bearer {token}"}
    )
    assert _count(app, "internal.outputs", "no_credential") == 0
    client.get("/api/v1/internal/outputs/nope/model.3mf")
    assert _count(app, "internal.outputs", "no_credential") == 1


def test_rotate_and_revoke(client: TestClient) -> None:
    rows = client.post("/api/v1/auth/service-credentials/rotate", json={"caller": "agent"}).json()
    agents = [row for row in rows if row["caller"] == "agent"]
    assert len(agents) == 2
    for row in agents:
        rows = client.delete(f"/api/v1/auth/service-credentials/{row['id']}").json()
    # Revoking the last live one minted a replacement at once.
    live = [row for row in rows if row["caller"] == "agent" and row["live"]]
    assert len(live) == 1
    assert client.delete(f"/api/v1/auth/service-credentials/{agents[0]['id']}").status_code == 404
