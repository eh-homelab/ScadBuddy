"""Duplicating a template, built-in or mine, records its upstream (#156)."""

from __future__ import annotations

import json
import os
import shutil
import time
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.core.paths import DataPaths
from scadbuddy.core.settings import Settings
from scadbuddy.library.catalogue import (
    DUPLICATE_STAGING_MAX_AGE,
    DUPLICATE_STAGING_PREFIX,
    Catalogue,
    ModelMeta,
)
from scadbuddy.library.history import GitTimeoutError, ModelHistory, RevisionNotFoundError
from scadbuddy.main import create_app
from scadbuddy.render.bambu3mf import PLATE_THUMBNAIL
from scadbuddy.render.solids import WRAPPER_PREFIX
from tests.api.conftest import PNG_BYTES, wait_for_job

pytestmark = pytest.mark.requires_git

BUILTIN = "builtin:name-keychain"
SOURCE = 'width = 10;\nlabel = "hi";\n'
THUMBNAIL = PNG_BYTES + b"\x00"


@pytest.fixture
def bundled_meta() -> dict[str, Any]:
    # No library pinned: one whose checkout is not on the volume refuses the source
    # writes and schema reads these tests make (#93). The test that needs one
    # parametrizes this.
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


PIN = {"name": "BOSL2", "url": "https://x.invalid/b.git", "ref": "v1", "commit": "a" * 40}


@pytest.mark.parametrize(
    "bundled_meta", [{"name": "Name keychain", "tags": ["keychain"], "libraries": [PIN]}]
)
def test_a_duplicate_keeps_the_rest_of_model_json(client: TestClient, paths: DataPaths) -> None:
    """The library pins (#93), or anything else the metadata carries, travel along:
    the duplicate renders against the same library versions until it re-pins."""
    _duplicate(client, BUILTIN, "My keychain")

    stored = json.loads(paths.model_meta("my-keychain").read_text(encoding="utf-8"))

    assert stored["libraries"] == [PIN]
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
    assert not any(path.name.startswith(DUPLICATE_STAGING_PREFIX) for path in paths.cache.iterdir())


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
    return not any(path.name.startswith(DUPLICATE_STAGING_PREFIX) for path in paths.cache.iterdir())


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


