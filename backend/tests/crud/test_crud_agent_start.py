"""What the agent read at its start and per request over its own SQL, through CRUD routes
(#6501, `api/crud_agent_start.py`): only the ``agent`` credential reaches them, the ledger
answers the rows of the files asked for, and a session lookup answers a live session by its
cookie's hash only, as the view ``auth_sessions_agent`` does."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from scadbuddy.api import crud_agent_start, crud_areas
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
from scadbuddy.render.agent_migrations import migrate_agent
from tests.conftest import PgPool
from tests.crud.crud_support import _NoSessions, _Noted

pytestmark = pytest.mark.requires_postgres

BASE = "/api/v1/internal/crud/agent"


def _app(pool: PgPool) -> FastAPI:
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
        settings=SimpleNamespace(auth_mode="enforce"),
        components=SimpleNamespace(get=lambda key: parts[key]),
        settings_store=SimpleNamespace(pool=pool),
    )
    app.include_router(crud_agent_start.router, prefix="/api/v1")
    app.include_router(crud_areas.router, prefix="/api/v1")
    install_problem_handlers(app)
    app.add_middleware(AuthGate, sessions=lambda: _NoSessions(), credentials=lambda: credentials)
    return app


def _token(app: FastAPI, caller: ServiceCaller = "agent") -> dict[str, str]:
    credentials = app.state.scadbuddy.components.get(AUTH).credentials
    minted = InProcessProvider(caller, lambda: credentials).token()
    assert minted is not None
    return {"authorization": f"Bearer {minted}"}


@pytest.fixture
def client(pg_pool: PgPool) -> Iterator[tuple[TestClient, dict[str, str]]]:
    app = _app(pg_pool)
    with TestClient(app) as c:
        yield c, _token(app)


def _signed_in(pool: PgPool, cookie: str, **cols: str) -> str:
    id_hash = hashlib.sha256(cookie.encode()).digest()
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO auth_sessions (id_hash, principal, issuer, subject, name, email, kind,"
            " idle_expires_at, absolute_expires_at, revoked_at) VALUES (%s, %s, 'iss', 'sub',"
            " 'Ada', 'ada@example.com', 'login', now() + %s::interval,"
            " now() + interval '1 day', %s)",
            (
                id_hash,
                cols.get("principal", "user:ada"),
                cols.get("idle", "1 hour"),
                cols.get("revoked"),
            ),
        )
    return id_hash.hex()


def test_only_the_agent_credential_reaches_the_routes(pg_pool: PgPool) -> None:
    app = _app(pg_pool)
    with TestClient(app) as c:
        routes = [
            ("/migrations/ledger", {"ids": []}),
            ("/auth-sessions/lookup", {"id_hash": "0" * 64}),
        ]
        for path, body in routes:
            assert c.post(f"{BASE}{path}", json=body).status_code == 401
            for caller in ("render", "print", "projects"):
                wrong = c.post(f"{BASE}{path}", json=body, headers=_token(app, caller))
                assert wrong.status_code == 403
        listed = c.get(f"{BASE}/areas", headers=_token(app)).json()["areas"]
        assert {"migrations", "auth-sessions"} <= set(listed)


def test_the_ledger_before_and_after_the_api_migrates(
    client: tuple[TestClient, dict[str, str]], pg_pool: PgPool
) -> None:
    c, headers = client
    first = "20261003T0000Z_initial"

    def ledger(ids: list[str]) -> Any:
        got = c.post(f"{BASE}/migrations/ledger", json={"ids": ids}, headers=headers)
        assert got.status_code == 200, got.text
        return got.json()

    with pg_pool.connection() as conn:
        conn.execute("DROP TABLE IF EXISTS ai_migrations")
    assert ledger([first]) == {"found": False, "keyed": False, "rows": []}
    with pg_pool.connection() as conn:
        migrate_agent(conn)
        row = conn.execute("SELECT id, checksum FROM ai_migrations ORDER BY id LIMIT 1").fetchone()
    assert row is not None
    answer = ledger([row["id"], "29991231T2359Z_not_applied"])
    assert answer == {"found": True, "keyed": True, "rows": [row]}


@pytest.mark.parametrize(
    "ids",
    [["../areas"], ["x"], ["20261003T0000Z_Upper"], ["20261003T0000Z_" + "a" * 101]],
)
def test_a_malformed_migration_id_is_refused(
    client: tuple[TestClient, dict[str, str]], ids: list[str]
) -> None:
    c, headers = client
    got = c.post(f"{BASE}/migrations/ledger", json={"ids": ids}, headers=headers)
    assert got.status_code == 422


def test_a_ledger_ask_is_bounded(client: tuple[TestClient, dict[str, str]]) -> None:
    c, headers = client
    many = {"ids": ["20261003T0000Z_x"] * 5001}
    assert c.post(f"{BASE}/migrations/ledger", json=many, headers=headers).status_code == 422
    # Past the byte cap, both routes refuse before they parse the body.
    big = b'{"ids": [], "pad": "' + b"x" * (40 * 1024 * 1024) + b'"}'
    for path in ("migrations/ledger", "auth-sessions/lookup"):
        refused = c.post(
            f"{BASE}/{path}", content=big, headers={**headers, "content-type": "application/json"}
        )
        assert refused.status_code == 413


def test_a_live_session_is_found_by_its_hash_only(
    client: tuple[TestClient, dict[str, str]], pg_pool: PgPool
) -> None:
    c, headers = client
    live = _signed_in(pg_pool, "cookie-live")
    idle = _signed_in(pg_pool, "cookie-idle", idle="-1 second")
    revoked = _signed_in(pg_pool, "cookie-revoked", revoked="2026-01-01")

    def lookup(id_hash: str) -> Any:
        got = c.post(f"{BASE}/auth-sessions/lookup", json={"id_hash": id_hash}, headers=headers)
        assert got.status_code == 200, got.text
        return got.json()["session"]

    assert lookup(live) == {"principal": "user:ada", "name": "Ada", "kind": "login"}
    for gone in (idle, revoked, "0" * 64):
        assert lookup(gone) is None


@pytest.mark.parametrize("id_hash", ["", "0" * 63, "0" * 65, "G" * 64, "A" * 64, "../areas"])
def test_a_malformed_hash_is_refused(
    client: tuple[TestClient, dict[str, str]], id_hash: str
) -> None:
    c, headers = client
    got = c.post(f"{BASE}/auth-sessions/lookup", json={"id_hash": id_hash}, headers=headers)
    assert got.status_code == 422
