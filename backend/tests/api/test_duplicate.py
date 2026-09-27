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
from scadbuddy.library.catalogue import Catalogue, ModelMeta
from scadbuddy.library.history import GitTimeoutError, ModelHistory
from scadbuddy.render.solids import WRAPPER_PREFIX
from tests.api.conftest import PNG_BYTES

pytestmark = pytest.mark.requires_git

BUILTIN = "builtin:name-keychain"
SOURCE = 'width = 10;\nlabel = "hi";\n'
THUMBNAIL = PNG_BYTES + b"\x00"


@pytest.fixture
def bundled_meta() -> dict[str, Any]:
    # No library declared: one without a pin refuses the source writes and schema
    # reads these tests make (#93). The test that needs one parametrizes this.
    return {"name": "Name keychain", "tags": ["keychain"]}


@pytest.fixture
def bundled(seed_dir: Path, bundled_meta: dict[str, Any]) -> Path:
    directory = seed_dir / "name-keychain"
    directory.mkdir()
    (directory / "model.scad").write_text(SOURCE, encoding="utf-8")
    (directory / "model.json").write_text(json.dumps(bundled_meta), encoding="utf-8")
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


@pytest.mark.parametrize(
    "bundled_meta", [{"name": "Name keychain", "tags": ["keychain"], "libraries": ["BOSL2"]}]
)
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


def test_a_duplicate_is_the_revision_it_records_as_base(
    client: TestClient, paths: DataPaths
) -> None:
    """The copy comes from `base`, not the working tree: a source write that lands
    after `base` was read must not show up in a copy that claims the older one."""
    mine = _duplicate(client, BUILTIN, "Mine")
    # Uncommitted, as a PUT in flight between reading `base` and copying would be.
    paths.model_source(mine["slug"]).write_text("width = 99;\n", encoding="utf-8")

    again = _duplicate(client, mine["slug"], "Again")

    assert again["upstream"]["base"] == mine["version"]
    assert client.get(f"/api/v1/models/{again['slug']}/source").text == SOURCE


def _no_staging_left(paths: DataPaths) -> bool:
    return not any(path.name.startswith("duplicate-") for path in paths.cache.iterdir())


def _interrupt_first_claim(monkeypatch: pytest.MonkeyPatch, interloper: Any) -> None:
    """Run ``interloper`` once, right after the first slug claim, before its writer
    fills or renames into the directory: the window two racing requests share."""
    claim = Catalogue._claim
    fired: list[bool] = []

    def claiming(self: Catalogue, slug: str) -> Path:
        directory = claim(self, slug)
        if not fired:
            fired.append(True)
            interloper(directory)
        return directory

    monkeypatch.setattr(Catalogue, "_claim", claiming)