def test_a_copy_deleted_right_after_its_commit_is_a_404_naming_the_copy(
    client: TestClient, paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The upstream is still there: the 404 names the copy that went, not it (#215)."""
    commit = Catalogue._commit

    def commit_then_delete(self: Catalogue, message: str, *slugs: str) -> str | None:
        revision = commit(self, message, *slugs)
        if message.startswith("Duplicate "):
            shutil.rmtree(paths.model_dir("copy"))
        return revision

    monkeypatch.setattr(Catalogue, "_commit", commit_then_delete)

    response = client.post(f"/api/v1/models/{BUILTIN}/duplicate", json={"name": "Copy"})

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "no model named 'copy'"
    assert client.get(f"/api/v1/models/{BUILTIN}").status_code == 200


def test_an_upstream_deleted_mid_copy_is_a_404_naming_the_upstream(
    client: TestClient, paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The upstream goes between the route's check and the copy: the 404 names it,
    not the copy that was never made (#392)."""
    mine = _duplicate(client, BUILTIN, "Mine")["slug"]

    def export(self: ModelHistory, slug: str, commit: str, dest: Path) -> None:
        shutil.rmtree(paths.model_dir(mine))
        raise RevisionNotFoundError(f"{slug!r} does not exist at {commit}")

    monkeypatch.setattr(ModelHistory, "export", export)

    response = client.post(f"/api/v1/models/{mine}/duplicate", json={"name": "Copy"})

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == f"no model named {mine!r}"
    assert not paths.model_dir("copy").exists()
    assert _no_staging_left(paths)


def test_the_boot_sweeps_a_crashed_duplicates_staging(app: FastAPI, paths: DataPaths) -> None:
    """A duplicate killed mid-copy leaves its staging folder; the next boot clears it,
    and leaves the rest of the cache alone (#212)."""
    staged = paths.cache / f"{DUPLICATE_STAGING_PREFIX}dead" / "copy"
    staged.mkdir(parents=True)
    (staged / "model.scad").write_text(SOURCE, encoding="utf-8")
    old = time.time() - DUPLICATE_STAGING_MAX_AGE - 60
    os.utime(staged.parent, (old, old))
    kept = paths.cache / "keep-me"
    kept.mkdir()

    with TestClient(app):
        pass

    assert _no_staging_left(paths)
    assert kept.is_dir()


def test_a_duplicate_sweeps_staging_an_earlier_one_crashed_out_of(
    client: TestClient, paths: DataPaths
) -> None:
    """A single replica that crashed and restarted inside the hour still gets it
    cleared, by the next duplicate once it is old enough."""
    staged = paths.cache / f"{DUPLICATE_STAGING_PREFIX}dead" / "copy"
    staged.mkdir(parents=True)
    old = time.time() - DUPLICATE_STAGING_MAX_AGE - 60
    os.utime(staged.parent, (old, old))

    response = client.post(f"/api/v1/models/{BUILTIN}/duplicate", json={"name": "Copy"})

    assert response.status_code == 201, response.text
    assert _no_staging_left(paths)


def test_a_sweep_failure_after_a_duplicate_is_not_the_duplicates_failure(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The copy is committed; tidying up after it is best-effort."""

    def sweep(self: Catalogue) -> list[str]:
        raise PermissionError("cache unreadable")

    monkeypatch.setattr(Catalogue, "sweep_duplicate_staging", sweep)

    response = client.post(f"/api/v1/models/{BUILTIN}/duplicate", json={"name": "Copy"})

    assert response.status_code == 201, response.text


def test_a_staging_the_sweep_cannot_read_does_not_keep_the_rest(
    paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalogue = Catalogue(paths)
    old = time.time() - DUPLICATE_STAGING_MAX_AGE - 60
    for name in ("a", "b"):
        staged = paths.cache / f"{DUPLICATE_STAGING_PREFIX}{name}"
        staged.mkdir(parents=True)
        os.utime(staged, (old, old))
    real_stat = Path.stat

    def stat(self: Path, *args: Any, **kwargs: Any) -> os.stat_result:
        if self.name == f"{DUPLICATE_STAGING_PREFIX}a":
            raise PermissionError("unreadable")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat)

    assert catalogue.sweep_duplicate_staging() == [f"{DUPLICATE_STAGING_PREFIX}b"]


def test_the_boot_leaves_a_fresh_duplicate_staging_alone(app: FastAPI, paths: DataPaths) -> None:
    """Another replica sharing /data may be mid-copy into it."""
    staged = paths.cache / f"{DUPLICATE_STAGING_PREFIX}live" / "copy"
    staged.mkdir(parents=True)

    with TestClient(app):
        pass

    assert staged.is_dir()


def test_a_failed_boot_sweep_of_duplicate_staging_does_not_stop_the_boot(
    app: FastAPI, caplog: pytest.LogCaptureFixture
) -> None:
    with (
        patch.object(Catalogue, "sweep_duplicate_staging", side_effect=OSError("EIO")),
        TestClient(app) as client,
    ):
        assert client.get("/healthz").status_code == 200
    assert "could not sweep duplicate staging folders" in caplog.text


def _stage(paths: DataPaths, name: str, age: float) -> Path:
    staged = paths.cache / f"{DUPLICATE_STAGING_PREFIX}{name}"
    staged.mkdir(parents=True)
    then = time.time() - age
    os.utime(staged, (then, then))
    return staged


def test_the_staging_max_age_is_the_setting(settings: Settings, paths: DataPaths) -> None:
    """SCADBUDDY_DUPLICATE_STAGING_MAX_AGE moves the line between crashed and in flight."""
    older = _stage(paths, "older", 120)
    newer = _stage(paths, "newer", 30)

    with TestClient(create_app(settings.model_copy(update={"duplicate_staging_max_age": 60}))):
        pass

    assert not older.exists()
    assert newer.is_dir()


def test_the_periodic_sweep_clears_old_duplicate_staging(
    settings: Settings, paths: DataPaths
) -> None:
    """Without waiting for the next boot or duplicate (#397)."""
    periodic = settings.model_copy(update={"asset_sweep_interval": 0.05})
    with TestClient(create_app(periodic)):
        staged = _stage(paths, "late", DUPLICATE_STAGING_MAX_AGE + 60)
        deadline = time.monotonic() + 10
        while staged.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not staged.exists()


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
    catalogue = Catalogue(paths, wrapper_prefix=WRAPPER_PREFIX)
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


# ── #179's details on a duplicate ─────────────────────────────────────────────


def _generate_with_cover(client: TestClient, paths: DataPaths, model_id: str, cover: bytes) -> str:
    """Render and save an output whose 3MF carries ``cover`` as its plate image."""
    job_id = client.post(f"/api/v1/models/{model_id}/render", json={"params": {}}).json()["job_id"]
    assert wait_for_job(client, job_id)["status"] == "done"
    with zipfile.ZipFile(paths.job_work_dir(job_id) / "model.3mf", "a") as archive:
        archive.writestr(PLATE_THUMBNAIL, cover)
    saved = client.post(f"/api/v1/models/{model_id}/outputs", json={"job_id": job_id})
    assert saved.status_code == 201, saved.text
    output_id: str = saved.json()["id"]
    return output_id


def test_a_duplicate_takes_the_thumbnail_and_readme_as_its_own_and_can_edit_them(
    client: TestClient, bundled: Path
) -> None:
    """The copy is the upstream's directory, sidecars included; on a template of
    mine they are editable, and editing them leaves the upstream alone."""
    record = _duplicate(client, BUILTIN, "My keychain")
    slug = record["slug"]
    assert (record["origin"], record["thumbnail_source"]) == ("mine", "model")
    assert client.get(f"/api/v1/models/{slug}/thumbnail").content == THUMBNAIL

    replaced = client.put(
        f"/api/v1/models/{slug}/thumbnail", files={"file": ("t.png", PNG_BYTES, "image/png")}
    )
    readme = client.put(f"/api/v1/models/{slug}/readme", json={"content": "# Mine\n"})
    patched = client.patch(f"/api/v1/models/{slug}", json={"name": "Keyring"})

    assert (replaced.status_code, readme.status_code, patched.status_code) == (200, 200, 200)
    assert client.get(f"/api/v1/models/{slug}/thumbnail").content == PNG_BYTES
    assert client.get(f"/api/v1/models/{BUILTIN}/thumbnail").content == THUMBNAIL
    assert client.get(f"/api/v1/models/{BUILTIN}/readme").status_code == 404
    # The details edits never touch what the duplicate records as its upstream.
    assert patched.json()["upstream"] == record["upstream"]


def test_a_duplicate_carries_the_upstreams_readme(app: FastAPI, bundled: Path) -> None:
    (bundled / "README.md").write_text("# Name keychain\n", encoding="utf-8")
    with TestClient(app) as client:
        slug = _duplicate(client, BUILTIN, "My keychain")["slug"]
        assert client.get(f"/api/v1/models/{slug}/readme").text == "# Name keychain\n"


def test_a_duplicate_has_no_plate_fallback_until_it_is_generated(
    client: TestClient, paths: DataPaths
) -> None:
    """Outputs are derived, so not copied: the upstream's plate image is not the
    duplicate's, and a slug a gone model used leaves no cached cover behind."""
    created = client.post("/api/v1/models", json={"name": "Upstream", "source": SOURCE})
    model = created.json()["slug"]
    _generate_with_cover(client, paths, model, PNG_BYTES + b"upstream")
    assert client.get(f"/api/v1/models/{model}").json()["thumbnail_source"] == "output"

    # A model of the duplicate's slug that was generated, listed, then deleted.
    gone = client.post("/api/v1/models", json={"name": "Copy", "source": SOURCE}).json()["slug"]
    _generate_with_cover(client, paths, gone, PNG_BYTES + b"gone")
    assert client.get("/api/v1/models").status_code == 200
    assert client.delete(f"/api/v1/models/{gone}").status_code == 204

    record = _duplicate(client, model, "Copy")
    assert record["slug"] == gone
    assert (record["has_thumbnail"], record["thumbnail_output_id"]) == (False, None)

    own = _generate_with_cover(client, paths, gone, PNG_BYTES + b"own")
    after = client.get(f"/api/v1/models/{gone}").json()
    assert (after["thumbnail_source"], after["thumbnail_output_id"]) == ("output", own)
    assert client.get(f"/api/v1/models/{gone}/thumbnail").content == PNG_BYTES + b"own"


@pytest.mark.parametrize(
    "bundled_meta",
    [
        {
            "name": "Name keychain",
            "origin_url": "javascript:alert(document.domain)",
            "upstream": {"id": "elsewhere", "path": "elsewhere", "base": None},
        }
    ],
)
def test_a_built_ins_model_json_sets_neither_link_nor_upstream_on_it_or_its_copy(
    client: TestClient, paths: DataPaths
) -> None:
    builtin = client.get(f"/api/v1/models/{BUILTIN}").json()
    assert (builtin["origin_url"], builtin["upstream"]) == (None, None)

    record = _duplicate(client, BUILTIN, "My keychain")

    assert record["origin_url"] is None
    assert record["upstream"]["id"] == BUILTIN
    assert "origin_url" not in json.loads(paths.model_meta(record["slug"]).read_text("utf-8"))


def test_a_dropped_model_json_cannot_claim_an_upstream(client: TestClient) -> None:
    """Only a duplicate records one; as with `origin_url`, an upload's is dropped."""
    meta = {"name": "Widget", "upstream": {"id": BUILTIN, "path": "_builtin/x", "base": None}}
    response = client.post(
        "/api/v1/models",
        files={
            "file": ("widget.scad", SOURCE.encode(), "application/octet-stream"),
            "meta": ("model.json", json.dumps(meta).encode(), "application/json"),
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["upstream"] is None


def test_the_boot_sweeps_a_claim_a_crashed_create_stranded(app: FastAPI, paths: DataPaths) -> None:
    """A create or duplicate killed between claiming its slug and writing it leaves an
    empty directory: 404 to a GET, 409 to a retry. The boot moves an old one to the
    tombstones and leaves a fresh one, which another replica may be claiming (#218)."""
    stranded = paths.model_dir("stranded")
    stranded.mkdir(parents=True)
    old = time.time() - DUPLICATE_STAGING_MAX_AGE - 60
    os.utime(stranded, (old, old))
    fresh = paths.model_dir("fresh")
    fresh.mkdir()

    with TestClient(app) as client:
        assert not stranded.exists()
        assert fresh.is_dir()
        created = client.post("/api/v1/models", json={"name": "Stranded", "source": SOURCE})
        assert created.status_code == 201, created.text
        taken = client.post("/api/v1/models", json={"name": "Fresh", "source": SOURCE})
        assert taken.status_code == 409


def test_the_claim_sweep_leaves_a_model_whose_source_is_only_missing_from_disk(
    paths: DataPaths,
) -> None:
    """Tracked at HEAD, it is a model to restore, not a claim to sweep."""
    history = ModelHistory(paths.models, wrapper_prefix=WRAPPER_PREFIX)
    history.ensure_repo()
    catalogue = Catalogue(paths, history, duplicate_staging_max_age=0)
    catalogue.create("kept", SOURCE, ModelMeta(name="Kept"))
    paths.model_source("kept").unlink()

    assert catalogue.sweep_stranded_claims() == []
    assert paths.model_dir("kept").is_dir()
