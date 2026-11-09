"""The edge's bot check (#5283, spec 2026-11-02 §5.8): the edge holds no Turnstile secret;
it forwards the widget's token to the CRUD side and acts on the verdict, and its page's
CSP names Cloudflare only while the check is on."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.crud.client import CrudUnavailableError
from scadbuddy.storefront.app import create_app
from scadbuddy.storefront.static import STOREFRONT_CSP, TURNSTILE_CSP, TURNSTILE_ORIGIN
from scadbuddy.storefront.turnstile import (
    BOT_CHECK_FAILED,
    BOT_CHECK_UNAVAILABLE,
    SITE_KEY_TTL,
    SiteKey,
)
from scadbuddy.storefront.wire import CLIENT_HEADER, TURNSTILE_HEADER, VISITOR_HEADER
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
