"""`PrintLinkStore` (#306): which Bambuddy archives an output's prints produced."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from scadbuddy.bambuddy.print_links import PrintLink, PrintLinkStore
from scadbuddy.bambuddy.uploads import BambuddyUploadStore, LibraryCopy, SlicedCopy
from scadbuddy.core.paths import DataPaths
from scadbuddy.render.pg_store import PostgresJobStore

OUTPUT = "a" * 32
OTHER = "b" * 32

pytestmark = pytest.mark.requires_postgres


@pytest.fixture
def jobs(pg_conninfo: str, tmp_path: Path) -> Iterator[PostgresJobStore]:
    store = PostgresJobStore(pg_conninfo, DataPaths(tmp_path / "data"), pool_size=2)
    store.open()
    try:
        yield store
    finally:
        store.close()


@pytest.fixture
def links(jobs: PostgresJobStore) -> PrintLinkStore:
    return PrintLinkStore(jobs.pool)


def link(archive_id: int, **fields: object) -> PrintLink:
    return PrintLink(archive_id=archive_id, matched_by="queue_item", **fields)  # type: ignore[arg-type]


def test_the_table_is_created_on_a_fresh_database(jobs: PostgresJobStore, pg_conninfo: str) -> None:
    with psycopg.connect(pg_conninfo) as conn:
        columns = {
            row[0]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_schema = current_schema() AND table_name = 'output_bambuddy_prints'"
            )
        }
    assert {"output_id", "archive_id", "queue_item_id", "matched_by", "first_seen"} <= columns


async def test_links_list_in_the_order_they_were_seen(links: PrintLinkStore) -> None:
    await links.record(OUTPUT, link(18, queue_item_id=34, plate_id=1, printer_id=1))
    await links.record(OUTPUT, link(32, queue_item_id=90))
    await links.record(OTHER, link(40))

    assert [row.archive_id for row in await links.for_output(OUTPUT)] == [18, 32]
    [first, _] = await links.for_output(OUTPUT)
    assert (first.queue_item_id, first.plate_id, first.printer_id) == (34, 1, 1)
    assert first.first_seen is not None


async def test_recording_the_same_archive_again_keeps_the_first_sighting(
    links: PrintLinkStore,
) -> None:
    await links.record(OUTPUT, link(16, queue_item_id=27))
    first = (await links.for_output(OUTPUT))[0].first_seen
    await links.record(OUTPUT, link(16, queue_item_id=31))

    [only] = await links.for_output(OUTPUT)
    assert (only.queue_item_id, only.first_seen) == (27, first)


async def test_an_archive_is_looked_up_by_id(links: PrintLinkStore) -> None:
    await links.record(OUTPUT, link(18))

    assert await links.output_for(18) == OUTPUT
    assert await links.output_for(99) is None


async def test_the_queue_items_already_linked(links: PrintLinkStore) -> None:
    await links.record(OUTPUT, link(18, queue_item_id=34))
    await links.record(OUTPUT, PrintLink(archive_id=19, matched_by="content_hash"))

    assert await links.linked_queue_items(OUTPUT) == {34}


async def test_deleting_outputs_deletes_their_links(links: PrintLinkStore) -> None:
    await links.record(OUTPUT, link(18))
    await links.record(OTHER, link(40))

    await links.delete_outputs([OUTPUT])

    assert await links.for_output(OUTPUT) == []
    assert await links.output_for(40) == OTHER


async def test_a_slice_keeps_the_hash_of_its_file(jobs: PostgresJobStore) -> None:
    uploads = BambuddyUploadStore(jobs.pool)
    await uploads.record(OUTPUT, LibraryCopy(id=11, folder_id=2, target_key="H2C"))
    await uploads.record_sliced(OUTPUT, 11, SlicedCopy(id=21))

    await uploads.record_slice_hash(OUTPUT, 21, "f0744d1e")

    [copy] = await uploads.for_output(OUTPUT)
    assert copy.sliced == [SlicedCopy(id=21, file_hash="f0744d1e")]


async def test_a_page_has_one_row_per_archive_newest_first(links: PrintLinkStore) -> None:
    # #308: the prints list is driven by this table, keyed by archive.
    await links.record(OUTPUT, link(18, printer_id=1))
    await links.record(OUTPUT, link(32))
    await links.record(OTHER, link(40))
    # A second output reaching the same archive: the first one to see it keeps it.
    await links.record(OTHER, link(18))

    page = await links.page(limit=10)

    assert [(row.archive_id, row.output_id) for row in page] == [
        (40, OTHER),
        (32, OUTPUT),
        (18, OUTPUT),
    ]
    assert page[2].printer_id == 1


async def test_a_page_continues_below_a_cursor_and_is_bounded(links: PrintLinkStore) -> None:
    for archive_id in (10, 11, 12, 13):
        await links.record(OUTPUT, link(archive_id))

    assert [row.archive_id for row in await links.page(limit=2)] == [13, 12]
    assert [row.archive_id for row in await links.page(before=12, limit=2)] == [11, 10]
    assert await links.page(before=10, limit=2) == []


async def test_a_page_can_be_limited_to_some_outputs(links: PrintLinkStore) -> None:
    await links.record(OUTPUT, link(18))
    await links.record(OTHER, link(40))

    assert [row.archive_id for row in await links.page(limit=10, output_ids=[OUTPUT])] == [18]
    assert await links.page(limit=10, output_ids=[]) == []


async def test_one_archive_is_found_with_its_output(links: PrintLinkStore) -> None:
    await links.record(OUTPUT, link(18, plate_id=2))

    found = await links.linked(18)

    assert found is not None
    assert (found.output_id, found.archive_id, found.plate_id) == (OUTPUT, 18, 2)
    assert await links.linked(99) is None
