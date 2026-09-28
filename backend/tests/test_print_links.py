"""`PrintLinkStore` (#306): which Bambuddy archives an output's prints produced."""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest

from scadbuddy.bambuddy.print_links import PrintLink, PrintLinkStore
from scadbuddy.bambuddy.uploads import BambuddyUploadStore, LibraryCopy, SlicedCopy
from scadbuddy.render.projection import JobProjection

OUTPUT = "a" * 32
OTHER = "b" * 32

pytestmark = pytest.mark.requires_postgres


@pytest.fixture
def jobs(pg_conninfo: str) -> Iterator[JobProjection]:
    store = JobProjection(pg_conninfo, pool_size=2)
    store.open()
    try:
        yield store
    finally:
        store.close()


@pytest.fixture
def links(jobs: JobProjection) -> PrintLinkStore:
    return PrintLinkStore(jobs.pool)


def link(archive_id: int, **fields: object) -> PrintLink:
    return PrintLink(archive_id=archive_id, matched_by="queue_item", **fields)  # type: ignore[arg-type]


def test_the_table_is_created_on_a_fresh_database(jobs: JobProjection, pg_conninfo: str) -> None:
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


async def test_a_slice_keeps_the_hash_of_its_file(jobs: JobProjection) -> None:
    uploads = BambuddyUploadStore(jobs.pool)
    await uploads.record(OUTPUT, LibraryCopy(id=11, folder_id=2, target_key="H2C"))
    await uploads.record_sliced(OUTPUT, 11, SlicedCopy(id=21))

    await uploads.record_slice_hash(OUTPUT, 21, "f0744d1e")

    [copy] = await uploads.for_output(OUTPUT)
    assert copy.sliced == [SlicedCopy(id=21, file_hash="f0744d1e")]
