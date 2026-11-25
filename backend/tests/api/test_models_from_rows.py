"""`slugs`, `list_models` and `record` read the `templates` rows (#7317 phase 2, plan
Task 7; phase 3: the app's start copies first, so they read rows from the start). A test
that edits template files by hand runs the copy (`_copy`), as production's next copy run
repairs such an edit. The lists are compared with what the files say: each template's
`Catalogue._fresh_record`, through the routes' own selection, sort and window."""

from __future__ import annotations

import dataclasses
import json
import shutil
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import TypeAdapter

from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.api.models import ModelFacets, select_models, sort_models, tag_counts
from scadbuddy.core.paths import DataPaths
from scadbuddy.library import catalogue as catalogue_module
from scadbuddy.library.catalogue import (
    Catalogue,
    InvalidModelMetaError,
    ModelNotFoundError,
    ModelRecord,
)
from scadbuddy.library.history import ModelHistory
from scadbuddy.library.output_copy import copy_outputs
from scadbuddy.library.template_refresh import TemplateRefresh
from scadbuddy.library.template_rows import TemplateRows
from tests.api.conftest import PNG_BYTES, set_plate_image, wait_for_job
from tests.support.operations import press
from tests.support.template_rows import sync_template_rows

pytestmark = [pytest.mark.requires_git, pytest.mark.requires_postgres]

BUILTIN = "builtin:name-keychain"
COVER = b"\x89PNG\r\n\x1a\n" + b"an output's plate cover"
PREVIEW = b"\x89PNG\r\n\x1a\n" + b"a default-render preview"


@pytest.fixture
def bundled(seed_dir: Path) -> Path:
    directory = seed_dir / "name-keychain"
    directory.mkdir()
    (directory / "model.scad").write_text('width = 10;\nlabel = "hi";\n', encoding="utf-8")
    meta = {"name": "Name keychain", "tags": ["keychain", "Gift"]}
    (directory / "model.json").write_text(json.dumps(meta), encoding="utf-8")
    (directory / "thumbnail.png").write_bytes(PNG_BYTES)
    return directory


@pytest.fixture
def client(app: FastAPI, bundled: Path) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        _state(test_client).catalogue.serve_previews = True
        yield test_client


def _state(client: TestClient) -> AppState:
    state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    return state


def _rows(client: TestClient) -> TemplateRows:
    refresh = _state(client).catalogue.refresh
    assert refresh is not None
    rows: TemplateRows = refresh.rows
    return rows


def _create(client: TestClient, slug: str, meta: dict[str, Any] | None = None) -> None:
    files = {"file": (f"{slug}.scad", b"width = 10;\ncube(width);\n", "text/plain")}
    if meta is not None:
        files["meta"] = ("model.json", json.dumps(meta).encode(), "application/json")
    response = client.post("/api/v1/models", files=files, headers=press())
    assert response.status_code == 201, response.text


def _output(client: TestClient, slug: str) -> str:
    job_id = client.post(f"/api/v1/models/{slug}/render", json={"params": {"width": 12}}).json()[
        "job_id"
    ]
    wait_for_job(client, job_id)
    set_plate_image(client, job_id, COVER)
    response = client.post(
        f"/api/v1/models/{slug}/outputs", json={"job_id": job_id}, headers=press()
    )
    assert response.status_code == 201, response.text
    output_id: str = response.json()["id"]
    return output_id


def _copy(client: TestClient) -> None:
    """The outputs copied first (so the output covers read rows), then the templates:
    the copy that repairs the hand edits a test made (#7317 phase 3: the app's start
    already copied once, so the catalogue reads rows before it too)."""
    state = _state(client)
    outputs = state.outputs.rows
    assert outputs is not None
    if not outputs.copied():
        copy_outputs(state.outputs, outputs, state.projection.conninfo)
    deadline = time.monotonic() + 30
    while not outputs.copied():
        assert time.monotonic() < deadline, "no finished outputs copy"
        time.sleep(0.1)
    sync_template_rows(client)


