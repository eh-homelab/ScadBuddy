"""The storefront page's static files (#5282, spec 2026-11-02 §5.1): the edge serves the
``storefront.html`` entry and its own chunks only, never the back office's bundle."""

from __future__ import annotations

import asyncio
import json
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.routing import Mount

from scadbuddy.storefront.app import create_app
from scadbuddy.storefront.static import (
    IMMUTABLE,
    REVALIDATE,
    STOREFRONT_CSP,
    TURNSTILE_CSP,
    TURNSTILE_ORIGIN,
    StorefrontStatic,
)
from scadbuddy.storefront.upload_origin import ORIGIN_TTL, UploadOrigin, bare_origin
from tests.storefront.test_edge import (  # noqa: F401  (fixtures)
    CLIENT,
    CRUD,
    LISTINGS,
    crud_mock,
    edge_env,
    load,
)

STOREFRONT_CHUNK = "storefront-abc123.js"
STOREFRONT_CSS = "storefront-abc123.css"
SHARED_CHUNK = "money-def456.js"
LAZY_CHUNK = "Preview-aaa111.js"
BACK_OFFICE_ONLY_CHUNK = "index-zzz999.js"


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    (tmp_path / "assets").mkdir()
    (tmp_path / ".vite").mkdir()
    chunks = (STOREFRONT_CHUNK, STOREFRONT_CSS, SHARED_CHUNK, LAZY_CHUNK, BACK_OFFICE_ONLY_CHUNK)
    for name in chunks:
        (tmp_path / "assets" / name).write_text(f"/* {name} */")
    (tmp_path / "storefront.html").write_text("<!doctype html><title>Shop</title>")
    (tmp_path / "index.html").write_text("<!doctype html><title>Back office</title>")
    manifest = {
        "storefront.html": {
            "file": f"assets/{STOREFRONT_CHUNK}",
            "css": [f"assets/{STOREFRONT_CSS}"],
            "imports": ["_money.js"],
            "dynamicImports": ["src/components/Preview.tsx"],
            "isEntry": True,
        },
        "_money.js": {"file": f"assets/{SHARED_CHUNK}"},
        "src/components/Preview.tsx": {"file": f"assets/{LAZY_CHUNK}", "imports": ["_money.js"]},
        "index.html": {
            "file": f"assets/{BACK_OFFICE_ONLY_CHUNK}",
            "imports": ["_money.js"],
            "isEntry": True,
        },
    }
    (tmp_path / ".vite" / "manifest.json").write_text(json.dumps(manifest))
    return tmp_path


def static_app(dist: Path) -> Starlette:
    return Starlette(routes=[Mount("/", app=StorefrontStatic(dist))])


def test_only_the_storefront_entry_is_served(dist: Path) -> None:
    client = TestClient(static_app(dist))
    assert client.get("/l/abc").text == (dist / "storefront.html").read_text()
    assert client.get(f"/assets/{STOREFRONT_CHUNK}").status_code == 200
    assert client.get(f"/assets/{BACK_OFFICE_ONLY_CHUNK}").status_code == 404
    assert client.get("/index.html").status_code == 404


def test_the_entry_closure_is_followed(dist: Path) -> None:
    client = TestClient(static_app(dist))
    for name in (STOREFRONT_CSS, SHARED_CHUNK, LAZY_CHUNK):
        response = client.get(f"/assets/{name}")
        assert response.status_code == 200, name
        assert response.headers["cache-control"] == IMMUTABLE


def test_the_page_is_revalidated_and_has_a_policy(dist: Path) -> None:
    client = TestClient(static_app(dist))
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["cache-control"] == REVALIDATE
    assert "default-src 'self'" in response.headers["content-security-policy"]


PAYPAL_ORIGINS = ("https://www.paypal.com", "https://www.sandbox.paypal.com")


def directive(csp: str, name: str) -> str:
    return next(part for part in csp.split("; ") if part.startswith(f"{name} "))


def test_only_the_pay_path_allows_paypal(dist: Path) -> None:
    """The pay page (#5283 phase 4) loads PayPal's SDK, and shows its buttons in frames
    that call PayPal; no other path may."""
    client = TestClient(static_app(dist))
    pay = client.get("/pay").headers["content-security-policy"]
    for name in ("script-src", "frame-src", "connect-src"):
        for origin in PAYPAL_ORIGINS:
            assert origin in directive(pay, name), (name, origin)
    assert "'self'" in directive(pay, "script-src")
    assert "'self'" in directive(pay, "connect-src")
    for path in ("/", "/o", "/l/abc", "/draft", "/pay/x", "/payment"):
        csp = client.get(path).headers["content-security-policy"]
        assert csp == STOREFRONT_CSP, path
        assert "paypal" not in csp


