"""``SCADBUDDY_AUTH_MODE=enforce`` (#4975 phase 8, spec 2026-11-01 §8.2, §9.1, §5.4): the
start check, and the gate refusing what ``observe`` counts."""

from __future__ import annotations

import base64
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from scadbuddy.api import realtime
from scadbuddy.api.deps import STATE_ATTR
from scadbuddy.auth.check import FORBIDDEN, SIGNED_OUT
from scadbuddy.auth.component import AUTH
from scadbuddy.auth.gate import SESSION_COOKIE
from scadbuddy.auth.oidc import OidcClient
from scadbuddy.auth.service_credentials import ServiceCaller, TokenReader
from scadbuddy.auth.start_check import LoginNotUsableError
from scadbuddy.core.settings import AuthMode, Settings
from scadbuddy.main import create_app
from tests.api.test_customers import ADA
from tests.auth.fake_oidc import CLIENT_ID, CLIENT_SECRET, ISSUER, FakeIssuer

PUBLIC = "https://scadbuddy.test"
HEADER = {"X-ScadBuddy-Request": "1"}
ENABLED: dict[str, Any] = {
    "enabled": True,
    "client_secret": CLIENT_SECRET,
    "allowed_subjects": ["alice"],
}


@pytest.fixture
def settings(settings: Settings, tmp_path: Path) -> Settings:
    key = tmp_path / "kek"
    key.write_text(base64.b64encode(os.urandom(32)).decode())
    return settings.model_copy(
        update={
            "secret_key_file": key,
            "public_url": PUBLIC,
            "auth_oidc_issuer": ISSUER,
            "auth_oidc_client_id": CLIENT_ID,
        }
    )


@pytest.fixture
def issuer() -> FakeIssuer:
    return FakeIssuer()


def _app(
    settings: Settings, mode: AuthMode, transport: httpx.AsyncBaseTransport, **update: Any
) -> FastAPI:
    app = create_app(settings.model_copy(update={"auth_mode": mode, **update}))
    # Before the start, which runs the issuer's discovery in `enforce`.
    getattr(app.state, STATE_ATTR).components.get(AUTH).oidc = OidcClient(transport=transport)
    return app


def _configure(settings: Settings, issuer: FakeIssuer, **changes: Any) -> None:
    """Sign-in set up as the README says: in `off`, through Settings."""
    with TestClient(_app(settings, "off", issuer.transport()), base_url=PUBLIC) as client:
        response = client.put("/api/v1/auth/config", json={**ENABLED, **changes})
        assert response.status_code == 200, response.text


def _start(app: FastAPI) -> None:
    with TestClient(app, base_url=PUBLIC):
        pass


@pytest.fixture
def enforced(settings: Settings, issuer: FakeIssuer) -> Iterator[TestClient]:
    _configure(settings, issuer)
    with TestClient(_app(settings, "enforce", issuer.transport()), base_url=PUBLIC) as client:
        yield client


def _sign_in(client: TestClient) -> None:
    app = client.app
    assert isinstance(app, FastAPI)
    auth = getattr(app.state, STATE_ATTR).components.get(AUTH)
    token, _ = auth.sessions.create(issuer=ISSUER, subject="alice", kind="login")
    client.cookies.set(SESSION_COOKIE, token)


# The start check (§9.1).


def test_enforce_refuses_to_start_without_a_login(settings: Settings, issuer: FakeIssuer) -> None:
    with pytest.raises(LoginNotUsableError) as refused:
        _start(_app(settings, "enforce", issuer.transport()))
    message = str(refused.value)
    assert "sign-in is not switched on" in message
    assert "no client secret" in message
    assert "no sign-in rule" in message


def test_enforce_names_the_missing_deployment_variables(
    settings: Settings, issuer: FakeIssuer
) -> None:
    _configure(settings, issuer)
    with pytest.raises(LoginNotUsableError) as refused:
        _start(
            _app(
                settings,
                "enforce",
                issuer.transport(),
                auth_oidc_issuer="",
                auth_oidc_client_id="",
            )
        )
    message = str(refused.value)
    assert "SCADBUDDY_AUTH_OIDC_ISSUER is not set" in message
    assert "SCADBUDDY_AUTH_OIDC_CLIENT_ID is not set" in message
    # The secret was sealed for the issuer and client id it was saved with.
    assert "no client secret" in message


def test_enforce_refuses_to_start_when_the_issuer_does_not_answer(
    settings: Settings, issuer: FakeIssuer
) -> None:
    _configure(settings, issuer)
    down = httpx.MockTransport(lambda request: httpx.Response(503))
    with pytest.raises(LoginNotUsableError, match=r"the issuer cannot be used.*HTTP 503"):
        _start(_app(settings, "enforce", down))


def test_enforce_starts_with_a_usable_login(enforced: TestClient) -> None:
    assert enforced.get("/healthz").status_code == 200
    session = enforced.get("/api/v1/auth/session").json()
    assert session["mode"] == "enforce" and session["login_enabled"] is True


