from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.api.deps import get_catalogue
from scadbuddy.core.problems import ApiError
from scadbuddy.core.settings import Settings
from scadbuddy.library.catalogue import Catalogue
from scadbuddy.main import create_app

INDEX = "<!doctype html><title>ScadBuddy</title>"


def test_a_404_is_an_rfc_9457_document(client: TestClient) -> None:
    response = client.get("/api/v1/models/missing")
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json() == {
        "type": "about:blank",
        "title": "Not Found",
        "status": 404,
        "detail": "no model named 'missing'",
        "instance": "/api/v1/models/missing",
    }


def test_a_client_that_hung_up_gets_a_named_title() -> None:
    """Not the generic "Error": 499 is nginx's, so no library table names it."""
    assert ApiError(499, "gone").title == "Client Closed Request"


def test_a_body_that_does_not_validate_lists_the_offending_fields(client: TestClient) -> None:
    response = client.post("/api/v1/models/demo/render", json={"params": "not a mapping"})
    assert response.status_code == 422
    body = response.json()
    assert body["title"] == "Unprocessable Content"
    assert body["errors"][0]["loc"] == ["body", "params"]


def test_the_wrong_method_is_a_problem_document_too(client: TestClient) -> None:
    response = client.post("/healthz")
    assert response.status_code == 405
    assert response.headers["content-type"] == "application/problem+json"


def test_an_unhandled_error_becomes_a_500_problem(app: FastAPI) -> None:
    def explode() -> Catalogue:
        raise RuntimeError("boom")

    app.dependency_overrides[get_catalogue] = explode
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/v1/models")

    assert response.status_code == 500
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["detail"] == "RuntimeError: boom"


@pytest.fixture
def frontend(tmp_path: Path) -> Path:
    directory = tmp_path / "dist"
    (directory / "assets").mkdir(parents=True)
    (directory / "index.html").write_text(INDEX, encoding="utf-8")
    (directory / "assets" / "app.js").write_text("console.log(1)\n", encoding="utf-8")
    return directory


def test_the_spa_is_served_with_a_fallback_for_client_routes(
    frontend: Path, data_dir: Path, seed_dir: Path, fake_openscad: str
) -> None:
    settings = Settings(
        openscad=fake_openscad,
        data_dir=data_dir,
        seed_models_dir=seed_dir,
        frontend_dir=frontend,
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/").text == INDEX
        assert client.get("/assets/app.js").status_code == 200
        # A deep link the router owns, not a file on disk.
        assert client.get("/models/name-keychain").text == INDEX
        # The API still wins the match, and still answers in problem+json.
        assert client.get("/api/v1/models").json() == []
        assert client.get("/healthz").json()["status"] == "ok"
        assert client.get("/api/v1/models/missing").status_code == 404


def test_without_a_bundle_the_api_is_served_alone(client: TestClient) -> None:
    assert client.get("/").status_code == 404
    assert client.get("/api/v1/models").status_code == 200