def test_a_create_racing_a_duplicate_for_its_slug_is_a_409(
    client: TestClient, paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    raced: list[Any] = []
    _interrupt_first_claim(
        monkeypatch,
        lambda _: raced.append(
            client.post(
                "/api/v1/models", json={"name": "Copy", "source": "cube(1);\n", "force": True}
            )
        ),
    )

    record = _duplicate(client, BUILTIN, "Copy")

    assert raced[0].status_code == 409, raced[0].text
    assert record["upstream"]["id"] == BUILTIN
    assert client.get("/api/v1/models/copy/source").text == SOURCE
    assert _no_staging_left(paths)


def test_a_duplicate_racing_a_create_for_its_slug_is_a_409(
    client: TestClient, paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    raced: list[Any] = []
    _interrupt_first_claim(
        monkeypatch,
        lambda _: raced.append(
            client.post(f"/api/v1/models/{BUILTIN}/duplicate", json={"name": "Copy"})
        ),
    )

    created = client.post(
        "/api/v1/models", json={"name": "Copy", "source": "cube(1);\n", "force": True}
    )

    assert created.status_code == 201, created.text
    assert raced[0].status_code == 409, raced[0].text
    assert client.get("/api/v1/models/copy/source").text == "cube(1);\n"
    assert client.get("/api/v1/models/copy").json()["upstream"] is None
    assert _no_staging_left(paths)


def test_a_claim_written_into_refuses_the_rename_and_keeps_what_was_written(
    client: TestClient, paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Something that bypassed the claim wrote into it: the rename fails, and what it
    wrote is neither overwritten nor removed with the claim."""
    _interrupt_first_claim(
        monkeypatch,
        lambda directory: (directory / "model.scad").write_text("cube(2);\n", encoding="utf-8"),
    )

    response = client.post(f"/api/v1/models/{BUILTIN}/duplicate", json={"name": "Copy"})

    assert response.status_code == 409, response.text
    assert paths.model_source("copy").read_text(encoding="utf-8") == "cube(2);\n"
    assert _no_staging_left(paths)


def test_a_git_failure_reading_the_upstream_is_a_problem_and_leaves_nothing(
    client: TestClient, paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    def export(self: ModelHistory, slug: str, commit: str, dest: Path) -> None:
        raise GitTimeoutError("git archive timed out after 1s")

    monkeypatch.setattr(ModelHistory, "export", export)

    response = client.post(f"/api/v1/models/{BUILTIN}/duplicate", json={"name": "Copy"})

    assert response.status_code == 500
    assert response.json()["detail"] == "git archive timed out after 1s"
    assert not paths.model_dir("copy").exists()
    assert _no_staging_left(paths)
    # The slug is still free.
    monkeypatch.undo()
    _duplicate(client, BUILTIN, "Copy")


def test_a_git_failure_reading_the_base_fails_the_duplicate(
    client: TestClient, paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not a silent fall back to the working tree with no base recorded."""

    def last_commit(self: ModelHistory, slug: str) -> str:
        raise GitTimeoutError("git log timed out after 1s")

    monkeypatch.setattr(ModelHistory, "last_commit", last_commit)

    response = client.post(f"/api/v1/models/{BUILTIN}/duplicate", json={"name": "Copy"})

    assert response.status_code == 500
    assert response.json()["detail"] == "git log timed out after 1s"
    assert not paths.model_dir("copy").exists()
    assert _no_staging_left(paths)


def test_an_unreadable_base_fails_the_duplicate(
    client: TestClient, paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`git log` failing without a timeout answers None, not an exception; that
    must not read as "no history" and copy the working tree."""
    monkeypatch.setattr(ModelHistory, "last_commit", lambda self, slug: None)

    response = client.post(f"/api/v1/models/{BUILTIN}/duplicate", json={"name": "Copy"})

    assert response.status_code == 500
    assert "could not read the current revision" in response.json()["detail"]
    assert not paths.model_dir("copy").exists()


def test_without_history_a_duplicate_copies_the_working_tree(paths: DataPaths) -> None:
    catalogue = Catalogue(paths)
    catalogue.create(
        "keychain", SOURCE, ModelMeta(name="Keychain", tags=["t"]), thumbnail=THUMBNAIL
    )
    # A source write in flight and a render's colour wrapper, which the copy must
    # both leave out.
    (paths.model_dir("keychain") / ".model-x.scad").write_text("torn", encoding="utf-8")
    (paths.model_dir("keychain") / f"{WRAPPER_PREFIX}0123abcd.scad").write_text(
        "wrapper", encoding="utf-8"
    )

    record = catalogue.duplicate("keychain", "copy", "Copy")

    assert record.version is None
    assert record.upstream is not None
    assert record.upstream.model_dump() == {
        "id": "keychain",
        "path": "keychain",
        "base": None,
        "dismissed": None,
    }
    assert record.name == "Copy"
    assert record.tags == ["t"]
    assert paths.model_source("copy").read_text(encoding="utf-8") == SOURCE
    assert catalogue.thumbnail_path("copy").read_bytes() == THUMBNAIL
    assert sorted(path.name for path in paths.model_dir("copy").iterdir()) == [
        "model.json",
        "model.scad",
        "thumbnail.png",
    ]
    assert _no_staging_left(paths)
