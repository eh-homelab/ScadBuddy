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
from tests.auth.test_service_token_exchange import (
    FORGER_KEY,
    POD_EXTRA,
    FakeApiserver,
    _verifier,
    hs256,
    signed,
    unsigned,
)

PUBLIC = "https://scadbuddy.test"
# Projected tokens as the fake cluster signs them; JWT_UNKNOWN is signed by a key it
# never published.
JWT_PRINT = signed("scadbuddy-print")
JWT_RENDER = signed("scadbuddy-render")
JWT_DEFAULT = signed("default")
JWT_UNKNOWN = signed("scadbuddy-print", key=FORGER_KEY)
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
    return FakeApiserver()


@pytest.fixture
def client(app: FastAPI, apiserver: FakeApiserver, tmp_path: Path) -> Iterator[TestClient]:
    getattr(app.state, STATE_ATTR).components.get(AUTH).verifiers["k8s"] = _verifier(
        apiserver, tmp_path
    )
    with TestClient(app, base_url=PUBLIC) as test_client:
        yield test_client


def _exchange(  # type: ignore[no-untyped-def]
    client: TestClient,
    token: str,
    token_type: str = JWT,
    grant: str = GRANT,
    client_id: str | None = None,
):
    data = {"grant_type": grant, "subject_token": token, "subject_token_type": token_type}
    if client_id is not None:
        data["client_id"] = client_id
    return client.post("/api/v1/auth/service-token", data=data)


def _count(app: FastAPI, name: str, labels: dict[str, str]) -> float:
    registry = getattr(app.state, STATE_ATTR).metrics.registry
    return registry.get_sample_value(name, labels) or 0.0


def test_a_pod_token_is_exchanged_for_a_15_minute_credential(
    client: TestClient, app: FastAPI
) -> None:
    response = _exchange(client, JWT_PRINT)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == 900
    assert body["scope"] == "crud.print internal.library internal.outputs"
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
    token = _exchange(client, JWT_RENDER).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    client.get("/api/v1/internal/outputs/nope/model.3mf", headers=headers)
    client.get("/api/v1/models", headers=headers)
    assert (
        _count(
            app,
            "scadbuddy_auth_would_refuse_total",
            {"class": "internal.outputs", "reason": "out_of_scope"},
        )
        == 1
    )
    assert (
        _count(app, "scadbuddy_auth_would_refuse_total", {"class": "api", "reason": "out_of_scope"})
        == 1
    )


@pytest.mark.parametrize("token", [JWT_DEFAULT, JWT_UNKNOWN])
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
    assert _exchange(client, JWT_PRINT, grant="client_credentials").status_code == 400
    assert _exchange(client, JWT_PRINT, token_type="urn:x").status_code == 400
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
        assert _exchange(client, JWT_PRINT).status_code == 401


def test_rate_limited_per_pod_with_retry_after(client: TestClient, app: FastAPI) -> None:
    for _ in range(6):
        assert _exchange(client, JWT_PRINT).status_code == 200
    response = _exchange(client, JWT_PRINT)
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
    token = _exchange(client, JWT_PRINT).json()["access_token"]
    callers = client.put("/api/v1/auth/service-callers/print/suspension").json()
    (row,) = [c for c in callers if c["caller"] == "print"]
    assert row["suspended_at"] is not None
    assert _exchange(client, JWT_PRINT).status_code == 401
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
    assert _exchange(client, JWT_PRINT).status_code == 200


def test_revoke_a_pods_credentials(client: TestClient) -> None:
    _exchange(client, JWT_PRINT)
    rows = client.delete(f"/api/v1/auth/service-credentials/pods/{POD_UID}").json()
    assert not [r for r in rows if r["pod_uid"] == POD_UID and r["live"]]
    assert client.delete(f"/api/v1/auth/service-credentials/pods/{POD_UID}").status_code == 404


def test_rotate_takes_the_legacy_callers_only(client: TestClient) -> None:
    response = client.post("/api/v1/auth/service-credentials/rotate", json={"caller": "render"})
    assert response.status_code == 422


# Forged and junk proofs never lock a real caller out (review of #5498): they are refused
# offline, before any TokenReview, and spend nothing.


def _limited(app: FastAPI, caller: str, proof: str = "k8s") -> float:
    labels = {"caller": caller, "proof": proof, "result": "limited"}
    return _count(app, "scadbuddy_service_token_exchanges_total", labels)


def test_forged_print_tokens_never_block_the_real_one(
    client: TestClient, app: FastAPI, apiserver: FakeApiserver
) -> None:
    forged = [
        *(signed("scadbuddy-print", key=FORGER_KEY, nonce=str(i)) for i in range(40)),
        *(unsigned("scadbuddy-print") for _ in range(40)),
        *(hs256("scadbuddy-print") for _ in range(40)),
    ]
    for token in forged:
        assert _exchange(client, token).status_code == 401
    assert apiserver.requests == []
    assert _exchange(client, JWT_PRINT).status_code == 200


def test_junk_tokens_never_cause_a_429_for_a_real_one(
    client: TestClient, apiserver: FakeApiserver
) -> None:
    for i in range(200):
        assert _exchange(client, f"not-a-jwt-{i}").status_code == 401
    assert apiserver.requests == []
    assert _exchange(client, JWT_PRINT).status_code == 200


def test_the_tokenreview_budget_counts_signed_tokens_only(
    client: TestClient, app: FastAPI, apiserver: FakeApiserver
) -> None:
    """60 TokenReviews a minute per API process, spent only by tokens the cluster
    signed for a known caller (here, pods deleted since)."""
    for i in range(60):
        token = signed("scadbuddy-print", nonce=f"deleted-{i}")
        apiserver.revoke(token)
        assert _exchange(client, token).status_code == 401
    assert len(apiserver.requests) == 60
    response = _exchange(client, JWT_PRINT)
    assert response.status_code == 429, response.text
    assert 1 <= int(response.headers["retry-after"]) <= 60
    assert len(apiserver.requests) == 60
    assert _limited(app, "print") == 1


def test_bootstrap_proofs_have_no_bucket(client: TestClient) -> None:
    for _ in range(50):
        wrong = secrets.token_urlsafe(32)
        assert _exchange(client, wrong, BOOTSTRAP, client_id="render").status_code == 401
    assert _exchange(client, RENDER_SECRET, BOOTSTRAP, client_id="render").status_code == 200


def test_a_bootstrap_secret_sent_under_another_name_is_refused(client: TestClient) -> None:
    assert _exchange(client, RENDER_SECRET, BOOTSTRAP, client_id="print").status_code == 401
    assert _exchange(client, RENDER_SECRET, BOOTSTRAP, client_id="nobody").status_code == 400
    assert _exchange(client, RENDER_SECRET, BOOTSTRAP, client_id="render").status_code == 200
