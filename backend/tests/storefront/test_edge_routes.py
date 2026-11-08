"""The edge's public routes and its limits (#5282, spec 2026-11-02 §5.1, §5.2, §5.8, plan
Task 6): the listings, the visitor cookie, the ``Origin`` check on writes and the
per-client request limit."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator, MutableMapping
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from scadbuddy.core.proxies import parse_cidr_list
from scadbuddy.crud.client import CrudUnavailableError
from scadbuddy.storefront import uploads
from scadbuddy.storefront.app import create_app
from scadbuddy.storefront.ratelimit import (
    MAX_CLIENTS,
    MAX_PER_PREFIX,
    OVERFLOW,
    READ_LIMIT,
    WINDOW,
    WRITE_LIMIT,
    RequestLimit,
    client_key,
)
from scadbuddy.storefront.routes import MAX_RENDER_BODY
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
    inner = f"/api/v1/internal/crud/storefront/listings/{LISTING_ID}"
    crud_mock["listing"].respond(404, json={**NOT_FOUND, "instance": inner}, headers=CRUD)
    r = edge.get(f"/api/store/v1/listings/{LISTING_ID}")
    # The visitor sees the edge's own path, never the CRUD side's.
    assert r.status_code == 404
    assert r.json() == {**NOT_FOUND, "instance": f"/api/store/v1/listings/{LISTING_ID}"}
    assert "internal" not in r.text


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


@pytest.mark.parametrize(
    ("address", "key"),
    [
        ("203.0.113.9", "203.0.113.9"),
        ("2001:db8:1:2:aaaa::1", "2001:db8:1:2::/64"),
        ("2001:db8:1:2:ffff:ffff:ffff:ffff", "2001:db8:1:2::/64"),
        ("2001:db8:1:3::1", "2001:db8:1:3::/64"),
        # An IPv4 address mapped into IPv6 is that IPv4 address.
        ("::ffff:203.0.113.9", "203.0.113.9"),
        ("[2001:db8:1:2::1]", "2001:db8:1:2::/64"),
        ("not an address", "not an address"),
    ],
)
def test_a_client_is_keyed_by_its_address_or_its_ipv6_64(address: str, key: str) -> None:
    assert client_key(address) == key


def test_two_addresses_in_one_64_share_a_limit() -> None:
    clock = Clock()
    limit = RequestLimit(_ok, prefix="/api/store/v1", trusted=(), clock=clock)  # type: ignore[arg-type]
    for n in range(WRITE_LIMIT):
        assert limit.note(f"2001:db8:1:2::{n + 1:x}", write=True) is None
    assert limit.note("2001:db8:1:2:dead:beef::1", write=True) is not None
    assert limit.note("2001:db8:1:3::1", write=True) is None


def small_table(clock: Clock, max_clients: int = 3) -> RequestLimit:
    return RequestLimit(
        _ok,  # type: ignore[arg-type]
        prefix="/api/store/v1",
        trusted=(),
        clock=clock,
        max_clients=max_clients,
    )


def test_a_full_table_sends_a_new_client_to_the_overflow_bucket() -> None:
    clock = Clock()
    limit = small_table(clock)
    for n in range(3):
        assert limit.note(f"198.51.100.{n}", write=True) is None
    # New clients share one bucket with the same limits: served, then limited.
    for n in range(WRITE_LIMIT):
        assert limit.note(f"203.0.113.{n}", write=True) is None
    retry = limit.note("203.0.113.200", write=True)
    assert retry is not None and retry >= 1
    assert limit.clients() == {"198.51.100.0", "198.51.100.1", "198.51.100.2", OVERFLOW}
    # The old clients keep their own history: one more write each still counts.
    for _ in range(WRITE_LIMIT - 1):
        assert limit.note("198.51.100.0", write=True) is None
    assert limit.note("198.51.100.0", write=True) is not None
    assert limit.note("198.51.100.1", write=True) is None
    # Once the old ones are idle past the window, a new client has its own entry.
    clock.now += WINDOW + 1
    assert limit.note("203.0.113.9", write=True) is None
    assert limit.clients() == {"203.0.113.9"}


def test_the_overflow_bucket_answers_through_the_middleware() -> None:
    clock = Clock()
    app = FastAPI(routes=[Route("/api/store/v1/x", _ok, methods=["POST"])])
    app.add_middleware(
        RequestLimit,
        prefix="/api/store/v1",
        trusted=parse_cidr_list("203.0.113.0/24"),
        clock=clock,
        max_clients=1,
    )
    client = TestClient(app, client=CLIENT)
    first = {"X-Forwarded-For": "198.51.100.1"}
    assert client.post("/api/store/v1/x", headers=first).status_code == 200
    codes = [
        client.post(
            "/api/store/v1/x", headers={"X-Forwarded-For": f"198.51.100.{n + 2}"}
        ).status_code
        for n in range(WRITE_LIMIT + 1)
    ]
    assert codes[:WRITE_LIMIT] == [200] * WRITE_LIMIT
    assert codes[-1] == 429
    r = client.post("/api/store/v1/x", headers={"X-Forwarded-For": "198.51.100.250"})
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
    assert r.json()["type"] == f"{STOREFRONT_PROBLEMS}rate_limited"
    # The client that was there first is untouched.
    assert client.post("/api/store/v1/x", headers=first).status_code == 200


def test_one_48_holds_at_most_65_entries() -> None:
    clock = Clock()
    limit = RequestLimit(_ok, prefix="/api/store/v1", trusted=(), clock=clock)  # type: ignore[arg-type]
    for n in range(70):
        assert limit.note(f"2001:db8:7:{n:x}::1", write=False) is None
    keys = limit.clients()
    assert len(keys) == MAX_PER_PREFIX + 1 == 65
    assert "2001:db8:7::/48" in keys
    # The /64s past the first 64 share the /48's allowance.
    for n in range(64, 70):
        assert limit.history(f"2001:db8:7:{n:x}::1") == (6, None)
    # An address of a /64 that has its own entry keeps it.
    assert limit.history("2001:db8:7:0::2") == (1, None)
    # Another /48 is not touched.
    assert limit.note("2001:db8:8:1::1", write=False) is None
    assert "2001:db8:8:1::/64" in limit.clients()


def test_the_48_count_follows_the_sweep() -> None:
    clock = Clock()
    limit = RequestLimit(_ok, prefix="/api/store/v1", trusted=(), clock=clock)  # type: ignore[arg-type]
    for n in range(MAX_PER_PREFIX):
        limit.note(f"2001:db8:7:{n:x}::1", write=False)
    clock.now += WINDOW + 1
    limit.note("198.51.100.1", write=False)
    # The /48's /64s left with the sweep, so a new /64 of it has its own entry again.
    limit.note("2001:db8:7:ff::1", write=False)
    assert "2001:db8:7:ff::/64" in limit.clients()


def test_the_table_stays_flat_over_many_clients() -> None:
    """60,000 clients, one request each, 10 ms apart: only the last window's are kept."""
    clock = Clock()
    limit = RequestLimit(_ok, prefix="/api/store/v1", trusted=(), clock=clock)  # type: ignore[arg-type]
    most = 0
    for n in range(60_000):
        clock.now += 0.01
        assert limit.note(f"10.{n >> 16 & 255}.{n >> 8 & 255}.{n & 255}", write=False) is None
        most = max(most, len(limit.clients()))
    assert most <= 2 * WINDOW / 0.01 + 1
    assert most < MAX_CLIENTS


