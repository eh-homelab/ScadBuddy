"""``SCADBUDDY_AUTH_MODE=observe`` (#4975 phase 3, spec 2026-11-01 §9.1): what ``enforce``
would refuse is served and counted; and break-glass (§9.3)."""

from __future__ import annotations

import base64
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR
from scadbuddy.auth import break_glass
from scadbuddy.auth.component import AUTH
from scadbuddy.auth.gate import SESSION_COOKIE
from scadbuddy.core.settings import Settings

PUBLIC = "https://scadbuddy.test"
HEADER = {"X-ScadBuddy-Request": "1"}


@pytest.fixture
def mode() -> str:
    return "observe"


@pytest.fixture
def settings(settings: Settings, tmp_path: Path, mode: str) -> Settings:
    key = tmp_path / "kek"
    key.write_text(base64.b64encode(os.urandom(32)).decode())
    return settings.model_copy(
        update={"secret_key_file": key, "public_url": PUBLIC, "auth_mode": mode}
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


def _sign_in(client: TestClient, app: FastAPI) -> None:
    auth = getattr(app.state, STATE_ATTR).components.get(AUTH)
    token, _ = auth.sessions.create(issuer="https://idp.test", subject="alice", kind="login")
    client.cookies.set(SESSION_COOKIE, token)


def test_signed_out_is_served_and_counted(client: TestClient, app: FastAPI) -> None:
    assert client.get("/api/v1/models").status_code == 200
    assert _count(app, "api", "signed_out") == 1
    # The probes and the sign-in routes are public.
    assert client.get("/healthz").status_code == 200
    assert client.get("/api/v1/auth/session").status_code == 200
    assert _count(app, "public", "signed_out") == 0


def test_a_signed_in_request_needs_the_header(client: TestClient, app: FastAPI) -> None:
    _sign_in(client, app)
    client.get("/api/v1/models", headers=HEADER)
    assert _count(app, "api", "no_request_header") == 0
    client.get("/api/v1/models")
    assert _count(app, "api", "no_request_header") == 1
    assert _count(app, "api", "signed_out") == 0


def test_a_scriptless_get_needs_no_header(client: TestClient, app: FastAPI) -> None:
    """Review of #5209: against the matched (mounted) route, not the module's router."""
    _sign_in(client, app)
    client.get("/api/v1/models/nope/thumbnail")
    client.get("/api/v1/outputs/nope/model.3mf")
    assert _count(app, "api", "no_request_header") == 0
    # The same request to a GET that is not listed is counted, so the above is not vacuous.
    client.get("/api/v1/models/nope")
    assert _count(app, "api", "no_request_header") == 1


def test_scriptless_takes_the_prefixed_path_too() -> None:
    from fastapi.routing import APIRoute

    from scadbuddy.api.models import router
    from scadbuddy.auth.routes import scriptless

    thumbnail = next(
        r
        for r in router.routes
        if isinstance(r, APIRoute)
        and r.path == "/models/{slug}/thumbnail"
        and "GET" in (r.methods or ())
    )
    assert isinstance(thumbnail, APIRoute)
    prefixed = APIRoute("/api/v1" + thumbnail.path, thumbnail.endpoint, methods=["GET"])
    assert scriptless(thumbnail) and scriptless(prefixed)


def test_the_internal_routes_need_a_credential(client: TestClient, app: FastAPI) -> None:
    client.get("/api/v1/internal/outputs/nope/model.3mf")
    assert _count(app, "internal.outputs", "no_credential") == 1


def test_a_person_route_signed_out(client: TestClient, app: FastAPI) -> None:
    client.get("/api/v1/auth/config")
    assert _count(app, "person", "signed_out") == 1


def test_the_realtime_socket_signed_out(client: TestClient, app: FastAPI) -> None:
    with client.websocket_connect("/api/v1/ws", headers={"origin": PUBLIC}) as socket:
        socket.close()
    assert _count(app, "api", "signed_out") == 1


@pytest.mark.parametrize("mode", ["off"])
def test_off_counts_nothing(client: TestClient, app: FastAPI) -> None:
    client.get("/api/v1/models")
    assert _count(app, "api", "signed_out") == 0


def test_break_glass(client: TestClient, app: FastAPI, settings: Settings) -> None:
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        code = break_glass.mint(conn)
        expired = break_glass.mint(conn, now=datetime.now(UTC) - timedelta(minutes=11))
    assert client.get("/api/v1/auth/break-glass", params={"code": expired}).status_code == 400
    page = client.get("/api/v1/auth/break-glass", params={"code": code})
    assert page.status_code == 200
    session = client.get("/api/v1/auth/session").json()
    assert session["signed_in"] is True and session["kind"] == "break_glass"
    expires = datetime.fromisoformat(session["expires_at"])
    assert expires <= datetime.now(UTC) + timedelta(hours=1)
    # Single use.
    client.cookies.clear()
    assert client.get("/api/v1/auth/break-glass", params={"code": code}).status_code == 400


def test_the_break_glass_url() -> None:
    assert (
        break_glass.url_for("https://sb.test/app", "c")
        == "https://sb.test/api/v1/auth/break-glass?code=c"
    )
    assert break_glass.url_for(None, "c") == "/api/v1/auth/break-glass?code=c"
