"""The edge's bot check (#5283, spec 2026-11-02 §5.8): the edge holds no Turnstile secret;
it forwards the widget's token to the CRUD side and acts on the verdict, and its page's
CSP names Cloudflare only while the check is on."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.crud.client import CrudUnavailableError
from scadbuddy.storefront.app import create_app
from scadbuddy.storefront.ratelimit import REQUEST_UPLOADS
from scadbuddy.storefront.static import STOREFRONT_CSP, TURNSTILE_CSP, TURNSTILE_ORIGIN
from scadbuddy.storefront.turnstile import (
    BOT_CHECK_FAILED,
    BOT_CHECK_UNAVAILABLE,
    FAILED_CHECKS,
    FAILED_WINDOW,
    FULL_WARNING_EVERY,
    PREFIX_FAILED_CHECKS,
    SITE_KEY_RETRY,
    SITE_KEY_TTL,
    FailedChecks,
    SiteKey,
)
from scadbuddy.storefront.wire import (
    CLIENT_HEADER,
    TURNSTILE_HEADER,
    TURNSTILE_VERIFY_SECONDS,
    VISITOR_HEADER,
)
from tests.storefront.test_edge import (  # noqa: F401  (fixtures)
    CLIENT,
    CRUD,
    crud_mock,
    edge_env,
    load,
)
from tests.storefront.test_static import dist  # noqa: F401  (fixture)

PUBLIC_URL = "https://shop.example.com"
REQUESTS = "/api/v1/internal/crud/storefront/draft/requests"
VIEW = {"id": "o1", "items": []}


@pytest.fixture
def edge(edge_env: None, crud_mock: respx.Router) -> Iterator[TestClient]:  # noqa: F811
    crud_mock.post(REQUESTS, name="request").respond(200, json=VIEW, headers=CRUD)
    with TestClient(create_app(load()), base_url="https://testserver", client=CLIENT) as client:
        yield client


def ask(edge: TestClient, token: str | None = "tok") -> object:
    headers = {"Origin": PUBLIC_URL, "Idempotency-Key": str(uuid.uuid4())}
    if token is not None:
        headers[TURNSTILE_HEADER] = token
    body = b'--b\r\nContent-Disposition: form-data; name="text"\r\n\r\nA hook\r\n--b--\r\n'
    return edge.post(
        "/api/store/v1/draft/requests",
        content=body,
        headers={**headers, "Content-Type": "multipart/form-data; boundary=b"},
    )


def test_a_request_sends_its_token_and_form_to_the_crud_side(
    edge: TestClient,
    crud_mock: respx.Router,  # noqa: F811
) -> None:
    crud_mock["turnstile"].respond(200, json={"verdict": "pass"}, headers=CRUD)
    r = ask(edge)
    assert r.status_code == 200, r.text  # type: ignore[attr-defined]
    sent = crud_mock["turnstile"].calls.last.request
    assert json.loads(sent.content) == {"token": "tok", "action": "request"}
    assert sent.headers[CLIENT_HEADER] == CLIENT[0] and len(sent.headers[VISITOR_HEADER]) == 43
    assert crud_mock["request"].called


def test_with_the_check_off_a_request_needs_no_token(
    edge: TestClient,
    crud_mock: respx.Router,  # noqa: F811
) -> None:
    r = ask(edge, token=None)
    assert r.status_code == 200  # type: ignore[attr-defined]
    assert json.loads(crud_mock["turnstile"].calls.last.request.content)["token"] is None


@pytest.mark.parametrize(
    ("verdict", "status", "problem"),
    [("fail", 403, BOT_CHECK_FAILED), ("unavailable", 503, BOT_CHECK_UNAVAILABLE)],
)
def test_a_failed_or_unavailable_check_stops_the_request_before_its_body(
    edge: TestClient,
    crud_mock: respx.Router,  # noqa: F811
    verdict: str,
    status: int,
    problem: str,
) -> None:
    crud_mock["turnstile"].respond(200, json={"verdict": verdict}, headers=CRUD)
    r = ask(edge)
    assert r.status_code == status  # type: ignore[attr-defined]
    assert r.json()["type"] == problem  # type: ignore[attr-defined]
    assert not crud_mock["request"].called


def neighbour(edge: TestClient, address: str) -> TestClient:
    """Another browser on the same edge, from ``address``."""
    return TestClient(edge.app, base_url="https://testserver", client=(address, 50001))


def test_failed_checks_spend_no_allowance_so_a_neighbour_still_uploads(
    edge: TestClient,
    crud_mock: respx.Router,  # noqa: F811
) -> None:
    bot = neighbour(edge, "2001:db8:1::1")
    customer = neighbour(edge, "2001:db8:1::2")  # the same /64: one upload allowance
    crud_mock["turnstile"].respond(200, json={"verdict": "fail"}, headers=CRUD)
    assert REQUEST_UPLOADS + 1 < FAILED_CHECKS
    for _ in range(REQUEST_UPLOADS + 1):
        assert ask(bot).status_code == 403  # type: ignore[attr-defined]
    crud_mock["turnstile"].respond(200, json={"verdict": "pass"}, headers=CRUD)
    r = ask(customer)
    assert r.status_code == 200, r.text  # type: ignore[attr-defined]


def test_the_failure_limit_stops_further_siteverify_calls(
    edge: TestClient,
    crud_mock: respx.Router,  # noqa: F811
) -> None:
    crud_mock["turnstile"].respond(200, json={"verdict": "fail"}, headers=CRUD)
    for _ in range(FAILED_CHECKS):
        assert ask(edge).status_code == 403  # type: ignore[attr-defined]
    calls = crud_mock["turnstile"].call_count
    r = ask(edge)
    assert r.status_code == 403  # type: ignore[attr-defined]
    assert r.json()["type"] == BOT_CHECK_FAILED  # type: ignore[attr-defined]
    assert crud_mock["turnstile"].call_count == calls
    # Another client is not held back.
    assert ask(neighbour(edge, "2001:db8:1::9")).status_code == 403  # type: ignore[attr-defined]
    assert crud_mock["turnstile"].call_count == calls + 1


def test_a_client_out_of_allowance_costs_no_siteverify_call(
    edge: TestClient,
    crud_mock: respx.Router,  # noqa: F811
) -> None:
    crud_mock["turnstile"].respond(200, json={"verdict": "pass"}, headers=CRUD)
    for _ in range(REQUEST_UPLOADS):
        assert ask(edge).status_code == 200  # type: ignore[attr-defined]
    calls = crud_mock["turnstile"].call_count
    assert ask(edge).status_code == 429  # type: ignore[attr-defined]
    assert crud_mock["turnstile"].call_count == calls


def test_failures_age_out_of_the_window() -> None:
    now = [0.0]
    failures = FailedChecks(clock=lambda: now[0])
    for _ in range(FAILED_CHECKS):
        failures.failed("203.0.113.9")
    assert failures.blocked("203.0.113.9")
    assert not failures.blocked("203.0.113.10")
    now[0] += FAILED_WINDOW
    assert not failures.blocked("203.0.113.9")


def test_rotating_addresses_inside_a_64_does_not_escape_the_limit(
    edge: TestClient,
    crud_mock: respx.Router,  # noqa: F811
) -> None:
    crud_mock["turnstile"].respond(200, json={"verdict": "fail"}, headers=CRUD)
    for n in range(FAILED_CHECKS):
        assert ask(neighbour(edge, f"2001:db8:1:2::{n + 1:x}")).status_code == 403  # type: ignore[attr-defined]
    calls = crud_mock["turnstile"].call_count
    r = ask(neighbour(edge, "2001:db8:1:2:ffff::1"))
    assert r.status_code == 403  # type: ignore[attr-defined]
    assert crud_mock["turnstile"].call_count == calls


@pytest.mark.parametrize(
    ("member", "inside", "outside"),
    [
        ("2001:db8:7:{n:x}::1", "2001:db8:7:ffff::1", "2001:db8:8::1"),  # IPv6 /48
        ("198.51.100.{n}", "198.51.100.250", "198.51.101.1"),  # IPv4 /24
    ],
)
def test_a_prefix_with_a_hundred_failures_is_blocked(
    member: str, inside: str, outside: str
) -> None:
    failures = FailedChecks(clock=lambda: 0.0)
    # Each client stays under its own limit; together they reach the prefix's.
    clients = [member.format(n=n) for n in range(PREFIX_FAILED_CHECKS // (FAILED_CHECKS - 1) + 1)]
    spent = 0
    for client in clients:
        for _ in range(FAILED_CHECKS - 1):
            if spent < PREFIX_FAILED_CHECKS:
                failures.failed(client)
                spent += 1
    assert spent == PREFIX_FAILED_CHECKS
    assert failures.blocked(inside)
    assert not failures.blocked(outside)


def test_eviction_drops_the_least_recently_failed_and_never_a_block() -> None:
    failures = FailedChecks(clock=lambda: 0.0, size=3)
    for _ in range(FAILED_CHECKS):
        failures.failed("203.0.113.1")  # blocked
    failures.failed("198.51.100.2")
    failures.failed("192.0.2.3")
    failures.failed("198.51.100.2")  # now more recent than 192.0.2.3
    failures.failed("203.0.114.4")  # full: evicts 192.0.2.3, not the block
    assert failures.blocked("203.0.113.1")
    # 198.51.100.2 kept its two failures: eight more block it.
    for _ in range(FAILED_CHECKS - 2):
        failures.failed("198.51.100.2")
    assert failures.blocked("198.51.100.2")
    # 192.0.2.3 lost its one: nine more do not.
    for _ in range(FAILED_CHECKS - 1):
        failures.failed("192.0.2.3")
    assert not failures.blocked("192.0.2.3")


def warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "scadbuddy.storefront.turnstile"]


def test_a_table_full_of_blocks_checks_new_clients_and_warns_once_a_minute(
    caplog: pytest.LogCaptureFixture,
) -> None:
    now = [0.0]
    failures = FailedChecks(clock=lambda: now[0], size=2)
    for client in ("203.0.113.1", "198.51.100.2"):
        for _ in range(FAILED_CHECKS):
            failures.failed(client)
    with caplog.at_level(logging.WARNING, logger="scadbuddy.storefront.turnstile"):
        for _ in range(FAILED_CHECKS + 1):
            failures.failed("192.0.2.3")
        # Not tracked, so never refused: the check still runs for it.
        assert not failures.blocked("192.0.2.3")
        assert failures.blocked("203.0.113.1") and failures.blocked("198.51.100.2")
        assert len(warnings(caplog)) == 1
        now[0] += FULL_WARNING_EVERY
        failures.failed("192.0.2.3")
        assert len(warnings(caplog)) == 2


def test_a_timed_out_check_is_sent_once_answers_503_and_is_not_a_failure(
    edge: TestClient,
    crud_mock: respx.Router,  # noqa: F811
) -> None:
    crud_mock["turnstile"].side_effect = httpx.ReadTimeout("no answer")
    for n in range(FAILED_CHECKS + 1):
        r = ask(edge)
        assert r.status_code == 503  # type: ignore[attr-defined]
        assert r.json()["type"] == BOT_CHECK_UNAVAILABLE  # type: ignore[attr-defined]
        # Exactly one CRUD call per check: never retried, never counted as a failure
        # (past FAILED_CHECKS a counted one would be refused 403 with no call).
        assert crud_mock["turnstile"].call_count == n + 1
    request = crud_mock["turnstile"].calls.last.request
    assert request.extensions["timeout"]["read"] == TURNSTILE_VERIFY_SECONDS == 15.0


def test_with_no_key_read_within_its_life_the_check_makes_no_call(
    edge: TestClient,
    crud_mock: respx.Router,  # noqa: F811
) -> None:
    crud_mock["site_key"].respond(200, json={"site_key": None}, headers=CRUD)
    assert edge.get("/api/store/v1/turnstile").json() == {"site_key": None}
    r = ask(edge, token=None)
    assert r.status_code == 200, r.text  # type: ignore[attr-defined]
    assert crud_mock["turnstile"].call_count == 0


def test_a_known_off_read_lasts_only_its_life() -> None:
    now = [0.0]
    site_key = SiteKey(clock=lambda: now[0])
    assert not site_key.known_off()
    site_key._read = (0.0, None)
    assert site_key.known_off()
    now[0] = SITE_KEY_TTL
    assert not site_key.known_off()
    site_key._read = (now[0], "key-1")
    assert not site_key.known_off()
    assert SITE_KEY_TTL <= 60


def test_an_expired_block_behind_a_later_one_is_swept() -> None:
    now = [0.0]
    failures = FailedChecks(clock=lambda: now[0], size=2)
    failures.failed("198.51.100.2")  # its block will start at t=0
    now[0] = 5.0
    for _ in range(FAILED_CHECKS):
        failures.failed("203.0.113.1")  # blocked first, until t=605
    now[0] = 6.0
    for _ in range(FAILED_CHECKS - 1):
        failures.failed("198.51.100.2")  # blocked second, until t=600
    assert failures.blocked("203.0.113.1") and failures.blocked("198.51.100.2")
    now[0] = 601.0
    # The table is full of blocks; the expired one, behind the other, must make room.
    for _ in range(FAILED_CHECKS):
        failures.failed("192.0.2.3")
    assert failures.blocked("192.0.2.3")
    assert failures.blocked("203.0.113.1")


def test_an_unavailable_check_spends_no_allowance(
    edge: TestClient,
    crud_mock: respx.Router,  # noqa: F811
) -> None:
    crud_mock["turnstile"].respond(200, json={"verdict": "unavailable"}, headers=CRUD)
    for _ in range(REQUEST_UPLOADS + 1):
        assert ask(edge).status_code == 503  # type: ignore[attr-defined]


def test_an_oversized_token_fails_before_any_call(
    edge: TestClient,
    crud_mock: respx.Router,  # noqa: F811
) -> None:
    r = ask(edge, token="t" * 2049)
    assert r.status_code == 403  # type: ignore[attr-defined]
    assert not crud_mock["turnstile"].called and not crud_mock["request"].called


def test_the_page_reads_the_site_key(
    edge: TestClient,
    crud_mock: respx.Router,  # noqa: F811
) -> None:
    assert edge.get("/api/store/v1/turnstile").json() == {"site_key": None}
    assert "authorization" in crud_mock["site_key"].calls.last.request.headers
    # No visitor header: the CSP reads the key outside any visitor.
    assert VISITOR_HEADER not in crud_mock["site_key"].calls.last.request.headers


class _Crud:
    def __init__(self) -> None:
        self.answers: list[str | Exception | None] = []
        self.reads = 0

    async def turnstile_site_key(self) -> str | None:
        self.reads += 1
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


async def test_the_site_key_is_read_once_per_ttl_and_kept_through_a_failed_read() -> None:
    now = [0.0]
    crud = _Crud()
    crud.answers = ["key-1", CrudUnavailableError("down", "unreached"), "key-2"]
    site_key = SiteKey(clock=lambda: now[0])
    assert await site_key.get(crud) == "key-1"  # type: ignore[arg-type]
    assert await site_key.get(crud) == "key-1"  # type: ignore[arg-type]
    assert crud.reads == 1
    now[0] += SITE_KEY_TTL
    assert await site_key.get(crud) == "key-1"  # type: ignore[arg-type]
    # A failed read is not tried again at once: page loads do not each wait for it.
    assert await site_key.get(crud) == "key-1"  # type: ignore[arg-type]
    assert crud.reads == 2
    now[0] += SITE_KEY_RETRY
    assert await site_key.get(crud) == "key-2"  # type: ignore[arg-type]
    assert crud.reads == 3


def test_the_csp_names_cloudflare_only_while_the_check_is_on(
    edge_env: None,  # noqa: F811
    crud_mock: respx.Router,  # noqa: F811
    dist: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SCADBUDDY_FRONTEND_DIR", str(dist))
    with TestClient(create_app(load()), base_url="https://testserver", client=CLIENT) as off:
        assert off.get("/").headers["content-security-policy"] == STOREFRONT_CSP
        assert TURNSTILE_ORIGIN not in off.get("/l/x").headers["content-security-policy"]
    crud_mock["site_key"].respond(200, json={"site_key": "0x4AAA-site"}, headers=CRUD)
    with TestClient(create_app(load()), base_url="https://testserver", client=CLIENT) as on:
        csp = on.get("/l/x").headers["content-security-policy"]
    assert csp == TURNSTILE_CSP
    directives = dict(d.strip().split(" ", 1) for d in csp.split(";") if d.strip())
    assert directives["script-src"] == f"'self' {TURNSTILE_ORIGIN}"
    assert directives["frame-src"] == TURNSTILE_ORIGIN
    # Cloudflare asks for connect-src only in pre-clearance mode, which the shop does
    # not use.
    assert directives["connect-src"] == "'self'"
