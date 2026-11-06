"""``POST /api/v1/auth/service-token`` through the app (#5494, spec 2026-11-05 §6.2, §6.5,
§7.2, §8 step 3): the exchange with a TokenReview proof (a fake apiserver) and a
bootstrap proof (a temp directory), its refusals, rate limit and counter, the scopes the
credential carries, and Settings' suspension and per-pod revocation."""

from __future__ import annotations

import base64
import os
import secrets
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR
from scadbuddy.auth.component import AUTH
from scadbuddy.core.settings import Settings
from tests.auth.test_service_token_exchange import POD_EXTRA, FakeApiserver, _verifier

PUBLIC = "https://scadbuddy.test"
GRANT = "urn:ietf:params:oauth:grant-type:token-exchange"
JWT = "urn:ietf:params:oauth:token-type:jwt"
BOOTSTRAP = "urn:scadbuddy:params:token-type:bootstrap"
POD_UID = POD_EXTRA["authentication.kubernetes.io/pod-uid"][0]
RENDER_SECRET = secrets.token_urlsafe(32)


@pytest.fixture
def settings(settings: Settings, tmp_path: Path) -> Settings:
    key = tmp_path / "kek"
    key.write_text(base64.b64encode(os.urandom(32)).decode())
    bootstrap = tmp_path / "bootstrap"
    bootstrap.mkdir()
    (bootstrap / "render").write_text(RENDER_SECRET)
    return settings.model_copy(
        update={
            "secret_key_file": key,
            "public_url": PUBLIC,
            "auth_mode": "observe",
            "service_bootstrap_dir": bootstrap,
        }
    )


@pytest.fixture
def apiserver() -> FakeApiserver:
    server = FakeApiserver()
    server.add("jwt-print", "system:serviceaccount:bambuddy:scadbuddy-print")
    server.add("jwt-render", "system:serviceaccount:bambuddy:scadbuddy-render")
    server.add("jwt-default", "system:serviceaccount:bambuddy:default")
    return server


@pytest.fixture
def client(app: FastAPI, apiserver: FakeApiserver, tmp_path: Path) -> Iterator[TestClient]:
    getattr(app.state, STATE_ATTR).components.get(AUTH).verifiers["k8s"] = _verifier(
        apiserver, tmp_path
    )
    with TestClient(app, base_url=PUBLIC) as test_client:
        yield test_client


def _exchange(client: TestClient, token: str, token_type: str = JWT, grant: str = GRANT):  # type: ignore[no-untyped-def]
    return client.post(
        "/api/v1/auth/service-token",
        data={"grant_type": grant, "subject_token": token, "subject_token_type": token_type},
    )


def _count(app: FastAPI, name: str, labels: dict[str, str]) -> float:
    registry = getattr(app.state, STATE_ATTR).metrics.registry
    return registry.get_sample_value(name, labels) or 0.0


def test_a_pod_token_is_exchanged_for_a_15_minute_credential(
    client: TestClient, app: FastAPI
) -> None:
    response = _exchange(client, "jwt-print")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == 900
    assert body["scope"] == "internal.library internal.outputs"
    assert body["issued_token_type"] == "urn:ietf:params:oauth:token-type:access_token"
    assert response.headers["cache-control"] == "no-store"
    token = body["access_token"]
    assert token.startswith("sbsvc_")
    client.get(
        "/api/v1/internal/outputs/nope/model.3mf", headers={"Authorization": f"Bearer {token}"}
    )
    assert (
        _count(
            app,
            "scadbuddy_auth_would_refuse_total",
            {"class": "internal.outputs", "reason": "no_credential"},
        )
        == 0
    )
    rows = client.get("/api/v1/auth/service-credentials").json()
    (row,) = [row for row in rows if row["proof"] == "k8s"]
    assert row["caller"] == "print"
    assert row["pod_name"] == "scadbuddy-print-6c9f-abcde"
    assert row["pod_uid"] == POD_UID
    assert (
        _count(
            app,
            "scadbuddy_service_token_exchanges_total",
            {"caller": "print", "proof": "k8s", "result": "issued"},
        )
        == 1
    )


def test_the_render_credential_reaches_nothing_else(client: TestClient, app: FastAPI) -> None:
    token = _exchange(client, "jwt-render").json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    client.get("/api/v1/internal/outputs/nope/model.3mf", headers=headers)
    client.get("/api/v1/models", headers=headers)
    assert (
        _count(
            app,
            "scadbuddy_auth_would_refuse_total",
            {"class": "internal.outputs", "reason": "no_credential"},
        )
        == 1
    )
    assert (
        _count(app, "scadbuddy_auth_would_refuse_total", {"class": "api", "reason": "out_of_scope"})
        == 1
    )


