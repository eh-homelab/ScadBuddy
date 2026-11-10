"""The edge's account routes (#5283, spec 2026-11-02 §5.2): the ``sb_session`` cookie is
the CRUD side's token under the edge's mac, never in a body; verify and sign-out are POST
only and refuse another site's page; a cookie the edge did not sign is never sent on."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.storefront.accounts import SESSION_COOKIE, sign, verify
from scadbuddy.storefront.app import create_app
from scadbuddy.storefront.sign_out import SIGN_OUT_INCOMPLETE, SignOutRetries
from scadbuddy.storefront.turnstile import BOT_CHECK_FAILED
from scadbuddy.storefront.visitors import VISITOR_COOKIE
from scadbuddy.storefront.wire import SESSION_HEADER, TURNSTILE_HEADER
from tests.storefront.test_edge import (  # noqa: F401  (fixtures)
    CLIENT,
    CRUD,
    crud_mock,
    edge_env,
    load,
)

PUBLIC_URL = "https://shop.example.com"
CRUD_ACCOUNT = "/api/v1/internal/crud/storefront/account"
STORE = "/api/store/v1/account"
TOKEN = "t" * 43
#: The edge's two 503 ``sign_out_incomplete`` bodies, which the storefront page's own
#: test (``frontend/src/storefront/pages/AccountPage.test.tsx``) serves and shows (#5283,
#: review of #5955): these tests keep it what the real edge answers.
INCOMPLETE = json.loads(
    (
        Path(__file__).resolve().parents[3] / "test-fixtures/storefront/sign-out-incomplete.json"
    ).read_text()
)
SIGN_IN = {"email": "ada@example.com", "code": "ABCD2345"}


@pytest.fixture
def edge(edge_env: None, crud_mock: respx.Router) -> Iterator[TestClient]:  # noqa: F811
    crud_mock.post(f"{CRUD_ACCOUNT}/verify", name="verify").respond(
        200, json={"email": "ada@example.com"}, headers={**CRUD, SESSION_HEADER: TOKEN}
    )
    crud_mock.post(f"{CRUD_ACCOUNT}/sign-out", name="sign_out").respond(204, headers=CRUD)
    crud_mock.post(f"{CRUD_ACCOUNT}/code", name="code").respond(204, headers=CRUD)
    crud_mock.get(CRUD_ACCOUNT, name="account").respond(
        200, json={"email": "ada@example.com"}, headers=CRUD
    )
    crud_mock.get(f"{CRUD_ACCOUNT}/orders", name="orders").respond(
        200, json={"orders": []}, headers=CRUD
    )
    with TestClient(create_app(load()), base_url="https://testserver", client=CLIENT) as client:
        yield client


def sign_in(edge: TestClient, origin: str = PUBLIC_URL) -> object:
    return edge.post(
        f"{STORE}/verify", json=SIGN_IN, headers={"Origin": origin, TURNSTILE_HEADER: "tok"}
    )


def set_cookies(response: object) -> list[str]:
    return response.headers.get_list("set-cookie")  # type: ignore[attr-defined,no-any-return]


def test_a_sign_in_sets_a_signed_session_cookie_and_no_token_in_the_body(
    edge: TestClient,
    crud_mock: respx.Router,  # noqa: F811
) -> None:
    r = sign_in(edge)
    assert r.status_code == 200, r.text  # type: ignore[attr-defined]
    assert r.json() == {"email": "ada@example.com"}  # type: ignore[attr-defined]
    assert TOKEN not in r.text  # type: ignore[attr-defined]
    (cookie,) = [c for c in set_cookies(r) if c.startswith(f"{SESSION_COOKIE}=")]
    for part in ("HttpOnly", "Secure", "SameSite=lax", "Path=/", "Max-Age=2592000"):
        assert part.lower() in cookie.lower()
    value = cookie.split(";", 1)[0].split("=", 1)[1]
    assert verify(value, edge.app.state.cookie_key) == TOKEN  # type: ignore[attr-defined]
    sent = crud_mock["verify"].calls.last.request
    assert json.loads(sent.content) == SIGN_IN
    assert SESSION_HEADER not in sent.headers
    assert json.loads(crud_mock["turnstile"].calls.last.request.content)["action"] == "sign_in"


def test_the_session_goes_on_to_the_crud_side_and_a_new_sign_in_names_the_old_one(
    edge: TestClient,
    crud_mock: respx.Router,  # noqa: F811
) -> None:
    sign_in(edge)
    assert edge.get(STORE).json() == {"email": "ada@example.com"}
    assert crud_mock["account"].calls.last.request.headers[SESSION_HEADER] == TOKEN
    assert edge.get(f"{STORE}/orders").json() == {"orders": []}
    assert crud_mock["orders"].calls.last.request.headers[SESSION_HEADER] == TOKEN
    sign_in(edge)
    assert crud_mock["verify"].calls.last.request.headers[SESSION_HEADER] == TOKEN


def test_a_failed_bot_check_stops_a_sign_in_before_the_crud_side(
    edge: TestClient,
    crud_mock: respx.Router,  # noqa: F811
) -> None:
    crud_mock["turnstile"].respond(200, json={"verdict": "fail"}, headers=CRUD)
    r = sign_in(edge)
    assert r.status_code == 403  # type: ignore[attr-defined]
    assert r.json()["type"] == BOT_CHECK_FAILED  # type: ignore[attr-defined]
    assert not crud_mock["verify"].called


@pytest.mark.parametrize("path", ["code", "verify", "sign-out"])
def test_verify_and_sign_out_refuse_another_sites_page(
    edge: TestClient,
    crud_mock: respx.Router,  # noqa: F811
    path: str,
) -> None:
    r = edge.post(f"{STORE}/{path}", json=SIGN_IN, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert not crud_mock["verify"].called and not crud_mock["sign_out"].called
    assert not crud_mock["code"].called
    assert not [c for c in set_cookies(r) if c.startswith(f"{SESSION_COOKIE}=")]


@pytest.mark.parametrize("path", ["code", "verify", "sign-out"])
def test_verify_and_sign_out_are_post_only(
    edge: TestClient,
    crud_mock: respx.Router,  # noqa: F811
    path: str,
) -> None:
    r = edge.get(f"{STORE}/{path}")
    # No route answers it: 405, or 404 from the page's mount when a bundle is built.
    assert r.status_code in (404, 405)
    assert not crud_mock["verify"].called and not crud_mock["sign_out"].called
    assert not crud_mock["code"].called


def test_sign_out_ends_the_session_clears_the_cookie_and_starts_a_new_visitor(
    edge: TestClient,
    crud_mock: respx.Router,  # noqa: F811
) -> None:
    sign_in(edge)
    before = edge.cookies.get(VISITOR_COOKIE)
    r = edge.post(f"{STORE}/sign-out", headers={"Origin": PUBLIC_URL})
    assert r.status_code == 204
    assert crud_mock["sign_out"].calls.last.request.headers[SESSION_HEADER] == TOKEN
    cookies = set_cookies(r)
    (cleared,) = [c for c in cookies if c.startswith(f"{SESSION_COOKIE}=")]
    assert "max-age=0" in cleared.lower()
    (visitor,) = [c for c in cookies if c.startswith(f"{VISITOR_COOKIE}=")]
    assert "httponly" in visitor.lower() and "secure" in visitor.lower()
    assert edge.cookies.get(VISITOR_COOKIE) != before
    # Signed out, the edge sends no session on and asks the CRUD side nothing.
    calls = crud_mock["account"].call_count
    assert edge.get(STORE).json() is None
    assert crud_mock["account"].call_count == calls


def refused(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("refused", request=request)


def test_a_sign_out_the_crud_side_did_not_answer_is_incomplete_and_queued(
    edge: TestClient,
    crud_mock: respx.Router,  # noqa: F811
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Review of #5953: the device is signed out (cookie cleared, a new visitor), but the
    answer is 503 ``sign_out_incomplete``, never success, and the revocation is queued."""
    sign_in(edge)
    before = edge.cookies.get(VISITOR_COOKIE)
    retries: SignOutRetries = edge.app.state.sign_out_retries  # type: ignore[attr-defined]
    crud_mock["sign_out"].side_effect = refused
    with caplog.at_level(logging.WARNING, logger="scadbuddy.storefront.routes"):
        r = edge.post(f"{STORE}/sign-out", headers={"Origin": PUBLIC_URL})
    assert r.status_code == 503
    assert r.json()["type"] == SIGN_OUT_INCOMPLETE
    assert "signed out on this device" in r.json()["detail"]
    cookies = set_cookies(r)
    (cleared,) = [c for c in cookies if c.startswith(f"{SESSION_COOKIE}=")]
    assert "max-age=0" in cleared.lower()
    assert len([c for c in cookies if c.startswith(f"{VISITOR_COOKIE}=")]) == 1
    assert edge.cookies.get(VISITOR_COOKIE) != before
    assert retries.pending == 1
    assert any("sign-out" in record.getMessage() for record in caplog.records)
    assert r.json() == INCOMPLETE["queued"]