def test_a_client_that_only_reads_has_no_write_history() -> None:
    clock = Clock()
    limit = RequestLimit(_ok, prefix="/api/store/v1", trusted=(), clock=clock)  # type: ignore[arg-type]
    limit.note("198.51.100.1", write=False)
    assert limit.history("198.51.100.1") == (1, None)


# -- renders (Task 7) ---------------------------------------------------------------------

JOB = "0123456789abcdef0123456789abcdef"
RENDERS = "/api/v1/internal/crud/storefront/renders"
LIMITED = {
    "type": f"{STOREFRONT_PROBLEMS}render_limited",
    "title": "Too Many Requests",
    "status": 429,
    "detail": "You have made a lot of renders. Wait a moment, then try again.",
    "limit": "per_minute",
}
BODY = {
    "listing_id": LISTING_ID,
    "listing_updated_at": "2026-11-08T00:00:00Z",
    "values": {"width": 40},
    "colours": {},
}
RENDER: dict[str, Any] = {
    "id": JOB,
    "status": "pending",
    "stage": None,
    "plates": [],
    "bbox_mm": None,
    "colors": None,
    "preview_url": None,
    "error": None,
}


def test_the_edge_passes_a_429_through(edge: TestClient, crud_mock: respx.Router) -> None:  # noqa: F811
    crud_mock["renders"].respond(429, headers={"Retry-After": "42", **CRUD}, json=LIMITED)
    r = edge.post("/api/store/v1/renders", json=BODY, headers={"Origin": PUBLIC_URL})
    assert (r.status_code, r.headers["retry-after"]) == (429, "42")
    assert r.json()["type"] == LIMITED["type"]
    assert r.json()["instance"] == "/api/store/v1/renders"


