# ruff: noqa: F811
"""A storefront session never reaches ``/api/v1``, and a staff session means nothing to the
storefront (#5283, spec 2026-11-02 §5.2). The two kinds of session are different tokens in
different tables, and each side reads only its own."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR
from scadbuddy.auth.check import SIGNED_OUT
from scadbuddy.auth.component import AUTH
from scadbuddy.core.settings import Settings
from scadbuddy.storefront.accounts import SESSION_COOKIE
from scadbuddy.storefront.wire import CLIENT_HEADER, SESSION_HEADER, VISITOR_HEADER
from tests.api.test_auth_enforce import (  # noqa: F401  (fixtures)
    HEADER,
    _bearer,
    enforced,
    issuer,
    settings,
)
from tests.api.test_crud_storefront_accounts import BASE, VISITOR, code_for
from tests.auth.fake_oidc import ISSUER

CLIENT = "203.0.113.9"


def _storefront_headers(
    client: TestClient, settings: Settings, session: str | None = None
) -> dict[str, str]:
    sent = {
        **_bearer(client, settings, "storefront"),
        CLIENT_HEADER: CLIENT,
        VISITOR_HEADER: VISITOR,
    }
    if session is not None:
        sent[SESSION_HEADER] = session
    return sent


def _storefront_session(client: TestClient, settings: Settings) -> str:
    code = code_for(settings, "ada@example.com")
    answer = client.post(
        f"{BASE}/verify",
        json={"email": "ada@example.com", "code": code},
        headers=_storefront_headers(client, settings),
    )
    assert answer.status_code == 200, answer.text
    return str(answer.headers[SESSION_HEADER])


def _staff_session(client: TestClient) -> str:
    """A back-office session token, made as the login callback makes it
    (`api/auth.py`, ``sessions.create`` with ``kind="login"``): the session cookie a
    browser holds carries this same token."""
    app = client.app
    assert isinstance(app, FastAPI)
    auth = getattr(app.state, STATE_ATTR).components.get(AUTH)
    token, _ = auth.sessions.create(issuer=ISSUER, subject="alice", kind="login")
    return str(token)


def test_a_storefront_session_is_refused_by_the_back_office(
    enforced: TestClient, settings: Settings
) -> None:
    session = _storefront_session(enforced, settings)
    # The token as the storefront edge sends it, and as a browser could hold it.
    for sent in (
        {"headers": {**HEADER, "Authorization": f"Bearer {session}"}},
        {"headers": {**HEADER, SESSION_HEADER: session}},
        {"headers": HEADER, "cookies": {SESSION_COOKIE: session}},
    ):
        enforced.cookies.clear()
        refused = enforced.get("/api/v1/orders", **sent)
        assert refused.status_code == 401, sent
        assert refused.json()["type"] == SIGNED_OUT
    # The session itself still stands: only the back office refused it.
    me = enforced.get(BASE, headers=_storefront_headers(enforced, settings, session))
    assert me.json() == {"email": "ada@example.com"}


def test_a_staff_session_is_no_storefront_account(enforced: TestClient, settings: Settings) -> None:
    staff = _staff_session(enforced)
    sent = _storefront_headers(enforced, settings, staff)
    assert enforced.get(BASE, headers=sent).json() is None
    orders = enforced.get(f"{BASE}/orders", headers=sent)
    assert orders.status_code == 401
    assert orders.json()["type"].endswith("/sign_in_required")
