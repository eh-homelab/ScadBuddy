"""The other readers of the templates read their rows once a copy run has finished
(#7317 phase 2, plan Task 8): the pin index, the library associations, the activity
facets' names, the print-import suggestions, the outputs' tag filter, the print
settings, the name check and the duplicates. The app's start copies first (phase 3), so
a test that edits files by hand runs the copy before it reads; each test reads, runs the
copy again, then reads the same with the `model.json` reads refused (the rows).
`library_users` keeps the files (spec §7.4)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from scadbuddy.activity.index import Count
from scadbuddy.activity.subject_labels import SubjectLabels
from scadbuddy.api.activity import _subject_labels
from scadbuddy.api.deps import announce_commits
from scadbuddy.api.print_imports import _suggestions
from scadbuddy.core.events import Event, UpstreamAvailable, VersionCommitted
from scadbuddy.core.paths import DataPaths
from scadbuddy.library.catalogue import Catalogue, InvalidModelMetaError
from scadbuddy.print_library.store import NativeFile
from tests.api.test_library_native import _three_mf, _upload, native_client
from tests.api.test_models_from_rows import (
    BUILTIN,
    _copy,
    _create,
    _output,
    _state,
    bundled,
    client,
)
from tests.support.operations import press

pytestmark = [pytest.mark.requires_git, pytest.mark.requires_postgres]

__all__ = ["bundled", "client", "native_client"]

PIN = {
    "name": "BOSL2",
    "url": "https://github.com/BelfrySCAD/BOSL2.git",
    "ref": "v2.0.761",
    "commit": "a" * 40,
}
#: A template with one colour parameter, ``base_color``.
COLOUR_SOURCE = 'base_color = "#FFFFFF"; // color\ncolor(base_color) cube(1);\n'


def _catalogue(client: TestClient) -> Catalogue:
    catalogue: Catalogue = _state(client).catalogue
    return catalogue


def _write_meta(paths: DataPaths, slug: str, meta: Any) -> None:
    """A hand edit: no refresh, so only the copy reads it."""
    text = meta if isinstance(meta, str) else json.dumps(meta)
    paths.model_meta(slug).write_text(text, encoding="utf-8")


def _no_meta_reads(monkeypatch: pytest.MonkeyPatch, *allowed: str) -> None:
    """Every `model.json` read but ``allowed``'s fails the test."""
    read = Catalogue.read_raw_meta

    def refused(self: Catalogue, slug: str) -> dict[str, Any]:
        if slug in allowed:
            return read(self, slug)
        raise AssertionError(f"read {slug}'s model.json")

    monkeypatch.setattr(Catalogue, "read_raw_meta", refused)