@pytest.fixture
def world(client: TestClient, paths: DataPaths) -> TestClient:
    """Templates of mine and a built-in, media (one added to the built-in), a duplicate
    whose upstream moved since, an output with a plate cover, a preview, and a template
    whose `model.json` is not JSON."""
    _create(client, "widget", {"name": "Widget", "tags": ["Gift", "box"], "description": "d"})
    assert (
        client.post(
            "/api/v1/models/widget/media",
            files={"file": ("cover.png", PNG_BYTES, "image/png")},
            headers=press(),
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/v1/models/{BUILTIN}/media",
            files={"file": ("added.png", PNG_BYTES + b"!", "image/png")},
            headers=press(),
        ).status_code
        == 200
    )
    duplicated = client.post(
        "/api/v1/models/widget/duplicate", json={"name": "Widget copy"}, headers=press()
    )
    assert duplicated.status_code == 201, duplicated.text
    patched = client.patch(
        "/api/v1/models/widget", json={"description": "moved on"}, headers=press()
    )
    assert patched.status_code == 200, patched.text
    _create(client, "gadget", {"name": "Gadget", "tags": ["box"]})
    _output(client, "gadget")
    _create(client, "plain")
    previews = _state(client).catalogue.previews
    assert previews is not None
    assert previews.write("plain", "f" * 64, PREVIEW)
    _create(client, "broken", {"name": "Broken"})
    # A hand edit: no refresh, so only the copy reads it.
    paths.model_meta("broken").write_text("{not json", encoding="utf-8")
    return client


#: Every `GET /models` the list answers, with and without the filters.
LIST_QUERIES: tuple[dict[str, Any], ...] = (
    {},
    {"limit": 2},
    {"limit": 2, "after": "gadget"},
    {"limit": 3, "after": "broken"},
    {"q": "widget"},
    {"tag": "box"},
    {"tag": "gift"},
    {"origin": "builtin"},
    {"origin": "mine"},
    {"sort": "name"},
    {"sort": "updated"},
)


def _lists(client: TestClient) -> list[tuple[int, str | None, Any]]:
    answers = []
    for query in LIST_QUERIES:
        response = client.get("/api/v1/models", params=query)
        answers.append(
            (response.status_code, response.headers.get("X-Total-Count"), response.json())
        )
    return answers


def _from_files(client: TestClient) -> tuple[list[str], list[ModelRecord]]:
    """Every template id, and the record the files give of each one a listing holds:
    `_fresh_record` with the fields a listing leaves out. A template whose `model.json`
    cannot be read is not listed."""
    catalogue = _state(client).catalogue
    slugs = catalogue.slugs()
    records = []
    for slug in slugs:
        try:
            record = catalogue._fresh_record(slug)
        except InvalidModelMetaError:
            continue
        records.append(record.model_copy(update={"previous_version": None, "ui_version": None}))
    return slugs, records


def _json(records: list[ModelRecord]) -> Any:
    return TypeAdapter(list[ModelRecord]).dump_python(records, mode="json", by_alias=True)


def _expected_lists(client: TestClient) -> list[tuple[int, str | None, Any]]:
    """`LIST_QUERIES` answered from the files: an unfiltered page is a window of the
    template ids (`X-Total-Count` counts them all), filled past a template left out; a
    filtered one goes through the route's own `select_models` and `sort_models`."""
    slugs, records = _from_files(client)
    answers: list[tuple[int, str | None, Any]] = []
    for query in LIST_QUERIES:
        if set(query) <= {"limit", "after"}:
            after = query.get("after")
            start = 0 if after is None else slugs.index(after) + 1
            shown = [record for record in records if slugs.index(record.slug) >= start]
            limit = query.get("limit")
            page = shown if limit is None else shown[:limit]
            answers.append((200, str(len(slugs)), _json(page)))
            continue
        tag = query.get("tag")
        selected = sort_models(
            select_models(
                records, query.get("q"), None if tag is None else [tag], query.get("origin")
            ),
            query.get("sort"),
        )
        answers.append((200, str(len(selected)), _json(selected)))
    return answers


def test_get_models_is_what_the_files_say(world: TestClient) -> None:
    _copy(world)

    answers = _lists(world)

    listed = {model["slug"]: model for model in answers[0][2]}
    # The fixture is what it says, so the comparison covers each source.
    assert listed["widget-copy"]["upstream_state"] == "update"
    assert listed["gadget"]["thumbnail_source"] == "output"
    assert listed["plain"]["thumbnail_source"] == "preview"
    assert listed["widget"]["thumbnail_source"] == "model"
    assert next(item["id"] for item in listed[BUILTIN]["media"]) == "thumbnail"
    assert len(listed[BUILTIN]["media"]) == 2
    assert answers == _expected_lists(world)


def test_model_facets_are_what_the_files_say(world: TestClient) -> None:
    queries: list[dict[str, Any]] = [
        {},
        {"q": "widget"},
        {"tag": ["box", "gift"]},
        {"origin": "mine"},
    ]
    _copy(world)
    _, records = _from_files(world)

    for query in queries:
        selected = select_models(records, query.get("q"), query.get("tag"), query.get("origin"))
        expected = ModelFacets(
            total=len(records),
            matched=len(selected),
            tags=tag_counts(selected, query.get("tag") or []),
        )
        answer = world.get("/api/v1/model-facets", params=query).json()
        assert answer == expected.model_dump(mode="json"), query


