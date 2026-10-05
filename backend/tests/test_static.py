"""The SPA mount survives a deploy under a page loaded before it (#395).

A page cached from an older build asks for chunks that no longer exist. Those must
404 rather than receive ``index.html`` (served as ``text/html``, the browser rejects it
as a module with a MIME error), and ``index.html`` itself must be revalidated so the
next load picks up the new build.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from scadbuddy.api.static import IMMUTABLE, REVALIDATE, SPAStaticFiles
from scadbuddy.core.problems import install_problem_handlers


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>ScadBuddy</title>", encoding="utf-8")
    (dist / "assets" / "index-abc123.js").write_text("export {}", encoding="utf-8")
    (dist / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    # As main.py composes it: the problem handlers answer any HTTPException the mount raises.
    app = FastAPI()
    install_problem_handlers(app)
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
    assert response.headers["cache-control"] == REVALIDATE


def test_missing_asset_404_has_no_csp(client: TestClient) -> None:
    response = client.get("/assets/Preview-fDSm7oxk.js")
    assert "content-security-policy" not in response.headers


def test_a_route_without_index_html_is_a_problem_document(
    client: TestClient, tmp_path: Path
) -> None:
    (tmp_path / "dist" / "index.html").unlink()
    response = client.get("/models/demo")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")


def test_missing_asset_is_never_a_404_page(client: TestClient, tmp_path: Path) -> None:
    # With a 404.html, StaticFiles returns the 404 instead of raising it.
    (tmp_path / "dist" / "404.html").write_text("<!doctype html><p>gone</p>", encoding="utf-8")
    response = client.get("/assets/Preview-fDSm7oxk.js")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/plain")
    assert response.headers["cache-control"] == REVALIDATE
    assert "content-security-policy" not in response.headers


def test_other_root_files_are_revalidated(client: TestClient) -> None:
    response = client.get("/favicon.svg")
    assert response.status_code == 200
    assert response.headers["cache-control"] == REVALIDATE


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/v1/nonsense"),
        ("GET", "/api/v1/jobs"),
        ("GET", "/api/v1/healthz"),
        ("GET", "/api"),
        ("HEAD", "/api/v1/nonsense"),
        ("POST", "/api/v1/nonsense"),
        ("DELETE", "/api/v1/models/x/nothing-here"),
    ],
)
def test_an_unknown_api_path_is_a_problem_404_not_the_spa(
    client: TestClient, method: str, path: str
) -> None:
    """#365: a wrong or retired API route is a JSON 404, not index.html with 200 (or a 405)."""
    response = client.request(method, path)
    assert response.status_code == 404
    if method != "HEAD":
        assert response.headers["content-type"] == "application/problem+json"
        assert response.json()["instance"] == path


@pytest.mark.parametrize("path", ["/apiary", "/models/api"])
def test_a_client_route_that_only_looks_like_api_still_gets_the_spa(
    client: TestClient, path: str
) -> None:
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


def test_a_real_api_route_with_the_wrong_method_is_still_a_405(tmp_path: Path) -> None:
    """The mount matches every path, so it also receives a known route's wrong method."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html>", encoding="utf-8")
    app = FastAPI()
    install_problem_handlers(app)

    @app.get("/api/v1/things/{thing}")
    def _thing(thing: str) -> dict[str, str]:
        return {"thing": thing}

    app.mount("/", SPAStaticFiles(dist), name="frontend")
    client = TestClient(app)
    assert client.get("/api/v1/things/a").json() == {"thing": "a"}
    response = client.post("/api/v1/things/a")
    assert response.status_code == 405
    assert response.headers["allow"] == "GET"
    assert response.headers["content-type"] == "application/problem+json"
    assert client.post("/api/v1/nope").status_code == 404

    # A path that names the route only once normalised is told which route it meant,
    # never that the route does not exist (and never redirected: nothing here redirects).
    slash = client.get("/api/v1/things/a/", follow_redirects=False)
    assert slash.status_code == 404
    assert slash.json()["detail"] == (
        "no API route matches GET /api/v1/things/a/; did you mean /api/v1/things/a?"
    )
    missing = client.get("/api/v1/nope/")
    assert missing.status_code == 404
    assert missing.json()["detail"] == "no API route matches GET /api/v1/nope/"
