"""The OIDC login (#4975 phase 2, spec 2026-11-01 §5.1, §5.2, §9.2) against the in-process
fake issuer (``tests/auth/fake_oidc.py``), in ``SCADBUDDY_AUTH_MODE=off``."""

from __future__ import annotations

import base64
import os
import re
from collections.abc import Iterator
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR
from scadbuddy.auth.component import AUTH
from scadbuddy.auth.gate import SESSION_COOKIE
from scadbuddy.auth.login_state import LOGIN_COOKIE
from scadbuddy.auth.oidc import OidcClient
from scadbuddy.core.settings import Settings
from scadbuddy.main import create_app
from tests.auth.fake_oidc import CLIENT_ID, CLIENT_SECRET, ISSUER, FakeIssuer

PUBLIC = "https://scadbuddy.test"
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


@pytest.fixture
def client(app: FastAPI, issuer: FakeIssuer) -> Iterator[TestClient]:
    # https, so the client's jar sends the `Secure` cookies back.
    with TestClient(app, base_url=PUBLIC) as test_client:
        auth = getattr(app.state, STATE_ATTR).components.get(AUTH)
        auth.oidc = OidcClient(transport=issuer.transport())
        yield test_client


def _enable(client: TestClient, **changes: Any) -> dict[str, Any]:
    response = client.put("/api/v1/auth/config", json={**ENABLED, **changes})
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _set_cookies(response: Any) -> dict[str, SimpleCookie]:
    cookies: dict[str, SimpleCookie] = {}
    for header in response.headers.get_list("set-cookie"):
        jar: SimpleCookie = SimpleCookie()
        jar.load(header)
        for name in jar:
            cookies[name] = jar
    return cookies


def _sign_in(
    client: TestClient, issuer: FakeIssuer, return_to: str = "/models", popup: bool = False
) -> Any:
    login = client.get(
        "/api/v1/auth/login",
        params={"return_to": return_to, "popup": popup},
        follow_redirects=False,
    )
    assert login.status_code == 302, login.text
    location = login.headers["location"]
    assert location.startswith(f"{ISSUER}/authorize?")
    return client.get("/api/v1/auth/callback", params=issuer.authorize(location))


def test_signed_out_by_default(client: TestClient) -> None:
    session = client.get("/api/v1/auth/session").json()
    assert session == {
        "signed_in": False,
        "user": None,
        "mode": "off",
        "login_enabled": False,
        "expires_at": None,
        "kind": None,
    }


def test_the_config_says_what_to_register(client: TestClient) -> None:
    view = client.get("/api/v1/auth/config").json()
    assert view["redirect_uri"] == f"{PUBLIC}/api/v1/auth/callback"
    assert view["has_client_secret"] is False
    assert view["cannot_enable_reason"] is None
    assert view["config"]["enabled"] is False


def test_switching_on_needs_a_rule(client: TestClient) -> None:
    response = client.put("/api/v1/auth/config", json={**ENABLED, "allowed_subjects": []})
    assert response.status_code == 422
    assert "sign-in rule" in response.json()["detail"]
    assert client.get("/api/v1/auth/config").json()["config"]["enabled"] is False


def test_a_failed_discovery_saves_nothing(client: TestClient, issuer: FakeIssuer) -> None:
    issuer.claimed_issuer = "https://someone-else.test"
    response = client.put("/api/v1/auth/config", json=ENABLED)
    assert response.status_code == 422
    assert response.json()["type"].endswith("/auth/discovery-failed")
    assert client.get("/api/v1/auth/config").json()["has_client_secret"] is False


def test_the_secret_is_kept_and_never_answered(client: TestClient) -> None:
    view = _enable(client)
    assert view["has_client_secret"] is True
    assert CLIENT_SECRET not in client.get("/api/v1/auth/config").text
    # Saved again without it: kept, while the issuer and client id stay.
    kept = _enable(client, client_secret=None, rp_logout=True)
    assert kept["has_client_secret"] is True and kept["config"]["rp_logout"] is True


def test_the_issuer_and_client_id_are_the_deployments(client: TestClient) -> None:
    """Security review of #5208: in mode off anyone may write the config, so it must not
    be able to point `/auth/login` (a redirect) or the discovery fetch anywhere."""
    view = _enable(client, issuer="https://evil.test", client_id="evil")
    assert view["config"]["issuer"] == ISSUER
    assert view["config"]["client_id"] == CLIENT_ID


def test_the_login_redirects_only_to_the_issuers_authorization_endpoint(
    client: TestClient, issuer: FakeIssuer
) -> None:
    _enable(client)
    client.put("/api/v1/auth/config", json={**ENABLED, "issuer": "https://evil.test"})
    login = client.get("/api/v1/auth/login", follow_redirects=False)
    assert login.status_code == 302
    location = urlsplit(login.headers["location"])
    authorize = urlsplit(f"{ISSUER}/authorize")  # what the fake's discovery names
    assert (location.scheme, location.netloc, location.path) == (
        authorize.scheme,
        authorize.netloc,
        authorize.path,
    )


