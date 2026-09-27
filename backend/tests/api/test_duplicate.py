"""Duplicating a template, built-in or mine, records its upstream (#156)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.core.paths import DataPaths
from tests.api.conftest import PNG_BYTES

pytestmark = pytest.mark.requires_git

BUILTIN = "builtin:name-keychain"
SOURCE = 'width = 10;\nlabel = "hi";\n'
THUMBNAIL = PNG_BYTES + b"\x00"


@pytest.fixture
def bundled(seed_dir: Path) -> Path:
    directory = seed_dir / "name-keychain"
    directory.mkdir()
    (directory / "model.scad").write_text(SOURCE, encoding="utf-8")
    (directory / "model.json").write_text(
        json.dumps({"name": "Name keychain", "tags": ["keychain"], "libraries": ["BOSL2"]}),
        encoding="utf-8",
    )
    (directory / "thumbnail.png").write_bytes(THUMBNAIL)
    return directory


@pytest.fixture
def client(app: FastAPI, bundled: Path) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def _versions(client: TestClient, model_id: str) -> list[dict[str, Any]]:
    response = client.get(f"/api/v1/models/{model_id}/versions")
    assert response.status_code == 200, response.text
    listed: list[dict[str, Any]] = response.json()
    return listed


def _duplicate(client: TestClient, model_id: str, name: str) -> dict[str, Any]:
    response = client.post(f"/api/v1/models/{model_id}/duplicate", json={"name": name})
    assert response.status_code == 201, response.text
    record: dict[str, Any] = response.json()
    return record


def test_a_duplicate_of_a_built_in_is_mine_and_points_at_it(client: TestClient) -> None:
    builtin_version = client.get(f"/api/v1/models/{BUILTIN}").json()["version"]

    record = _duplicate(client, BUILTIN, "My keychain")

    assert record["slug"] == "my-keychain"
    assert record["origin"] == "mine"
    assert record["name"] == "My keychain"
    assert record["tags"] == ["keychain"]
    assert record["has_thumbnail"] is True
    assert record["upstream"] == {
        "id": BUILTIN,
        "path": "_builtin/name-keychain",
        "base": builtin_version,
        "dismissed": None,
    }
    assert client.get("/api/v1/models/my-keychain/source").text == SOURCE
    assert client.get("/api/v1/models/my-keychain/thumbnail").content == THUMBNAIL
    assert [entry["message"] for entry in _versions(client, "my-keychain")] == [
        f"Duplicate {BUILTIN} as my-keychain"
    ]
    # Editable, where the built-in is not.
    edited = client.put(
        "/api/v1/models/my-keychain/source", json={"source": "width = 20;\n", "force": True}
    )
    assert edited.status_code == 200, edited.text
    assert client.get(f"/api/v1/models/{BUILTIN}/source").text == SOURCE


def test_a_duplicate_of_a_duplicate_tracks_its_immediate_parent(client: TestClient) -> None:
    _duplicate(client, BUILTIN, "My keychain")
    client.put("/api/v1/models/my-keychain/source", json={"source": "cube(1);\n", "force": True})
    parent_version = client.get("/api/v1/models/my-keychain").json()["version"]

    record = _duplicate(client, "my-keychain", "Another keychain")

    assert record["upstream"] == {
        "id": "my-keychain",
        "path": "my-keychain",
        "base": parent_version,
        "dismissed": None,
    }
    assert client.get("/api/v1/models/another-keychain/source").text == "cube(1);\n"


def test_a_duplicate_keeps_the_rest_of_model_json(client: TestClient, paths: DataPaths) -> None:
    """A library declaration (#93), or anything else the metadata carries, travels along."""
    _duplicate(client, BUILTIN, "My keychain")

    stored = json.loads(paths.model_meta("my-keychain").read_text(encoding="utf-8"))

    assert stored["libraries"] == ["BOSL2"]
    assert stored["upstream"]["id"] == BUILTIN


def test_a_metadata_edit_never_clobbers_the_upstream(client: TestClient) -> None:
    upstream = _duplicate(client, BUILTIN, "My keychain")["upstream"]

    patched = client.patch(
        "/api/v1/models/my-keychain", json={"name": "Renamed", "description": "mine now"}
    )

    assert patched.status_code == 200, patched.text
    assert patched.json()["name"] == "Renamed"
    assert patched.json()["upstream"] == upstream
    assert client.get("/api/v1/models/my-keychain").json()["upstream"] == upstream


def test_a_patch_cannot_set_the_upstream(client: TestClient, model: str) -> None:
    client.patch(
        f"/api/v1/models/{model}",
        json={"upstream": {"id": BUILTIN, "path": "x", "base": None}},
    )

    assert client.get(f"/api/v1/models/{model}").json()["upstream"] is None


def test_derived_state_is_not_copied(client: TestClient, paths: DataPaths) -> None:
    assert client.get(f"/api/v1/models/{BUILTIN}/schema").status_code == 200
    assert paths.model_schema_cache(BUILTIN).is_file()
    (paths.outputs / BUILTIN / "deadbeef").mkdir(parents=True)
    # What an earlier model of the new slug left behind.
    stale_output = paths.outputs / "my-keychain" / "cafe"
    stale_output.mkdir(parents=True)
    paths.model_schema_cache("my-keychain").write_text("{}", encoding="utf-8")
    paths.model_revision_dir("my-keychain", "0" * 40).mkdir(parents=True)

    _duplicate(client, BUILTIN, "My keychain")

    assert not (paths.outputs / "my-keychain").exists()
    assert not paths.model_schema_cache("my-keychain").exists()
    assert not (paths.model_revisions / "my-keychain").exists()
    assert client.get("/api/v1/models/my-keychain/outputs").json() == []
    assert (paths.outputs / BUILTIN / "deadbeef").is_dir()
    assert not any(path.name.startswith("duplicate-") for path in paths.cache.iterdir())


def test_a_name_is_refused_as_on_create(client: TestClient, model: str) -> None:
    invalid = client.post(f"/api/v1/models/{BUILTIN}/duplicate", json={"name": "!!!"})
    assert invalid.status_code == 422
    assert (
        invalid.json()["detail"]
        == client.post("/api/v1/models", json={"name": "!!!", "source": SOURCE}).json()["detail"]
    )

    taken = client.post(f"/api/v1/models/{BUILTIN}/duplicate", json={"name": "Demo"})
    assert taken.status_code == 409
    assert "'demo' already exists" in taken.json()["detail"]
    assert client.get(f"/api/v1/models/{model}").json()["upstream"] is None


def test_an_unknown_template_is_a_404(client: TestClient) -> None:
    for model_id in ("nope", "builtin:nope"):
        response = client.post(f"/api/v1/models/{model_id}/duplicate", json={"name": "Copy"})
        assert response.status_code == 404
    assert client.get("/api/v1/models/copy").status_code == 404