def test_a_render_is_forwarded_as_sent_with_a_key(
    edge: TestClient,
    crud_mock: respx.Router,  # noqa: F811
) -> None:
    route = crud_mock["renders"].respond(202, json=RENDER, headers=CRUD)
    # A number no float holds reaches the CRUD side as the visitor sent it, which refuses it.
    raw = '{"listing_id": "x", "values": {"width": 1' + "0" * 400 + "}}"
    r = edge.post(
        "/api/store/v1/renders",
        content=raw,
        headers={"Origin": PUBLIC_URL, "Content-Type": "application/json"},
    )
    assert r.status_code == 202 and r.json() == RENDER
    sent = route.calls.last.request
    assert sent.content == raw.encode()
    assert sent.headers["content-type"] == "application/json"
    assert uuid.UUID(sent.headers["idempotency-key"]).version == 4
    edge.post("/api/store/v1/renders", json=BODY, headers={"Origin": PUBLIC_URL})
    assert route.calls.last.request.headers["idempotency-key"] != sent.headers["idempotency-key"]


def test_a_render_body_too_large_never_reaches_the_crud_side(
    edge: TestClient,
    crud_mock: respx.Router,  # noqa: F811
) -> None:
    route = crud_mock["renders"]
    r = edge.post(
        "/api/store/v1/renders",
        content=b"{" + b" " * (MAX_RENDER_BODY + 1) + b"}",
        headers={"Origin": PUBLIC_URL, "Content-Type": "application/json"},
    )
    assert r.status_code == 413
    assert r.json()["type"] == f"{STOREFRONT_PROBLEMS}render_body_invalid"
    assert route.call_count == 0


def test_a_render_and_its_estimate_are_forwarded(
    edge: TestClient,
    crud_mock: respx.Router,  # noqa: F811
) -> None:
    status = crud_mock.get(f"{RENDERS}/{JOB}").respond(200, json=RENDER, headers=CRUD)
    estimate = crud_mock.get(f"{RENDERS}/{JOB}/estimate").respond(
        200, json={"estimate": True, "price_minor": 500}, headers=CRUD
    )
    assert edge.get(f"/api/store/v1/renders/{JOB}").json() == RENDER
    assert edge.get(f"/api/store/v1/renders/{JOB}/estimate").json()["price_minor"] == 500
    for route in (status, estimate):
        assert len(route.calls.last.request.headers[VISITOR_HEADER]) == 43


def test_another_visitors_render_passes_through_as_404(
    edge: TestClient,
    crud_mock: respx.Router,  # noqa: F811
) -> None:
    missing = {
        "type": f"{STOREFRONT_PROBLEMS}render_not_found",
        "title": "Not Found",
        "status": 404,
        "detail": "There is no such render.",
    }
    crud_mock.get(f"{RENDERS}/{JOB}").respond(404, json=missing, headers=CRUD)
    r = edge.get(f"/api/store/v1/renders/{JOB}")
    assert r.status_code == 404 and r.json()["type"] == missing["type"]


