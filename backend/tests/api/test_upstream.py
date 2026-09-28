"""Taking a duplicate's upstream updates: detect, preview, merge, dismiss, detach (#157)."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.core.paths import DataPaths
from scadbuddy.core.settings import Settings
from scadbuddy.library import catalogue as catalogue_module
from scadbuddy.library import upstream as upstream_module
from scadbuddy.library.catalogue import MERGE_ATTEMPTS, Catalogue, ModelMeta, ModelPatch
from scadbuddy.library.history import GitUnavailableError, ModelHistory
from scadbuddy.library.upstream import MergePlan, UpstreamStateError
from scadbuddy.main import create_app
from scadbuddy.render.solids import WRAPPER_PREFIX
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
        "patch": status["preview"]["patch"],
        "merged": both,
        "clean": True,
        "taken": [],
        "kept": [],
    }
    # The built-in's own change, headed by its slug, not its `_builtin/` mirror.
    assert (
        "--- a/name-keychain/model.scad\n+++ b/name-keychain/model.scad\n"
        in (status["preview"]["patch"])
    )
    assert '-layout = "row";\n+layout = "column";\n' in status["preview"]["patch"]
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


def test_a_merge_base_is_refused_where_it_cannot_apply(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    mine = _duplicate(client)
    _json(_put(client, model, "width = 11;\n"))
    other_model = _json(client.get(f"/api/v1/models/{model}"))["version"]
    before = _messages(client, MINE)
    meta_before = paths.model_meta(MINE).read_text(encoding="utf-8")
    edited = SOURCE.replace("hole = 3;", "hole = 5;")

    # Unknown, another model's revision, this duplicate's own (it touched only
    # the duplicate), and empty: none is a revision of the upstream.
    for bogus in ("0" * 40, other_model, other_model[:7], mine["version"], ""):
        refused = _put(client, MINE, edited, merge_base=bogus)
        assert refused.status_code == 422, (bogus, refused.text)
        assert _source(client) == SOURCE
        assert paths.model_meta(MINE).read_text(encoding="utf-8") == meta_before
        assert _messages(client, MINE) == before
    assert _upstream(client)["upstream"]["base"] == mine["upstream"]["base"]

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


def _racing(paths: DataPaths) -> Catalogue:
    """A duplicate of a template that has moved since, in a catalogue with history."""
    history = ModelHistory(paths.models, wrapper_prefix=WRAPPER_PREFIX)
    history.ensure_repo()
    catalogue = Catalogue(paths, history)
    catalogue.create("keychain", SOURCE, ModelMeta(name="Keychain"))
    catalogue.duplicate("keychain", "copy", "Copy")
    catalogue.write_source("keychain", SOURCE.replace("width = 40;", "width = 50;"))
    return catalogue


@pytest.mark.parametrize("action", ["dismiss", "resolve"])
def test_upstream_changes_and_metadata_edits_racing_both_land(
    paths: DataPaths, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    catalogue = _racing(paths)
    revision = catalogue.upstream_status("copy").revision
    assert revision is not None
    write = Catalogue.write_raw_meta

    # Each write lingers after its read, so two unlocked read-modify-writes both
    # read before either writes, and the second write loses the first's update.
    def slow_write(self: Catalogue, slug: str, meta: dict[str, Any]) -> None:
        time.sleep(0.3)
        write(self, slug, meta)

    monkeypatch.setattr(Catalogue, "write_raw_meta", slow_write)

    def upstream_change() -> None:
        if action == "dismiss":
            catalogue.dismiss_upstream("copy")
        else:
            catalogue.write_source("copy", SOURCE, merge_base=revision)

    threads = [
        threading.Thread(target=upstream_change),
        threading.Thread(target=lambda: catalogue.update("copy", ModelPatch(name="Renamed"))),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    raw = catalogue.read_raw_meta("copy")
    assert raw["name"] == "Renamed"
    if action == "dismiss":
        assert raw["upstream"]["dismissed"] == revision
    else:
        assert raw["upstream"]["base"] == revision


def _edit_after_planning(
    monkeypatch: pytest.MonkeyPatch, catalogue: Catalogue, edits: list[str]
) -> None:
    """Each time a merge is worked out, the next of ``edits`` lands before it is written."""
    plan = upstream_module.plan_merge

    def plan_then_edit(*args: Any) -> MergePlan:
        planned = plan(*args)
        if edits:
            catalogue.write_source("copy", edits.pop(0))
        return planned

    monkeypatch.setattr(catalogue_module, "plan_merge", plan_then_edit)


def test_a_merge_is_worked_out_again_when_the_template_moves_before_it_is_written(
    paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#228: planned outside the write lock, a merge the template has moved under
    must not write its stale result over the edit."""
    catalogue = _racing(paths)
    edited = SOURCE.replace('layout = "row";', 'layout = "column";')
    _edit_after_planning(monkeypatch, catalogue, [edited])

    _, plan = catalogue.merge_upstream("copy")

    both = edited.replace("width = 40;", "width = 50;")
    assert catalogue.paths.model_source("copy").read_text(encoding="utf-8") == both
    assert plan.preview.merged == both
    upstream = catalogue.record("copy").upstream
    assert upstream is not None and upstream.base == plan.revision


