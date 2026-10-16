"""`OutputPrintStore` (#1060): an output's last print in Postgres, so the print worker
records it without the data volume; an older ``meta.json`` still answers for an output
with no row."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scadbuddy.core.paths import DataPaths
from scadbuddy.library.output_prints import OutputPrintStore
from scadbuddy.library.outputs import META_NAME, OutputMeta, OutputStore, PlateSend
from scadbuddy.render.glb import BoundingBox
from scadbuddy.render.projection import JobProjection

pytestmark = pytest.mark.requires_postgres

SLUG = "sign"


@pytest.fixture
def prints(pg_conninfo: str) -> Iterator[OutputPrintStore]:
    jobs = JobProjection(pg_conninfo, pool_size=2)
    jobs.open()
    try:
        yield OutputPrintStore(jobs.pool)
    finally:
        jobs.close()


def saved(root: Path, output_id: str, **fields: object) -> OutputMeta:
    """An output record as an older release wrote it, last print and all."""
    meta = OutputMeta(
        id=output_id,
        slug=SLUG,
        job_id="j",
        created_at=datetime.now(UTC),
        bbox_mm=BoundingBox(min=(0, 0, 0), max=(1, 1, 1), size=(1, 1, 1)),
        **fields,  # type: ignore[arg-type]
    )
    directory = DataPaths(root=root).outputs / SLUG / output_id
    directory.mkdir(parents=True)
    (directory / META_NAME).write_text(meta.model_dump_json(), encoding="utf-8")
    return meta


def plate(plate_id: int, item: int) -> PlateSend:
    return PlateSend(plate_id=plate_id, queue_item_id=item, slice_job_id=9)


def test_a_recorded_print_overrides_the_files_last_print(
    tmp_path: Path, prints: OutputPrintStore
) -> None:
    store = OutputStore(DataPaths(root=tmp_path), prints=prints)
    meta = saved(tmp_path, "a" * 32, queue_item_id=1, print_route="slice_queue", project_id=7)

    prints.record(meta.id, queue_item_id=5, slice_job_id=9, project_id=None, plates=[plate(1, 5)])

    got = store.get(meta.id)
    assert (got.queue_item_id, got.slice_job_id, got.print_route) == (5, 9, "slice_queue")
    # Omitted, as `record_send` left it: the file's project still shows.
    assert got.project_id == 7
    assert [p.queue_item_id for p in got.plates] == [5]
    [listed] = store.list_for(SLUG)
    assert listed.queue_item_id == 5


def test_an_output_with_no_row_reads_its_file(tmp_path: Path, prints: OutputPrintStore) -> None:
    store = OutputStore(DataPaths(root=tmp_path), prints=prints)
    meta = saved(tmp_path, "b" * 32, queue_item_id=3, print_route="slice_queue")

    assert store.get(meta.id).queue_item_id == 3
    assert store.list_for(SLUG)[0].queue_item_id == 3


def test_a_second_record_keeps_the_project_and_replaces_the_plates(
    tmp_path: Path, prints: OutputPrintStore
) -> None:
    store = OutputStore(DataPaths(root=tmp_path), prints=prints)
    meta = saved(tmp_path, "c" * 32)

    prints.record(meta.id, queue_item_id=5, slice_job_id=9, project_id=4, plates=[plate(1, 5)])
    prints.record(
        meta.id, queue_item_id=6, slice_job_id=9, project_id=None, plates=[plate(1, 5), plate(2, 6)]
    )

    got = store.get(meta.id)
    assert (got.queue_item_id, got.project_id) == (6, 4)
    assert [p.plate_id for p in got.plates] == [1, 2]


def test_delete_forgets_the_row(tmp_path: Path, prints: OutputPrintStore) -> None:
    store = OutputStore(DataPaths(root=tmp_path), prints=prints)
    meta = saved(tmp_path, "d" * 32)
    prints.record(meta.id, queue_item_id=5, slice_job_id=9, project_id=None, plates=[])

    prints.delete(meta.id)

    assert store.get(meta.id).queue_item_id is None
    assert prints.for_outputs([meta.id]) == {}