@pytest.mark.parametrize("job_id", ["..", "not-a-job", "0" * 31, "A" * 32])
@pytest.mark.parametrize("tail", ["", "/preview.glb", "/estimate"])
def test_a_job_id_that_is_not_one_never_reaches_the_crud_side(
    edge: TestClient,
    crud_mock: respx.Router,  # noqa: F811
    job_id: str,
    tail: str,
) -> None:
    before = len(crud_mock.calls)
    r = edge.get(f"/api/store/v1/renders/{job_id}{tail}")
    assert r.status_code == 404
    assert len(crud_mock.calls) == before


def test_the_preview_is_streamed(edge: TestClient, crud_mock: respx.Router) -> None:  # noqa: F811
    chunks = [b"glTF", b"\x02\x00\x00\x00", b"x" * 100_000]
    closed: list[bool] = []

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            for chunk in chunks:
                yield chunk

        async def aclose(self) -> None:
            closed.append(True)

    route = crud_mock.get(f"{RENDERS}/{JOB}/preview.glb").mock(
        return_value=httpx.Response(
            200, headers={**CRUD, "Content-Type": "model/gltf-binary"}, stream=Body()
        )
    )
    r = edge.get(f"/api/store/v1/renders/{JOB}/preview.glb?plate=2")
    assert r.status_code == 200
    assert r.headers["content-type"] == "model/gltf-binary"
    assert r.content == b"".join(chunks)
    assert route.calls.last.request.url.params["plate"] == "2"
    # The edge reads it as a stream and closes it, never as one body.
    assert closed == [True]


def test_a_preview_refusal_passes_through(edge: TestClient, crud_mock: respx.Router) -> None:  # noqa: F811
    missing = {"type": f"{STOREFRONT_PROBLEMS}render_not_found", "status": 404}
    crud_mock.get(f"{RENDERS}/{JOB}/preview.glb").respond(404, json=missing, headers=CRUD)
    r = edge.get(f"/api/store/v1/renders/{JOB}/preview.glb")
    assert r.status_code == 404 and r.json()["type"] == missing["type"]


def test_a_dropped_preview_closes_its_stream_at_once(
    edge_env: None,  # noqa: F811
    crud_mock: respx.Router,  # noqa: F811
) -> None:
    """Review Minor 6: a browser that goes mid-stream (here, a send that fails) closes the
    CRUD side's stream before the request ends, not when the garbage collector runs."""
    closed: list[bool] = []

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            for chunk in (b"glTF", b"x" * 1000):
                yield chunk

        async def aclose(self) -> None:
            closed.append(True)

    crud_mock.get(f"{RENDERS}/{JOB}/preview.glb").mock(
        return_value=httpx.Response(
            200, headers={**CRUD, "Content-Type": "model/gltf-binary"}, stream=Body()
        )
    )
    app = create_app(load())
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": f"/api/store/v1/renders/{JOB}/preview.glb",
        "raw_path": f"/api/store/v1/renders/{JOB}/preview.glb".encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"testserver")],
        "client": CLIENT,
        "server": ("testserver", 443),
    }

    async def receive() -> dict[str, Any]:
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}

    async def send(message: MutableMapping[str, Any]) -> None:
        if message["type"] == "http.response.body" and message.get("body"):
            raise OSError("the browser went away")

    async def drive() -> None:
        with pytest.raises(OSError):
            await app(scope, receive, send)
        assert closed == [True]

    asyncio.run(drive())


# -- the draft (Task 8) -------------------------------------------------------------------

DRAFT = "/api/v1/internal/crud/storefront/draft"
ITEM = "6c1f9d0f-5d0c-4d43-9d33-0b6f0f1e6a11"
VIEW: dict[str, Any] = {
    "id": "0d0c0b0a-5d0c-4d43-9d33-6c1f9d0f6a11",
    "currency": "USD",
    "minor_exponent": 2,
    "items": [],
}


def test_the_draft_is_forwarded(edge: TestClient, crud_mock: respx.Router) -> None:  # noqa: F811
    route = crud_mock.get(DRAFT).respond(
        200, content=b"null", headers={**CRUD, "Content-Type": "application/json"}
    )
    r = edge.get("/api/store/v1/draft")
    assert r.status_code == 200 and r.json() is None
    assert len(route.calls.last.request.headers[VISITOR_HEADER]) == 43


