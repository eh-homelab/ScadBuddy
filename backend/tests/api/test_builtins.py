"""Built-in templates: mirrored from the image into ``_builtin/``, addressed as
``builtin:<slug>``, readable and renderable everywhere, writable nowhere (#155)."""

from __future__ import annotations

import json
import shutil
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.core.paths import DataPaths
from scadbuddy.core.settings import Settings
from scadbuddy.library.history import ModelHistory
from scadbuddy.library.slugs import MAX_SLUG_LENGTH
from scadbuddy.main import create_app
from scadbuddy.render.bambu3mf import PLATE_THUMBNAIL
from tests.api.conftest import PNG_BYTES, job_file, wait_for_job

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


@pytest.mark.requires_postgres
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


@pytest.mark.requires_postgres
def test_a_built_in_and_a_same_slug_template_of_mine_stay_apart(
    client: TestClient, paths: DataPaths
) -> None:
    """`builtin:keychain` and `keychain` share a slug, never jobs, outputs or history (#206)."""
    created = client.post("/api/v1/models", json={"name": "keychain", "source": "width = 5;\n"})
    assert created.status_code == 201, created.text
    mine = created.json()["slug"]
    assert mine == "keychain"

    jobs = {model_id: _finished_job(client, model_id) for model_id in (BUILTIN, mine)}
    assert {
        model_id: client.get(f"/api/v1/jobs/{job}").json()["slug"] for model_id, job in jobs.items()
    } == {
        BUILTIN: BUILTIN,
        mine: mine,
    }
    outputs = {}
    for model_id, job_id in jobs.items():
        response = client.post(
            f"/api/v1/models/{model_id}/outputs", json={"job_id": job_id, "name": "Blue"}
        )
        assert response.status_code == 201, response.text
        outputs[model_id] = response.json()

    for model_id, output in outputs.items():
        assert output["slug"] == model_id
        assert output["model_version"] == _versions(client, model_id)[0]["commit"]
        listed = client.get(f"/api/v1/models/{model_id}/outputs").json()
        assert [row["id"] for row in listed] == [output["id"]]
        assert (paths.outputs / model_id / output["id"]).is_dir()
    assert outputs[BUILTIN]["model_version"] != outputs[mine]["model_version"]
    assert [entry["message"] for entry in _versions(client, BUILTIN)] == [
        "Sync built-in templates from the image"
    ]
    assert "Sync built-in templates from the image" not in [
        entry["message"] for entry in _versions(client, mine)
    ]
    assert client.get(f"/api/v1/models/{BUILTIN}/source").text == SOURCE
    assert client.get(f"/api/v1/models/{mine}/source").text == "width = 5;\n"


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


# ── #179's details against a built-in (its writes are 403, above) ────────────


def test_a_built_ins_details_are_readable(client: TestClient, bundled: Path) -> None:
    record = client.get(f"/api/v1/models/{BUILTIN}").json()
    assert (record["thumbnail_source"], record["has_readme"]) == ("model", False)
    assert client.get(f"/api/v1/models/{BUILTIN}/thumbnail").content == THUMBNAIL
    assert client.get(f"/api/v1/models/{BUILTIN}/readme").status_code == 404


def test_a_built_ins_readme_is_served(app: FastAPI, bundled: Path) -> None:
    (bundled / "README.md").write_text("# Keychain\n", encoding="utf-8")
    with TestClient(app) as client:
        assert client.get(f"/api/v1/models/{BUILTIN}").json()["has_readme"] is True
        assert client.get(f"/api/v1/models/{BUILTIN}/readme").text == "# Keychain\n"


@pytest.mark.parametrize(
    "origin_url", ["javascript:alert(document.domain)", "https://example.com/keychain.scad"]
)
def test_a_built_ins_model_json_never_sets_origin_url(
    app: FastAPI, bundled: Path, paths: DataPaths, origin_url: str
) -> None:
    """Only `POST /models/import` sets it (#179). The mirror keeps the image's bytes,
    so the next boot's sync still sees nothing to change."""
    meta = {"name": "Keychain", "origin_url": origin_url}
    (bundled / "model.json").write_text(json.dumps(meta), encoding="utf-8")
    with TestClient(app) as client:
        assert client.get(f"/api/v1/models/{BUILTIN}").json()["origin_url"] is None
        listed = {model["slug"]: model for model in client.get("/api/v1/models").json()}
        assert listed[BUILTIN]["origin_url"] is None
    assert json.loads(paths.model_meta(BUILTIN).read_text(encoding="utf-8")) == meta


