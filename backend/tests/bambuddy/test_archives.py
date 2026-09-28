"""Issue #307 — the archive client, against bodies recorded from Bambuddy 1.2.5.6."""

from __future__ import annotations

from datetime import date

import httpx
import pytest
import respx

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.errors import SCOPE_PROBLEM, Scope
from scadbuddy.core.problems import ApiError
from tests.bambuddy.conftest import BASE_URL, recording

API = f"{BASE_URL}/api/v1"


@respx.mock
async def test_an_archive_reads_its_media_hash_and_run_counts(bambuddy: BambuddyClient) -> None:
    route = respx.get(f"{API}/archives/35").mock(
        return_value=httpx.Response(200, json=recording("archive-detail.json"))
    )

    archive = await bambuddy.archive(35)

    assert route.calls.last.request.headers["X-API-Key"] == "s3cret"
    assert archive.id == 35
    assert archive.status == "completed"
    assert archive.photos == ["finish_20260927_015703_93372185.jpg"]
    assert archive.finish_photo == "finish_20260927_015703_93372185.jpg"
    assert archive.timelapse_path is not None
    assert archive.source_3mf_path is not None
    assert archive.content_hash is not None and len(archive.content_hash) == 64
    assert archive.run_count == 1
    assert archive.filament_used_grams is not None


@respx.mock
async def test_the_runs_of_an_archive(bambuddy: BambuddyClient) -> None:
    respx.get(f"{API}/archives/35/runs").mock(
        return_value=httpx.Response(200, json=recording("archive-runs.json"))
    )

    runs = await bambuddy.archive_runs(35)

    assert runs.total == 1
    [run] = runs.items
    assert (run.archive_id, run.status, run.duration_seconds) == (35, "completed", 6437)


@respx.mock
async def test_timelapse_info_and_poster_frames(bambuddy: BambuddyClient) -> None:
    respx.get(f"{API}/archives/35/timelapse/info").mock(
        return_value=httpx.Response(200, json=recording("timelapse-info.json"))
    )
    respx.get(f"{API}/archives/35/timelapse/thumbnails").mock(
        return_value=httpx.Response(200, json=recording("timelapse-thumbnails.json"))
    )

    info = await bambuddy.timelapse_info(35)
    frames = await bambuddy.timelapse_thumbnails(35)

    assert (info.width, info.height, info.codec) == (1680, 1080, "h264")
    assert len(frames.thumbnails) == len(frames.timestamps) == 3


@respx.mock
async def test_printer_media_lists_what_is_still_on_the_printer(bambuddy: BambuddyClient) -> None:
    respx.get(f"{API}/archives/35/printer-media").mock(
        return_value=httpx.Response(200, json=recording("printer-media.json"))
    )

    media = await bambuddy.printer_media(35)

    assert media.local_timelapse is not None
    assert media.local_timelapse.name.endswith(".mp4")
    assert {row.kind for row in media.remote_files} == {"ipcam"}
    assert media.warnings == []


@respx.mock
async def test_archives_filter_by_printer_and_date(bambuddy: BambuddyClient) -> None:
    route = respx.get(f"{API}/archives/").mock(
        return_value=httpx.Response(200, json=recording("archives.json"))
    )

    rows = await bambuddy.archives(
        printer_id=1, date_from=date(2026, 9, 26), date_to=date(2026, 9, 27), limit=50
    )

    params = route.calls.last.request.url.params
    assert (params["printer_id"], params["date_from"], params["date_to"]) == (
        "1",
        "2026-09-26",
        "2026-09-27",
    )
    assert rows[0].id == 35


@respx.mock
async def test_selecting_a_timelapse_names_the_file_and_the_manage_archives_scope(
    bambuddy: BambuddyClient,
) -> None:
    route = respx.post(f"{API}/archives/35/timelapse/select").mock(
        return_value=httpx.Response(403, json={"detail": "Missing permission"})
    )

    with pytest.raises(ApiError) as caught:
        await bambuddy.select_timelapse(35, "video_2026-09-27_12-22-00.mp4")

    assert route.calls.last.request.url.params["filename"] == "video_2026-09-27_12-22-00.mp4"
    assert caught.value.type == SCOPE_PROBLEM
    assert caught.value.extensions["required_scope"] == Scope.MANAGE_ARCHIVES.value


@respx.mock
async def test_a_photo_is_uploaded_and_deleted(bambuddy: BambuddyClient) -> None:
    upload = respx.post(f"{API}/archives/35/photos").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "uploaded",
                "filename": "1a2b3c4d.jpg",
                "photos": ["finish_20260927_015703_93372185.jpg", "1a2b3c4d.jpg"],
            },
        )
    )
    delete = respx.delete(f"{API}/archives/35/photos/1a2b3c4d.jpg").mock(
        return_value=httpx.Response(200, json={"status": "deleted"})
    )

    uploaded = await bambuddy.upload_archive_photo(35, "shelf.jpg", b"\xff\xd8jpeg")
    await bambuddy.delete_archive_photo(35, "1a2b3c4d.jpg")

    assert uploaded.filename == "1a2b3c4d.jpg"
    assert b'filename="shelf.jpg"' in upload.calls.last.request.content
    assert delete.called


@respx.mock
async def test_stream_passes_the_range_through_and_answers_206(bambuddy: BambuddyClient) -> None:
    route = respx.get(f"{API}/archives/35/timelapse").mock(
        return_value=httpx.Response(
            206,
            content=b"x" * 100,
            headers={
                "Content-Type": "video/mp4",
                "Content-Range": "bytes 100-199/2143595",
                "Accept-Ranges": "bytes",
            },
        )
    )

    async with bambuddy.stream(
        "/archives/35/timelapse", what="play the timelapse", range_header="bytes=100-199"
    ) as upstream:
        body = b"".join([chunk async for chunk in upstream.aiter_raw()])

    request = route.calls.last.request
    assert request.headers["Range"] == "bytes=100-199"
    assert request.headers["X-API-Key"] == "s3cret"
    assert upstream.status_code == 206
    assert upstream.headers["Content-Range"] == "bytes 100-199/2143595"
    assert body == b"x" * 100


@respx.mock
async def test_stream_maps_a_missing_file_to_a_404_problem(bambuddy: BambuddyClient) -> None:
    respx.get(f"{API}/archives/35/timelapse").mock(
        return_value=httpx.Response(404, json={"detail": "Timelapse not found"})
    )

    with pytest.raises(ApiError) as caught:
        async with bambuddy.stream("/archives/35/timelapse", what="play the timelapse"):
            pass

    assert caught.value.status == 404
    assert "Timelapse not found" in caught.value.detail
