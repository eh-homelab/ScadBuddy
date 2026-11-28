"""Service credentials through the app (#4975 phase 4, spec 2026-11-01 §7.2): issued at
start, presented as a bearer, and the Settings routes; and how long an ended one's row is
kept (#8539, the stored-only setting `service_credential_retention_days`)."""

from __future__ import annotations

import base64
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy import main
from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.auth.component import AUTH
from scadbuddy.auth.service_credentials import (
    ServiceCaller,
    ServiceCredentialStore,
    _insert,
)
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
    # #5298: the projects and print workers exchange their credentials; they get no
    # sealed one.
    assert sorted(row["caller"] for row in rows) == ["agent"]
    assert all(row["live"] for row in rows)
    assert all("secret" not in key for row in rows for key in row)


def test_a_bearer_is_the_service(client: TestClient, app: FastAPI, settings: Settings) -> None:
    state = getattr(app.state, STATE_ATTR)
    # The print worker exchanges its proof for its credential (#5298), as here.
    store = ServiceCredentialStore(state.settings_store.pool)
    token = store.issue("print", "bootstrap", rate_limited=False).token
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


@pytest.mark.parametrize("caller", ["projects", "print"])
def test_a_worker_caller_cannot_be_rotated(client: TestClient, caller: str) -> None:
    response = client.post("/api/v1/auth/service-credentials/rotate", json={"caller": caller})
    assert response.status_code == 422


@pytest.mark.parametrize("caller", ["projects", "print"])
def test_the_start_revokes_a_workers_sealed_credential(
    client: TestClient, app: FastAPI, caller: ServiceCaller
) -> None:
    """#5298: one minted before this release is revoked at the API's start (and at every
    rotation), so it cannot be used; none is minted again. The print worker's since
    #5298 Task 35."""
    state = getattr(app.state, STATE_ATTR)
    with state.settings_store.pool.connection() as conn:
        _insert(conn, state.settings_store.kek, caller, datetime.now(UTC))
    _rotate_service_credentials(state)
    _rotate_service_credentials(state)
    rows = client.get("/api/v1/auth/service-credentials").json()
    sealed = [row for row in rows if row["caller"] == caller]
    assert len(sealed) == 1 and not sealed[0]["live"] and sealed[0]["revoked_at"]


def test_the_start_revokes_the_retired_roles(
    client: TestClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#5298 Task 11: the start's grants are followed by `revoke_retired`."""
    calls: list[object] = []

    def record(conn: object) -> list[str]:
        calls.append(conn)
        return []

    monkeypatch.setattr(main, "revoke_retired", record)
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


def test_a_failed_state_read_is_logged_and_still_rotates_the_credentials(
    client: TestClient,
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#5299 step 12: a `retired_state` that raises is logged, and the start still
    rotates its credentials (the step is never fatal)."""

    def refuse(conn: object) -> list[object]:
        raise RuntimeError("state read failed")

    monkeypatch.setattr(main, "retired_state", refuse)
    state = getattr(app.state, STATE_ATTR)
    with state.settings_store.pool.connection() as conn:
        _insert(conn, state.settings_store.kek, "projects", datetime.now(UTC))
    with caplog.at_level("ERROR", logger=main.logger.name):
        _grant_and_rotate_at_start(state)
    assert "could not read what the retired callers' roles still have" in caplog.text
    rows = client.get("/api/v1/auth/service-credentials").json()
    projects = [row for row in rows if row["caller"] == "projects"]
    assert len(projects) == 1 and not projects[0]["live"] and projects[0]["revoked_at"]


RETENTION = "service_credential_retention_days"


def test_the_retention_setting_round_trips(client: TestClient) -> None:
    """#8539: stored only, empty by default, 0 to ten years."""
    body = client.get("/api/v1/settings").json()
    assert body[RETENTION] is None
    assert RETENTION not in body["sources"]
    assert body["docs"][RETENTION]["title"] == "Keep ended service credentials for (days)"
    assert client.put("/api/v1/settings", json={RETENTION: 0}).status_code == 200
    assert client.get("/api/v1/settings").json()[RETENTION] == 0
    assert client.put("/api/v1/settings", json={RETENTION: -1}).status_code == 422
    assert client.put("/api/v1/settings", json={RETENTION: 3651}).status_code == 422
    assert client.put("/api/v1/settings", json={RETENTION: None}).status_code == 200
    assert client.get("/api/v1/settings").json()[RETENTION] is None


def test_the_sweep_deletes_ended_credentials_by_the_setting(client: TestClient) -> None:
    """#8539: 0 keeps every one, a number of days keeps them that long, empty is a day."""
    state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    credentials = state.components.get(AUTH).credentials
    two_days_ago = datetime.now(UTC) - timedelta(days=2)
    issued = credentials.issue(
        "render", "k8s", pod_name="p", pod_uid="u", rate_limited=False, now=two_days_ago
    )

    def kept() -> bool:
        return issued.credential_id in {c.id for c in credentials.list()}

    for days in (0, 3):
        assert client.put("/api/v1/settings", json={RETENTION: days}).status_code == 200
        _rotate_service_credentials(state)
        assert kept(), days
    assert client.put("/api/v1/settings", json={RETENTION: None}).status_code == 200
    _rotate_service_credentials(state)
    assert not kept()