@pytest.mark.parametrize("mode", ["off", "observe"])
def test_the_other_modes_start_without_a_login(
    settings: Settings, issuer: FakeIssuer, mode: AuthMode
) -> None:
    _start(_app(settings, mode, issuer.transport()))


# The gate's answers (§8.2).


def test_a_signed_out_request_is_refused(enforced: TestClient) -> None:
    response = enforced.get("/api/v1/models")
    assert response.status_code == 401
    assert response.json()["type"] == SIGNED_OUT
    # A page's request: no bearer asked for.
    assert "www-authenticate" not in response.headers
    # The probes and the sign-in routes stay public.
    assert enforced.get("/healthz").status_code == 200
    assert enforced.get("/api/v1/auth/session").status_code == 200
    assert enforced.get("/").status_code != 401


def test_a_service_route_asks_for_a_bearer(enforced: TestClient) -> None:
    response = enforced.get("/api/v1/internal/outputs/nope/model.3mf")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == 'Bearer realm="scadbuddy"'
    unknown = enforced.get("/api/v1/models", headers={"Authorization": "Bearer sbpat_nope"})
    assert unknown.status_code == 401
    assert unknown.headers["www-authenticate"] == 'Bearer realm="scadbuddy"'


def test_a_signed_in_request_is_served_with_the_header(enforced: TestClient) -> None:
    _sign_in(enforced)
    assert enforced.get("/api/v1/models", headers=HEADER).status_code == 200
    refused = enforced.get("/api/v1/models")
    assert refused.status_code == 403
    assert refused.json()["type"] == FORBIDDEN
    # A scriptless GET (an <img>) passes on the cookie alone: 404, not refused.
    assert enforced.get("/api/v1/models/nope/thumbnail").status_code == 404


def test_a_person_route_takes_no_access_token(enforced: TestClient) -> None:
    _sign_in(enforced)
    minted = enforced.post(
        "/api/v1/auth/access-tokens",
        json={"name": "fs", "scope": "write", "expires_in_days": 1},
        headers=HEADER,
    )
    assert minted.status_code in (200, 201), minted.text
    bearer = {"Authorization": f"Bearer {minted.json()['token']}"}
    enforced.cookies.clear()
    assert enforced.get("/api/v1/models", headers=bearer).status_code == 200
    refused = enforced.get("/api/v1/auth/sessions", headers=bearer)
    assert refused.status_code == 403
    assert refused.json()["type"] == FORBIDDEN


# Sockets (§5.4).


def test_a_signed_out_socket_is_closed_before_it_is_accepted(enforced: TestClient) -> None:
    with (
        pytest.raises(WebSocketDisconnect) as closed,
        enforced.websocket_connect("/api/v1/ws", headers={"origin": PUBLIC}),
    ):
        pass
    assert closed.value.code == 1008


def test_a_revoked_session_closes_its_socket_with_4401(
    enforced: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A ping a second: a socket still open after a few of them was not closed.
    monkeypatch.setattr(realtime, "PING_SECONDS", 1.0)
    _sign_in(enforced)
    with enforced.websocket_connect("/api/v1/ws", headers={"origin": PUBLIC}) as socket:
        socket.send_json({"type": "subscribe", "topics": ["models"]})
        assert socket.receive_json()["type"] == "subscribed"
        assert enforced.post("/api/v1/auth/logout", headers=HEADER).status_code == 200
        with pytest.raises(WebSocketDisconnect) as closed:
            for _ in range(5):
                socket.receive_json()
    assert closed.value.code == 4401


# Author headers (§7.3): in `enforce` a refused author is refused, never recorded.


def _bearer(client: TestClient, settings: Settings, caller: ServiceCaller) -> dict[str, str]:
    app = client.app
    assert isinstance(app, FastAPI)
    kek = getattr(app.state, STATE_ATTR).settings_store.kek
    token = TokenReader(caller, settings.database_url, kek).token()
    assert token is not None
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize(("caller", "refused"), [("projects", True), ("agent", False)])
def test_a_refused_author_is_refused_and_records_nothing(
    enforced: TestClient, settings: Settings, caller: ServiceCaller, refused: bool
) -> None:
    """The projects worker may name only its run's starter (a run token proves which);
    the agent's credential is believed, the control that keeps this from being vacuous."""
    headers = {"X-ScadBuddy-Agent-Author": "token:forged", **_bearer(enforced, settings, caller)}
    response = enforced.post("/api/v1/customers", json=ADA, headers=headers)
    if refused:
        assert response.status_code == 403, response.text
    else:
        assert response.status_code == 201, response.text
    _sign_in(enforced)
    listed = enforced.get("/api/v1/customers", headers=HEADER)
    assert listed.status_code == 200
    names = [customer["display_name"] for customer in listed.json()]
    assert names == ([] if refused else [ADA["display_name"]])
