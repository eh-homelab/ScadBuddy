"""The catalogue's readers read only the `templates` rows (#7317 phase 3, plan Task 2):
with no finished copy run they do not walk the folders or the git history. A template
with no row is not listed until the next copy run, and its own record is read from its
files (spec §7.6). The rows are written by each create's refresh; then every copy run is
deleted, so that no run has finished."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from scadbuddy.core.paths import DataPaths
from scadbuddy.library.catalogue import Catalogue
from scadbuddy.library.history import ModelHistory
from scadbuddy.render.solids import WRAPPER_PREFIX
from tests.api.conftest import PNG_BYTES
from tests.api.test_model_readers_from_rows import _no_meta_reads
from tests.api.test_models_from_rows import (
    _copy,
    _create,
    _refresh_fails,
    _rows,
    _state,
    bundled,
    client,
)
from tests.api.test_thumbnail_from_rows import _no_reads, _thumbnail
from tests.support.media import MemoryMediaStore
from tests.support.operations import press
from tests.support.template_rows import sync_template_rows

pytestmark = [pytest.mark.requires_git, pytest.mark.requires_postgres]

__all__ = ["bundled", "client"]

BUILTIN = "builtin:name-keychain"
CREATED = ("widget", "gadget")


def _no_finished_run(client: TestClient, pg_conninfo: str) -> None:
    """No copy run in the database, and none the app's rows remember (`copied` keeps a
    true answer)."""
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        conn.execute("DELETE FROM template_copy_runs")
    _rows(client)._copied = False


@pytest.fixture
def made(client: TestClient, pg_conninfo: str) -> TestClient:
    """Templates made through the API, so their rows come from the refresh, and the
    outputs copied (the list's output covers read rows); then no finished run."""
    _create(client, "widget", {"name": "Widget", "tags": ["box"]})
    _create(client, "gadget", {"name": "Gadget"})
    _copy(client)
    _no_finished_run(client, pg_conninfo)
    assert not _rows(client).copied()
    return client


def _listed(client: TestClient) -> list[str]:
    response = client.get("/api/v1/models")
    assert response.status_code == 200, response.text
    return [model["slug"] for model in response.json()]


def _no_walks(monkeypatch: pytest.MonkeyPatch, paths: DataPaths) -> None:
    """No file under the data volume is listed, stat'ed or read, and no git call runs.
    The one stat of the models' ``.git`` (`GitRepo.available`, judged once per listing)
    is no walk, so it is allowed."""
    git_dir = paths.models / ".git"
    for name in ("iterdir", "stat", "is_file", "read_text", "read_bytes"):
        original = getattr(Path, name)

        def guarded(self: Path, *args: Any, _original: Any = original, **kwargs: Any) -> Any:
            if self.is_relative_to(paths.root) and self != git_dir:
                raise AssertionError(f"touched {self}")
            return _original(self, *args, **kwargs)

        monkeypatch.setattr(Path, name, guarded)

    def git(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("ran git")

    monkeypatch.setattr(ModelHistory, "_run", git)


def test_lists_read_rows_with_no_finished_run(
    made: TestClient, paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalogue = _state(made).catalogue
    expected = [*CREATED, BUILTIN]
    _no_walks(monkeypatch, paths)

    assert sorted(catalogue.slugs()) == sorted(expected)
    assert sorted(record.slug for record in catalogue.list_models()) == sorted(expected)
    assert sorted(summary.slug for summary in catalogue.list_summaries()) == sorted(expected)
    assert sorted(catalogue.raw_metas()) == sorted(expected)
    assert catalogue.library_pin_index() == {}


def test_model_facets_read_rows_with_no_finished_run(
    made: TestClient, paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_walks(monkeypatch, paths)

    response = made.get("/api/v1/model-facets")

    assert response.status_code == 200, response.text
    assert response.json()["total"] == len(CREATED) + 1


def test_a_thumbnail_304_reads_no_file_with_no_finished_run(
    made: TestClient, paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    added = made.post(
        "/api/v1/models/widget/media",
        files={"file": ("cover.png", PNG_BYTES, "image/png")},
        headers=press(),
    )
    assert added.status_code == 200, added.text
    etag = _thumbnail(made, "widget").headers["etag"]
    _no_reads(monkeypatch, paths)

    again = _thumbnail(made, "widget", etag)

    assert (again.status_code, again.content) == (304, b"")
    assert again.headers["etag"] == etag


def test_a_rename_onto_a_taken_name_is_refused_with_no_finished_run(
    made: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The renamed model's own read is the write's.
    _no_meta_reads(monkeypatch, "widget")

    taken = made.patch("/api/v1/models/widget", json={"name": " gadget "}, headers=press())

    assert taken.status_code == 409, taken.text
    assert "gadget" in taken.json()["detail"]


def test_a_hand_made_template_is_listed_after_the_next_copy(
    made: TestClient, paths: DataPaths
) -> None:
    """Review Focus 1."""
    directory = paths.models / "handmade"
    directory.mkdir()
    (directory / "model.scad").write_text("cube(10);\n", encoding="utf-8")
    (directory / "model.json").write_text(json.dumps({"name": "Hand made"}), encoding="utf-8")

    assert "handmade" not in _listed(made)
    record = made.get("/api/v1/models/handmade")
    assert record.status_code == 200, record.text
    assert record.json()["name"] == "Hand made"

    sync_template_rows(made)

    assert "handmade" in _listed(made)


def test_a_create_whose_refresh_failed_is_readable_but_not_listed(
    made: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review Focus 2."""
    with monkeypatch.context() as failing:
        _refresh_fails(failing)
        created = made.post(
            "/api/v1/models",
            files={"file": ("late.scad", b"cube(1);\n", "text/plain")},
            headers=press(),
        )
        assert created.status_code == 201, created.text
        assert created.json()["slug"] == "late"
    assert _rows(made).get("late") is None

    record = made.get("/api/v1/models/late")
    assert record.status_code == 200, record.text
    assert "late" not in _listed(made)

    sync_template_rows(made)

    assert "late" in _listed(made)


def test_a_not_storable_row_is_read_from_its_file_only(
    made: TestClient, paths: DataPaths, pg_conninfo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review Focus 4: a `model.json` Postgres cannot store (NaN) is listed, summarised,
    name-checked and judged as a duplicate from its own file, with no walk."""
    duplicated = made.post(
        "/api/v1/models/widget/duplicate", json={"name": "Odd copy"}, headers=press()
    )
    assert duplicated.status_code == 201, duplicated.text
    slug = duplicated.json()["slug"]
    meta = json.loads(paths.model_meta(slug).read_text(encoding="utf-8"))
    meta["ratio"] = float("nan")
    paths.model_meta(slug).write_text(json.dumps(meta), encoding="utf-8")
    sync_template_rows(made)
    row = _rows(made).get(slug)
    assert row is not None and row.meta is None
    _no_finished_run(made, pg_conninfo)
    catalogue = _state(made).catalogue
    models = paths.models
    iterdir = Path.iterdir

    def no_walk(self: Path) -> Any:
        if self.is_relative_to(models):
            raise AssertionError(f"walked {self}")
        return iterdir(self)

    monkeypatch.setattr(Path, "iterdir", no_walk)

    assert slug in _listed(made)
    assert slug in [summary.slug for summary in catalogue.list_summaries()]
    assert catalogue.duplicates_of("widget") == [slug]
    taken = made.patch("/api/v1/models/gadget", json={"name": "Odd copy"}, headers=press())
    assert taken.status_code == 409, taken.text


def test_list_readers_refuse_without_rows(paths: DataPaths) -> None:
    """Review Focus 5."""
    catalogue = Catalogue(paths, media_store=MemoryMediaStore(), wrapper_prefix=WRAPPER_PREFIX)
    assert catalogue.rows is None

    with pytest.raises(RuntimeError, match="#7317"):
        catalogue.slugs()
    with pytest.raises(RuntimeError, match="#7317"):
        catalogue.list_models()