def test_a_sign_out_the_full_queue_cannot_take_promises_no_retry(
    edge: TestClient,
    crud_mock: respx.Router,  # noqa: F811
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Review of #5953: with the queue full, nothing is queued, the log says so, and the
    answer says the session ends on its own instead of promising retries."""
    app = edge.app
    app.state.sign_out_retries = SignOutRetries(  # type: ignore[attr-defined]
        app.state.crud.account_sign_out,  # type: ignore[attr-defined]
        limit=0,
    )
    sign_in(edge)
    crud_mock["sign_out"].side_effect = refused
    with caplog.at_level(logging.WARNING, logger="scadbuddy.storefront"):
        r = edge.post(f"{STORE}/sign-out", headers={"Origin": PUBLIC_URL})
    assert r.status_code == 503
    assert r.json()["type"] == SIGN_OUT_INCOMPLETE
    assert r.json() == INCOMPLETE["not_queued"]
    # Review of #5957: one warning per full-queue sign-out, the route's.
    warned = [rec for rec in caplog.records if rec.levelno >= logging.WARNING]
    assert [rec.name for rec in warned] == ["scadbuddy.storefront.routes"]
    assert r.json()["detail"].endswith("it ends on its own within 30 days.")
    (cleared,) = [c for c in set_cookies(r) if c.startswith(f"{SESSION_COOKIE}=")]
    assert "max-age=0" in cleared.lower()
    assert app.state.sign_out_retries.pending == 0  # type: ignore[attr-defined]
    messages = [record.getMessage() for record in caplog.records]
    assert any("not queued" in m for m in messages)
    assert not any("; queued" in m for m in messages)


def test_a_queued_sign_out_ends_the_session_once_the_crud_side_answers(
    edge: TestClient,
    crud_mock: respx.Router,  # noqa: F811
) -> None:
    app = edge.app
    app.state.sign_out_retries = SignOutRetries(  # type: ignore[attr-defined]
        app.state.crud.account_sign_out,  # type: ignore[attr-defined]
        delays=(0.05, 0.05, 0.05, 0.05, 0.05),
    )
    sign_in(edge)
    down = True
    answered = 0

    def until_back(request: httpx.Request) -> httpx.Response:
        nonlocal answered
        if down:
            raise httpx.ConnectError("refused", request=request)
        answered += 1
        return httpx.Response(204, headers=CRUD)

    crud_mock["sign_out"].side_effect = until_back
    r = edge.post(f"{STORE}/sign-out", headers={"Origin": PUBLIC_URL})
    assert r.status_code == 503
    down = False  # the CRUD side is back
    for _ in range(500):
        if answered and app.state.sign_out_retries.pending == 0:  # type: ignore[attr-defined]
            break
        time.sleep(0.01)
    assert answered == 1
    assert app.state.sign_out_retries.pending == 0  # type: ignore[attr-defined]
    assert crud_mock["sign_out"].calls.last.request.headers[SESSION_HEADER] == TOKEN


def test_sign_out_with_no_visitor_cookie_sets_one_visitor(
    edge: TestClient,
    crud_mock: respx.Router,  # noqa: F811
) -> None:
    sign_in(edge)
    # The visitor cookie is gone: the middleware would make a visitor, and so does the route.
    edge.cookies.delete(VISITOR_COOKIE)
    r = edge.post(f"{STORE}/sign-out", headers={"Origin": PUBLIC_URL})
    assert r.status_code == 204
    assert len([c for c in set_cookies(r) if c.startswith(f"{VISITOR_COOKIE}=")]) == 1


@pytest.mark.parametrize(
    "cookie",
    [
        f"s1.{TOKEN}.{'A' * 43}",  # a mac the edge did not make
        f"v1.{TOKEN}.{'A' * 43}",
        "not-a-cookie",
    ],
)
def test_a_cookie_the_edge_did_not_sign_is_never_sent_on(
    edge: TestClient,
    crud_mock: respx.Router,  # noqa: F811
    cookie: str,
) -> None:
    edge.cookies.set(SESSION_COOKIE, cookie, domain="testserver")
    assert edge.get(STORE).json() is None
    r = edge.get(f"{STORE}/orders")
    assert r.status_code == 401
    assert r.json()["type"].endswith("/sign_in_required")
    assert not crud_mock["account"].called and not crud_mock["orders"].called


def test_a_visitor_cookie_never_verifies_as_a_session(edge: TestClient) -> None:
    key: bytes = edge.app.state.cookie_key  # type: ignore[attr-defined]
    from scadbuddy.storefront.visitors import sign as sign_visitor

    assert verify(sign_visitor(TOKEN, key), key) is None
    assert verify(sign(TOKEN, key), key) == TOKEN


def test_a_session_the_crud_side_ended_clears_the_cookie(
    edge: TestClient,
    crud_mock: respx.Router,  # noqa: F811
) -> None:
    sign_in(edge)
    crud_mock["account"].respond(
        200, content=b"null", headers={**CRUD, "Content-Type": "application/json"}
    )
    r = edge.get(STORE)
    assert r.json() is None
    assert any(
        c.startswith(f"{SESSION_COOKIE}=") and "max-age=0" in c.lower() for c in set_cookies(r)
    )


def test_a_sign_in_answer_without_a_session_is_the_edges_fault(
    edge: TestClient,
    crud_mock: respx.Router,  # noqa: F811
) -> None:
    crud_mock["verify"].respond(200, json={"email": "ada@example.com"}, headers=CRUD)
    r = sign_in(edge)
    assert r.status_code == 502  # type: ignore[attr-defined]
    assert not [c for c in set_cookies(r) if c.startswith(f"{SESSION_COOKIE}=")]


def ask_code(edge: TestClient) -> Any:
    return edge.post(
        f"{STORE}/code",
        json={"email": "ada@example.com"},
        headers={"Origin": PUBLIC_URL, TURNSTILE_HEADER: "tok"},
    )


def test_a_code_request_takes_the_sign_up_check_and_answers_as_the_crud_side_did(
    edge: TestClient,
    crud_mock: respx.Router,  # noqa: F811
) -> None:
    r = ask_code(edge)
    assert r.status_code == 204, r.text
    assert r.content == b""
    sent = crud_mock["code"].calls.last.request
    assert json.loads(sent.content) == {"email": "ada@example.com"}
    assert json.loads(crud_mock["turnstile"].calls.last.request.content)["action"] == "sign_up"
    assert not [c for c in set_cookies(r) if c.startswith(f"{SESSION_COOKIE}=")]


def test_a_failed_bot_check_stops_a_code_request_before_the_crud_side(
    edge: TestClient,
    crud_mock: respx.Router,  # noqa: F811
) -> None:
    crud_mock["turnstile"].respond(200, json={"verdict": "fail"}, headers=CRUD)
    r = ask_code(edge)
    assert r.status_code == 403
    assert r.json()["type"] == BOT_CHECK_FAILED
    assert not crud_mock["code"].called


def test_a_code_request_is_sent_once(
    edge: TestClient,
    crud_mock: respx.Router,  # noqa: F811
) -> None:
    """Never retried: a second try would make a second code and mail, and count against
    the visitor's limits."""
    crud_mock["code"].side_effect = refused
    r = ask_code(edge)
    assert r.status_code == 503
    assert crud_mock["code"].call_count == 1


def test_a_code_request_passes_on_the_crud_sides_limit(
    edge: TestClient,
    crud_mock: respx.Router,  # noqa: F811
) -> None:
    crud_mock["code"].respond(
        429,
        json={
            "type": "https://scadbuddy.dev/problems/storefront/sign_in_limited",
            "title": "Too Many Requests",
            "status": 429,
            "detail": "Too many codes asked for. Try again in 60 seconds.",
            "retry_after": 60,
        },
        headers={**CRUD, "Retry-After": "60", "Content-Type": "application/problem+json"},
    )
    r = ask_code(edge)
    assert r.status_code == 429
    assert r.json()["type"].endswith("/sign_in_limited")
    assert r.headers["Retry-After"] == "60"
