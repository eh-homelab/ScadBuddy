"""Issue #306 — an output ends up with the archive(s) its print produced, on both routes,
including when its queue item is gone by the first poll.

The shapes follow what the spike saw on the live Bambuddy 1.2.5.6 (print-history plan
§1, L8-L10): a dispatched item carries ``archive_id``; an archive's ``content_hash``
equals its sliced file's ``file_hash``; one sliced file printed three times has three
archives with that one hash.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import psycopg
import pytest
import respx

from scadbuddy.bambuddy import progress as progress_module
from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.linking import (
    ARCHIVE_OVERLAP,
    ARCHIVE_PAGE,
    MAX_ARCHIVE_PAGES,
    SCAN_AFTER,
    SCAN_BEFORE,
    link_by_hash,
    link_library_prints,
)
from scadbuddy.bambuddy.print_links import PrintLink, PrintLinkStore
from scadbuddy.bambuddy.progress import progress_for
from scadbuddy.bambuddy.projects import attach_results
from scadbuddy.bambuddy.uploads import BambuddyUploadStore, LibraryCopy, SlicedCopy
from scadbuddy.library.outputs import OutputMeta
from scadbuddy.render.glb import BoundingBox
from scadbuddy.render.projection import JobProjection
from tests.bambuddy.conftest import BASE_URL, recording

API = f"{BASE_URL}/api/v1"
OUTPUT = "0" * 32
HASH = "f0744d1ee745" + "0" * 52

pytestmark = pytest.mark.requires_postgres


@pytest.fixture(autouse=True)
def _no_recent_scans() -> Iterator[None]:
    """Each test starts with no output scanned by hash lately."""
    progress_module._last_hash_scan.clear()
    yield
    progress_module._last_hash_scan.clear()


@pytest.fixture
def pool_store(pg_conninfo: str) -> Iterator[JobProjection]:
    store = JobProjection(pg_conninfo, pool_size=2)
    store.open()
    try:
        yield store
    finally:
        store.close()


@pytest.fixture
def links(pool_store: JobProjection) -> PrintLinkStore:
    return PrintLinkStore(pool_store.pool)


@pytest.fixture
def uploads(pool_store: JobProjection) -> BambuddyUploadStore:
    return BambuddyUploadStore(pool_store.pool)


def meta(**overrides: object) -> OutputMeta:
    body: dict[str, object] = {
        "id": OUTPUT,
        "slug": "demo",
        "job_id": "job",
        "created_at": "2026-09-26T00:00:00Z",
        "bbox_mm": BoundingBox(min=(0, 0, 0), max=(1, 1, 1), size=(1, 1, 1)),
    }
    body.update(overrides)
    return OutputMeta.model_validate(body)


def queue_item(item_id: int, **fields: Any) -> dict[str, Any]:
    return {**recording("queue-item.json"), "id": item_id, **fields}


def queued(**overrides: object) -> OutputMeta:
    return meta(print_route="slice_queue", queue_item_id=34, **overrides)


@respx.mock
async def test_a_dispatched_queue_item_links_its_archive(
    bambuddy: BambuddyClient, links: PrintLinkStore, uploads: BambuddyUploadStore
) -> None:
    respx.get(f"{API}/queue/34").mock(
        return_value=httpx.Response(
            200,
            json=queue_item(34, status="printing", archive_id=18, printer_id=1, plate_id=1),
        )
    )

    await progress_for(bambuddy, queued(), uploads=uploads, links=links)

    [link] = await links.for_output(OUTPUT)
    assert (link.archive_id, link.matched_by, link.queue_item_id) == (18, "queue_item", 34)
    assert (link.printer_id, link.plate_id) == (1, 1)


@respx.mock
async def test_an_item_not_yet_dispatched_links_nothing(
    bambuddy: BambuddyClient, links: PrintLinkStore, uploads: BambuddyUploadStore
) -> None:
    respx.get(f"{API}/queue/34").mock(
        return_value=httpx.Response(200, json=queue_item(34, status="pending", archive_id=None))
    )

    await progress_for(bambuddy, queued(), uploads=uploads, links=links)

    assert await links.for_output(OUTPUT) == []


async def _sliced(uploads: BambuddyUploadStore, sliced_id: int = 80) -> None:
    await uploads.record(OUTPUT, LibraryCopy(id=11, folder_id=2, target_key="H2C"))
    await uploads.record_sliced(OUTPUT, 11, SlicedCopy(id=sliced_id))


def archives_page(*rows: dict[str, Any]) -> respx.Route:
    return respx.get(f"{API}/archives/").mock(return_value=httpx.Response(200, json=list(rows)))


def archive_row(archive_id: int, content_hash: str | None) -> dict[str, Any]:
    return {
        "id": archive_id,
        "printer_id": 1,
        "status": "completed",
        "created_at": "2026-09-27T03:40:10",
        "content_hash": content_hash,
    }


@respx.mock
async def test_a_settled_print_is_not_scanned_for_again_once_it_is_linked(
    bambuddy: BambuddyClient, links: PrintLinkStore, uploads: BambuddyUploadStore
) -> None:
    """The progress dialog reads a settled print on every open; the archive list is
    scanned once, not each time (#522 review)."""
    await _sliced(uploads)
    respx.get(f"{API}/queue/34").mock(return_value=httpx.Response(404, json={"detail": "gone"}))
    respx.get(f"{API}/library/files/80").mock(
        return_value=httpx.Response(
            200, json={"id": 80, "filename": "name-keychain.gcode.3mf", "file_hash": HASH}
        )
    )
    scan = archives_page(archive_row(18, HASH))

    await progress_for(bambuddy, queued(), uploads=uploads, links=links)
    await progress_for(bambuddy, queued(), uploads=uploads, links=links)

    assert scan.call_count == 1
    assert [link.archive_id for link in await links.for_output(OUTPUT)] == [18]


@respx.mock
async def test_one_plates_link_does_not_keep_another_plates_archive_from_being_found(
    bambuddy: BambuddyClient, links: PrintLinkStore, uploads: BambuddyUploadStore
) -> None:
    """Plate 1's item was linked before it went; plate 2's never was. Both are gone,
    and the read still scans for plate 2's archive, whichever plate is read first."""
    await _sliced(uploads)
    await links.record(
        OUTPUT, PrintLink(archive_id=17, matched_by="queue_item", queue_item_id=51, plate_id=1)
    )
    for entry in (51, 52):
        respx.get(f"{API}/queue/{entry}").mock(
            return_value=httpx.Response(404, json={"detail": "gone"})
        )
    for job in (9, 10):
        respx.get(f"{API}/slice-jobs/{job}").mock(
            return_value=httpx.Response(200, json={"id": job, "status": "completed"})
        )
    respx.get(f"{API}/library/files/80").mock(
        return_value=httpx.Response(
            200, json={"id": 80, "filename": "name-keychain.gcode.3mf", "file_hash": HASH}
        )
    )
    archives_page(archive_row(17, "other"), archive_row(18, HASH))
    plates = meta(
        print_route="slice_queue",
        queue_item_id=52,
        slice_job_id=10,
        plates=[
            {"plate_id": 1, "queue_item_id": 51, "slice_job_id": 9},
            {"plate_id": 2, "queue_item_id": 52, "slice_job_id": 10},
        ],
    )

    await progress_for(bambuddy, plates, uploads=uploads, links=links)

    assert sorted(link.archive_id for link in await links.for_output(OUTPUT)) == [17, 18]


@respx.mock
async def test_an_item_linked_before_it_went_is_not_scanned_for(
    bambuddy: BambuddyClient, links: PrintLinkStore, uploads: BambuddyUploadStore
) -> None:
    await _sliced(uploads)
    await links.record(OUTPUT, PrintLink(archive_id=18, matched_by="queue_item", queue_item_id=34))
    respx.get(f"{API}/queue/34").mock(return_value=httpx.Response(404, json={"detail": "gone"}))
    scan = archives_page(archive_row(18, HASH))

    await progress_for(bambuddy, queued(), uploads=uploads, links=links)

    assert not scan.called


@respx.mock
async def test_a_queue_item_gone_by_the_first_poll_is_found_by_hash(
    bambuddy: BambuddyClient, links: PrintLinkStore, uploads: BambuddyUploadStore
) -> None:
    await _sliced(uploads)
    respx.get(f"{API}/queue/34").mock(return_value=httpx.Response(404, json={"detail": "gone"}))
    file = respx.get(f"{API}/library/files/80").mock(
        return_value=httpx.Response(
            200, json={"id": 80, "filename": "name-keychain.gcode.3mf", "file_hash": HASH}
        )
    )
    scan = archives_page(archive_row(17, "other"), archive_row(18, HASH))

    await progress_for(bambuddy, queued(), uploads=uploads, links=links)

    [link] = await links.for_output(OUTPUT)
    assert (link.archive_id, link.matched_by) == (18, "content_hash")
    # The window is the days around the output's sends, and the hash is kept so it is
    # read only once.
    window = await uploads.sent_between(OUTPUT)
    assert window is not None
    params = scan.calls.last.request.url.params
    assert params["date_from"] == (window[0] - SCAN_BEFORE).date().isoformat()
    assert params["date_to"] == (window[1] + SCAN_AFTER).date().isoformat()
    [copy] = await uploads.for_output(OUTPUT)
    assert copy.sliced[0].file_hash == HASH
    await link_by_hash(bambuddy, uploads, links, queued())
    assert file.call_count == 1


@respx.mock
async def test_one_sliced_file_printed_three_times_links_three_archives(
    bambuddy: BambuddyClient, links: PrintLinkStore, uploads: BambuddyUploadStore
) -> None:
    await _sliced(uploads, sliced_id=66)
    await uploads.record_slice_hash(OUTPUT, 66, HASH)
    archives_page(archive_row(16, HASH), archive_row(17, HASH), archive_row(23, HASH))

    found = await link_by_hash(bambuddy, uploads, links, meta())

    assert sorted(link.archive_id for link in found) == [16, 17, 23]
    assert sorted(link.archive_id for link in await links.for_output(OUTPUT)) == [16, 17, 23]


@respx.mock
async def test_the_hash_scan_pages_until_bambuddy_runs_out(
    bambuddy: BambuddyClient, links: PrintLinkStore, uploads: BambuddyUploadStore
) -> None:
    await _sliced(uploads)
    await uploads.record_slice_hash(OUTPUT, 80, HASH)
    full = [archive_row(1000 + n, "x") for n in range(100)]
    scan = respx.get(f"{API}/archives/").mock(
        side_effect=[
            httpx.Response(200, json=full),
            httpx.Response(200, json=[archive_row(18, HASH)]),
        ]
    )

    await link_by_hash(bambuddy, uploads, links, meta())

    assert [call.request.url.params.get("offset") for call in scan.calls] == [None, "90"]
    assert [link.archive_id for link in await links.for_output(OUTPUT)] == [18]


def shifting_archives(rows: list[dict[str, Any]], shift: int) -> respx.Route:
    """A live ``GET /archives/`` whose list moves by ``shift`` rows after the first
    read: archives created at its head (``shift > 0``), or deleted from it."""
    live = list(rows)
    reads = 0

    def page(request: httpx.Request) -> httpx.Response:
        nonlocal live, reads
        offset = int(request.url.params.get("offset", "0"))
        limit = int(request.url.params["limit"])
        body = live[offset : offset + limit]
        reads += 1
        if reads == 1:
            if shift > 0:
                live = [archive_row(9000 + n, "new") for n in range(shift)] + live
            else:
                live = live[-shift:]
        return httpx.Response(200, json=body)

    return respx.get(f"{API}/archives/").mock(side_effect=page)


@respx.mock
@pytest.mark.parametrize("shift", [1, ARCHIVE_OVERLAP])
async def test_archives_created_between_page_reads_do_not_link_a_print_twice(
    bambuddy: BambuddyClient, links: PrintLinkStore, uploads: BambuddyUploadStore, shift: int
) -> None:
    """The last row of the first page moves onto the second page and is read again."""
    await _sliced(uploads)
    await uploads.record_slice_hash(OUTPUT, 80, HASH)
    rows = [archive_row(1000 + n, "x") for n in range(150)]
    rows[ARCHIVE_PAGE - 1] = archive_row(18, HASH)
    rows[140] = archive_row(19, HASH)
    scan = shifting_archives(rows, shift)

    found = await link_by_hash(bambuddy, uploads, links, meta())

    assert scan.call_count == 2
    assert [link.archive_id for link in found] == [18, 19]


@respx.mock
@pytest.mark.parametrize("shift", [1, ARCHIVE_OVERLAP])
async def test_archives_deleted_between_page_reads_do_not_hide_a_print(
    bambuddy: BambuddyClient, links: PrintLinkStore, uploads: BambuddyUploadStore, shift: int
) -> None:
    """The first row of the second page moves back onto the first page, which was
    already read; stepping by a whole page would miss it."""
    await _sliced(uploads)
    await uploads.record_slice_hash(OUTPUT, 80, HASH)
    rows = [archive_row(1000 + n, "x") for n in range(150)]
    rows[ARCHIVE_PAGE] = archive_row(18, HASH)
    scan = shifting_archives(rows, -shift)

    found = await link_by_hash(bambuddy, uploads, links, meta())

    assert scan.call_count == 2
    assert [link.archive_id for link in found] == [18]
    assert [link.archive_id for link in await links.for_output(OUTPUT)] == [18]


@respx.mock
async def test_without_a_sliced_file_there_is_nothing_to_scan_for(
    bambuddy: BambuddyClient, links: PrintLinkStore, uploads: BambuddyUploadStore
) -> None:
    scan = archives_page()

    assert await link_by_hash(bambuddy, uploads, links, meta()) == []
    assert not scan.called


@respx.mock
async def test_attaching_to_a_project_records_the_archives_it_found(
    bambuddy: BambuddyClient, links: PrintLinkStore
) -> None:
    respx.get(f"{API}/queue/90").mock(
        return_value=httpx.Response(200, json=queue_item(90, status="completed", archive_id=32))
    )
    respx.post(f"{API}/projects/7/add-queue").mock(return_value=httpx.Response(200, json={}))
    respx.post(f"{API}/projects/7/add-archives").mock(return_value=httpx.Response(200, json={}))

    respx.get(f"{API}/queue/91").mock(
        return_value=httpx.Response(200, json=queue_item(91, status="completed", archive_id=77))
    )

    await attach_results(
        bambuddy, 7, queue_item_ids=[90, 91], output_id=OUTPUT, links=links, linkable={90}
    )

    assert [link.archive_id for link in await links.for_output(OUTPUT)] == [32]


@respx.mock
async def test_attaching_links_nothing_the_output_does_not_own(
    bambuddy: BambuddyClient, links: PrintLinkStore
) -> None:
    respx.get(f"{API}/queue/91").mock(
        return_value=httpx.Response(200, json=queue_item(91, status="completed", archive_id=77))
    )
    respx.post(f"{API}/projects/7/add-queue").mock(return_value=httpx.Response(200, json={}))
    respx.post(f"{API}/projects/7/add-archives").mock(return_value=httpx.Response(200, json={}))

    result = await attach_results(bambuddy, 7, queue_item_ids=[91], output_id=OUTPUT, links=links)

    assert result.archive_ids == [77], "still filed under the project"
    assert await links.for_output(OUTPUT) == []


@respx.mock
async def test_a_scan_that_runs_out_of_pages_stops_inside_its_window(
    bambuddy: BambuddyClient,
    links: PrintLinkStore,
    uploads: BambuddyUploadStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """More archives in the window than one scan reads: it reads MAX_ARCHIVE_PAGES,
    links what it saw, says so, and never asks past the days around the sends."""
    await _sliced(uploads)
    await uploads.record_slice_hash(OUTPUT, 80, HASH)
    window = await uploads.sent_between(OUTPUT)
    assert window is not None
    pages = [[archive_row(1000 + page * 100 + n, "x") for n in range(100)] for page in range(11)]
    pages[MAX_ARCHIVE_PAGES - 1][50] = archive_row(18, HASH)
    pages[MAX_ARCHIVE_PAGES][50] = archive_row(19, HASH)
    scan = respx.get(f"{API}/archives/").mock(
        side_effect=[httpx.Response(200, json=rows) for rows in pages]
    )

    # An output rendered long before it was sent: the window starts at the send.
    found = await link_by_hash(bambuddy, uploads, links, meta(created_at="2025-01-01T00:00:00Z"))

    assert scan.call_count == MAX_ARCHIVE_PAGES
    assert [link.archive_id for link in found] == [18]
    for call in scan.calls:
        assert call.request.url.params["date_from"] == (window[0] - SCAN_BEFORE).date().isoformat()
        assert call.request.url.params["date_to"] == (window[1] + SCAN_AFTER).date().isoformat()
    assert "stopped scanning archives" in caplog.text


async def _library_items(links: PrintLinkStore, *ids: int) -> None:
    for queue_item_id in ids:
        await links.record_library(89, queue_item_id, plate_id=1, printer_id=1)


async def _pending(links: PrintLinkStore) -> set[int]:
    return {row.queue_item_id for row in await links.pending_library(50)}


@respx.mock
async def test_a_library_run_queued_long_ago_is_still_linked_once_dispatched(
    bambuddy: BambuddyClient, links: PrintLinkStore, pool_store: JobProjection
) -> None:
    """#1664: nothing ages an item out; it is read until Bambuddy is done with it."""
    await _library_items(links, 51)
    with pool_store.pool.connection() as conn:
        conn.execute("UPDATE library_bambuddy_prints SET first_seen = now() - interval '60 days'")
    respx.get(f"{API}/queue/51").mock(
        return_value=httpx.Response(200, json=queue_item(51, status="printing", archive_id=90))
    )

    await link_library_prints(bambuddy, links)

    linked = await links.linked(90)
    assert linked is not None and linked.library_file_id == 89
    assert await _pending(links) == set()


@respx.mock
async def test_a_library_item_settled_without_an_archive_is_not_read_again(
    bambuddy: BambuddyClient, links: PrintLinkStore
) -> None:
    await _library_items(links, 51, 52, 53)
    for item_id, item_status in ((51, "cancelled"), (52, "pending"), (53, "skipped")):
        respx.get(f"{API}/queue/{item_id}").mock(
            return_value=httpx.Response(
                200, json=queue_item(item_id, status=item_status, archive_id=None)
            )
        )

    await link_library_prints(bambuddy, links)

    assert await _pending(links) == {52}


@respx.mock
async def test_a_database_error_on_one_library_item_does_not_stop_the_others(
    bambuddy: BambuddyClient, links: PrintLinkStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1662: it is logged and the item is left to the next call."""
    await _library_items(links, 51, 52)
    for item_id, archive_id in ((51, 90), (52, 91)):
        respx.get(f"{API}/queue/{item_id}").mock(
            return_value=httpx.Response(
                200, json=queue_item(item_id, status="printing", archive_id=archive_id)
            )
        )
    real = links.link_library

    async def failing(queue_item_id: int, archive_id: int, name: str | None) -> None:
        if queue_item_id == 51:
            raise psycopg.OperationalError("connection lost")
        await real(queue_item_id, archive_id, name)

    monkeypatch.setattr(links, "link_library", failing)

    await link_library_prints(bambuddy, links)

    assert await links.linked(90) is None
    assert await links.linked(91) is not None
    assert await _pending(links) == {51}


async def test_a_database_error_reading_the_library_items_links_nothing_and_raises_nothing(
    bambuddy: BambuddyClient, links: PrintLinkStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failing(limit: int) -> list[object]:
        raise psycopg.OperationalError("connection lost")

    monkeypatch.setattr(links, "pending_library", failing)

    await link_library_prints(bambuddy, links)
