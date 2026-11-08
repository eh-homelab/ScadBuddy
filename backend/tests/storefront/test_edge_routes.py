"""The edge's public routes and its limits (#5282, spec 2026-11-02 §5.1, §5.2, §5.8, plan
Task 6): the listings, the visitor cookie, the ``Origin`` check on writes and the
per-client request limit."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from scadbuddy.crud.client import CrudUnavailableError
from scadbuddy.storefront.app import create_app
from scadbuddy.storefront.ratelimit import READ_LIMIT, WINDOW, WRITE_LIMIT, RequestLimit
from scadbuddy.storefront.wire import CLIENT_HEADER, STOREFRONT_PROBLEMS, VISITOR_HEADER
from tests.storefront.test_edge import (  # noqa: F401  (fixtures)
    CLIENT,
    CRUD,
    LISTINGS,
    crud_mock,
    edge_env,
    load,
)

PUBLIC_URL = "https://shop.example.com"
LISTING_ID = "0b6f0f1e-5d0c-4d43-9d33-6c1f9d0f6a11"
LISTING = {"id": LISTING_ID, "title": "Demo"}
NOT_FOUND = {
    "type": f"{STOREFRONT_PROBLEMS}listing_not_found",
    "title": "Not Found",
    "status": 404,
    "detail": "There is no such listing.",
}


@pytest.fixture
def edge(edge_env: None, crud_mock: respx.Router) -> Iterator[TestClient]:  # noqa: F811
    crud_mock.get(
        f"/api/v1/internal/crud/storefront/listings/{LISTING_ID}", name="listing"
    ).respond(200, json=LISTING, headers=CRUD)
    # https, so the client sends the `Secure` cookie back, as a browser does.
    with TestClient(create_app(load()), base_url="https://testserver", client=CLIENT) as client:
        yield client


def test_the_first_request_sets_a_visitor_cookie(edge: TestClient) -> None:
    cookie = edge.get("/api/store/v1/listings").headers["set-cookie"]
    assert cookie.startswith("sb_visitor=") and "HttpOnly" in cookie and "Secure" in cookie
    assert "samesite=lax" in cookie.lower() and f"Max-Age={400 * 24 * 3600}" in cookie
    # The TestClient keeps the cookie: the next request sends it and gets none.
    assert "set-cookie" not in edge.get("/api/store/v1/listings").headers


def test_one_listing_is_forwarded(edge: TestClient, crud_mock: respx.Router) -> None:  # noqa: F811
    r = edge.get(f"/api/store/v1/listings/{LISTING_ID}")
    assert r.status_code == 200 and r.json() == LISTING
    sent = crud_mock["listing"].calls.last.request
    assert sent.headers[CLIENT_HEADER] == CLIENT[0]
    assert len(sent.headers[VISITOR_HEADER]) == 43


def test_an_unknown_listing_passes_through(edge: TestClient, crud_mock: respx.Router) -> None:  # noqa: F811
    crud_mock["listing"].respond(404, json=NOT_FOUND, headers=CRUD)
    r = edge.get(f"/api/store/v1/listings/{LISTING_ID}")
    assert r.status_code == 404 and r.json() == NOT_FOUND


@pytest.mark.parametrize("listing_id", ["..", "not-a-listing", "%2E%2E", "A" * 36])
def test_a_listing_id_that_is_not_one_never_reaches_the_crud_side(
    edge: TestClient,
    crud_mock: respx.Router,  # noqa: F811
    listing_id: str,
) -> None:
    before = len(crud_mock.calls)
    r = edge.get(f"/api/store/v1/listings/{listing_id}")
    assert r.status_code == 404
    assert len(crud_mock.calls) == before


def test_a_write_from_another_origin_is_refused_at_the_edge(
    edge: TestClient,
    crud_mock: respx.Router,  # noqa: F811
) -> None:
    r = edge.post("/api/store/v1/renders", json={}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert r.json()["detail"] == "This request must come from the shop's own page."
    assert not [c for c in crud_mock.calls if "/renders" in str(c.request.url)]
    # The shop's own page passes the gate (no route takes it yet).
    own = edge.post("/api/store/v1/renders", json={}, headers={"Origin": PUBLIC_URL})
    assert own.status_code != 403


def test_the_request_limit_answers_429(edge: TestClient) -> None:
    codes = [edge.get("/api/store/v1/listings").status_code for _ in range(READ_LIMIT + 1)]
    assert codes[:READ_LIMIT] == [200] * READ_LIMIT and codes[-1] == 429
    r = edge.get("/api/store/v1/listings")
    assert r.status_code == 429
    assert r.json()["type"] == f"{STOREFRONT_PROBLEMS}rate_limited"
    assert r.headers["content-type"] == "application/problem+json"
    assert 1 <= int(r.headers["retry-after"]) <= 60
    # The health check is not the visitor's to spend.
    assert edge.get("/healthz").status_code == 200


class _Down:
    async def wait_for_areas(self, stop: asyncio.Event) -> bool:
        return True

    async def aclose(self) -> None: ...

    async def listings(self, **_kwargs: Any) -> Any:
        raise CrudUnavailableError("the API did not answer", "unreached")


def test_a_crud_service_out_of_reach_is_503(edge_env: None) -> None:  # noqa: F811
    app = create_app(load(), crud=_Down())  # type: ignore[arg-type]
    with TestClient(app, client=CLIENT) as client:
        r = client.get("/api/store/v1/listings")
    assert r.status_code == 503
    assert r.json()["detail"] == "The shop is unavailable. Try again later."


# -- RequestLimit alone, on a clock the test moves -----------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def _ok(request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


def limited(clock: Clock, trusted: str = "") -> TestClient:
    from scadbuddy.core.proxies import parse_cidr_list

    app = FastAPI(routes=[Route("/api/store/v1/x", _ok, methods=["GET", "POST"])])
    app.add_middleware(
        RequestLimit, prefix="/api/store/v1", trusted=parse_cidr_list(trusted), clock=clock
    )
    return TestClient(app, client=CLIENT)


def test_writes_have_their_own_smaller_limit() -> None:
    clock = Clock()
    client = limited(clock)
    codes = [client.post("/api/store/v1/x").status_code for _ in range(WRITE_LIMIT + 1)]
    assert codes[:WRITE_LIMIT] == [200] * WRITE_LIMIT and codes[-1] == 429
    # Reads still have their own.
    assert client.get("/api/store/v1/x").status_code == 200


def test_the_window_slides() -> None:
    clock = Clock()
    client = limited(clock)
    for _ in range(WRITE_LIMIT - 1):
        assert client.post("/api/store/v1/x").status_code == 200
    clock.now += 30
    assert client.post("/api/store/v1/x").status_code == 200
    refused = client.post("/api/store/v1/x")
    assert refused.status_code == 429
    # The oldest leave the window 30 s from now.
    assert refused.headers["retry-after"] == "30"
    clock.now += 30
    assert client.post("/api/store/v1/x").status_code == 200
    clock.now += WINDOW
    for _ in range(WRITE_LIMIT):
        assert client.post("/api/store/v1/x").status_code == 200


def test_each_client_has_its_own_limit() -> None:
    clock = Clock()
    client = limited(clock, trusted="203.0.113.0/24")
    for _ in range(WRITE_LIMIT):
        client.post("/api/store/v1/x", headers={"X-Forwarded-For": "198.51.100.1"})
    one = client.post("/api/store/v1/x", headers={"X-Forwarded-For": "198.51.100.1"})
    other = client.post("/api/store/v1/x", headers={"X-Forwarded-For": "198.51.100.2"})
    assert (one.status_code, other.status_code) == (429, 200)


def test_a_path_outside_the_prefix_is_not_counted() -> None:
    clock = Clock()
    app = FastAPI(routes=[Route("/healthz", _ok)])
    app.add_middleware(RequestLimit, prefix="/api/store/v1", trusted=(), clock=clock)
    client = TestClient(app, client=CLIENT)
    assert all(client.get("/healthz").status_code == 200 for _ in range(READ_LIMIT + 1))


def test_a_quiet_client_is_forgotten() -> None:
    clock = Clock()
    limit = RequestLimit(_ok, prefix="/api/store/v1", trusted=(), clock=clock)  # type: ignore[arg-type]
    limit.note("198.51.100.1", write=False)
    clock.now += WINDOW + 1
    limit.note("198.51.100.2", write=False)
    assert limit.clients() == {"198.51.100.2"}


UNAVAILABLE = {
    "type": f"{STOREFRONT_PROBLEMS}storefront_needs_s3",
    "title": "Service Unavailable",
    "status": 503,
    "detail": "The shop is not available right now.",
    "reason": "not_configured",
}


def test_a_shop_not_set_up_reads_plainly_and_at_once(
    edge: TestClient,
    crud_mock: respx.Router,  # noqa: F811
) -> None:
    crud_mock["listings"].respond(503, json=UNAVAILABLE, headers=CRUD)
    r = edge.get("/api/store/v1/listings")
    assert r.status_code == 503
    assert r.json()["detail"] == "The shop is not available right now."
    assert "store_backend" not in r.text
    # Not retried for the CRUD client's budget: a setting does not fix itself.
    assert crud_mock["listings"].call_count == 1


@pytest.mark.parametrize("origin", ["http://localhost:5173", "http://127.0.0.1", "http://[::1]:80"])
def test_a_write_from_a_loopback_origin_is_refused(edge: TestClient, origin: str) -> None:
    r = edge.post("/api/store/v1/renders", json={}, headers={"Origin": origin})
    assert r.status_code == 403


def test_a_write_with_no_origin_passes_the_gate(edge: TestClient) -> None:
    assert edge.post("/api/store/v1/renders", json={}).status_code != 403


def test_a_loopback_shop_takes_loopback_writes(
    edge_env: None,  # noqa: F811
    crud_mock: respx.Router,  # noqa: F811
) -> None:
    app = create_app(load(storefront_public_url="https://localhost:8083"))
    with TestClient(app, base_url="https://testserver", client=CLIENT) as client:
        for origin in ("https://localhost:8083", "http://127.0.0.1:5173"):
            r = client.post("/api/store/v1/renders", json={}, headers={"Origin": origin})
            assert r.status_code != 403
        r = client.post("/api/store/v1/renders", json={}, headers={"Origin": PUBLIC_URL})
        assert r.status_code == 403