def test_a_new_client_id_needs_the_secret_again(client: TestClient, settings: Settings) -> None:
    _enable(client)
    # A redeploy with another client id: the secret sealed for the old one is gone.
    redeployed = create_app(settings.model_copy(update={"auth_oidc_client_id": "other"}))
    with TestClient(redeployed, base_url=PUBLIC) as again:
        view = again.get("/api/v1/auth/config").json()
    assert view["config"]["client_id"] == "other"
    assert view["has_client_secret"] is False


def test_sign_in(client: TestClient, issuer: FakeIssuer) -> None:
    _enable(client)
    page = _sign_in(client, issuer, return_to="/models?tag=x")
    assert page.status_code == 200, page.text
    cookies = _set_cookies(page)
    session = cookies[SESSION_COOKIE][SESSION_COOKIE]
    assert session["secure"] and session["httponly"]
    assert session["samesite"].lower() == "strict" and session["path"] == "/"
    assert not session["domain"]
    assert cookies[LOGIN_COOKIE][LOGIN_COOKIE]["max-age"] == "0"
    # The page goes on to return_to on the public origin, under a nonce of its own.
    assert f'"next": "{PUBLIC}/models?tag=x"' in page.text
    nonce = re.search(r"script-src 'nonce-([^']+)'", page.headers["content-security-policy"])
    assert nonce is not None and f'nonce="{nonce.group(1)}"' in page.text
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    # The token was redeemed with the PKCE verifier and the registered redirect URI.
    (request,) = issuer.token_requests
    assert request["redirect_uri"] == f"{PUBLIC}/api/v1/auth/callback"
    assert request["code_verifier"]

    signed_in = client.get("/api/v1/auth/session").json()
    assert signed_in["signed_in"] is True
    assert signed_in["user"] == {
        "principal": f"user:{ISSUER}#alice",
        "name": "Alice",
        "email": "a@example.test",
    }
    assert signed_in["login_enabled"] is True


@pytest.mark.parametrize(
    "return_to", ["//evil.test/x", "/\\evil.test", "/%2F%2Fevil.test", "https://evil.test"]
)
def test_an_unsafe_return_to_goes_home(
    client: TestClient, issuer: FakeIssuer, return_to: str
) -> None:
    _enable(client)
    page = _sign_in(client, issuer, return_to=return_to)
    assert f'"next": "{PUBLIC}/"' in page.text
    assert "evil" not in page.text


def test_a_popup_tells_its_opener(client: TestClient, issuer: FakeIssuer) -> None:
    _enable(client)
    page = _sign_in(client, issuer, popup=True)
    assert '"popup": true' in page.text
    assert '"type": "scadbuddy:signed-in"' in page.text
    assert f'"origin": "{PUBLIC}"' in page.text


@pytest.mark.parametrize(
    "misbehaviour",
    ["iss", "aud", "nonce", "expired", "future_iat", "alg_none", "hs256", "unknown_kid", "no_sub"],
)
def test_a_bad_id_token_is_refused(
    client: TestClient, issuer: FakeIssuer, misbehaviour: Any
) -> None:
    _enable(client)
    issuer.misbehave = misbehaviour
    page = _sign_in(client, issuer)
    assert page.status_code == 400, page.text
    assert "Sign-in failed" in page.text
    assert SESSION_COOKIE not in _set_cookies(page)
    assert client.get("/api/v1/auth/session").json()["signed_in"] is False


def test_an_unknown_kid_does_not_refetch_within_30_seconds(
    client: TestClient, issuer: FakeIssuer
) -> None:
    _enable(client)  # fetched the keys just now
    issuer.misbehave = "unknown_kid"
    fetches = issuer.jwks_fetches
    _sign_in(client, issuer)
    _sign_in(client, issuer)
    assert issuer.jwks_fetches == fetches


def test_a_subject_outside_the_rule_is_refused_by_name(
    client: TestClient, issuer: FakeIssuer
) -> None:
    _enable(client)
    issuer.user = {"sub": "mallory"}
    page = _sign_in(client, issuer)
    assert page.status_code == 403
    assert "mallory at https://idp.test may not sign in" in page.text


def test_a_group_rule(client: TestClient, issuer: FakeIssuer) -> None:
    _enable(client, allowed_subjects=[], group_claim="groups", group_value="scadbuddy")
    issuer.user = {"sub": "bob", "groups": ["family", "scadbuddy"]}
    assert _sign_in(client, issuer).status_code == 200
    assert client.get("/api/v1/auth/session").json()["user"]["principal"].endswith("#bob")


