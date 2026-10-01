"""The library calls the blob store makes (spec 2026-09-27 §6.3)."""

from __future__ import annotations

import logging

import httpx
import pytest
import respx

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.errors import NOT_FOUND_PROBLEM, SCOPE_PROBLEM
from scadbuddy.core.problems import ApiError
from tests.bambuddy.conftest import BASE_URL, shaped

API = f"{BASE_URL}/api/v1"


@respx.mock
async def test_an_svg_uploads_with_its_own_media_type_into_a_folder(
    bambuddy: BambuddyClient,
) -> None:
    route = respx.post(f"{API}/library/files").mock(
        return_value=httpx.Response(
            200, json=shaped("FileUploadResponse", id=41, filename="asset-x.svg", file_size=6)
        )
    )
    uploaded = await bambuddy.upload_library_file(
        "asset-x.svg", b"<svg/>", folder_id=11, media_type="image/svg+xml"
    )
    assert uploaded.id == 41
    request = route.calls.last.request
    assert request.url.params["folder_id"] == "11"
    assert b'filename="asset-x.svg"' in request.content
    assert b"Content-Type: image/svg+xml" in request.content


@respx.mock
async def test_a_file_downloads_by_id_without_a_folder_scan(bambuddy: BambuddyClient) -> None:
    respx.get(f"{API}/library/files/41/download").mock(
        return_value=httpx.Response(200, content=b"x" * 100_000)
    )
    folders = respx.get(f"{API}/library/folders")
    chunks = [chunk async for chunk in bambuddy.download_library_file(41)]
    assert b"".join(chunks) == b"x" * 100_000
    assert not folders.called


@respx.mock
async def test_a_missing_file_is_a_404_problem(bambuddy: BambuddyClient) -> None:
    respx.get(f"{API}/library/files/9/download").mock(
        return_value=httpx.Response(404, json={"detail": "File not found"})
    )
    with pytest.raises(ApiError) as caught:
        async for _ in bambuddy.download_library_file(9):
            pass
    assert caught.value.status == 404 and caught.value.type == NOT_FOUND_PROBLEM


@respx.mock
async def test_a_download_that_fails_on_the_network_is_logged(
    bambuddy: BambuddyClient, caplog: pytest.LogCaptureFixture
) -> None:
    respx.get(f"{API}/library/files/9/download").mock(
        side_effect=httpx.ConnectError("no route to host")
    )
    with (
        caplog.at_level(logging.WARNING, logger="scadbuddy.bambuddy.client"),
        pytest.raises(ApiError),
    ):
        async for _ in bambuddy.download_library_file(9):
            pass
    [record] = [r for r in caplog.records if r.getMessage() == "bambuddy request failed"]
    assert record.__dict__["path"] == "/library/files/9/download"


@respx.mock
async def test_a_key_without_manage_library_is_named(bambuddy: BambuddyClient) -> None:
    respx.post(f"{API}/library/files").mock(
        return_value=httpx.Response(403, json={"detail": "Forbidden"})
    )
    with pytest.raises(ApiError) as caught:
        await bambuddy.upload_library_file("a.zip", b"z", folder_id=1, media_type="application/zip")
    assert caught.value.type == SCOPE_PROBLEM and "Manage Library" in caught.value.detail


def test_a_library_file_reads_its_folder_and_hash() -> None:
    from scadbuddy.bambuddy.models import LibraryFile

    row = shaped("FileResponse", id=5, filename="a.zip", folder_id=11, file_hash="ab" * 32)
    parsed = LibraryFile.model_validate(row)
    assert (parsed.folder_id, parsed.file_hash) == (11, "ab" * 32)
