"""The SPA mount survives a deploy under a page loaded before it (#395).

A page cached from an older build asks for chunks that no longer exist. Those must
404 rather than receive ``index.html`` (served as ``text/html``, the browser rejects it
as a module with a MIME error), and ``index.html`` itself must be revalidated so the
next load picks up the new build.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from scadbuddy.api.static import IMMUTABLE, REVALIDATE, SPAStaticFiles


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>ScadBuddy</title>", encoding="utf-8")
    (dist / "assets" / "index-abc123.js").write_text("export {}", encoding="utf-8")
    (dist / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    app = Starlette()
    app.mount("/", SPAStaticFiles(dist), name="frontend")
    return TestClient(app)


@pytest.mark.parametrize("path", ["/", "/index.html", "/models/demo", "/models/demo/customize"])
def test_index_and_client_routes_are_revalidated(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == REVALIDATE


def test_hashed_asset_is_immutable(client: TestClient) -> None:
    response = client.get("/assets/index-abc123.js")
    assert response.status_code == 200
    assert response.headers["cache-control"] == IMMUTABLE


def test_revalidated_asset_keeps_immutable(client: TestClient) -> None:
    etag = client.get("/assets/index-abc123.js").headers["etag"]
    response = client.get("/assets/index-abc123.js", headers={"if-none-match": etag})
    assert response.status_code == 304
    assert response.headers["cache-control"] == IMMUTABLE


def test_ranged_asset_keeps_immutable(client: TestClient) -> None:
    # FileResponse picks 206 when it is sent, after get_response: there it is still 200.
    response = client.get("/assets/index-abc123.js", headers={"range": "bytes=0-3"})
    assert response.status_code == 206
    assert response.headers["cache-control"] == IMMUTABLE


@pytest.mark.parametrize("path", ["/assets/Preview-fDSm7oxk.js", "/assets/nested/gone.css"])
def test_missing_asset_is_a_404_not_the_spa(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 404
    assert not response.headers.get("content-type", "").startswith("text/html")


def test_other_root_files_are_revalidated(client: TestClient) -> None:
    response = client.get("/favicon.svg")
    assert response.status_code == 200
    assert response.headers["cache-control"] == REVALIDATE