def test_a_merge_the_template_keeps_moving_under_is_refused(
    paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalogue = _racing(paths)
    base = catalogue.upstream_status("copy").upstream.base
    edits = [SOURCE.replace('layout = "row";', f'layout = "{n}";') for n in range(MERGE_ATTEMPTS)]
    _edit_after_planning(monkeypatch, catalogue, list(edits))

    with pytest.raises(UpstreamStateError):
        catalogue.merge_upstream("copy")

    assert catalogue.paths.model_source("copy").read_text(encoding="utf-8") == edits[-1]
    upstream = catalogue.record("copy").upstream
    assert upstream is not None and upstream.base == base


def test_an_edit_racing_a_merges_write_waits_for_it(
    paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#370: a plain edit arriving after a merge's staleness check but before its
    write must not land in between, where the merge would overwrite it."""
    catalogue = _racing(paths)
    history = catalogue.history
    assert history is not None
    edited = SOURCE.replace("hole = 3;", "hole = 5;")
    replace = Catalogue._replace_source
    edit_written = threading.Event()

    def watched_replace(self: Catalogue, slug: str, source: str) -> None:
        replace(self, slug, source)
        if source == edited:
            edit_written.set()

    monkeypatch.setattr(Catalogue, "_replace_source", watched_replace)
    still_applies = MergePlan.still_applies
    edit = threading.Thread(target=lambda: catalogue.write_source("copy", edited))

    # The merge holds the write lock from here to its commit. An edit written
    # without it lands now, and the merge's write then clobbers it; one written
    # under it cannot land before the merge's commit, so this wait times out.
    def check_then_edit(self: MergePlan, directory: Path) -> bool:
        applies = still_applies(self, directory)
        edit.start()
        edit_written.wait(timeout=1)
        return applies

    monkeypatch.setattr(MergePlan, "still_applies", check_then_edit)

    _, plan = catalogue.merge_upstream("copy")
    edit.join()

    assert catalogue.paths.model_source("copy").read_text(encoding="utf-8") == edited
    assert history.show("HEAD", "copy/model.scad").decode() == edited
    assert [revision.message for revision in history.log("copy")][:2] == [
        "Edit copy source",
        "Merge keychain into copy",
    ]
    assert plan.preview.merged != edited


# ── #179's details through an upstream merge ─────────────────────────────────


def test_a_merge_takes_the_upstreams_new_thumbnail_and_readme(
    client: TestClient, paths: DataPaths
) -> None:
    """Set through #179's routes on a template of mine, they reach its duplicate the
    way any other file does, and the duplicate serves them as its own afterwards."""
    upstream = _json(client.post("/api/v1/models", json={"name": "Parent", "source": SOURCE}), 201)
    child = _duplicate(client, upstream["slug"], "Child")
    assert child["has_thumbnail"] is False
    before = client.get(f"/api/v1/models/{child['slug']}/thumbnail")
    assert before.status_code == 404

    new_thumbnail = PNG_BYTES + b"parent"
    _json(
        client.put(
            f"/api/v1/models/{upstream['slug']}/thumbnail",
            files={"file": ("t.png", new_thumbnail, "image/png")},
        )
    )
    _json(client.put(f"/api/v1/models/{upstream['slug']}/readme", json={"content": "# P\n"}))

    status = _upstream(client, child["slug"])
    assert status["state"] == "update"
    assert status["preview"]["taken"] == ["README.md", "thumbnail.png"]

    merged = _json(client.post(f"/api/v1/models/{child['slug']}/upstream/merge"))

    assert (merged["model"]["thumbnail_source"], merged["model"]["has_readme"]) == ("model", True)
    served = client.get(f"/api/v1/models/{child['slug']}/thumbnail")
    assert served.content == new_thumbnail
    assert (
        served.headers["etag"]
        == client.get(f"/api/v1/models/{upstream['slug']}/thumbnail").headers["etag"]
    )
    assert client.get(f"/api/v1/models/{child['slug']}/readme").text == "# P\n"
    # Its own details stay editable after the merge.
    assert client.delete(f"/api/v1/models/{child['slug']}/thumbnail").status_code == 200
    assert paths.model_meta(child["slug"]).is_file()


@pytest.mark.parametrize("action", ["merge", "dismiss", "detach"])
def test_a_built_ins_upstream_actions_and_details_writes_are_all_refused(
    client: TestClient, action: str
) -> None:
    response = client.post(f"/api/v1/models/{BUILTIN}/upstream/{action}")
    assert response.status_code == 403, response.text
    readme = client.put(f"/api/v1/models/{BUILTIN}/readme", json={"content": "# x\n"})
    assert readme.status_code == 403
