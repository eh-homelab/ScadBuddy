"""Service credentials through the app (#4975 phase 4, spec 2026-11-01 §7.2): issued at
start, presented as a bearer, and the Settings routes."""

from __future__ import annotations

import base64
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy import main
from scadbuddy.api.deps import STATE_ATTR
from scadbuddy.auth.service_credentials import TokenReader, _insert
from scadbuddy.core.settings import Settings
from scadbuddy.main import _grant_and_rotate_at_start, _rotate_service_credentials

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
    # #5298: the projects worker exchanges its credential; it gets no sealed one.
    assert sorted(row["caller"] for row in rows) == ["agent", "print"]
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


def test_the_projects_caller_cannot_be_rotated(client: TestClient) -> None:
    response = client.post("/api/v1/auth/service-credentials/rotate", json={"caller": "projects"})
    assert response.status_code == 422


def test_the_start_revokes_a_sealed_projects_credential(client: TestClient, app: FastAPI) -> None:
    """#5298: one minted before this release is revoked at the API's start (and at every
    rotation), so it cannot be used; none is minted again."""
    state = getattr(app.state, STATE_ATTR)
    with state.settings_store.pool.connection() as conn:
        _insert(conn, state.settings_store.kek, "projects", datetime.now(UTC))
    _rotate_service_credentials(state)
    _rotate_service_credentials(state)
    rows = client.get("/api/v1/auth/service-credentials").json()
    projects = [row for row in rows if row["caller"] == "projects"]
    assert len(projects) == 1 and not projects[0]["live"] and projects[0]["revoked_at"]


def test_the_start_revokes_the_retired_roles(
    client: TestClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#5298 Task 11: the start's grants are followed by `revoke_retired`."""
    calls: list[object] = []
    monkeypatch.setattr(main, "revoke_retired", lambda conn: calls.append(conn) or [])
    _grant_and_rotate_at_start(getattr(app.state, STATE_ATTR))
    assert len(calls) == 1


def test_a_failed_revoke_still_rotates_the_credentials(
    client: TestClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A revoke that raises must not cost the start its rotation: the sealed projects
    credential is still revoked."""

    def refuse(conn: object) -> list[str]:
        raise RuntimeError("revoke failed")

    monkeypatch.setattr(main, "revoke_retired", refuse)
    state = getattr(app.state, STATE_ATTR)
    with state.settings_store.pool.connection() as conn:
        _insert(conn, state.settings_store.kek, "projects", datetime.now(UTC))
    _grant_and_rotate_at_start(state)
    rows = client.get("/api/v1/auth/service-credentials").json()
    projects = [row for row in rows if row["caller"] == "projects"]
    assert len(projects) == 1 and not projects[0]["live"] and projects[0]["revoked_at"]