def test_every_page_may_put_to_the_upload_origin(dist: Path) -> None:
    """#6133: the page PUTs a customer's model files straight to the store's bucket."""

    async def bucket() -> str | None:
        return "https://atl1.digitaloceanspaces.com"

    app = StorefrontStatic(dist, upload_origin=bucket)
    client = TestClient(Starlette(routes=[Mount("/", app=app)]))
    for path in ("/", "/print-my-model", "/pay"):
        csp = client.get(path).headers["content-security-policy"]
        connect = directive(csp, "connect-src").split()
        assert connect[:2] == ["connect-src", "'self'"], path
        assert "https://atl1.digitaloceanspaces.com" in connect, path
        assert "digitaloceanspaces" not in directive(csp, "script-src")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://atl1.digitaloceanspaces.com", "https://atl1.digitaloceanspaces.com"),
        ("http://127.0.0.1:9000", "http://127.0.0.1:9000"),
        ("https://b.example/path", None),
        ("https://b.example?x=1", None),
        ("javascript:alert(1)", None),
        ("https://b.example; script-src *", None),
        ("https://u@b.example", None),
        ("", None),
        (None, None),
    ],
)
def test_only_a_bare_origin_reaches_the_csp(value: str | None, expected: str | None) -> None:
    assert bare_origin(value) == expected


def test_an_answer_that_is_not_json_keeps_the_last_origin() -> None:
    """A garbled answer is a failed read, never an error on every page load."""
    now = [0.0]
    answers: list[Any] = ["https://bucket.example", ValueError("Expecting value")]

    class Crud:
        async def upload_origin(self) -> str | None:
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return str(answer)

    origin = UploadOrigin(clock=lambda: now[0])
    crud: Any = Crud()
    assert asyncio.run(origin.get(crud)) == "https://bucket.example"
    now[0] = ORIGIN_TTL + 1
    assert asyncio.run(origin.get(crud)) == "https://bucket.example"


def test_the_pay_path_keeps_the_bot_check_origin_while_a_site_key_is_set(dist: Path) -> None:
    async def on() -> bool:
        return True

    client = TestClient(Starlette(routes=[Mount("/", app=StorefrontStatic(dist, turnstile=on))]))
    pay = client.get("/pay").headers["content-security-policy"]
    for name in ("script-src", "frame-src"):
        assert TURNSTILE_ORIGIN in directive(pay, name)
        for origin in PAYPAL_ORIGINS:
            assert origin in directive(pay, name)
    assert client.get("/o").headers["content-security-policy"] == TURNSTILE_CSP


@pytest.mark.parametrize(
    "path",
    [
        "/api/store/v1/nothing",
        "/api/v1/models",
        "/.vite/manifest.json",
        "/mockServiceWorker.js",
        "/assets/../index.html",
        "/assets/missing.js",
    ],
)
def test_everything_else_is_404(dist: Path, path: str) -> None:
    assert TestClient(static_app(dist)).get(path).status_code == 404


@pytest.mark.parametrize(
    "raw",
    [
        "/assets/..%2Findex.html",
        "/assets/%2e%2e/index.html",
        "/assets/%2E%2E%2Findex.html",
        "/assets%2F..%2Findex.html",
        "/assets%2F..%2F.vite%2Fmanifest.json",
        "/l/x%2F..%2F..%2Findex.html",
        "/%2e%2e/storefront.html",
    ],
)
def test_an_encoded_traversal_is_404(dist: Path, raw: str) -> None:
    """Sent as written: httpx removes literal dot segments, not encoded ones."""
    url = httpx.URL(f"http://testserver{raw}")
    assert url.raw_path == raw.encode()
    response = TestClient(static_app(dist)).get(url)
    assert response.status_code == 404
    assert response.text == "Not Found"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/store/v1/listings"),
        ("GET", "/api/store/v1/draft/items/abc"),
        ("DELETE", "/api/store/v1/nothing"),
    ],
)
def test_an_api_path_no_route_took_is_the_edges_problem_document(
    dist: Path,
    edge_env: None,  # noqa: F811
    crud_mock: respx.Router,  # noqa: F811
    method: str,
    path: str,
) -> None:
    """Starlette gives the `/` mount a request whose path a route matches but whose
    method it does not: still a problem document, not plain text."""
    app = create_app(load(frontend_dir=dist))
    with TestClient(app, base_url="https://testserver", client=CLIENT) as client:
        response = client.request(method, path)
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["status"] == 404


def test_a_websocket_is_closed_not_an_error(dist: Path) -> None:
    sent: list[MutableMapping[str, Any]] = []

    async def receive() -> MutableMapping[str, Any]:
        return {"type": "websocket.connect"}

    async def send(message: MutableMapping[str, Any]) -> None:
        sent.append(message)

    scope = {"type": "websocket", "path": "/nothing", "root_path": ""}
    asyncio.run(StorefrontStatic(dist)(scope, receive, send))
    assert [m["type"] for m in sent] == ["websocket.close"]


def test_a_write_is_refused(dist: Path) -> None:
    assert TestClient(static_app(dist)).post("/").status_code == 405


def test_a_bundle_without_the_entry_is_refused(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        StorefrontStatic(tmp_path)


def test_the_edge_serves_the_page_after_its_routes(
    dist: Path,
    edge_env: None,  # noqa: F811
    crud_mock: respx.Router,  # noqa: F811
) -> None:
    crud_mock.get("/api/v1/internal/crud/storefront/listings").respond(
        200, json=LISTINGS, headers=CRUD
    )
    app = create_app(load(frontend_dir=dist))
    with TestClient(app, base_url="https://testserver", client=CLIENT) as client:
        assert client.get("/api/store/v1/listings").json() == LISTINGS
        assert client.get("/draft").text == (dist / "storefront.html").read_text()
        assert client.get(f"/assets/{BACK_OFFICE_ONLY_CHUNK}").status_code == 404
