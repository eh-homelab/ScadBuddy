"""Built-in templates: mirrored from the image, addressed as ``builtin:<slug>``, read-only."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.core.paths import DataPaths
from tests.api.conftest import wait_for_job

SLUG = "name-keychain"
ID = f"builtin:{SLUG}"
# Binary to git, as every real PNG is (its IHDR length alone carries NULs).
PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
SOURCE = 'width = 10;\nlabel = "hi";\n'


@pytest.fixture
def bundled(seed_dir: Path) -> Path:
    directory = seed_dir / SLUG
    directory.mkdir()
    (directory / "model.scad").write_text(SOURCE, encoding="utf-8")
    (directory / "model.json").write_text(json.dumps({"name": "Name keychain"}), encoding="utf-8")
    (directory / "thumbnail.png").write_bytes(PNG_BYTES)
    return directory


@pytest.fixture
def client(app: FastAPI, bundled: Path) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def test_a_fresh_install_lists_the_builtin(client: TestClient) -> None:
    body = client.get("/api/v1/models").json()

    assert [(row["slug"], row["origin"], row["name"]) for row in body] == [
        (ID, "builtin", "Name keychain")
    ]


def test_a_template_of_mine_says_so(client: TestClient, model: str) -> None:
    assert client.get(f"/api/v1/models/{model}").json()["origin"] == "mine"


@pytest.mark.parametrize("spelling", [ID, "builtin%3Aname-keychain"])
def test_the_read_routes_take_a_builtin_id(client: TestClient, spelling: str) -> None:
    record = client.get(f"/api/v1/models/{spelling}")
    assert record.status_code == 200, record.text
    assert record.json()["slug"] == ID
    assert record.json()["has_thumbnail"] is True

    assert client.get(f"/api/v1/models/{spelling}/source").text == SOURCE
    schema = client.get(f"/api/v1/models/{spelling}/schema")
    assert [p["name"] for p in schema.json()["parameters"]] == ["width", "label"]
    assert client.get(f"/api/v1/models/{spelling}/thumbnail").content == PNG_BYTES


def test_an_unknown_builtin_is_a_404(client: TestClient) -> None:
    assert client.get("/api/v1/models/builtin:missing").status_code == 404
    # Mine and built-in are separate namespaces: the bare slug is not the built-in.
    assert client.get(f"/api/v1/models/{SLUG}").status_code == 404


def test_a_malformed_id_is_a_422(client: TestClient) -> None:
    for bad in ("builtin:", "builtin:Bad", "other:name", "builtin:builtin:x"):
        assert client.get(f"/api/v1/models/{bad}").status_code == 422, bad


def test_the_derived_caches_stay_out_of_the_mirror(client: TestClient, paths: DataPaths) -> None:
    assert client.get(f"/api/v1/models/{ID}/schema").status_code == 200

    assert paths.model_schema_cache(ID) == paths.cache / "schema" / "_builtin-name-keychain.json"
    assert paths.model_schema_cache(ID).is_file()
    assert not (paths.models / "_builtin" / SLUG / "schema.json").exists()


def test_a_builtin_renders_and_saves_an_output(client: TestClient, paths: DataPaths) -> None:
    accepted = client.post(f"/api/v1/models/{ID}/render", json={"params": {"width": 12}})
    assert accepted.status_code == 202, accepted.text
    job = wait_for_job(client, accepted.json()["job_id"])
    assert job["status"] == "done", job["error"]
    assert job["slug"] == ID

    created = client.post(f"/api/v1/models/{ID}/outputs", json={"job_id": job["id"], "name": "A"})
    assert created.status_code == 201, created.text
    output = created.json()
    assert output["slug"] == ID
    assert (paths.outputs / "_builtin-name-keychain" / output["id"]).is_dir()
    assert [row["id"] for row in client.get(f"/api/v1/models/{ID}/outputs").json()] == [
        output["id"]
    ]
    assert client.get(f"/api/v1/outputs/{output['id']}").status_code == 200
    download = client.get(f"/api/v1/outputs/{output['id']}/model.3mf")
    # The bare slug: a ':' is no character for a filename.
    assert 'filename="name-keychain-a.3mf"' in download.headers["content-disposition"]
    assert client.get(f"/api/v1/outputs/{output['id']}/edit").json()["slug"] == ID


def test_the_write_routes_refuse_a_builtin(client: TestClient, paths: DataPaths) -> None:
    refused = [
        client.put(f"/api/v1/models/{ID}/source", json={"source": "cube(1);\n"}),
        client.patch(f"/api/v1/models/{ID}", json={"name": "Renamed"}),
        client.delete(f"/api/v1/models/{ID}"),
        client.post(f"/api/v1/models/{ID}/versions/{'0' * 40}/restore"),
    ]

    assert [response.status_code for response in refused] == [403] * 4
    assert refused[0].headers["content-type"] == "application/problem+json"
    assert paths.model_source(ID).read_text(encoding="utf-8") == SOURCE
    assert client.get(f"/api/v1/models/{ID}").json()["name"] == "Name keychain"


def test_the_parse_check_resolves_includes_against_a_builtin(client: TestClient) -> None:
    response = client.post("/api/v1/models/check", json={"source": SOURCE, "slug": ID})
    assert response.status_code == 200, response.text


@pytest.mark.requires_git
def test_a_builtins_history_is_its_own(client: TestClient, model: str) -> None:
    listed = client.get(f"/api/v1/models/{ID}/versions").json()

    assert [entry["message"] for entry in listed] == ["Sync built-in templates from the image"]
    # Relative to the template's own directory, as for mine.
    assert sorted(change["path"] for change in listed[0]["files"]) == [
        "model.json",
        "model.scad",
        "thumbnail.png",
    ]
    commit = listed[0]["commit"]
    assert client.get(f"/api/v1/models/{ID}").json()["version"] == commit
    assert client.get(f"/api/v1/models/{ID}/versions/{commit}/source").text == SOURCE
    diff = client.get(f"/api/v1/models/{ID}/versions/{commit}/diff").json()
    assert diff["slug"] == ID
    assert "model.scad" in [change["path"] for change in diff["files"]]
    assert "+width = 10;" in diff["patch"]
    schema = client.get(f"/api/v1/models/{ID}/versions/{commit}/schema")
    assert schema.status_code == 200, schema.text
    # Mine is untouched by the built-in's history, and vice versa.
    mine = client.get(f"/api/v1/models/{model}/versions").json()
    assert commit not in [entry["commit"] for entry in mine]