@pytest.mark.parametrize("token", ["jwt-default", "jwt-unknown"])
def test_a_bad_proof_is_401_and_says_nothing_more(
    client: TestClient, app: FastAPI, token: str
) -> None:
    response = _exchange(client, token)
    assert response.status_code == 401, response.text
    assert response.json()["type"] == "https://scadbuddy.dev/problems/auth/signed-out"
    assert "default" not in response.text
    assert (
        _count(
            app,
            "scadbuddy_service_token_exchanges_total",
            {"caller": "unknown", "proof": "k8s", "result": "refused"},
        )
        == 1
    )


def test_a_bad_request_is_400(client: TestClient) -> None:
    assert _exchange(client, "jwt-print", grant="client_credentials").status_code == 400
    assert _exchange(client, "jwt-print", token_type="urn:x").status_code == 400
    assert _exchange(client, "").status_code == 400
    assert client.post("/api/v1/auth/service-token", data={}).status_code == 400


def test_the_bootstrap_proof(client: TestClient) -> None:
    response = _exchange(client, RENDER_SECRET, BOOTSTRAP)
    assert response.status_code == 200, response.text
    assert response.json()["scope"] == "crud.render"
    assert _exchange(client, secrets.token_urlsafe(32), BOOTSTRAP).status_code == 401
    # A JWT is not a bootstrap secret, nor the reverse.
    assert _exchange(client, RENDER_SECRET, JWT).status_code == 401


def test_without_a_bootstrap_dir_the_bootstrap_proof_is_refused(
    settings: Settings, apiserver: FakeApiserver, tmp_path: Path
) -> None:
    from scadbuddy.main import create_app

    app = create_app(settings.model_copy(update={"service_bootstrap_dir": None}))
    with TestClient(app, base_url=PUBLIC) as client:
        assert _exchange(client, RENDER_SECRET, BOOTSTRAP).status_code == 401
        # Nor, outside a cluster, a JWT.
        assert _exchange(client, "jwt-print").status_code == 401


def test_rate_limited_per_pod_with_retry_after(client: TestClient, app: FastAPI) -> None:
    for _ in range(6):
        assert _exchange(client, "jwt-print").status_code == 200
    response = _exchange(client, "jwt-print")
    assert response.status_code == 429, response.text
    assert 1 <= int(response.headers["retry-after"]) <= 60
    assert (
        _count(
            app,
            "scadbuddy_service_token_exchanges_total",
            {"caller": "print", "proof": "k8s", "result": "rate_limited"},
        )
        == 1
    )


def test_suspend_and_resume_a_caller(client: TestClient, app: FastAPI) -> None:
    token = _exchange(client, "jwt-print").json()["access_token"]
    callers = client.put("/api/v1/auth/service-callers/print/suspension").json()
    (row,) = [c for c in callers if c["caller"] == "print"]
    assert row["suspended_at"] is not None
    assert _exchange(client, "jwt-print").status_code == 401
    rows = client.get("/api/v1/auth/service-credentials").json()
    assert not [r for r in rows if r["caller"] == "print" and r["live"]]
    # Its old credential is refused on the gate's lookup.
    client.get(
        "/api/v1/internal/outputs/nope/model.3mf", headers={"Authorization": f"Bearer {token}"}
    )
    refused = {"class": "internal.outputs", "reason": "no_credential"}
    assert _count(app, "scadbuddy_auth_would_refuse_total", refused) == 1
    labels = {"caller": "print", "proof": "k8s", "result": "suspended"}
    assert _count(app, "scadbuddy_service_token_exchanges_total", labels) == 1
    callers = client.delete("/api/v1/auth/service-callers/print/suspension").json()
    assert all(c["suspended_at"] is None for c in callers)
    assert _exchange(client, "jwt-print").status_code == 200


def test_revoke_a_pods_credentials(client: TestClient) -> None:
    _exchange(client, "jwt-print")
    rows = client.delete(f"/api/v1/auth/service-credentials/pods/{POD_UID}").json()
    assert not [r for r in rows if r["pod_uid"] == POD_UID and r["live"]]
    assert client.delete(f"/api/v1/auth/service-credentials/pods/{POD_UID}").status_code == 404


def test_rotate_takes_the_legacy_callers_only(client: TestClient) -> None:
    response = client.post("/api/v1/auth/service-credentials/rotate", json={"caller": "render"})
    assert response.status_code == 422
