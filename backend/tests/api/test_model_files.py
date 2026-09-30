"""The other `.scad` files of a multi-file model (#252)."""

from __future__ import annotations

import threading
from typing import Any

import pytest
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.api.model_files import MAX_SOURCE_FILES
from scadbuddy.core.paths import DataPaths
from scadbuddy.library.catalogue import TooManySourceFilesError
from scadbuddy.render.provenance import source_version

pytestmark = pytest.mark.requires_git

SLUG = "keychain"
MAIN = "include <parts.scad>\nwidth = 10;\nbar(width);\n"
PARTS = "module bar(w) { cube([w, 2, 2]); }\n"


def upload(client: TestClient) -> dict[str, Any]:
    response = client.post(
        "/api/v1/models",
        files={"file": (f"{SLUG}.scad", MAIN.encode(), "application/octet-stream")},
        data={"force": "true"},
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def put(client: TestClient, name: str, content: str, message: str | None = None) -> Any:
    return client.put(
        f"/api/v1/models/{SLUG}/files/{name}", json={"content": content, "message": message}
    )


def test_a_sibling_file_is_written_read_listed_and_versioned(
    client: TestClient, paths: DataPaths
) -> None:
    first = upload(client)["version"]

    written = put(client, "parts.scad", PARTS, "Add the bar module")

    assert written.status_code == 200, written.text
    assert written.json()["version"] != first
    assert (paths.model_dir(SLUG) / "parts.scad").read_text() == PARTS
    assert client.get(f"/api/v1/models/{SLUG}/files/parts.scad").text == PARTS
    assert client.get(f"/api/v1/models/{SLUG}/files/model.scad").text == MAIN
    listed = client.get(f"/api/v1/models/{SLUG}/files").json()
    assert [(f["name"], f["main"]) for f in listed] == [("model.scad", True), ("parts.scad", False)]
    assert listed[1]["size"] == len(PARTS)
    latest = client.get(f"/api/v1/models/{SLUG}/versions").json()[0]
    assert latest["message"] == "Add the bar module"
    assert [f["path"] for f in latest["files"]] == ["parts.scad"]


def test_a_sibling_edit_changes_what_a_render_is_keyed_by(
    client: TestClient, paths: DataPaths
) -> None:
    upload(client)
    put(client, "parts.scad", PARTS)
    before = source_version(paths.model_dir(SLUG))
    put(client, "parts.scad", PARTS.replace("2, 2", "3, 3"))
    assert source_version(paths.model_dir(SLUG)) != before


def test_a_sibling_is_removed_as_a_revision_and_restorable(
    client: TestClient, paths: DataPaths
) -> None:
    upload(client)
    with_parts = put(client, "parts.scad", PARTS).json()["version"]

    removed = client.delete(f"/api/v1/models/{SLUG}/files/parts.scad")

    assert removed.status_code == 200, removed.text
    assert not (paths.model_dir(SLUG) / "parts.scad").exists()
    assert client.get(f"/api/v1/models/{SLUG}/files/parts.scad").status_code == 404
    assert client.delete(f"/api/v1/models/{SLUG}/files/parts.scad").status_code == 404
    assert client.post(f"/api/v1/models/{SLUG}/versions/{with_parts}/restore").status_code == 200
    assert (paths.model_dir(SLUG) / "parts.scad").read_text() == PARTS


def test_the_main_source_is_written_only_through_put_source(client: TestClient) -> None:
    upload(client)
    assert put(client, "model.scad", "cube(1);\n").status_code == 409
    assert client.delete(f"/api/v1/models/{SLUG}/files/model.scad").status_code == 409
    # And the catalogue itself refuses it, for any caller but the route (#773).
    state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    with pytest.raises(ValueError, match="write_source"):
        state.catalogue.write_file(SLUG, "model.scad", "cube(1);\n")
    with pytest.raises(ValueError, match="write_source"):
        state.catalogue.write_file(SLUG, "model.scad", None)


@pytest.mark.parametrize("name", ["../model.json", ".hidden.scad", "README.md", "sub%2Fx.scad"])
def test_only_bare_scad_names_are_files(client: TestClient, name: str) -> None:
    upload(client)
    assert client.get(f"/api/v1/models/{SLUG}/files/{name}").status_code in (404, 422)
    assert put(client, name, "x = 1;\n").status_code in (404, 405, 422)


def test_a_nul_is_refused(client: TestClient) -> None:
    upload(client)
    assert put(client, "parts.scad", "a\x00b").status_code == 422


def test_the_file_count_is_capped(client: TestClient, paths: DataPaths) -> None:
    upload(client)
    for index in range(MAX_SOURCE_FILES - 1):
        (paths.model_dir(SLUG) / f"f{index}.scad").write_text("x = 1;\n")
    refused = put(client, "one-more.scad", "x = 1;\n")
    assert refused.status_code == 422
    assert "the most a model may hold" in refused.json()["detail"]
    # Replacing one that is there is still fine.
    assert put(client, "f0.scad", "x = 2;\n").status_code == 200


def test_a_built_in_is_read_only_and_an_unknown_model_is_a_404(client: TestClient) -> None:
    # Refused as read-only before any lookup, so 403 whether or not it exists.
    assert put_to(client, "builtin:keychain").status_code == 403
    assert put_to(client, "builtin:no-such-model").status_code == 403
    assert put_to(client, "nope").status_code == 404
    assert client.get("/api/v1/models/nope/files").status_code == 404


def put_to(client: TestClient, slug: str) -> Any:
    return client.put(f"/api/v1/models/{slug}/files/parts.scad", json={"content": PARTS})


@pytest.mark.parametrize("git", [True, False], ids=["with history", "without history"])
def test_two_new_files_at_once_cannot_both_pass_the_cap(
    client: TestClient, paths: DataPaths, monkeypatch: pytest.MonkeyPatch, git: bool
) -> None:
    # The count is taken under the write lock (PR #752 review), so of two new files
    # racing for the last place exactly one lands.
    upload(client)
    for index in range(MAX_SOURCE_FILES - 2):
        (paths.model_dir(SLUG) / f"f{index}.scad").write_text("x = 1;\n")
    state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    if not git:
        # With no history there is no write lock; the catalogue's own still holds.
        monkeypatch.setattr(state.catalogue, "history", None)
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def write(name: str) -> None:
        barrier.wait()
        try:
            state.catalogue.write_file(SLUG, name, "x = 1;\n", max_files=MAX_SOURCE_FILES)
            outcomes.append("written")
        except TooManySourceFilesError:
            outcomes.append("refused")

    threads = [threading.Thread(target=write, args=(name,)) for name in ("a.scad", "b.scad")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["refused", "written"]
    assert len(list(paths.model_dir(SLUG).glob("*.scad"))) == MAX_SOURCE_FILES


def test_a_file_deleted_while_the_list_is_read_is_left_out(
    client: TestClient, paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Listed, then removed by another request before its size is read (PR #752
    # review): the list leaves it out rather than failing.
    upload(client)
    state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    listing = state.catalogue.source_files
    gone = paths.model_dir(SLUG) / "gone.scad"
    monkeypatch.setattr(state.catalogue, "source_files", lambda slug: [*listing(slug), gone])

    response = client.get(f"/api/v1/models/{SLUG}/files")

    assert response.status_code == 200, response.text
    assert [f["name"] for f in response.json()] == ["model.scad"]