def test_an_item_is_forwarded_as_sent_with_a_key(
    edge: TestClient,
    crud_mock: respx.Router,  # noqa: F811
) -> None:
    route = crud_mock.post(f"{DRAFT}/items").respond(200, json=VIEW, headers=CRUD)
    raw = '{"listing_id": "x", "values": {"width": 1' + "0" * 400 + "}}"
    r = edge.post(
        "/api/store/v1/draft/items",
        content=raw,
        headers={"Origin": PUBLIC_URL, "Content-Type": "application/json"},
    )
    assert r.status_code == 200 and r.json() == VIEW
    sent = route.calls.last.request
    assert sent.content == raw.encode()
    assert uuid.UUID(sent.headers["idempotency-key"]).version == 4


def test_an_item_body_too_large_never_reaches_the_crud_side(
    edge: TestClient,
    crud_mock: respx.Router,  # noqa: F811
) -> None:
    route = crud_mock.post(f"{DRAFT}/items")
    r = edge.post(
        "/api/store/v1/draft/items",
        content=b"{" + b" " * (MAX_RENDER_BODY + 1) + b"}",
        headers={"Origin": PUBLIC_URL, "Content-Type": "application/json"},
    )
    assert r.status_code == 413
    assert r.json()["type"] == f"{STOREFRONT_PROBLEMS}draft_body_invalid"
    assert route.call_count == 0


def test_an_item_from_another_origin_is_refused_at_the_edge(
    edge: TestClient,
    crud_mock: respx.Router,  # noqa: F811
) -> None:
    add = crud_mock.post(f"{DRAFT}/items")
    remove = crud_mock.delete(f"{DRAFT}/items/{ITEM}")
    evil = {"Origin": "https://evil.example"}
    assert edge.post("/api/store/v1/draft/items", json={}, headers=evil).status_code == 403
    assert edge.delete(f"/api/store/v1/draft/items/{ITEM}", headers=evil).status_code == 403
    assert (add.call_count, remove.call_count) == (0, 0)


def test_a_removal_is_forwarded_with_a_key(
    edge: TestClient,
    crud_mock: respx.Router,  # noqa: F811
) -> None:
    route = crud_mock.delete(f"{DRAFT}/items/{ITEM}").respond(200, json=VIEW, headers=CRUD)
    r = edge.delete(f"/api/store/v1/draft/items/{ITEM}", headers={"Origin": PUBLIC_URL})
    assert r.status_code == 200 and r.json() == VIEW
    assert uuid.UUID(route.calls.last.request.headers["idempotency-key"]).version == 4


def test_another_visitors_item_passes_through_as_404(
    edge: TestClient,
    crud_mock: respx.Router,  # noqa: F811
) -> None:
    missing = {
        "type": f"{STOREFRONT_PROBLEMS}item_not_found",
        "title": "Not Found",
        "status": 404,
        "detail": "There is no such item.",
    }
    crud_mock.delete(f"{DRAFT}/items/{ITEM}").respond(404, json=missing, headers=CRUD)
    r = edge.delete(f"/api/store/v1/draft/items/{ITEM}", headers={"Origin": PUBLIC_URL})
    assert r.status_code == 404 and r.json()["type"] == missing["type"]


@pytest.mark.parametrize("item_id", ["..", "not-an-item", "%2E%2E", "A" * 36])
def test_an_item_id_that_is_not_one_never_reaches_the_crud_side(
    edge: TestClient,
    crud_mock: respx.Router,  # noqa: F811
    item_id: str,
) -> None:
    before = len(crud_mock.calls)
    r = edge.delete(f"/api/store/v1/draft/items/{item_id}", headers={"Origin": PUBLIC_URL})
    # ".." leaves the path whole: ``DELETE /draft`` is no route.
    assert r.status_code == (405 if item_id == ".." else 404)
    assert len(crud_mock.calls) == before


# -- design requests (Task 9) ---------------------------------------------------------------

REQUESTS = "/api/v1/internal/crud/storefront/draft/requests"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
BIG = 25 * 1024 * 1024


