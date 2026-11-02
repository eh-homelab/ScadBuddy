"""The git remote's settings and its lag (#4974): the token is write-only, stored only
sealed under the KEK, and never in any read."""

from __future__ import annotations

import base64
import os
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from scadbuddy.core.settings import Settings
from scadbuddy.main import create_app

TOKEN = "glpat-s3cret-token-4974"
URL = "https://git.invalid/scadbuddy/history.git"


@pytest.fixture
def keyed_client(settings: Settings, tmp_path: Path) -> Iterator[TestClient]:
    kek = tmp_path / "kek"
    kek.write_text(base64.b64encode(os.urandom(32)).decode())
    with TestClient(create_app(settings.model_copy(update={"secret_key_file": kek}))) as client:
        yield client


def test_no_remote_is_set_by_default(client: TestClient) -> None:
    body = client.get("/api/v1/settings").json()
    assert (body["git_remote_url"], body["git_remote_username"]) == (None, None)
    assert body["has_git_remote_token"] is False
    assert "git_remote_token" not in body
    assert client.get("/api/v1/settings/git-remote").json() == {
        "configured": False,
        "repositories": [],
    }


def test_a_token_is_refused_without_the_kek(client: TestClient) -> None:
    response = client.put(
        "/api/v1/settings", json={"git_remote_url": URL, "git_remote_token": TOKEN}
    )
    assert response.status_code == 422
    assert "SCADBUDDY_SECRET_KEY_FILE" in response.text
    assert TOKEN not in response.text
    assert client.get("/api/v1/settings").json()["git_remote_url"] is None


def test_a_url_with_a_credential_is_refused_without_echoing_it(client: TestClient) -> None:
    response = client.put(
        "/api/v1/settings", json={"git_remote_url": f"https://me:{TOKEN}@git.invalid/r.git"}
    )
    assert response.status_code == 422
    assert TOKEN not in response.text


def test_the_token_is_sealed_and_never_read_back(
    keyed_client: TestClient, settings: Settings
) -> None:
    response = keyed_client.put(
        "/api/v1/settings",
        json={"git_remote_url": URL, "git_remote_username": "me", "git_remote_token": TOKEN},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["git_remote_url"], body["git_remote_username"]) == (URL, "me")
    assert body["has_git_remote_token"] is True
    assert TOKEN not in response.text
    for path in ("/api/v1/settings", "/api/v1/settings/git-remote"):
        assert TOKEN not in keyed_client.get(path).text
    with psycopg.connect(settings.database_url) as conn:
        row = conn.execute("SELECT value FROM settings WHERE name = 'git_remote_token'").fetchone()
    assert row is not None and "sealed" in row[0] and TOKEN not in str(row[0])
    # `""` clears it.
    cleared = keyed_client.put("/api/v1/settings", json={"git_remote_token": ""}).json()
    assert cleared["has_git_remote_token"] is False


def test_the_lag_names_both_repositories(client: TestClient) -> None:
    """Nothing was ever pushed to this remote, so every commit is behind."""
    assert client.put("/api/v1/settings", json={"git_remote_url": URL}).status_code == 200
    status = client.get("/api/v1/settings/git-remote").json()
    assert status["configured"] is True
    by_name = {repo["repository"]: repo for repo in status["repositories"]}
    assert set(by_name) == {"models", "flows"}
    models = by_name["models"]
    assert (models["branch"], models["pushed_commit"]) == ("models", None)
    assert models["head"] is not None and models["behind"] >= 1
    assert models["oldest_unpushed_at"] is not None
