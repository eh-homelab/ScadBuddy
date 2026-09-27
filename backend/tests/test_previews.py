"""The default-render preview store (#179 follow-up) and its place in the catalogue."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scadbuddy.core.paths import DataPaths
from scadbuddy.library.catalogue import Catalogue
from scadbuddy.library.previews import PREVIEW_ID_LENGTH, PreviewStore, source_key

SLUG = "widget"


@pytest.fixture
def paths(tmp_path: Path) -> DataPaths:
    data = DataPaths(tmp_path / "data")
    data.ensure()
    data.model_dir(SLUG).mkdir(parents=True)
    data.model_source(SLUG).write_text("cube(10);\n", encoding="utf-8")
    return data


def test_the_source_key_follows_the_source_and_its_libraries_only(paths: DataPaths) -> None:
    key = source_key(paths, SLUG)

    (paths.model_dir(SLUG) / "README.md").write_text("# Widget\n", encoding="utf-8")
    paths.model_meta(SLUG).write_text(json.dumps({"name": "Widget"}), encoding="utf-8")
    assert source_key(paths, SLUG) == key

    paths.model_meta(SLUG).write_text(json.dumps({"libraries": ["BOSL2"]}), encoding="utf-8")
    with_library = source_key(paths, SLUG)
    assert with_library != key

    paths.model_source(SLUG).write_text("cube(12);\n", encoding="utf-8")
    assert source_key(paths, SLUG) not in (key, with_library)
    assert source_key(paths, "gone") is None


def test_a_preview_is_served_only_once_rendered_and_a_failure_serves_nothing(
    paths: DataPaths,
) -> None:
    store = PreviewStore(paths)
    assert (store.image(SLUG), store.preview_id(SLUG)) == (None, None)

    store.write(SLUG, "a" * 64, b"png")
    assert store.image(SLUG) == b"png"
    assert store.preview_id(SLUG) == "a" * PREVIEW_ID_LENGTH

    store.record_failure(SLUG, "b" * 64, "openscad exited with 1")
    record = store.record(SLUG)
    assert record is not None
    assert (record.key, record.ok, record.error) == ("b" * 64, False, "openscad exited with 1")
    assert (store.image(SLUG), store.preview_id(SLUG)) == (None, None)

    store.drop(SLUG)
    assert store.record(SLUG) is None


def test_an_unreadable_record_is_no_preview(paths: DataPaths) -> None:
    store = PreviewStore(paths)
    store.write(SLUG, "a" * 64, b"png")
    paths.model_preview_record(SLUG).write_text("{not json", encoding="utf-8")

    assert store.record(SLUG) is None
    assert store.preview_id(SLUG) is None


def test_the_orphan_sweep_takes_a_gone_models_preview_but_not_a_write_in_flight(
    paths: DataPaths,
) -> None:
    store = PreviewStore(paths)
    store.write("gone", "a" * 64, b"png")
    store.write(SLUG, "a" * 64, b"png")
    in_flight = paths.previews / ".gone-abc123.png"
    in_flight.write_bytes(b"half")

    removed = Catalogue(paths).sweep_orphans()

    assert sorted(removed) == ["cache/previews/gone.json", "cache/previews/gone.png"]
    assert store.image(SLUG) == b"png"
    assert in_flight.exists()


def test_the_catalogue_ranks_its_own_image_over_the_preview(paths: DataPaths) -> None:
    store = PreviewStore(paths)
    catalogue = Catalogue(paths, previews=store)
    store.write(SLUG, "a" * 64, b"preview")

    origin = catalogue.thumbnail_source(SLUG)
    assert (origin.source, origin.preview_id) == ("preview", "a" * PREVIEW_ID_LENGTH)
    assert catalogue.thumbnail(SLUG) == b"preview"

    catalogue.write_thumbnail(SLUG, b"own")
    assert catalogue.thumbnail_source(SLUG).source == "model"
    assert catalogue.thumbnail(SLUG) == b"own"
    assert store.image(SLUG) is None