def test_get_model_is_what_the_files_say(world: TestClient) -> None:
    _copy(world)
    catalogue = _state(world).catalogue

    for slug in ["widget", "widget-copy", "gadget", "plain", BUILTIN]:
        response = world.get(f"/api/v1/models/{slug}")
        assert response.status_code == 200, response.text
        assert response.json() == catalogue._fresh_record(slug).model_dump(mode="json"), slug
    assert world.get("/api/v1/models/widget").json()["previous_version"] is not None
    # Its `model.json` is not JSON: the answer says so, as its files do.
    broken = world.get("/api/v1/models/broken")
    assert broken.status_code == 409, broken.text
    assert "not JSON" in broken.json()["detail"]
    with pytest.raises(InvalidModelMetaError, match="not JSON"):
        catalogue._fresh_record("broken")
    assert world.get("/api/v1/models/missing").status_code == 404


def test_unreadable_meta_counts_but_is_not_listed(world: TestClient) -> None:
    _copy(world)

    response = world.get("/api/v1/models")
    slugs = _state(world).catalogue.slugs()
    assert "broken" in slugs
    assert response.headers["X-Total-Count"] == str(len(slugs))
    assert "broken" not in [model["slug"] for model in response.json()]
    facets = world.get("/api/v1/model-facets").json()
    assert facets["total"] == len(slugs) - 1