def test_a_built_in_without_a_thumbnail_shows_its_first_plate_image(
    app: FastAPI, bundled: Path, paths: DataPaths
) -> None:
    (bundled / "thumbnail.png").unlink()
    cover = PNG_BYTES + b"plate"
    with TestClient(app) as client:
        assert client.get(f"/api/v1/models/{BUILTIN}").json()["has_thumbnail"] is False
        job_id = _finished_job(client, BUILTIN)
        with zipfile.ZipFile(job_file(paths, job_id, "model.3mf"), "a") as archive:
            archive.writestr(PLATE_THUMBNAIL, cover)
        saved = client.post(f"/api/v1/models/{BUILTIN}/outputs", json={"job_id": job_id})
        assert saved.status_code == 201, saved.text

        record = client.get(f"/api/v1/models/{BUILTIN}").json()
        assert record["thumbnail_source"] == "output"
        assert client.get(f"/api/v1/models/{BUILTIN}/thumbnail").content == cover


def test_linking_a_seeded_template_drops_an_origin_url_its_image_carried(
    settings: Settings, bundled: Path, paths: DataPaths
) -> None:
    """Linked, a seeded copy is a duplicate of the built-in, and a duplicate never
    carries a link its image's model.json supplied (#179); the rest of it stays."""
    meta = {"name": "Keychain", "origin_url": "javascript:alert(document.domain)"}
    (bundled / "model.json").write_text(json.dumps(meta), encoding="utf-8")
    history = ModelHistory(paths.models)
    history.ensure_repo()
    shutil.copytree(bundled, paths.model_dir("keychain"))
    history.commit("Seed keychain from the image", "keychain")

    with TestClient(create_app(settings)) as client:
        model = client.get("/api/v1/models/keychain").json()
        assert (model["name"], model["origin_url"]) == ("Keychain", None)
        assert model["upstream"]["id"] == BUILTIN
        # Its own thumbnail came with the seed, and is still its own.
        assert model["thumbnail_source"] == "model"
    assert "origin_url" not in json.loads(paths.model_meta("keychain").read_text("utf-8"))


def test_boot_links_a_template_the_old_seed_copied_in(
    settings: Settings, bundled: Path, paths: DataPaths
) -> None:
    """An install seeded before #155: its copy becomes a duplicate of the built-in (#158)."""
    history = ModelHistory(paths.models)
    history.ensure_repo()
    shutil.copytree(bundled, paths.model_dir("keychain"))
    seed = history.commit("Seed keychain from the image", "keychain")

    with TestClient(create_app(settings)) as client:
        model = client.get("/api/v1/models/keychain").json()
        assert model["origin"] == "mine"
        assert model["upstream"] == {
            "id": BUILTIN,
            "path": "keychain",
            "base": seed,
            "dismissed": None,
        }
        assert _versions(client, "keychain")[0]["message"] == (
            "Link seeded templates to their built-ins"
        )
    head = history.head()

    # A second boot finds nothing to link and commits nothing.
    with TestClient(create_app(settings)):
        pass
    assert history.head() == head


def test_the_boot_survives_a_built_in_with_an_invalid_model_json(
    app: FastAPI, bundled: Path
) -> None:
    """The sync mirrors it as it is; the listing leaves it out; reading it names why."""
    (bundled / "model.json").write_text('{"name": "Keychain", "tags": 5}', encoding="utf-8")
    with TestClient(app) as client:
        listing = client.get("/api/v1/models")
        assert listing.status_code == 200
        assert BUILTIN not in {model["slug"] for model in listing.json()}
        response = client.get(f"/api/v1/models/{BUILTIN}")
        assert response.status_code == 409
        assert "tags" in response.json()["detail"]
