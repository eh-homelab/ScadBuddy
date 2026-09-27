"""Taking a duplicate's upstream updates: detect, preview, merge, dismiss, detach (#157)."""

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
from scadbuddy.library.catalogue import Catalogue, ModelMeta
from scadbuddy.library.history import GitUnavailableError
from scadbuddy.main import create_app
from tests.api.conftest import PNG_BYTES

pytestmark = pytest.mark.requires_git

BUILTIN = "builtin:name-keychain"
MINE = "my-keychain"
# Far enough apart that an edit to `hole` and one to `layout` merge cleanly.
SOURCE = 'hole = 3;\nwidth = 40;\nheight = 12;\ndepth = 3;\nlabel = "hi";\nlayout = "row";\n'
THUMBNAIL = PNG_BYTES + b"\x00"


@pytest.fixture
def bundled(seed_dir: Path) -> Path:
    directory = seed_dir / "name-keychain"
    directory.mkdir()
    (directory / "model.scad").write_text(SOURCE, encoding="utf-8")
    (directory / "model.json").write_text(json.dumps({"name": "Name keychain"}), encoding="utf-8")
    (directory / "README.md").write_text("# Name keychain\n", encoding="utf-8")
    (directory / "thumbnail.png").write_bytes(THUMBNAIL)
    return directory