def test_the_callback_needs_its_own_login_cookie(client: TestClient, issuer: FakeIssuer) -> None:
    _enable(client)
    login = client.get("/api/v1/auth/login", follow_redirects=False)
    query = issuer.authorize(login.headers["location"])
    client.cookies.delete(LOGIN_COOKIE)
    assert client.get("/api/v1/auth/callback", params=query).status_code == 400
    client.cookies.set(LOGIN_COOKIE, "tampered")
    assert client.get("/api/v1/auth/callback", params=query).status_code == 400


def test_the_callback_checks_state(client: TestClient, issuer: FakeIssuer) -> None:
    _enable(client)
    login = client.get("/api/v1/auth/login", follow_redirects=False)
    query = issuer.authorize(login.headers["location"])
    page = client.get("/api/v1/auth/callback", params={**query, "state": "forged"})
    assert page.status_code == 400
    assert "state does not match" in page.text


def test_login_refuses_while_off(client: TestClient) -> None:
    response = client.get("/api/v1/auth/login", follow_redirects=False)
    assert response.status_code == 409


def test_logout(client: TestClient, issuer: FakeIssuer) -> None:
    _enable(client, rp_logout=True)
    _sign_in(client, issuer)
    response = client.post("/api/v1/auth/logout")
    assert response.status_code == 200
    url = response.json()["end_session_url"]
    assert url.startswith(f"{ISSUER}/logout?") and f"client_id={CLIENT_ID}" in url
    assert client.get("/api/v1/auth/session").json()["signed_in"] is False
    # Signed out already: the same answer, no IdP round trip needed.
    assert client.post("/api/v1/auth/logout").json() == {"end_session_url": None}


def test_logout_stays_local_by_default(client: TestClient, issuer: FakeIssuer) -> None:
    _enable(client)
    _sign_in(client, issuer)
    assert client.post("/api/v1/auth/logout").json() == {"end_session_url": None}


def test_the_persons_sessions(client: TestClient, issuer: FakeIssuer, app: FastAPI) -> None:
    assert client.get("/api/v1/auth/sessions").status_code == 401
    _enable(client)
    _sign_in(client, issuer)
    first = client.cookies.get(SESSION_COOKIE)
    # A second device: another sign-in, its cookie set aside.
    _sign_in(client, issuer)
    sessions = client.get("/api/v1/auth/sessions").json()
    assert len(sessions) == 2
    assert [s["current"] for s in sessions].count(True) == 1
    other = next(s for s in sessions if not s["current"])
    assert first not in str(sessions)
    assert client.delete(f"/api/v1/auth/sessions/{other['id']}").status_code == 204
    assert client.delete(f"/api/v1/auth/sessions/{other['id']}").status_code == 404
    assert len(client.get("/api/v1/auth/sessions").json()) == 1
    assert client.post("/api/v1/auth/sessions/revoke-all").json() == {"revoked": 1}
    assert client.get("/api/v1/auth/session").json()["signed_in"] is False


def test_the_test_button(client: TestClient, settings: Settings) -> None:
    # A body naming another issuer is ignored: only the deployment's is fetched.
    report = client.post("/api/v1/auth/config/test", json={"issuer": "http://10.0.0.1"}).json()
    assert report == {"issuer": ISSUER, "keys": 1, "end_session": True}
    for issuer, says in (("http://idp.test", "https"), ("", "SCADBUDDY_AUTH_OIDC_ISSUER")):
        app = create_app(settings.model_copy(update={"auth_oidc_issuer": issuer}))
        with TestClient(app, base_url=PUBLIC) as other:
            refused = other.post("/api/v1/auth/config/test")
        assert refused.status_code == 422
        assert says in refused.json()["detail"]


def test_the_issuer_is_fetched_from_public_addresses_only(settings: Settings) -> None:
    """Security review of #5208: the discovery, keys and token fetches go through the
    same public-only egress as a URL import, unless the deployment says otherwise."""
    app = create_app(settings.model_copy(update={"auth_oidc_issuer": "https://127.0.0.1:9"}))
    with TestClient(app, base_url=PUBLIC) as client:
        refused = client.post("/api/v1/auth/config/test")
    assert refused.status_code == 422
    assert "not at a public address" in refused.json()["detail"]


def test_without_a_key_the_login_cannot_be_switched_on(settings: Settings) -> None:
    app = create_app(settings.model_copy(update={"secret_key_file": None}))
    with TestClient(app, base_url=PUBLIC) as client:
        view = client.get("/api/v1/auth/config").json()
        assert "SCADBUDDY_SECRET_KEY_FILE" in view["cannot_enable_reason"]
        assert client.put("/api/v1/auth/config", json=ENABLED).status_code == 422
