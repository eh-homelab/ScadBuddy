"""``AuthGate`` in ``SCADBUDDY_AUTH_MODE=off`` (#4975 plan phase 1): it resolves the caller
and refuses nothing."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import psycopg
import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient

from scadbuddy.api.cross_site import CrossSiteGate
from scadbuddy.auth.component import CallerDep
from scadbuddy.auth.gate import SESSION_COOKIE, AuthGate
from scadbuddy.auth.models import AuthSession, Caller, Person
from scadbuddy.auth.tokens import new_token
from scadbuddy.core.settings import Settings
from scadbuddy.main import create_app
from tests.conftest import UNUSED_DATABASE_URL, UNUSED_TEMPORAL_ADDRESS

LIVE = new_token()
AT = datetime(2026, 11, 1, tzinfo=UTC)
SESSION = AuthSession(
    id_hash=b"\0" * 32,
    principal="user:https://idp.example#alice",
    issuer="https://idp.example",
    subject="alice",
    name="Alice",
    email=None,
    kind="login",
    created_at=AT,
    last_seen_at=AT,
    idle_expires_at=AT,
    absolute_expires_at=AT,
)


class FakeSessions:
    def __init__(self, error: Exception | None = None) -> None:
        self.looked_up: list[str] = []
        self.error = error

    def resolve(self, token: str) -> AuthSession | None:
        self.looked_up.append(token)
        if self.error is not None:
            raise self.error
        return SESSION if token == LIVE else None


def _describe(caller: Caller) -> str:
    return caller.principal if isinstance(caller, Person) else type(caller).__name__


def _client(sessions: FakeSessions) -> TestClient:
    app = FastAPI()

    @app.post("/api/v1/whoami")
    def whoami(caller: CallerDep) -> str:
        return _describe(caller)

    @app.websocket("/api/v1/ws")
    async def socket(websocket: WebSocket, caller: CallerDep) -> None:
        await websocket.accept()
        await websocket.send_text(_describe(caller))
        await websocket.close()

    app.add_middleware(AuthGate, sessions=lambda: sessions)
    return TestClient(app)


def test_no_cookie_is_anonymous_without_a_lookup() -> None:
    sessions = FakeSessions()
    response = _client(sessions).post("/api/v1/whoami", headers={"cookie": "other=1"})
    assert response.status_code == 200
    assert response.json() == "Anonymous"
    assert sessions.looked_up == []


def test_a_live_session_is_the_person() -> None:
    client = _client(FakeSessions())
    client.cookies.set(SESSION_COOKIE, LIVE)
    assert client.post("/api/v1/whoami").json() == SESSION.principal


# Literal, not new_token(): xdist workers must collect the same ids.
@pytest.mark.parametrize("token", ["A" * 43, "not-a-token"], ids=["well-formed", "malformed"])
def test_an_unknown_session_is_served_as_anonymous(token: str) -> None:
    client = _client(FakeSessions())
    client.cookies.set(SESSION_COOKIE, token)
    response = client.post("/api/v1/whoami")
    assert response.status_code == 200
    assert response.json() == "Anonymous"


def test_a_failed_lookup_is_served_as_anonymous() -> None:
    client = _client(FakeSessions(error=psycopg.OperationalError("down")))
    client.cookies.set(SESSION_COOKIE, LIVE)
    response = client.post("/api/v1/whoami")
    assert response.status_code == 200
    assert response.json() == "Anonymous"


def test_a_socket_gets_its_caller() -> None:
    client = _client(FakeSessions())
    client.cookies.set(SESSION_COOKIE, LIVE)
    with client.websocket_connect("/api/v1/ws") as socket:
        assert socket.receive_text() == SESSION.principal


def test_the_app_installs_it_inside_the_cross_site_gate() -> None:
    app = create_app(
        Settings(
            frontend_dir=Path("/nonexistent"),
            database_url=UNUSED_DATABASE_URL,
            temporal_address=UNUSED_TEMPORAL_ADDRESS,
        )
    )
    # Outermost first: the cross-site gate refuses a foreign write before any lookup.
    order = [cast(object, m.cls) for m in app.user_middleware]
    assert order.index(CrossSiteGate) < order.index(AuthGate)
