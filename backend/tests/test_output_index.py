"""`OutputIndex` (#4777, order operations spec 2026-11-07 §3.1): every model's saved
outputs as rows, so one query lists, filters and pages them; the files stay the record."""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scadbuddy.core.paths import DataPaths
from scadbuddy.library.output_index import (
    AfterNotListedError,
    OutputIndex,
    OutputQuery,
    params_text,
)
from scadbuddy.library.outputs import META_NAME, PARAMS_NAME, OutputMeta, OutputStore
from scadbuddy.render.glb import BoundingBox
from scadbuddy.render.projection import JobProjection

pytestmark = pytest.mark.requires_postgres

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)


@pytest.fixture
def projection(pg_conninfo: str) -> Iterator[JobProjection]:
    jobs = JobProjection(pg_conninfo, pool_size=2)
    jobs.open()
    try:
        yield jobs
    finally:
        jobs.close()


@pytest.fixture
def store(tmp_path: Path, projection: JobProjection) -> OutputStore:
    paths = DataPaths(root=tmp_path)
    outputs = OutputStore(paths)
    outputs.index = OutputIndex(projection.pool, paths)
    return outputs


def index_of(store: OutputStore) -> OutputIndex:
    assert store.index is not None
    return store.index


def saved(
    store: OutputStore,
    slug: str,
    output_id: str,
    *,
    age: timedelta = timedelta(0),
    name: str | None = None,
    params: dict[str, object] | None = None,
) -> OutputMeta:
    """An output's files as a save writes them, with no index row."""
    meta = OutputMeta(
        id=output_id,
        slug=slug,
        name=name,
        job_id="j",
        created_at=NOW - age,
        bbox_mm=BoundingBox(min=(0, 0, 0), max=(1, 1, 1), size=(1, 1, 1)),
    )
    directory = store.paths.outputs / slug / output_id
    directory.mkdir(parents=True)
    (directory / META_NAME).write_text(meta.model_dump_json(), encoding="utf-8")
    (directory / PARAMS_NAME).write_text(json.dumps(params or {}), encoding="utf-8")
    return meta


def ids(store: OutputStore, **query: object) -> list[str]:
    found, _ = index_of(store).query(OutputQuery(**query))  # type: ignore[arg-type]
    return found


def test_params_text_keeps_only_what_differs_from_the_defaults() -> None:
    params = {"width": 12, "label": "hi", "round": True, "colour": "#FF0000"}
    defaults = {"width": 10, "label": "hi", "round": False}
    assert params_text(params, defaults) == "colour=#FF0000 round=true width=12"
    # No defaults known: every parameter is searchable.
    assert params_text({"label": "hi"}, {}) == "label=hi"


def test_the_sync_fills_in_saved_outputs_and_drops_gone_ones(store: OutputStore) -> None:
    saved(store, "sign", "a" * 32, age=timedelta(hours=2))
    saved(store, "tag", "b" * 32, age=timedelta(hours=1))
    assert ids(store) == []
    assert index_of(store).sync(store) == (2, 0)
    assert ids(store) == ["b" * 32, "a" * 32]
    # Run again it changes nothing.
    assert index_of(store).sync(store) == (0, 0)
    shutil.rmtree(store.paths.outputs / "tag" / ("b" * 32))
    assert index_of(store).sync(store) == (0, 1)
    assert ids(store) == ["a" * 32]


def test_a_row_written_while_the_sync_reads_is_kept(store: OutputStore) -> None:
    """A save between the sync's read of the files and its drop is not dropped: only rows
    indexed before the sync began go."""
    meta = saved(store, "sign", "c" * 32)
    index = index_of(store)
    started = index.now()
    shutil.rmtree(store.paths.outputs / "sign")
    index.record(meta, {})
    assert index.drop_missing(set(), before=started) == 0
    assert ids(store) == ["c" * 32]


def test_the_query_filters_by_time_model_and_text(store: OutputStore) -> None:
    rows: list[tuple[OutputMeta, dict[str, object]]] = [
        (saved(store, "sign", "1" * 32, age=timedelta(days=3), name="Door"), {"w": 30}),
        (saved(store, "sign", "2" * 32, age=timedelta(hours=5)), {"label": "Ada"}),
        (saved(store, "tag", "3" * 32, age=timedelta(minutes=5), name="Gift"), {"w": 10}),
    ]
    for meta, params in rows:
        index_of(store).record(meta, params)
    assert ids(store, since=NOW - timedelta(days=1)) == ["3" * 32, "2" * 32]
    assert ids(store, until=NOW - timedelta(days=1)) == ["1" * 32]
    assert ids(store, slugs=["sign"]) == ["2" * 32, "1" * 32]
    assert ids(store, slugs=[]) == []
    # `q` matches the name or the parameter text, case folded; % and _ are literal.
    assert ids(store, q="door") == ["1" * 32]
    assert ids(store, q="ada") == ["2" * 32]
    assert ids(store, q="w=") == ["3" * 32, "1" * 32]
    assert ids(store, q="%") == []
    found, total = index_of(store).query(OutputQuery(limit=2))
    assert (found, total) == (["3" * 32, "2" * 32], 3)
    found, total = index_of(store).query(OutputQuery(limit=2, after="2" * 32))
    assert (found, total) == (["1" * 32], 3)
    with pytest.raises(AfterNotListedError):
        index_of(store).query(OutputQuery(after="9" * 32))


def test_rename_and_delete_keep_the_row_in_step(store: OutputStore) -> None:
    meta = saved(store, "sign", "d" * 32, name="Old")
    index_of(store).record(meta, {})
    store.rename(meta.id, "Brand new")
    assert ids(store, q="brand") == [meta.id]
    store.delete(meta.id)
    assert ids(store) == []
