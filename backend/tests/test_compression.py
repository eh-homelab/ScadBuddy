"""Responses go out gzipped, and only where gzip helps (#1033)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.responses import Response, StreamingResponse
from starlette.testclient import TestClient

from scadbuddy.api.compression import MINIMUM_SIZE, Compression
from scadbuddy.api.static import SPAStaticFiles
from scadbuddy.core.settings import Settings
from scadbuddy.main import create_app
from scadbuddy.tools.export_openapi import UNUSED_DATABASE_URL, UNUSED_TEMPORAL_ADDRESS

BIG_JS = "export const x = 1;\n" * 2000
BIG_JSON = [{"slug": f"model-{i}", "name": "A model"} for i in range(200)]
GZIP = {"accept-encoding": "gzip, br"}
IDENTITY = {"accept-encoding": "identity"}


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>ScadBuddy</title>", encoding="utf-8")
    (dist / "assets" / "index-abc123.js").write_text(BIG_JS, encoding="utf-8")

    app = FastAPI()

    @app.get("/api/v1/models")
    def models() -> list[dict[str, str]]:
        return BIG_JSON

    @app.get("/api/v1/small")
    def small() -> dict[str, str]:
        return {"ok": "yes"}

    @app.get("/api/v1/download.3mf")
    def three_mf() -> Response:
        return Response(b"PK\x03\x04" + b"\0" * 50_000, media_type="model/3mf")

    @app.get("/api/v1/extra.bin")
    def extra() -> Response:
        return Response(b"x" * 50_000, media_type="application/octet-stream")

    @app.get("/api/v1/events")
    def events() -> StreamingResponse:
        async def body() -> AsyncIterator[bytes]:
            yield b"data: x\n\n" * 500

        return StreamingResponse(body(), media_type="text/event-stream")

    @app.get("/api/v1/encoded")
    def encoded() -> Response:
        return Response(b"x" * 5000, media_type="text/plain", headers={"content-encoding": "br"})

    app.mount("/", SPAStaticFiles(dist), name="frontend")
    app.add_middleware(Compression)
    return TestClient(app)


def test_json_is_gzipped(client: TestClient) -> None:
    response = client.get("/api/v1/models", headers=GZIP)
    assert response.headers["content-encoding"] == "gzip"
    assert "accept-encoding" in response.headers["vary"].lower()
    assert int(response.headers["content-length"]) < len(response.content) / 3
    assert response.json() == BIG_JSON


def test_asset_is_gzipped_and_still_immutable(client: TestClient) -> None:
    response = client.get("/assets/index-abc123.js", headers=GZIP)
    assert response.headers["content-encoding"] == "gzip"
    assert response.headers["cache-control"].endswith("immutable")
    assert response.text == BIG_JS


def test_gzipped_asset_has_a_weak_etag_that_still_revalidates(client: TestClient) -> None:
    etag = client.get("/assets/index-abc123.js", headers=GZIP).headers["etag"]
    assert etag.startswith('W/"')
    again = client.get("/assets/index-abc123.js", headers={**GZIP, "if-none-match": etag})
    assert again.status_code == 304


def test_identity_keeps_the_strong_etag(client: TestClient) -> None:
    response = client.get("/assets/index-abc123.js", headers=IDENTITY)
    assert "content-encoding" not in response.headers
    assert response.headers["etag"].startswith('"')
    assert response.text == BIG_JS


def test_range_is_not_gzipped(client: TestClient) -> None:
    response = client.get("/assets/index-abc123.js", headers={**GZIP, "range": "bytes=0-9"})
    assert response.status_code == 206
    assert "content-encoding" not in response.headers
    assert response.content == BIG_JS.encode()[:10]


def test_small_response_is_not_gzipped(client: TestClient) -> None:
    response = client.get("/api/v1/small", headers=GZIP)
    assert len(response.content) < MINIMUM_SIZE
    assert "content-encoding" not in response.headers


@pytest.mark.parametrize("path", ["/api/v1/download.3mf", "/api/v1/extra.bin", "/api/v1/events"])
def test_compressed_or_streamed_types_pass_through(client: TestClient, path: str) -> None:
    response = client.get(path, headers=GZIP)
    assert "content-encoding" not in response.headers


def test_an_encoded_response_is_left_alone(client: TestClient) -> None:
    # Streamed and never read: the body is not real brotli for httpx to decode.
    with client.stream("GET", "/api/v1/encoded", headers=GZIP) as response:
        assert response.headers["content-encoding"] == "br"


def test_the_app_installs_it() -> None:
    app = create_app(
        Settings(
            frontend_dir=Path("/nonexistent"),
            database_url=UNUSED_DATABASE_URL,
            temporal_address=UNUSED_TEMPORAL_ADDRESS,
        )
    )
    assert Compression in [cast(object, m.cls) for m in app.user_middleware]
