"""The two keys the agent derived from its own KEK, through CRUD routes (#5299 step 12):
only the ``agent`` credential reaches them, the input-hash key is the one the agent
derived (so a hash written before still matches), and a fetch grant minted here passes
the asset route's check."""

from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from scadbuddy.api import crud_agent_keys, crud_areas
from scadbuddy.auth.check import auth_check
from scadbuddy.auth.component import AUTH
from scadbuddy.auth.gate import AuthGate
from scadbuddy.auth.service_credentials import (
    InProcessProvider,
    ServiceCaller,
    ServiceCredentialStore,
)
from scadbuddy.core.metrics import Metrics
from scadbuddy.core.problems import install_problem_handlers
from scadbuddy.core.secrets import kek_from_base64
from scadbuddy.library.fetch_grants import FetchGrantError, fetch_grant_key, verify
from tests.conftest import PgPool
from tests.crud.crud_support import _NoSessions, _Noted

pytestmark = pytest.mark.requires_postgres

BASE = "/api/v1/internal/crud/agent"
KEK_B64 = base64.b64encode(b"\x31" * 32).decode()
#: The agent's own derivation of that KEK's key (Node, `hkdfSync('sha256', kek, empty,
#: 'scadbuddy:ai_approvals:input_hash:v1', 32)`, as `approvals/service.ts` ran it).
AGENT_INPUT_HASH_KEY = "Sl3Jx+pP/OVUmJWyOmEw4ogfnYnnhW/M0UZfNJrtyEw="


def _app(pool: PgPool, tmp_path: Path, *, kek: str | None) -> FastAPI:
    key_file = None
    if kek is not None:
        key_file = tmp_path / f"kek-{uuid.uuid4().hex}"
        key_file.write_text(kek)
    credentials = ServiceCredentialStore(pool)
    parts: dict[Any, Any] = {
        AUTH: SimpleNamespace(
            would_refuse=_Noted(),
            verifiers={},
            credentials=credentials,
            core=SimpleNamespace(metrics=Metrics()),
        ),
    }
    app = FastAPI(dependencies=[Depends(auth_check)])
    app.state.scadbuddy = SimpleNamespace(
        settings=SimpleNamespace(
            auth_mode="enforce", secret_key_file=key_file, secret_key_previous_file=None
        ),
        components=SimpleNamespace(get=lambda key: parts[key]),
        settings_store=SimpleNamespace(pool=pool),
    )
    app.include_router(crud_agent_keys.router, prefix="/api/v1")
    app.include_router(crud_areas.router, prefix="/api/v1")
    install_problem_handlers(app)
    app.add_middleware(AuthGate, sessions=lambda: _NoSessions(), credentials=lambda: credentials)
    return app


def _token(app: FastAPI, caller: ServiceCaller = "agent") -> dict[str, str]:
    credentials = app.state.scadbuddy.components.get(AUTH).credentials
    minted = InProcessProvider(caller, lambda: credentials).token()
    assert minted is not None
    return {"authorization": f"Bearer {minted}"}


def test_the_input_hash_key_is_the_agents_own(pg_pool: PgPool, tmp_path: Path) -> None:
    app = _app(pg_pool, tmp_path, kek=KEK_B64)
    with TestClient(app) as client:
        answer = client.get(f"{BASE}/keys/input-hash", headers=_token(app))
    assert answer.status_code == 200
    assert answer.json() == {"key": AGENT_INPUT_HASH_KEY}
    assert "no-store" in answer.headers["cache-control"]


def test_a_minted_grant_passes_the_asset_routes_check(pg_pool: PgPool, tmp_path: Path) -> None:
    app = _app(pg_pool, tmp_path, kek=KEK_B64)
    url = "https://files.example.com/a.stl"
    with TestClient(app) as client:
        answer = client.post(
            f"{BASE}/fetch-grants",
            json={"host": "files.example.com", "url": url},
            headers=_token(app),
        )
    assert answer.status_code == 200
    grant = answer.json()["grant"]
    key = fetch_grant_key(kek_from_base64(KEK_B64))
    now = datetime.now(UTC)
    assert verify(key, grant, host="files.example.com", url=url, now=now)
    with pytest.raises(FetchGrantError):
        verify(key, grant, host="other.example.com", url=url, now=now)


@pytest.mark.parametrize(
    "body",
    [
        {"host": "", "url": "https://a.example/x"},
        {"host": "a.example", "url": "https://a.example/" + "x" * crud_agent_keys.URL_MAX},
        {"host": "a.example", "url": "https://a.example/x", "extra": 1},
        {"host": "b.example", "url": "https://a.example/x"},
        {"host": "a.example", "url": "http://a.example/x"},
    ],
    ids=["empty-host", "long-url", "unknown-field", "other-host", "not-https"],
)
def test_a_malformed_ask_is_refused(pg_pool: PgPool, tmp_path: Path, body: dict[str, Any]) -> None:
    app = _app(pg_pool, tmp_path, kek=KEK_B64)
    with TestClient(app) as client:
        answer = client.post(f"{BASE}/fetch-grants", json=body, headers=_token(app))
    assert answer.status_code == 422


def test_another_caller_is_refused(pg_pool: PgPool, tmp_path: Path) -> None:
    app = _app(pg_pool, tmp_path, kek=KEK_B64)
    with TestClient(app) as client:
        key = client.get(f"{BASE}/keys/input-hash", headers=_token(app, "render"))
        grant = client.post(
            f"{BASE}/fetch-grants",
            json={"host": "a.example", "url": "https://a.example/x"},
            headers=_token(app, "render"),
        )
        bare = client.get(f"{BASE}/keys/input-hash")
    assert key.status_code == 403
    assert grant.status_code == 403
    assert bare.status_code == 401


def test_no_kek_is_kek_missing(pg_pool: PgPool, tmp_path: Path) -> None:
    app = _app(pg_pool, tmp_path, kek=None)
    with TestClient(app) as client:
        key = client.get(f"{BASE}/keys/input-hash", headers=_token(app))
        grant = client.post(
            f"{BASE}/fetch-grants",
            json={"host": "a.example", "url": "https://a.example/x"},
            headers=_token(app),
        )
    assert (key.status_code, key.json()["reason"]) == (503, "kek-missing")
    assert (grant.status_code, grant.json()["reason"]) == (503, "kek-missing")


def test_the_area_is_listed(pg_pool: PgPool, tmp_path: Path) -> None:
    app = _app(pg_pool, tmp_path, kek=KEK_B64)
    with TestClient(app) as client:
        areas = client.get(f"{BASE}/areas", headers=_token(app)).json()["areas"]
    assert "keys" in areas