def _no_list_models(monkeypatch: pytest.MonkeyPatch) -> None:
    def refused(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("built whole records")

    monkeypatch.setattr(Catalogue, "list_models", refused)


def test_pin_index_is_the_same_after_the_copy(
    client: TestClient, paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    for slug in ("pinned", "unvalidated", "notjson"):
        _create(client, slug)
    other = {**PIN, "name": "MCAD"}
    # The first entry of a name counts; one that is no pin is skipped.
    _write_meta(paths, "pinned", {"name": "Pinned", "libraries": [PIN, {**PIN, "ref": "x"}, 5]})
    # A model.json that does not validate still pins.
    _write_meta(paths, "unvalidated", {"name": 5, "libraries": [other]})
    # One that is not JSON pins nothing here (`library_users` counts it, §7.4).
    _write_meta(paths, "notjson", '{"libraries": [BOSL2')
    # The hand edits reach the rows by the copy (#7317 phase 3: the lists read rows only).
    _copy(client)
    catalogue = _catalogue(client)
    before = catalogue.library_pin_index()
    assert [slug for slug, _ in before["BOSL2"]] == ["pinned"]
    assert [slug for slug, _ in before["MCAD"]] == ["unvalidated"]

    _copy(client)
    _no_meta_reads(monkeypatch)

    assert catalogue.library_pin_index() == before


def test_library_users_still_reads_the_files(
    client: TestClient, paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec §7.4: a removal counts a pin its row does not know (a hand edit, a failed
    refresh); the pin index reads the rows."""
    _create(client, "pinned")
    _copy(client)
    _write_meta(paths, "pinned", {"name": "Pinned", "libraries": [PIN]})
    catalogue = _catalogue(client)

    assert catalogue.library_users("BOSL2") == ["pinned"]
    assert [slug for slug, _ in catalogue.library_pins("BOSL2")] == ["pinned"]
    assert "BOSL2" not in catalogue.library_pin_index()


def test_library_associations_are_the_same_after_the_copy(
    native_client: TestClient, paths: DataPaths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file: NativeFile = _upload(native_client, _three_mf(tmp_path / "fixed.3mf"))
    other: NativeFile = _upload(native_client, _three_mf(tmp_path / "petg.3mf"))
    _create(native_client, "meshy", {"name": "Meshy"})
    _create(native_client, "plain", {"name": "Plain"})
    _write_meta(
        paths,
        "meshy",
        {
            "name": "Meshy",
            "mesh_source": {
                "kind": "bambuddy_library",
                "name": "fixed.3mf",
                "sha256": "0" * 64,
                "file_id": file.id,
            },
        },
    )
    made = native_client.post(
        f"/api/v1/print/library/{other.id}/associations",
        json={"target": "model:plain", "relation": "related_to"},
    )
    assert made.status_code == 201, made.text
    paths_read = [
        f"/api/v1/print/library/{file.id}/associations",
        f"/api/v1/print/library/{other.id}/associations",
        "/api/v1/models/meshy/associations",
        "/api/v1/models/plain/associations",
    ]

    def read() -> list[Any]:
        answers = []
        for path in paths_read:
            response = native_client.get(path)
            assert response.status_code == 200, response.text
            answers.append(response.json())
        return answers

    # The hand edit reaches the rows by the copy (#7317 phase 3: the lists read rows only).
    _copy(native_client)
    before = read()
    assert before[0]["associations"][0]["target_name"] == "Meshy"
    assert before[1]["associations"][0]["target_name"] == "Plain"

    _copy(native_client)
    _no_meta_reads(monkeypatch)

    assert read() == before


def test_a_model_removed_by_hand_is_no_files_implied_source(
    native_client: TestClient, paths: DataPaths, tmp_path: Path
) -> None:
    """#7631 review: a row whose folder is gone implies no `mesh_source` association,
    as before the copy, when the walk did not find the folder."""
    file: NativeFile = _upload(native_client, _three_mf(tmp_path / "fixed.3mf"))
    _create(native_client, "meshy", {"name": "Meshy"})
    _write_meta(
        paths,
        "meshy",
        {
            "name": "Meshy",
            "mesh_source": {
                "kind": "bambuddy_library",
                "name": "fixed.3mf",
                "sha256": "0" * 64,
                "file_id": file.id,
            },
        },
    )
    _copy(native_client)
    shutil.rmtree(paths.model_dir("meshy"))

    response = native_client.get(f"/api/v1/print/library/{file.id}/associations")

    assert response.status_code == 200, response.text
    assert response.json()["associations"] == []


def test_activity_facets_name_models_after_the_copy(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _create(client, "widget", {"name": "Widget"})
    state = _state(client)
    subjects = [Count("widget", 2), Count(BUILTIN, 1), Count("library:12", 1)]

    def labels() -> dict[str, str]:
        found: dict[str, str] = _subject_labels(
            subjects, state.catalogue, state.outputs, SubjectLabels()
        )
        return found

    before = labels()
    assert before == {"widget": "Widget", BUILTIN: "Name keychain"}

    _copy(client)
    _no_list_models(monkeypatch)
    _no_meta_reads(monkeypatch)

    assert labels() == before


def test_print_import_suggestions_are_the_same_after_the_copy(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _create(client, "basketball-trophy", {"name": "Basketball trophy"})
    _create(client, "trophy", {"name": "Trophy"})
    catalogue = _catalogue(client)
    archive: Any = SimpleNamespace(
        print_name="basketball_trophy_elan", filename="basketball_trophy_elan.gcode.3mf"
    )
    before = _suggestions(catalogue, archive)
    assert [suggestion.slug for suggestion in before] == ["basketball-trophy", "trophy"]

    _copy(client)
    _no_list_models(monkeypatch)
    _no_meta_reads(monkeypatch)

    assert _suggestions(catalogue, archive) == before


def test_outputs_tag_filter_is_the_same_after_the_copy(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _create(client, "gadget", {"name": "Gadget", "tags": ["Kitchen"]})
    _create(client, "gizmo", {"name": "Gizmo", "tags": ["garden"]})
    kitchen = _output(client, "gadget")
    _output(client, "gizmo")

    def tagged(tag: str) -> list[str]:
        response = client.get("/api/v1/outputs", params={"tag": tag})
        assert response.status_code == 200, response.text
        return [row["id"] for row in response.json()]

    before = (tagged("kitchen"), tagged("nothing"))
    assert before == ([kitchen], [])

    _copy(client)
    _no_list_models(monkeypatch)

    assert (tagged("kitchen"), tagged("nothing")) == before


def test_print_settings_come_from_the_row(
    client: TestClient, paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    _create(client, "settings", {"name": "Settings"})
    _write_meta(
        paths,
        "settings",
        {
            "name": "Settings",
            "print_settings": {"enable_support": "1"},
            "part_print_settings": {"base_color": {"wall_loops": 3}},
        },
    )
    paths.model_source("settings").write_text(COLOUR_SOURCE, encoding="utf-8")
    _create(client, "broken", {"name": "Broken"})
    _write_meta(paths, "broken", "{not json")
    # The hand edits reach the rows by the copy (#7317 phase 3: the lists read rows only).
    _copy(client)
    catalogue = _catalogue(client)
    before = (catalogue.print_settings("settings"), catalogue.part_print_settings("settings"))
    assert before[0] == {"enable_support": "1"}

    _copy(client)
    _no_meta_reads(monkeypatch)

    def refused(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("read model.scad")

    monkeypatch.setattr(Path, "read_text", refused)

    assert (catalogue.print_settings("settings"), catalogue.part_print_settings("settings")) == (
        before
    )
    assert catalogue.print_settings("gone") == {}
    assert catalogue.part_print_settings("gone") == {}
    with pytest.raises(InvalidModelMetaError, match="not JSON"):
        catalogue.print_settings("broken")


def test_part_print_settings_refuse_an_unknown_colour_from_the_row(
    client: TestClient,
    paths: DataPaths,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _create(client, "typo", {"name": "Typo"})
    _write_meta(
        paths, "typo", {"name": "Typo", "part_print_settings": {"base_colour": {"wall_loops": 3}}}
    )
    paths.model_source("typo").write_text(COLOUR_SOURCE, encoding="utf-8")
    _create(client, "unread", {"name": "Unread"})
    _write_meta(
        paths,
        "unread",
        {"name": "Unread", "part_print_settings": {"shell_color": {"wall_loops": 3}}},
    )
    # A model.scad that is not UTF-8: no colour parameters known, so only a warning.
    paths.model_source("unread").write_bytes(b"\xff\xfe cube(1);\n")
    # The hand edits reach the rows by the copy (#7317 phase 3: the lists read rows only).
    _copy(client)
    catalogue = _catalogue(client)
    refusal = "'base_colour', which is not a // color"
    with pytest.raises(InvalidModelMetaError, match=refusal):
        catalogue.part_print_settings("typo")
    before = catalogue.part_print_settings("unread")

    _copy(client)
    _no_meta_reads(monkeypatch)
    caplog.clear()

    with pytest.raises(InvalidModelMetaError, match=refusal):
        catalogue.part_print_settings("typo")
    assert catalogue.part_print_settings("unread") == before
    assert "'shell_color', which is not a // color parameter" in caplog.text


def test_a_rename_onto_a_taken_name_is_refused_from_the_rows(
    client: TestClient, paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    _create(client, "gadget", {"name": "Gadget"})
    _create(client, "widget", {"name": "Widget"})
    _create(client, "broken", {"name": "Broken"})
    _write_meta(paths, "broken", "{not json")
    _copy(client)
    # The renamed model's own read is the write's.
    _no_meta_reads(monkeypatch, "widget")

    taken = client.patch("/api/v1/models/widget", json={"name": " gadget "}, headers=press())
    assert taken.status_code == 409, taken.text
    assert "gadget" in taken.json()["detail"]
    free = client.patch("/api/v1/models/widget", json={"name": "Broken"}, headers=press())
    assert free.status_code == 200, free.text


def test_duplicates_of_reads_rows(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _create(client, "widget", {"name": "Widget"})
    duplicated = client.post(
        "/api/v1/models/widget/duplicate", json={"name": "Widget copy"}, headers=press()
    )
    assert duplicated.status_code == 201, duplicated.text
    catalogue = _catalogue(client)
    before = catalogue.duplicates_of("widget")
    assert before == ["widget-copy"]

    _copy(client)
    _no_meta_reads(monkeypatch)

    def walked(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("walked the folders")

    # Task 8 review I1: no walk of `models/` after the copy.
    monkeypatch.setattr(Path, "iterdir", walked)

    assert catalogue.duplicates_of("widget") == before
    assert catalogue.duplicates_of("widget-copy") == []


def test_a_duplicate_whose_folder_is_gone_is_not_counted(
    client: TestClient, paths: DataPaths
) -> None:
    """Final review M4: a duplicate's row left by a hand removal is no duplicate, so
    the delete check does not answer 409 naming a template that is not there."""
    _create(client, "widget", {"name": "Widget"})
    duplicated = client.post(
        "/api/v1/models/widget/duplicate", json={"name": "Widget copy"}, headers=press()
    )
    assert duplicated.status_code == 201, duplicated.text
    _copy(client)
    shutil.rmtree(paths.model_dir("widget-copy"))
    catalogue = _catalogue(client)
    assert catalogue.rows is not None and catalogue.rows.get("widget-copy") is not None

    assert catalogue.duplicates_of("widget") == []
    deleted = client.delete("/api/v1/models/widget", headers=press())
    assert deleted.status_code in (200, 204), deleted.text


def test_a_row_whose_folder_is_gone_has_no_settings_and_holds_no_name(
    client: TestClient, paths: DataPaths
) -> None:
    """Task 8 review M2: a row left by a hand removal (or a delete whose row drop
    failed) answers as `record` does: no print settings, and its name is free."""
    _create(client, "widget", {"name": "Widget"})
    _create(client, "gone", {"name": "Gone"})
    _write_meta(
        paths,
        "gone",
        {
            "name": "Gone",
            "print_settings": {"enable_support": "1"},
            "part_print_settings": {"base_color": {"wall_loops": 3}},
        },
    )
    paths.model_source("gone").write_text(COLOUR_SOURCE, encoding="utf-8")
    _copy(client)
    shutil.rmtree(paths.model_dir("gone"))
    catalogue = _catalogue(client)
    assert catalogue.rows is not None and catalogue.rows.get("gone") is not None

    assert catalogue.print_settings("gone") == {}
    assert catalogue.part_print_settings("gone") == {}
    renamed = client.patch("/api/v1/models/widget", json={"name": "Gone"}, headers=press())
    assert renamed.status_code == 200, renamed.text


def test_a_database_error_skips_only_that_templates_duplicates() -> None:
    """Task 8 review M4: the commit hook goes on past a template whose duplicates could
    not be read from the rows, as it goes on past an `OSError`."""
    published: list[Event] = []

    def duplicates_of(model_id: str) -> list[str]:
        if model_id == "broken":
            raise psycopg.OperationalError("the server closed the connection")
        return [f"{model_id}-copy"]

    catalogue: Any = SimpleNamespace(exists=lambda _: True, duplicates_of=duplicates_of)
    bus: Any = SimpleNamespace(publish=published.append)

    announce_commits(bus, catalogue)("c0ffee", ["broken", "widget"])

    assert [event.model_dump(exclude={"id", "at"}) for event in published] == [
        event.model_dump(exclude={"id", "at"})
        for event in (
            VersionCommitted(slug="broken", commit="c0ffee"),
            VersionCommitted(slug="widget", commit="c0ffee"),
            UpstreamAvailable(slug="widget-copy", upstream="widget", commit="c0ffee"),
        )
    ]
