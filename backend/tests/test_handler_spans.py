"""The spans that tell where a handler's time goes (#7245): the thread wait, the output
store's NFS globs and the asset sweep's walk."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import threading
from pathlib import Path

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from scadbuddy.core.tracing import THREAD_WAIT_ATTRIBUTE, span, traced_thread
from scadbuddy.library.assets import referenced_asset_ids
from tests.conftest import PgPool
from tests.support.row_outputs import output_store
from tests.test_output_rows import make_row

OUTPUT_ID = "a" * 32


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


async def test_a_cancelled_wait_leaves_no_write_on_an_ended_span(
    spans: InMemorySpanExporter, caplog: pytest.LogCaptureFixture
) -> None:
    """The thread outlives the cancelled await and the span. It writes nothing to it."""
    started = threading.Event()
    release = threading.Event()

    def work() -> None:
        started.set()
        release.wait(5)

    caplog.set_level(logging.WARNING)
    task = asyncio.ensure_future(traced_thread("cancelled", work))
    await asyncio.to_thread(started.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    await asyncio.sleep(0.05)
    (cancelled,) = [s for s in spans.get_finished_spans() if s.name == "cancelled"]
    assert cancelled.attributes is not None
    assert THREAD_WAIT_ATTRIBUTE not in cancelled.attributes
    assert "ended span" not in caplog.text


@pytest.mark.requires_postgres
def test_the_asset_references_scan_is_one_span_with_its_counts(
    spans: InMemorySpanExporter, tmp_path: Path, pg_pool: PgPool
) -> None:
    """The scan of the output rows (#7257): an output with no params and no model blob
    has no 3MF to open."""
    store = output_store(tmp_path, pg_pool)
    store.rows.insert(dataclasses.replace(make_row(OUTPUT_ID), params={}))
    with span("sweep"):
        asyncio.run(referenced_asset_ids(store.paths, [{"file": "not an asset"}], store))
    (walk,) = [s for s in spans.get_finished_spans() if s.name == "assets.referenced"]
    assert walk.attributes is not None
    assert walk.attributes["scadbuddy.outputs"] == 1
    assert walk.attributes["scadbuddy.files"] == 0
    assert walk.attributes["scadbuddy.archives"] == 0
    assert walk.attributes["scadbuddy.assets"] == 0
    assert trace.get_current_span() is trace.INVALID_SPAN
