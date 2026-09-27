"""Built-in templates: mirrored from the image into ``_builtin/``, addressed as
``builtin:<slug>``, readable and renderable everywhere, writable nowhere (#155)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.core.paths import DataPaths
from scadbuddy.core.settings import Settings
from scadbuddy.library.slugs import MAX_SLUG_LENGTH
from scadbuddy.main import create_app
from tests.api.conftest import PNG_BYTES, wait_for_job

pytestmark = pytest.mark.requires_git

BUILTIN = "builtin:keychain"
SOURCE = 'width = 10;\nlabel = "hi";\n'
# A NUL, as every real PNG has: without one git takes the file for text and
# `git diff` emits bytes that are not UTF-8.
THUMBNAIL = PNG_BYTES + b"\x00"


@pytest.fixture
def bundled(seed_dir: Path) -> Path:
    directory = seed_dir / "keychain"
    directory.mkdir()
    (directory / "model.scad").write_text(SOURCE, encoding="utf-8")
    (directory / "model.json").write_text(json.dumps({"name": "Keychain"}), encoding="utf-8")
    (directory / "thumbnail.png").write_bytes(THUMBNAIL)
    return directory


@pytest.fixture
def client(app: FastAPI, bundled: Path) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def _finished_job(client: TestClient, model_id: str) -> str:
    response = client.post(f"/api/v1/models/{model_id}/render", json={"params": {}})
    assert response.status_code == 202, response.text
    job_id: str = response.json()["job_id"]
    assert wait_for_job(client, job_id)["status"] == "done"
    return job_id


def _versions(client: TestClient, model_id: str) -> list[dict[str, Any]]:
    response = client.get(f"/api/v1/models/{model_id}/versions")
    assert response.status_code == 200, response.text
    listed: list[dict[str, Any]] = response.json()
    return listed


def test_the_list_carries_each_templates_origin(client: TestClient, model: str) -> None:
    listed = {row["slug"]: row for row in client.get("/api/v1/models").json()}

    assert {slug: row["origin"] for slug, row in listed.items()} == {
        BUILTIN: "builtin",
        model: "mine",
    }
    assert listed[BUILTIN]["name"] == "Keychain"
    assert listed[BUILTIN]["version"] is not None


def test_every_read_route_takes_a_built_in(client: TestClient) -> None:
    record = client.get(f"/api/v1/models/{BUILTIN}")
    assert record.status_code == 200
    assert record.json()["origin"] == "builtin"
    # As the frontend sends it: `encodeURIComponent` escapes the `:`.
    assert client.get("/api/v1/models/builtin%3Akeychain").json() == record.json()
    assert client.get(f"/api/v1/models/{BUILTIN}/source").text == SOURCE
    schema = client.get(f"/api/v1/models/{BUILTIN}/schema")
    assert schema.status_code == 200
    assert [p["name"] for p in schema.json()["parameters"]] == ["width", "label"]
    assert client.get(f"/api/v1/models/{BUILTIN}/thumbnail").content == THUMBNAIL


def test_a_built_in_renders_and_keeps_its_outputs(client: TestClient, paths: DataPaths) -> None:
    job_id = _finished_job(client, BUILTIN)

    created = client.post(
        f"/api/v1/models/{BUILTIN}/outputs", json={"job_id": job_id, "name": "Blue"}
    )
    assert created.status_code == 201, created.text
    output = created.json()
    assert output["slug"] == BUILTIN
    assert output["model_version"] == _versions(client, BUILTIN)[0]["commit"]
    assert [row["id"] for row in client.get(f"/api/v1/models/{BUILTIN}/outputs").json()] == [
        output["id"]
    ]
    # `:` has no business in a file name a browser saves.
    download = client.get(f"/api/v1/outputs/{output['id']}/model.3mf")
    assert 'filename="keychain-blue.3mf"' in download.headers["content-disposition"]


def test_a_built_ins_history_is_its_own(client: TestClient, model: str) -> None:
    listed = _versions(client, BUILTIN)

    assert [entry["message"] for entry in listed] == ["Sync built-in templates from the image"]
    assert listed[0]["current"] is True
    # Relative to the built-in's own directory, as for a template of mine.
    assert sorted(change["path"] for change in listed[0]["files"]) == [
        "model.json",
        "model.scad",
        "thumbnail.png",
    ]
    commit = listed[0]["commit"]
    assert client.get(f"/api/v1/models/{BUILTIN}/versions/{commit}/source").text == SOURCE
    diff = client.get(f"/api/v1/models/{BUILTIN}/versions/{commit}/diff").json()
    assert "+width = 10;" in diff["patch"]
    assert {change["path"] for change in diff["files"]} == {
        "model.json",
        "model.scad",
        "thumbnail.png",
    }


def test_a_built_ins_diff_never_names_the_mirror(client: TestClient) -> None:
    commit = _versions(client, BUILTIN)[0]["commit"]

    patch = client.get(f"/api/v1/models/{BUILTIN}/versions/{commit}/diff").json()["patch"]

    assert "_builtin/" not in patch
    # The headers a template of mine with the same slug would show.
    assert "diff --git a/keychain/model.scad b/keychain/model.scad" in patch
    assert "+++ b/keychain/model.scad" in patch


@pytest.mark.parametrize(
    ("method", "suffix", "body"),
    [
        ("PUT", "/source", {"source": "cube(1);\n", "force": True}),
        ("PATCH", "", {"name": "Mine now"}),
        ("DELETE", "", None),
        ("POST", "/versions/{commit}/restore", None),
        ("DELETE", "/thumbnail", None),
        ("PUT", "/readme", {"content": "# Mine now\n"}),
        ("DELETE", "/readme", None),
    ],
)
def test_a_built_in_cannot_be_changed(
    client: TestClient, seed_dir: Path, method: str, suffix: str, body: dict[str, Any] | None
) -> None:
    commit = _versions(client, BUILTIN)[0]["commit"]

    response = client.request(
        method, f"/api/v1/models/{BUILTIN}{suffix.format(commit=commit)}", json=body
    )

    assert response.status_code == 403, response.text
    assert "built-in" in response.json()["detail"]
    assert client.get(f"/api/v1/models/{BUILTIN}/source").text == SOURCE
    assert len(_versions(client, BUILTIN)) == 1


def test_a_built_ins_thumbnail_cannot_be_replaced(client: TestClient) -> None:
    before = client.get(f"/api/v1/models/{BUILTIN}/thumbnail").content
    png = b"\x89PNG\r\n\x1a\n" + b"\0" * 16

    response = client.put(
        f"/api/v1/models/{BUILTIN}/thumbnail", files={"file": ("t.png", png, "image/png")}
    )

    assert response.status_code == 403, response.text
    assert client.get(f"/api/v1/models/{BUILTIN}/thumbnail").content == before
    assert len(_versions(client, BUILTIN)) == 1


def test_a_changed_image_lands_as_one_sync_commit(
    client: TestClient, settings: Settings, bundled: Path
) -> None:
    (bundled / "model.scad").write_text("width = 20;\n", encoding="utf-8")

    # A restart on the new image.
    with TestClient(create_app(settings)) as client:
        assert client.get(f"/api/v1/models/{BUILTIN}/source").text == "width = 20;\n"
        assert [entry["message"] for entry in _versions(client, BUILTIN)] == [
            "Sync built-in templates from the image",
            "Sync built-in templates from the image",
        ]


def test_a_built_ins_derived_files_live_while_it_does(
    app: FastAPI, settings: Settings, bundled: Path, paths: DataPaths
) -> None:
    with TestClient(app) as client:
        _finished_job(client, BUILTIN)
        client.get(f"/api/v1/models/{BUILTIN}/schema")
        (paths.outputs / BUILTIN / "deadbeef").mkdir(parents=True)
        paths.model_revision_dir(BUILTIN, "0" * 40).mkdir(parents=True)
        catalogue = client.app.state.scadbuddy.catalogue  # type: ignore[attr-defined]
        assert catalogue.sweep_orphans() == []
        assert paths.model_schema_cache(BUILTIN).is_file()

    # Dropped from the image: the next boot removes it, and its leftovers with it.
    for child in bundled.iterdir():
        child.unlink()
    bundled.rmdir()
    with TestClient(create_app(settings)) as client:
        assert client.get(f"/api/v1/models/{BUILTIN}").status_code == 404
        assert not (paths.outputs / BUILTIN).exists()
        assert not (paths.model_revisions / BUILTIN).exists()
        assert not paths.model_schema_cache(BUILTIN).exists()


def test_the_mirror_is_never_a_template_of_mine(client: TestClient) -> None:
    assert client.get("/api/v1/models/_builtin").status_code == 422
    assert client.get("/api/v1/models/builtin:_builtin").status_code == 422

    created = client.post("/api/v1/models", json={"name": "_builtin", "source": SOURCE})

    assert created.status_code == 201, created.text
    assert created.json()["slug"] == "builtin"
    assert created.json()["origin"] == "mine"
    assert sorted(row["slug"] for row in client.get("/api/v1/models").json()) == [
        "builtin",
        BUILTIN,
    ]


def test_the_id_of_a_longest_legal_slug_is_not_refused_for_length(client: TestClient) -> None:
    slug = "x" * MAX_SLUG_LENGTH

    assert client.get(f"/api/v1/models/builtin:{slug}").status_code == 404
    assert client.get(f"/api/v1/models/builtin:{slug}x").status_code == 422
