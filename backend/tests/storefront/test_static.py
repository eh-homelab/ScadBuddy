"""The storefront page's static files (#5282, spec 2026-11-02 §5.1): the edge serves the
``storefront.html`` entry and its own chunks only, never the back office's bundle."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.routing import Mount

from scadbuddy.storefront.app import create_app
from scadbuddy.storefront.static import IMMUTABLE, REVALIDATE, StorefrontStatic
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