@pytest.fixture
def client(app: FastAPI, bundled: Path) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def _json(response: Any, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    return response.json()


def _duplicate(client: TestClient, model_id: str = BUILTIN, name: str = "My keychain") -> Any:
    return _json(client.post(f"/api/v1/models/{model_id}/duplicate", json={"name": name}), 201)


def _put(client: TestClient, slug: str, source: str, **query: str) -> Any:
    return client.put(
        f"/api/v1/models/{slug}/source", params=query, json={"source": source, "force": True}
    )


def _messages(client: TestClient, slug: str) -> list[str]:
    return [entry["message"] for entry in _json(client.get(f"/api/v1/models/{slug}/versions"))]


def _upstream(client: TestClient, slug: str = MINE) -> Any:
    return _json(client.get(f"/api/v1/models/{slug}/upstream"))


def _source(client: TestClient, slug: str = MINE) -> str:
    text: str = client.get(f"/api/v1/models/{slug}/source").text
    return text


def _restart_with(settings: Settings, bundled: Path, source: str) -> None:
    """A new image whose built-in has ``source``, and the boot that syncs it."""
    (bundled / "model.scad").write_text(source, encoding="utf-8")
    with TestClient(create_app(settings)):
        pass


def _listed(client: TestClient, slug: str = MINE) -> Any:
    return next(m for m in _json(client.get("/api/v1/models")) if m["slug"] == slug)


def test_a_fresh_duplicate_is_current(client: TestClient) -> None:
    record = _duplicate(client)

    status = _upstream(client)

    assert status["state"] == "current"
    assert status["revision"] == record["upstream"]["base"]
    assert status["preview"] is None
    assert record["upstream_state"] == "current"
    assert _listed(client)["upstream_state"] == "current"
    assert _listed(client, BUILTIN)["upstream_state"] is None


def test_a_built_in_update_merges_clean_into_an_edited_duplicate(
    client: TestClient, settings: Settings, bundled: Path
) -> None:
    """The acceptance case: edit the duplicate's hole, change the built-in's layout,
    restart; the duplicate reports the update, merges clean and has both changes."""
    _duplicate(client)
    ours = SOURCE.replace("hole = 3;", "hole = 4;")
    _json(_put(client, MINE, ours))
    theirs = SOURCE.replace('layout = "row";', 'layout = "column";')
    _restart_with(settings, bundled, theirs)
    builtin_version = _json(client.get(f"/api/v1/models/{BUILTIN}"))["version"]
    both = ours.replace('layout = "row";', 'layout = "column";')

    status = _upstream(client)
    assert status["state"] == "update"
    assert status["revision"] == builtin_version
    assert status["preview"] == {
        "ours": ours,
        "base": SOURCE,
        "theirs": theirs,
        "merged": both,
        "clean": True,
        "taken": [],
        "kept": [],
    }
    # From the one history walk the listing makes, and from a single record alike.
    assert _listed(client)["upstream_state"] == "update"
    assert _json(client.get(f"/api/v1/models/{MINE}"))["upstream_state"] == "update"
    before = _messages(client, MINE)

    merged = _json(client.post(f"/api/v1/models/{MINE}/upstream/merge"))

    assert _source(client) == both
    assert merged["model"]["upstream"] == {
        "id": BUILTIN,
        "path": "_builtin/name-keychain",
        "base": builtin_version,
        "dismissed": None,
    }
    assert merged["model"]["upstream_state"] == "current"
    assert _messages(client, MINE) == [f"Merge {BUILTIN} into {MINE}", *before]
    assert _upstream(client)["state"] == "current"
    assert merged["model"]["name"] == "My keychain"


def test_a_conflict_is_a_409_and_saving_the_resolution_advances_base(
    client: TestClient, settings: Settings, bundled: Path, paths: DataPaths
) -> None:
    _duplicate(client)
    ours = SOURCE.replace('layout = "row";', 'layout = "grid";')
    _json(_put(client, MINE, ours))
    _restart_with(settings, bundled, SOURCE.replace('layout = "row";', 'layout = "column";'))
    builtin_version = _json(client.get(f"/api/v1/models/{BUILTIN}"))["version"]
    before = _messages(client, MINE)
    meta_before = paths.model_meta(MINE).read_text(encoding="utf-8")

    preview = _upstream(client)["preview"]
    assert preview["clean"] is False
    refused = _json(client.post(f"/api/v1/models/{MINE}/upstream/merge"), 409)

    assert refused["merge_base"] == builtin_version
    assert refused["conflicts"] == 1
    assert refused["merged"] == preview["merged"]
    assert f"<<<<<<< {MINE}\n" in refused["merged"]
    assert f"||||||| {BUILTIN}@" in refused["merged"]
    assert f">>>>>>> {BUILTIN}\n" in refused["merged"]
    # Nothing was written.
    assert _source(client) == ours
    assert paths.model_meta(MINE).read_text(encoding="utf-8") == meta_before
    assert _messages(client, MINE) == before

    # A leftover marker cannot be saved, `force` or not.
    leftover = _put(client, MINE, refused["merged"], merge_base=builtin_version)
    assert leftover.status_code == 422, leftover.text
    assert "conflict markers" in leftover.json()["detail"]

    resolved = SOURCE.replace('layout = "row";', 'layout = "grid-column";')
    saved = _json(_put(client, MINE, resolved, merge_base=builtin_version[:12]))

    assert _source(client) == resolved
    assert saved["upstream"]["base"] == builtin_version
    assert saved["upstream"]["path"] == "_builtin/name-keychain"
    assert saved["upstream_state"] == "current"
    assert _messages(client, MINE) == [f"Merge {BUILTIN} into {MINE}", *before]


def test_a_merge_base_is_refused_where_it_cannot_apply(client: TestClient, model: str) -> None:
    _duplicate(client)

    unknown = _put(client, MINE, SOURCE, merge_base="0" * 40)
    assert unknown.status_code == 422, unknown.text

    head = _json(client.get(f"/api/v1/models/{BUILTIN}"))["version"]
    not_a_duplicate = _put(client, model, SOURCE, merge_base=head)
    assert not_a_duplicate.status_code == 409, not_a_duplicate.text
    assert "not a duplicate" in not_a_duplicate.json()["detail"]


def test_dismissing_hides_the_update_until_the_upstream_moves_again(
    client: TestClient, settings: Settings, bundled: Path
) -> None:
    _duplicate(client)
    refused = client.post(f"/api/v1/models/{MINE}/upstream/dismiss")
    assert refused.status_code == 409, refused.text
    assert refused.json()["state"] == "current"

    _restart_with(settings, bundled, SOURCE.replace("width = 40;", "width = 50;"))
    first = _json(client.get(f"/api/v1/models/{BUILTIN}"))["version"]

    dismissed = _json(client.post(f"/api/v1/models/{MINE}/upstream/dismiss"))

    assert dismissed["upstream"]["dismissed"] == first
    assert dismissed["upstream_state"] == "dismissed"
    assert _listed(client)["upstream_state"] == "dismissed"
    status = _upstream(client)
    assert status["state"] == "dismissed"
    assert status["preview"] is None
    assert _messages(client, MINE)[0] == f"Dismiss {BUILTIN} update in {MINE}"
    assert _source(client) == SOURCE

    _restart_with(settings, bundled, SOURCE.replace("width = 40;", "width = 60;"))
    assert _upstream(client)["state"] == "update"

    merged = _json(client.post(f"/api/v1/models/{MINE}/upstream/merge"))
    assert merged["model"]["upstream"]["dismissed"] is None
    assert _source(client) == SOURCE.replace("width = 40;", "width = 60;")


def test_a_dismissed_update_can_still_be_merged(
    client: TestClient, settings: Settings, bundled: Path
) -> None:
    _duplicate(client)
    _restart_with(settings, bundled, SOURCE.replace("width = 40;", "width = 50;"))
    _json(client.post(f"/api/v1/models/{MINE}/upstream/dismiss"))

    merged = _json(client.post(f"/api/v1/models/{MINE}/upstream/merge"))

    assert merged["model"]["upstream"]["dismissed"] is None
    assert merged["model"]["upstream_state"] == "current"


def test_merging_when_current_is_a_409(client: TestClient) -> None:
    _duplicate(client)

    refused = client.post(f"/api/v1/models/{MINE}/upstream/merge")

    assert refused.status_code == 409, refused.text
    assert refused.json()["state"] == "current"


def test_a_duplicate_of_mine_takes_its_parents_edits(client: TestClient) -> None:
    _duplicate(client)
    _duplicate(client, MINE, "Variant")
    _json(_put(client, MINE, SOURCE.replace("height = 12;", "height = 20;")))

    assert _upstream(client, "variant")["state"] == "update"
    merged = _json(client.post("/api/v1/models/variant/upstream/merge"))

    assert _source(client, "variant") == SOURCE.replace("height = 12;", "height = 20;")
    assert merged["model"]["upstream"]["id"] == MINE
    assert merged["model"]["upstream"]["path"] == MINE


def test_other_files_follow_the_upstream_unless_changed_here(
    client: TestClient, settings: Settings, bundled: Path, paths: DataPaths
) -> None:
    _duplicate(client)
    (bundled / "README.md").write_text("# Name keychain, v2\n", encoding="utf-8")
    (bundled / "thumbnail.png").write_bytes(THUMBNAIL + b"upstream")
    (bundled / "parts.scad").write_text("module part() {}\n", encoding="utf-8")
    (bundled / "model.json").write_text(json.dumps({"name": "Renamed upstream"}), "utf-8")
    ours_thumbnail = THUMBNAIL + b"mine"
    paths.model_dir(MINE).joinpath("thumbnail.png").write_bytes(ours_thumbnail)
    _restart_with(settings, bundled, SOURCE)

    preview = _upstream(client)["preview"]
    assert preview["clean"] is True
    assert preview["taken"] == ["README.md", "parts.scad"]
    assert preview["kept"] == ["thumbnail.png"]

    merged = _json(client.post(f"/api/v1/models/{MINE}/upstream/merge"))

    assert merged["taken"] == ["README.md", "parts.scad"]
    assert merged["kept"] == ["thumbnail.png"]
    directory = paths.model_dir(MINE)
    assert (directory / "README.md").read_text(encoding="utf-8") == "# Name keychain, v2\n"
    assert (directory / "parts.scad").is_file()
    assert (directory / "thumbnail.png").read_bytes() == ours_thumbnail
    # `model.json` is always ours.
    assert merged["model"]["name"] == "My keychain"

    # A file the upstream drops goes too, where it is unchanged here.
    (bundled / "parts.scad").unlink()
    _restart_with(settings, bundled, SOURCE)
    assert _upstream(client)["preview"]["taken"] == ["parts.scad"]
    _json(client.post(f"/api/v1/models/{MINE}/upstream/merge"))
    assert not (directory / "parts.scad").exists()


def test_a_deleted_upstream_is_gone_and_can_be_detached(client: TestClient) -> None:
    _duplicate(client)
    _duplicate(client, MINE, "Variant")
    refused = client.post("/api/v1/models/variant/upstream/detach")
    assert refused.status_code == 409, refused.text
    assert refused.json()["state"] == "current"

    # Deleting a template with duplicates says how many first.
    guarded = client.delete(f"/api/v1/models/{MINE}")
    assert guarded.status_code == 409, guarded.text
    assert guarded.json()["duplicates"] == 1
    assert guarded.json()["slugs"] == ["variant"]
    assert client.get(f"/api/v1/models/{MINE}").status_code == 200
    assert client.delete(f"/api/v1/models/{MINE}", params={"force": "true"}).status_code == 204

    status = _upstream(client, "variant")
    assert status["state"] == "gone"
    assert status["revision"] is None
    assert _listed(client, "variant")["upstream_state"] == "gone"
    for action in ("merge", "dismiss"):
        response = client.post(f"/api/v1/models/variant/upstream/{action}")
        assert response.status_code == 409, response.text
        assert response.json()["state"] == "gone"

    detached = _json(client.post("/api/v1/models/variant/upstream/detach"))

    assert detached["upstream"] is None
    assert detached["upstream_state"] is None
    assert _messages(client, "variant")[0] == f"Detach variant from {MINE}"
    assert client.get("/api/v1/models/variant/upstream").status_code == 404
    assert _source(client, "variant") == SOURCE


def test_a_built_in_dropped_from_the_image_is_gone(
    client: TestClient, settings: Settings, bundled: Path
) -> None:
    _duplicate(client)
    for child in bundled.iterdir():
        child.unlink()
    bundled.rmdir()
    with TestClient(create_app(settings)):
        pass

    assert _upstream(client)["state"] == "gone"


def test_a_template_without_an_upstream_has_none(client: TestClient, model: str) -> None:
    for model_id in (model, BUILTIN):
        response = client.get(f"/api/v1/models/{model_id}/upstream")
        assert response.status_code == 404, response.text
    assert client.get("/api/v1/models/nope/upstream").status_code == 404
    for action in ("merge", "dismiss", "detach"):
        assert client.post(f"/api/v1/models/{model}/upstream/{action}").status_code == 404
        assert client.post(f"/api/v1/models/{BUILTIN}/upstream/{action}").status_code == 403


def test_without_history_there_is_no_upstream_state(paths: DataPaths) -> None:
    catalogue = Catalogue(paths)
    catalogue.create("keychain", SOURCE, ModelMeta(name="Keychain"))
    catalogue.duplicate("keychain", "copy", "Copy")

    assert catalogue.record("copy").upstream_state is None
    with pytest.raises(GitUnavailableError):
        catalogue.upstream_status("copy")
