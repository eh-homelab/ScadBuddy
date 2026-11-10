"""The storefront edge process (#5282, spec 2026-11-02 §5.1, §10): it holds no database."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any, NoReturn

import psycopg
import psycopg_pool
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from scadbuddy.storefront.__main__ import main
from scadbuddy.storefront.app import create_app
from scadbuddy.storefront.settings import (
    FORBIDDEN_ENV,
    EdgeRefusal,
    StorefrontSettings,
    check_environment,
)
from scadbuddy.storefront.visitors import VISITOR_COOKIE
from scadbuddy.storefront.wire import CLIENT_HEADER, VISITOR_HEADER

BACKEND = Path(__file__).resolve().parents[2]
API = "http://api.internal:8000"
TOKEN = "sbsvc_" + "0" * 16 + "." + "t" * 43
CRUD = {"X-ScadBuddy-Crud": "1"}
LISTINGS = {"listings": [], "requests_enabled": False}
#: The browser's address, as the edge's peer.
CLIENT = ("203.0.113.9", 50000)


def load(**overrides: object) -> StorefrontSettings:
    """The settings from the environment, the way the process reads them."""
    return StorefrontSettings(**overrides)  # type: ignore[arg-type]


@pytest.fixture
def edge_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bootstrap = tmp_path / "bootstrap"
    bootstrap.write_text("proof")
    cookie_key = tmp_path / "cookie.key"
    cookie_key.write_bytes(os.urandom(32))
    for name in FORBIDDEN_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("SCADBUDDY_SERVICE_TOKEN_FILE", raising=False)
    monkeypatch.setenv("SCADBUDDY_API_INTERNAL_URL", "http://api.internal:8000")
    monkeypatch.setenv("SCADBUDDY_SERVICE_BOOTSTRAP_FILE", str(bootstrap))
    monkeypatch.setenv("SCADBUDDY_STOREFRONT_COOKIE_KEY_FILE", str(cookie_key))
    monkeypatch.setenv("SCADBUDDY_STOREFRONT_PUBLIC_URL", "https://shop.example.com")


@pytest.fixture
def crud_mock() -> Iterator[respx.Router]:
    """The CRUD service: the exchange, the storefront's areas, its listings, its renders
    and whether design requests are on."""
    with respx.mock(base_url=API, assert_all_called=False) as router:
        router.post("/api/v1/auth/service-token", name="exchange").respond(
            200, json={"access_token": TOKEN, "expires_in": 900}
        )
        router.get("/api/v1/internal/crud/storefront/areas", name="areas").respond(
            200,
            json={
                "areas": ["listings", "renders", "drafts", "requests", "pay", "turnstile", "order"]
            },
            headers=CRUD,
        )
        router.get("/api/v1/internal/crud/storefront/listings", name="listings").respond(
            200, json=LISTINGS, headers=CRUD
        )
        router.post("/api/v1/internal/crud/storefront/renders", name="renders").respond(
            202, json={}, headers=CRUD
        )
        router.get(
            "/api/v1/internal/crud/storefront/requests-enabled", name="requests_enabled"
        ).respond(200, json={"enabled": True}, headers=CRUD)
        # The bot check, off (#5283): no site key, and every token passes.
        router.get("/api/v1/internal/crud/storefront/turnstile", name="site_key").respond(
            200, json={"site_key": None}, headers=CRUD
        )
        router.post("/api/v1/internal/crud/storefront/turnstile/verify", name="turnstile").respond(
            200, json={"verdict": "off"}, headers=CRUD
        )
        yield router


@pytest.fixture
def edge_app(edge_env: None, crud_mock: respx.Router) -> FastAPI:
    return create_app(load())


def route_paths(app: FastAPI) -> list[str]:
    """Every path the app serves, looking inside an included router (FastAPI 0.162)."""
    paths: list[str] = []
    routes: list[Any] = list(app.routes)
    for route in routes:
        if hasattr(route, "original_router"):
            prefix = route.include_context.prefix
            paths += [prefix + r.path for r in route.original_router.routes]
        else:
            paths.append(route.path)
    return paths


def test_no_back_office_route_is_mounted(edge_app: FastAPI) -> None:
    paths = route_paths(edge_app)
    assert "/healthz" in paths
    assert [p for p in paths if p.startswith("/api/") and not p.startswith("/api/store/v1")] == []
    with TestClient(edge_app) as c:
        assert c.get("/api/v1/orders").status_code == 404
        assert c.get("/healthz").json() == {"status": "ok"}


@pytest.mark.parametrize("name", FORBIDDEN_ENV)
def test_a_database_or_key_variable_refuses_the_start(name: str) -> None:
    with pytest.raises(EdgeRefusal) as refused:
        check_environment({name: "x"})
    assert refused.value.reasons == [
        f"{name} is set; the storefront holds no database secret or key"
    ]


def test_a_clean_environment_is_accepted() -> None:
    check_environment({"PATH": "/usr/bin"})


def test_main_exits_2_with_a_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCADBUDDY_DATABASE_URL", "postgresql://x")
    assert main([]) == 2


def test_the_edge_starts_and_opens_no_database_connection(
    edge_env: None, monkeypatch: pytest.MonkeyPatch, crud_mock: respx.Router
) -> None:
    def refuse(*_a: object, **_k: object) -> NoReturn:
        raise AssertionError("the storefront opened a database connection")

    monkeypatch.setattr(psycopg.Connection, "connect", refuse)
    monkeypatch.setattr(psycopg_pool.ConnectionPool, "__init__", refuse)
    with TestClient(create_app(load()), client=CLIENT) as c:
        assert c.get("/healthz").status_code == 200
        r = c.get("/api/store/v1/listings")
        assert r.status_code == 200
        assert r.json() == LISTINGS
    sent = crud_mock["listings"].calls.last.request
    assert sent.headers["Authorization"] == f"Bearer {TOKEN}"
    cookie = r.headers["set-cookie"]
    assert cookie.startswith(f"{VISITOR_COOKIE}=v1.")
    visitor = cookie.split(";")[0].split("=", 1)[1].split(".")[1]
    assert sent.headers[VISITOR_HEADER] == visitor
    assert sent.headers[CLIENT_HEADER] == CLIENT[0]
    # The lifespan waited for the API's storefront areas before it served.
    assert crud_mock["areas"].called


def test_a_crud_refusal_passes_through_with_its_retry_after(
    edge_env: None, crud_mock: respx.Router
) -> None:
    limited = {"type": "https://scadbuddy.dev/problems/storefront/render_limited", "status": 429}
    crud_mock["listings"].respond(429, json=limited, headers={**CRUD, "Retry-After": "7"})
    with TestClient(create_app(load()), client=CLIENT) as c:
        r = c.get("/api/store/v1/listings")
    assert r.status_code == 429
    assert r.headers["Retry-After"] == "7"
    assert r.headers["content-type"] == "application/problem+json"
    # The edge's own path, not the CRUD route's.
    assert r.json() == {**limited, "instance": "/api/store/v1/listings"}


def test_a_trusted_proxy_names_the_client_and_is_parsed_once(
    edge_env: None, crud_mock: respx.Router, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scadbuddy.storefront.app as edge
    from scadbuddy.core.proxies import parse_cidr_list as real

    parsed: list[str] = []

    def counting(raw: str) -> Any:
        parsed.append(raw)
        return real(raw)

    monkeypatch.setattr(edge, "parse_cidr_list", counting)
    app = create_app(load(trusted_proxies="203.0.113.0/24"))
    with TestClient(app, client=CLIENT) as c:
        for _ in range(2):
            r = c.get("/api/store/v1/listings", headers={"X-Forwarded-For": "198.51.100.7"})
            assert r.status_code == 200
    assert crud_mock["listings"].calls.last.request.headers[CLIENT_HEADER] == "198.51.100.7"
    assert parsed == ["203.0.113.0/24"]


def test_a_good_cookie_keeps_its_visitor(edge_env: None, crud_mock: respx.Router) -> None:
    with TestClient(create_app(load()), client=CLIENT) as c:
        first = c.get("/api/store/v1/listings").headers["set-cookie"].split(";")[0]
        name, value = first.split("=", 1)
        r = c.get("/api/store/v1/listings", cookies={name: value})
    assert "set-cookie" not in r.headers
    sent = crud_mock["listings"].calls.last.request
    assert sent.headers[VISITOR_HEADER] == value.split(".")[1]


def test_a_forged_cookie_gets_a_new_visitor(edge_env: None, crud_mock: respx.Router) -> None:
    with TestClient(create_app(load()), client=CLIENT) as c:
        r = c.get("/api/store/v1/listings", cookies={VISITOR_COOKIE: "v1." + "a" * 43 + ".forged"})
    assert f"{VISITOR_COOKIE}=v1." in r.headers["set-cookie"]
    assert crud_mock["listings"].calls.last.request.headers[VISITOR_HEADER] != "a" * 43


def test_the_edge_imports_nothing_that_touches_the_database() -> None:
    out = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import scadbuddy.storefront.app, sys;"
            " forbidden = {'psycopg_pool', 'scadbuddy.orders', 'scadbuddy.render',"
            " 'scadbuddy.workflows', 'scadbuddy.store', 'scadbuddy.library', 'scadbuddy.main'};"
            " print(sorted({'.'.join(m.split('.')[:2]) for m in sys.modules} & forbidden))",
        ],
        capture_output=True,
        text=True,
        check=True,
        cwd=BACKEND,
    )
    assert out.stdout.strip() == "[]"


def test_a_short_cookie_key_is_refused(edge_env: None, tmp_path: Path) -> None:
    short = tmp_path / "short.key"
    short.write_bytes(os.urandom(31))
    with pytest.raises(ValidationError, match="at least 32 bytes"):
        load(storefront_cookie_key_file=short)


@pytest.mark.parametrize(
    "url", ["http://shop.example.com", "https://shop.example.com/shop", "shop.example.com"]
)
def test_the_public_url_is_an_https_origin(edge_env: None, url: str) -> None:
    with pytest.raises(ValidationError, match="STOREFRONT_PUBLIC_URL"):
        load(storefront_public_url=url)


def test_exactly_one_credential_proof_is_required(
    edge_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proof = tmp_path / "proof"
    proof.write_text("p")
    with pytest.raises(ValidationError, match="exactly one"):
        load(service_token_file=proof)
    monkeypatch.delenv("SCADBUDDY_SERVICE_BOOTSTRAP_FILE")
    with pytest.raises(ValidationError, match="exactly one"):
        load()
    assert load(service_token_file=proof).service_token_file == proof


def test_the_trusted_proxies_are_cidrs(edge_env: None) -> None:
    with pytest.raises(ValidationError, match="TRUSTED_PROXIES"):
        load(trusted_proxies="nonsense")


def test_an_octal_escaped_mac_is_a_new_visitor(edge_env: None, crud_mock: respx.Router) -> None:
    """`SimpleCookie` would decode the quoted escape to a non-ASCII mac."""
    forged = "v1." + "a" * 43 + "." + "\\351"
    with TestClient(create_app(load()), client=CLIENT) as c:
        r = c.get("/api/store/v1/listings", headers={"cookie": f'{VISITOR_COOKIE}="{forged}"'})
    assert r.status_code == 200
    assert f"{VISITOR_COOKIE}=v1." in r.headers["set-cookie"]
    assert crud_mock["listings"].calls.last.request.headers[VISITOR_HEADER] != "a" * 43


def test_another_odd_cookie_keeps_the_visitor(edge_env: None, crud_mock: respx.Router) -> None:
    """`SimpleCookie` drops the whole header for `foo={"a":1}`; the edge reads it as
    Starlette does."""
    with TestClient(create_app(load()), client=CLIENT) as c:
        first = c.get("/api/store/v1/listings").headers["set-cookie"].split(";")[0]
        r = c.get("/api/store/v1/listings", headers={"cookie": f'foo={{"a":1}}; {first}'})
    assert "set-cookie" not in r.headers
    sent = crud_mock["listings"].calls.last.request
    assert sent.headers[VISITOR_HEADER] == first.split("=", 1)[1].split(".")[1]


@pytest.mark.parametrize(
    "problem",
    [
        {"type": "https://scadbuddy.dev/problems/auth/forbidden", "reason": "wrong_caller"},
        {"type": "https://scadbuddy.dev/problems/storefront/visitor_id", "detail": "x"},
        {"type": "https://scadbuddy.dev/problems/storefront/store_client", "detail": "x"},
        {"type": "about:blank", "detail": "the request did not match the expected shape"},
    ],
)
def test_an_internal_crud_refusal_is_not_shown(
    edge_env: None,
    crud_mock: respx.Router,
    problem: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    crud_mock["listings"].respond(403, json={**problem, "status": 403}, headers=CRUD)
    with TestClient(create_app(load()), client=CLIENT) as c:
        r = c.get("/api/store/v1/listings")
    assert r.status_code == 502
    assert r.headers["content-type"] == "application/problem+json"
    shown = r.json()
    assert shown["detail"] == "The shop could not answer this request."
    assert "reason" not in shown
    assert shown["type"] == "about:blank" and shown["status"] == 502
    assert problem["type"] in caplog.text


class _Waiting:
    """A CRUD client whose API never lists the areas: only ``stop`` ends the wait."""

    def __init__(self) -> None:
        self.closed = False

    async def wait_for_areas(self, stop: asyncio.Event) -> bool:
        await stop.wait()
        return False

    async def aclose(self) -> None:
        self.closed = True


async def test_a_sigterm_ends_the_wait_for_the_areas(edge_env: None) -> None:
    """Before the API answers, a SIGTERM must not wait for the kill. uvicorn's own handler
    (here `seen`) still runs."""
    seen: list[int] = []
    served: list[bool] = []
    previous = signal.signal(signal.SIGTERM, lambda signum, _frame: seen.append(signum))
    crud = _Waiting()
    app = create_app(load(), crud=crud)  # type: ignore[arg-type]
    try:
        asyncio.get_running_loop().call_later(0.2, os.kill, os.getpid(), signal.SIGTERM)
        async with asyncio.timeout(5):
            # A stop before the areas ends the start: the app never serves.
            with pytest.raises(RuntimeError):
                async with app.router.lifespan_context(app):
                    served.append(True)
    finally:
        signal.signal(signal.SIGTERM, previous)
    assert seen == [signal.SIGTERM]
    assert served == []
    assert crud.closed
