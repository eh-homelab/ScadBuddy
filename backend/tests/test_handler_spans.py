"""The spans that tell where a handler's time goes (#7245): the thread wait, the output
store's NFS globs and the asset sweep's walk."""

from __future__ import annotations

from pathlib import Path

from opentelemetry import trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from scadbuddy.api.outputs import _live
from scadbuddy.core.paths import DataPaths
from scadbuddy.core.tracing import THREAD_WAIT_ATTRIBUTE, span, traced_thread
from scadbuddy.library.assets import referenced_asset_ids
from scadbuddy.library.outputs import META_NAME, OutputStore

OUTPUT_ID = "a" * 32


def _output(paths: DataPaths, slug: str, output_id: str) -> None:
    directory = paths.outputs / slug / output_id
    directory.mkdir(parents=True)
    (directory / META_NAME).write_text("{}", encoding="utf-8")


async def test_traced_thread_records_the_wait_and_parents_the_threads_spans(
    spans: InMemorySpanExporter,
) -> None:
    def work(value: int) -> int:
        with span("inside"):
            return value * 2

    assert await traced_thread("outer", work, 21, attributes={"scadbuddy.slug": "demo"}) == 42
    finished = {s.name: s for s in spans.get_finished_spans()}
    outer, inside = finished["outer"], finished["inside"]
    assert outer.attributes is not None
    assert outer.attributes["scadbuddy.slug"] == "demo"
    waited = outer.attributes[THREAD_WAIT_ATTRIBUTE]
    assert isinstance(waited, float) and waited >= 0
    assert inside.parent is not None
    assert inside.parent.span_id == outer.context.span_id


async def test_the_output_glob_is_a_span_and_a_remembered_hit_is_not(
    spans: InMemorySpanExporter, tmp_path: Path
) -> None:
    paths = DataPaths(tmp_path)
    _output(paths, "demo", OUTPUT_ID)
    store = OutputStore(paths)
    missing = "b" * 32

    assert await _live(store, {OUTPUT_ID, missing}) == {OUTPUT_ID}
    finished = spans.get_finished_spans()
    (existing,) = [s for s in finished if s.name == "outputs.existing"]
    assert existing.attributes is not None
    assert existing.attributes["scadbuddy.outputs"] == 2
    assert existing.attributes["scadbuddy.found"] == 1
    assert THREAD_WAIT_ATTRIBUTE in existing.attributes
    globs = {
        s.attributes["scadbuddy.output_id"]: s
        for s in finished
        if s.name == "outputs.find_dir" and s.attributes is not None
    }
    assert set(globs) == {OUTPUT_ID, missing}
    assert globs[OUTPUT_ID].attributes == {
        "scadbuddy.output_id": OUTPUT_ID,
        "scadbuddy.remembered": False,
        "scadbuddy.found": True,
    }
    missed = globs[missing].attributes
    assert missed is not None
    assert missed["scadbuddy.found"] is False
    for found in globs.values():
        assert found.parent is not None
        assert found.parent.span_id == existing.context.span_id

    spans.clear()
    store.directory(OUTPUT_ID)
    assert [s.name for s in spans.get_finished_spans()] == []


def test_the_asset_references_walk_is_one_span_with_its_counts(
    spans: InMemorySpanExporter, tmp_path: Path
) -> None:
    paths = DataPaths(tmp_path)
    _output(paths, "demo", OUTPUT_ID)
    (paths.outputs / "demo" / OUTPUT_ID / "params.json").write_text("{}", encoding="utf-8")
    with span("sweep"):
        referenced_asset_ids(paths, [{"file": "not an asset"}])
    (walk,) = [s for s in spans.get_finished_spans() if s.name == "assets.referenced"]
    assert walk.attributes is not None
    assert walk.attributes["scadbuddy.outputs"] == 1
    assert walk.attributes["scadbuddy.files"] == 2
    assert walk.attributes["scadbuddy.archives"] == 0
    assert walk.attributes["scadbuddy.assets"] == 0
    assert trace.get_current_span() is trace.INVALID_SPAN