def test_list_models_makes_no_file_or_git_call_after_the_copy(
    world: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _copy(world)
    catalogue = _state(world).catalogue
    expected = [record.slug for record in catalogue.list_models()]

    def refused(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a file or git call")

    # The listing asks once whether git is there (`_has_history`, review M3): the
    # one stat of `.git` a listing makes, answered here without the disk.
    monkeypatch.setattr(ModelHistory, "available", property(lambda self: True))
    for name in ("stat", "is_file", "iterdir", "read_text", "read_bytes"):
        monkeypatch.setattr(Path, name, refused)
    monkeypatch.setattr(ModelHistory, "_run", refused)

    assert [record.slug for record in catalogue.list_models()] == expected
    assert catalogue.slugs()


def test_slugs_order_is_the_walks_order(client: TestClient, paths: DataPaths) -> None:
    for slug in ("b", "B", "_x", "é"):
        (paths.models / slug).mkdir()
        (paths.models / slug / "model.scad").write_text("cube(1);\n", encoding="utf-8")

    _copy(client)

    # The walk's order: by code point, mine before the built-ins.
    assert _state(client).catalogue.slugs() == ["B", "_x", "b", "é", BUILTIN]


def test_a_template_with_no_row_is_not_listed_after_the_copy(
    world: TestClient, paths: DataPaths
) -> None:
    _copy(world)
    # An old pod's create: the files, and no row.
    (paths.models / "unrowed").mkdir()
    (paths.models / "unrowed" / "model.scad").write_text("cube(1);\n", encoding="utf-8")

    listed = [model["slug"] for model in world.get("/api/v1/models").json()]
    assert "unrowed" not in listed
    assert "unrowed" not in _state(world).catalogue.slugs()


def test_record_of_a_template_with_no_row_falls_back_to_its_files(
    world: TestClient, paths: DataPaths, caplog: pytest.LogCaptureFixture
) -> None:
    _copy(world)
    (paths.models / "unrowed").mkdir()
    (paths.models / "unrowed" / "model.scad").write_text("cube(1);\n", encoding="utf-8")
    (paths.models / "unrowed" / "model.json").write_text('{"name": "No row"}', encoding="utf-8")
    assert _rows(world).get("unrowed") is None

    response = world.get("/api/v1/models/unrowed")

    assert response.status_code == 200, response.text
    assert response.json()["name"] == "No row"
    assert any("no row" in record.getMessage() for record in caplog.records)
    with pytest.raises(ModelNotFoundError):
        _state(world).catalogue.record("nowhere")


def test_a_write_answers_its_change_when_the_refresh_failed(
    world: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _copy(world)
    rows = _rows(world)

    def failing(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("the row write failed")

    # The read before the HEAD transaction, and the no-commit path's put.
    monkeypatch.setattr(TemplateRefresh, "read", failing)
    monkeypatch.setattr(TemplateRows, "put", failing)

    response = world.patch("/api/v1/models/widget", json={"name": "Renamed"}, headers=press())

    assert response.status_code == 200, response.text
    assert response.json()["name"] == "Renamed"
    row = rows.get("widget")
    assert row is not None and row.name == "Widget"


def test_the_catalogue_reads_rows_from_the_start(world: TestClient) -> None:
    """The app's start copied first (phase 3, `main._first_template_copy`)."""
    catalogue: Catalogue = _state(world).catalogue
    assert catalogue.rows is _rows(world)
    assert _rows(world).copied()


def _refresh_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every refresh fails: the files and the media rows change, the `templates` row
    does not (the HEAD transaction's read, and the no-commit path's put)."""

    def failing(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("the row write failed")

    monkeypatch.setattr(TemplateRefresh, "read", failing)
    monkeypatch.setattr(TemplateRows, "put", failing)


def test_media_the_row_does_not_know_yet_is_not_missing(
    world: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review I1: the media rows commit before the template's row; an item the row's
    `files` lacks is read from the disk, not shown as missing."""
    _copy(world)
    _refresh_fails(monkeypatch)
    added = world.post(
        "/api/v1/models/plain/media",
        files={"file": ("first.png", PNG_BYTES, "image/png")},
        headers=press(),
    )
    assert added.status_code == 200, added.text
    row = _rows(world).get("plain")
    assert row is not None and not any(key.startswith("media/") for key in row.files)

    [item] = world.get("/api/v1/models/plain").json()["media"]
    assert (item["missing"], item["size"]) == (False, len(PNG_BYTES))
    listed = {model["slug"]: model for model in world.get("/api/v1/models").json()}
    [item] = listed["plain"]["media"]
    assert (item["missing"], item["size"]) == (False, len(PNG_BYTES))
    assert listed["plain"]["thumbnail_source"] == "model"


def test_media_really_missing_after_the_row_caught_up_costs_no_stat(
    world: TestClient, paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task 8 review M1: a media file that is gone when its row was refreshed is
    `missing` with no stat; only a row older than the media rows (a lag, the test
    above) reads the disk."""
    _copy(world)
    added = world.post(
        "/api/v1/models/plain/media",
        files={"file": ("first.png", PNG_BYTES, "image/png")},
        headers=press(),
    )
    assert added.status_code == 200, added.text
    [item] = added.json()["media"]
    (paths.model_dir("plain") / "media" / item["file"]).unlink()
    refresh = _state(world).catalogue.refresh
    assert refresh is not None
    refresh.refresh_locked(["plain"], path="no_commit")
    row = _rows(world).get("plain")
    assert row is not None and f"media/{item['file']}" not in row.files

    def stat(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("stat'ed a media file")

    monkeypatch.setattr(catalogue_module, "_file_size", stat)

    [item] = world.get("/api/v1/models/plain").json()["media"]
    assert (item["missing"], item["size"]) == (True, None)
    listed = {model["slug"]: model for model in world.get("/api/v1/models").json()}
    [item] = listed["plain"]["media"]
    assert (item["missing"], item["size"]) == (True, None)


def test_a_row_whose_folder_is_gone_is_not_found(world: TestClient, paths: DataPaths) -> None:
    """Review M1: a hand removal leaves the row; the record and the source are 404."""
    _copy(world)
    shutil.rmtree(paths.model_dir("plain"))
    assert _rows(world).get("plain") is not None

    assert world.get("/api/v1/models/plain").status_code == 404
    assert world.get("/api/v1/models/plain/source").status_code == 404


def test_a_meta_postgres_cannot_store_is_read_from_its_file(
    world: TestClient, paths: DataPaths
) -> None:
    """Review M2: a `model.json` jsonb refuses (NaN) has no `meta` in its row; its
    record, its place in the list (the tag filter and the facets too) and its summary
    are read from the file (#7631 review)."""
    _create(world, "nan", {"name": "Not a number"})
    paths.model_meta("nan").write_text(
        '{"name": "Not a number", "tags": ["odd"], "ratio": NaN}', encoding="utf-8"
    )
    _copy(world)
    row = _rows(world).get("nan")
    assert row is not None and row.meta is None
    assert row.meta_error is not None and row.meta_error.startswith("not storable")
    catalogue = _state(world).catalogue

    def read() -> tuple[Any, ...]:
        record = world.get("/api/v1/models/nan")
        odd = world.get("/api/v1/models", params={"tag": "odd"})
        return (
            (record.status_code, record.json()),
            _lists(world),
            (odd.status_code, odd.json()),
            world.get("/api/v1/model-facets").json(),
            catalogue.list_summaries(),
            catalogue.list_summaries(["nan", "widget"]),
        )

    before = read()
    assert before[0][0] == 200, before[0][1]
    assert "nan" in [model["slug"] for model in before[1][0][2]]
    assert [model["slug"] for model in before[2][1]] == ["nan"]
    assert "nan" in [summary.slug for summary in before[4]]
    assert [summary.slug for summary in before[5]] == ["nan", "widget"]
    assert before[0][1] == catalogue._fresh_record("nan").model_dump(mode="json")
    assert before[1] == _expected_lists(world)

    _copy(world)

    assert read() == before


def test_a_not_storable_row_whose_folder_is_gone_is_in_neither_list(
    world: TestClient, paths: DataPaths
) -> None:
    """#7661: a not-storable row whose folder was removed by hand is left out of the
    summaries, as the list leaves it out."""
    _create(world, "nan", {"name": "Not a number"})
    paths.model_meta("nan").write_text(
        '{"name": "Not a number", "tags": ["odd"], "ratio": NaN}', encoding="utf-8"
    )
    _copy(world)
    row = _rows(world).get("nan")
    assert row is not None and row.meta is None
    shutil.rmtree(paths.model_dir("nan"))
    catalogue = _state(world).catalogue

    listed = [record.slug for record in catalogue.list_models()]
    assert "nan" not in listed
    assert [summary.slug for summary in catalogue.list_summaries()] == listed
    assert [summary.slug for summary in catalogue.list_summaries(["nan", "widget"])] == [
        record.slug for record in catalogue.list_models(["nan", "widget"])
    ]


def test_list_and_record_agree_on_upstream_state_with_no_row_version(world: TestClient) -> None:
    """Review M3: a duplicate whose row has no version (git failed at its refresh) is
    judged against its upstream in the list as in its record."""
    _copy(world)
    rows = _rows(world)
    row = rows.get("widget-copy")
    assert row is not None
    rows.put([dataclasses.replace(row, version=None)], [])

    record = world.get("/api/v1/models/widget-copy").json()
    listed = {model["slug"]: model for model in world.get("/api/v1/models").json()}
    assert record["upstream_state"] == "update"
    assert listed["widget-copy"]["upstream_state"] == record["upstream_state"]


def _statements(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every statement sent from here on, as its text."""
    sent: list[str] = []
    execute = psycopg.Cursor.execute
    executemany = psycopg.Cursor.executemany

    def counted(self: psycopg.Cursor[Any], query: Any, *args: Any, **kwargs: Any) -> Any:
        sent.append(str(query))
        return execute(self, query, *args, **kwargs)

    def counted_many(self: psycopg.Cursor[Any], query: Any, *args: Any, **kwargs: Any) -> Any:
        sent.append(str(query))
        return executemany(self, query, *args, **kwargs)

    monkeypatch.setattr(psycopg.Cursor, "execute", counted)
    monkeypatch.setattr(psycopg.Cursor, "executemany", counted_many)
    return sent


def test_a_listing_from_rows_sends_a_fixed_number_of_statements(
    world: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review M4 (spec §5.4): the statements of a list do not grow with the templates."""
    _copy(world)
    catalogue = _state(world).catalogue
    catalogue.list_models()  # `copied()` is remembered from here on

    def count(slugs: list[str] | None) -> int:
        with pytest.MonkeyPatch.context() as patch:
            sent = _statements(patch)
            catalogue.list_models(slugs)
        return len(sent)

    templates = len(catalogue.slugs())
    small = (count(None), count(catalogue.slugs()))
    for index in range(4):
        _create(world, f"more-{index}", {"name": f"More {index}", "tags": ["box"]})
        added = world.post(
            f"/api/v1/models/more-{index}/media",
            files={"file": ("cover.png", PNG_BYTES, "image/png")},
            headers=press(),
        )
        assert added.status_code == 200, added.text
    duplicated = world.post(
        "/api/v1/models/more-0/duplicate", json={"name": "More copy"}, headers=press()
    )
    assert duplicated.status_code == 201, duplicated.text

    assert len(catalogue.slugs()) == templates + 5
    assert (count(None), count(catalogue.slugs())) == small


def test_a_window_of_the_list_reads_only_its_rows(
    world: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review M5: a page's chunk loads its own rows (and its upstreams'), never the table."""
    _copy(world)
    catalogue = _state(world).catalogue
    whole = {record.slug: record for record in catalogue.list_models()}

    def refused(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the whole table was read")

    monkeypatch.setattr(TemplateRows, "all", refused)

    [copy] = catalogue.list_models(["widget-copy"])
    assert copy == whole["widget-copy"]
    assert copy.upstream_state == "update"
