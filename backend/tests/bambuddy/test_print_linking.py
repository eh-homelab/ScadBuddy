"""Issue #306 — an output ends up with the archive(s) its print produced, on both routes,
including when its queue item is gone by the first poll.

The shapes follow what the spike saw on the live Bambuddy 1.2.5.6 (print-history plan
§1, L8-L10): a dispatched item carries ``archive_id``; an archive's ``content_hash``
equals its sliced file's ``file_hash``; one sliced file printed three times has three
archives with that one hash.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.linking import link_by_hash
from scadbuddy.bambuddy.print_links import PrintLinkStore
from scadbuddy.bambuddy.progress import progress_for
from scadbuddy.bambuddy.projects import attach_results
from scadbuddy.bambuddy.uploads import BambuddyUploadStore, LibraryCopy, SlicedCopy
from scadbuddy.core.paths import DataPaths
from scadbuddy.library.outputs import OutputMeta
from scadbuddy.render.glb import BoundingBox
from scadbuddy.render.pg_store import PostgresJobStore
from tests.bambuddy.conftest import BASE_URL, recording

API = f"{BASE_URL}/api/v1"
OUTPUT = "0" * 32
HASH = "f0744d1ee745" + "0" * 52

pytestmark = pytest.mark.requires_postgres


@pytest.fixture
def pool_store(pg_conninfo: str, tmp_path: Path) -> Iterator[PostgresJobStore]:
    store = PostgresJobStore(pg_conninfo, DataPaths(tmp_path / "data"), pool_size=2)
    store.open()
    try:
        yield store
    finally:
        store.close()


@pytest.fixture
def links(pool_store: PostgresJobStore) -> PrintLinkStore:
    return PrintLinkStore(pool_store.pool)


@pytest.fixture
def uploads(pool_store: PostgresJobStore) -> BambuddyUploadStore:
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
    # The window starts at the output, and the hash is kept so it is read only once.
    assert scan.calls.last.request.url.params["date_from"] == "2026-09-26"
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

    assert [call.request.url.params.get("offset") for call in scan.calls] == [None, "100"]
    assert [link.archive_id for link in await links.for_output(OUTPUT)] == [18]


@respx.mock
async def test_without_a_sliced_file_there_is_nothing_to_scan_for(
    bambuddy: BambuddyClient, links: PrintLinkStore, uploads: BambuddyUploadStore
) -> None:
    scan = archives_page()

    assert await link_by_hash(bambuddy, uploads, links, meta()) == []
    assert not scan.called


@respx.mock
async def test_a_pipeline_run_links_through_its_jobs_queue_entries(
    bambuddy: BambuddyClient, links: PrintLinkStore, uploads: BambuddyUploadStore
) -> None:
    run = recording("pipeline-run.json")
    run["jobs"] = [
        {**run["jobs"][0], "queue_entry_id": 90, "status": "completed"},
        {**run["jobs"][0], "id": 99, "queue_entry_id": None, "status": "pending"},
    ]
    respx.get(f"{API}/pipeline-runs/1").mock(return_value=httpx.Response(200, json=run))
    item = respx.get(f"{API}/queue/90").mock(
        return_value=httpx.Response(200, json=queue_item(90, status="completed", archive_id=32))
    )
    printed = meta(print_route="pipeline", pipeline_run_id=1)

    await progress_for(bambuddy, printed, uploads=uploads, links=links)
    await progress_for(bambuddy, printed, uploads=uploads, links=links)

    assert [link.archive_id for link in await links.for_output(OUTPUT)] == [32]
    assert item.call_count == 1, "a linked queue entry is not read again"


@respx.mock
async def test_attaching_to_a_project_records_the_archives_it_found(
    bambuddy: BambuddyClient, links: PrintLinkStore
) -> None:
    respx.get(f"{API}/queue/90").mock(
        return_value=httpx.Response(200, json=queue_item(90, status="completed", archive_id=32))
    )
    respx.post(f"{API}/projects/7/add-queue").mock(return_value=httpx.Response(200, json={}))
    respx.post(f"{API}/projects/7/add-archives").mock(return_value=httpx.Response(200, json={}))

    await attach_results(bambuddy, 7, queue_item_ids=[90], output_id=OUTPUT, links=links)

    assert [link.archive_id for link in await links.for_output(OUTPUT)] == [32]