def _ask(edge: TestClient, files: Any = None, **kwargs: Any) -> Any:
    return edge.post(
        "/api/store/v1/draft/requests",
        data={"text": "A hook"},
        files=files or [],
        headers={"Origin": PUBLIC_URL},
        **kwargs,
    )


def test_a_request_is_streamed_on_with_a_key(
    edge: TestClient,
    crud_mock: respx.Router,  # noqa: F811
) -> None:
    route = crud_mock.post(REQUESTS).respond(200, json=VIEW, headers=CRUD)
    r = _ask(edge, [("files", ("a.png", PNG, "image/png"))])
    assert r.status_code == 200 and r.json() == VIEW
    sent = route.calls.last.request
    assert sent.headers["content-type"].startswith("multipart/form-data; boundary=")
    assert uuid.UUID(sent.headers["idempotency-key"]).version == 4
    assert b'name="text"' in sent.content and b"A hook" in sent.content
    assert b'filename="a.png"' in sent.content and PNG in sent.content


def test_the_edge_refuses_an_oversized_file_before_the_crud_call(
    edge: TestClient,
    crud_mock: respx.Router,  # noqa: F811
) -> None:
    files = [("files", ("big.png", PNG + b"0" * BIG, "image/png"))]
    r = _ask(edge, files)
    assert r.status_code == 413
    assert r.json()["type"] == f"{STOREFRONT_PROBLEMS}file_too_large"
    assert not [c for c in crud_mock.calls if "/draft/requests" in str(c.request.url)]


def test_the_edge_refuses_too_many_files_before_the_crud_call(
    edge: TestClient,
    crud_mock: respx.Router,  # noqa: F811
) -> None:
    files = [("files", (f"{n}.png", PNG, "image/png")) for n in range(11)]
    r = _ask(edge, files)
    assert r.status_code == 422
    assert r.json()["type"] == f"{STOREFRONT_PROBLEMS}too_many_files"
    assert not [c for c in crud_mock.calls if "/draft/requests" in str(c.request.url)]


def test_the_edge_caps_the_whole_body(
    edge: TestClient,
    crud_mock: respx.Router,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(uploads, "MAX_REQUEST_BODY", 1024)
    r = _ask(edge, [("files", ("a.png", PNG + b"0" * 2048, "image/png"))])
    assert r.status_code == 413
    assert r.json()["type"] == f"{STOREFRONT_PROBLEMS}file_too_large"
    assert not [c for c in crud_mock.calls if "/draft/requests" in str(c.request.url)]


@pytest.mark.parametrize(
    "body",
    [
        b'--b\r\nContent-Disposition: form-data; name="text"\r\nX-Pad: '
        + b"p" * 20000
        + b"\r\n\r\nhi\r\n--b--\r\n",
        b'--b\r\nContent-Disposition: form-data; name="other"\r\n\r\nhi\r\n--b--\r\n',
        b"".join(
            b'--b\r\nContent-Disposition: form-data; name="text"\r\n\r\nhi\r\n' for _ in range(3)
        )
        + b"--b--\r\n",
    ],
)
def test_the_edge_refuses_a_body_that_is_not_a_request(
    edge: TestClient,
    crud_mock: respx.Router,  # noqa: F811
    body: bytes,
) -> None:
    r = edge.post(
        "/api/store/v1/draft/requests",
        content=body,
        headers={"Origin": PUBLIC_URL, "Content-Type": "multipart/form-data; boundary=b"},
    )
    assert r.status_code == 422
    assert r.json()["type"] == f"{STOREFRONT_PROBLEMS}draft_body_invalid"
    assert not [c for c in crud_mock.calls if "/draft/requests" in str(c.request.url)]


def test_a_request_refusal_passes_through(
    edge: TestClient,
    crud_mock: respx.Router,  # noqa: F811
) -> None:
    refused = {
        "type": f"{STOREFRONT_PROBLEMS}file_type",
        "title": "Unsupported Media Type",
        "status": 415,
        "detail": "Send photos, videos or PDFs.",
    }
    crud_mock.post(REQUESTS).respond(415, json=refused, headers=CRUD)
    r = _ask(edge, [("files", ("x.svg", b"<svg/>", "image/svg+xml"))])
    assert r.status_code == 415 and r.json()["type"] == refused["type"]
