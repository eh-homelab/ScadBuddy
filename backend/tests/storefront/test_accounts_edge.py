"""The edge's account routes (#5283, spec 2026-11-02 §5.2): the ``sb_session`` cookie is
the CRUD side's token under the edge's mac, never in a body; verify and sign-out are POST
only and refuse another site's page; a cookie the edge did not sign is never sent on."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator

import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.storefront.accounts import SESSION_COOKIE, sign, verify
from scadbuddy.storefront.app import create_app
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
SIGN_IN = {"email": "ada@example.com", "code": "ABCD2345"}


@pytest.fixture
def edge(edge_env: None, crud_mock: respx.Router) -> Iterator[TestClient]:  # noqa: F811
    crud_mock.post(f"{CRUD_ACCOUNT}/verify", name="verify").respond(
        200, json={"email": "ada@example.com"}, headers={**CRUD, SESSION_HEADER: TOKEN}
    )
    crud_mock.post(f"{CRUD_ACCOUNT}/sign-out", name="sign_out").respond(204, headers=CRUD)
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


def test_a_submit_sends_the_session_on_and_an_anonymous_one_sends_none(
    edge: TestClient,
    crud_mock: respx.Router,  # noqa: F811
) -> None:
    """The CRUD side takes the submit's account from the session (`store_account`)."""
    route = crud_mock.post("/api/v1/internal/crud/storefront/draft/submit").respond(
        200, json={"id": "o1", "number": "ORD-2026-1", "email": "ada@example.com"}, headers=CRUD
    )
    body = {"draft_id": "o1", "name": "Ada", "email": "ada@example.com"}

    def submit() -> None:
        r = edge.post(
            "/api/store/v1/draft/submit",
            json=body,
            headers={"Origin": PUBLIC_URL, "Idempotency-Key": str(uuid.uuid4())},
        )
        assert r.status_code == 200, r.text

    submit()
    assert SESSION_HEADER not in route.calls.last.request.headers
    sign_in(edge)
    submit()
    assert route.calls.last.request.headers[SESSION_HEADER] == TOKEN


def test_a_failed_bot_check_stops_a_sign_in_before_the_crud_side(
    edge: TestClient,
    crud_mock: respx.Router,  # noqa: F811
) -> None:
    crud_mock["turnstile"].respond(200, json={"verdict": "fail"}, headers=CRUD)
    r = sign_in(edge)
    assert r.status_code == 403  # type: ignore[attr-defined]
    assert r.json()["type"] == BOT_CHECK_FAILED  # type: ignore[attr-defined]
    assert not crud_mock["verify"].called


@pytest.mark.parametrize("path", ["verify", "sign-out"])
def test_verify_and_sign_out_refuse_another_sites_page(
    edge: TestClient,
    crud_mock: respx.Router,  # noqa: F811
    path: str,
) -> None:
    r = edge.post(f"{STORE}/{path}", json=SIGN_IN, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert not crud_mock["verify"].called and not crud_mock["sign_out"].called
    assert not [c for c in set_cookies(r) if c.startswith(f"{SESSION_COOKIE}=")]


@pytest.mark.parametrize("path", ["verify", "sign-out"])
def test_verify_and_sign_out_are_post_only(
    edge: TestClient,
    crud_mock: respx.Router,  # noqa: F811
    path: str,
) -> None:
    r = edge.get(f"{STORE}/{path}")
    # No route answers it: 405, or 404 from the page's mount when a bundle is built.
    assert r.status_code in (404, 405)
    assert not crud_mock["verify"].called and not crud_mock["sign_out"].called


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
        f"v1.{TOKEN}.{'A' * 43}",  # a version the edge does not make
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


def test_the_order_page_sends_the_session_on_and_an_anonymous_one_sends_none(
    edge: TestClient,
    crud_mock: respx.Router,  # noqa: F811
) -> None:
    """``/o?order=<id>`` (#5283 Task 5): the signed-in reader's ``order_id`` goes in the
    body, and the store session in its header, so the CRUD side checks the account."""
    route = crud_mock.post("/api/v1/internal/crud/storefront/order/view").respond(
        200, json={}, headers=CRUD
    )
    body = {"order_id": "0d0c0b0a-5d0c-4d43-9d33-6c1f9d0f6a11"}
    edge.post("/api/store/v1/order/view", json=body, headers={"Origin": PUBLIC_URL})
    assert SESSION_HEADER not in route.calls.last.request.headers
    sign_in(edge)
    edge.post("/api/store/v1/order/view", json=body, headers={"Origin": PUBLIC_URL})
    sent = route.calls.last.request
    assert sent.headers[SESSION_HEADER] == TOKEN
    assert json.loads(sent.content) == body


def test_a_forged_session_cookie_is_not_sent_on_with_the_order_page(
    edge: TestClient,
    crud_mock: respx.Router,  # noqa: F811
) -> None:
    route = crud_mock.post("/api/v1/internal/crud/storefront/order/view").respond(
        200, json={}, headers=CRUD
    )
    edge.cookies.set(SESSION_COOKIE, f"s1.{TOKEN}.{'m' * 43}")
    edge.post(
        "/api/store/v1/order/view",
        json={"order_id": "0d0c0b0a-5d0c-4d43-9d33-6c1f9d0f6a11"},
        headers={"Origin": PUBLIC_URL},
    )
    assert SESSION_HEADER not in route.calls.last.request.headers
