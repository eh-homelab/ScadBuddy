"""The print worker's writes of a native per-print copy (#3818 tier A Task 4): through
the API's hidden internal route, as its reads are (`RemoteLibrary`, plan
2026-10-17-native-print-library Task 7a)."""

from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from scadbuddy.core.problems import ApiError
from scadbuddy.core.settings import Settings
from scadbuddy.library.settings_store import S3Settings
from scadbuddy.main import create_app
from scadbuddy.print_library.ids import is_native_id
from scadbuddy.print_library.remote import RemoteLibrary
from tests.api.test_library_native import _native, _native_settings, _three_mf

pytestmark = pytest.mark.requires_postgres


@pytest.fixture
def native_client(settings: Settings, s3: S3Settings) -> Iterator[TestClient]:
    with TestClient(create_app(_native_settings(settings, s3))) as client:
        yield client


def _remote(client: TestClient) -> RemoteLibrary:
    return RemoteLibrary(
        "http://scadbuddy", httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app))
    )


def test_the_print_worker_writes_through_the_internal_route(
    native_client: TestClient, tmp_path: Path
) -> None:
    payload = _three_mf(tmp_path / "bracket.3mf").read_bytes()
    assert native_client.portal is not None
    folder = native_client.portal.call(_native(native_client).create_folder, "Prints", None).id

    async def write() -> tuple[Any, ...]:
        remote = _remote(native_client)
        try:
            uploaded = await remote.upload("bracket (ScadBuddy).3mf", io.BytesIO(payload), folder)
            listed = await remote.library_listing(folder_id=folder)
            top = await remote.library_listing(folder_id=None)
            file = await remote.library_file(uploaded.id)
            with pytest.raises(ApiError) as missing:
                await remote.upload("x.3mf", io.BytesIO(payload), folder + 999)
        finally:
            await remote.aclose()
        return uploaded, listed, top, file, missing.value

    uploaded, listed, top, file, missing = native_client.portal.call(write)

    assert is_native_id(uploaded.id)
    assert [row.id for row in listed] == [uploaded.id]
    assert top == []
    assert (file.filename, file.folder_id, file.file_size) == (
        "bracket (ScadBuddy).3mf",
        folder,
        len(payload),
    )
    assert missing.status == 404
    shown = native_client.get("/api/v1/print/library", params={"folder_id": folder})
    assert shown.status_code == 200, shown.text
    assert [entry["id"] for entry in shown.json()["files"]] == [uploaded.id]
    row = native_client.portal.call(_native(native_client)._file, uploaded.id)
    assert (row.output_id, row.filed) == (None, False)
    paths = native_client.get("/openapi.json").json()["paths"]
    assert not [path for path in paths if path.startswith("/api/v1/internal/library")]


def test_the_internal_write_on_bambuddy_answers_404(client: TestClient) -> None:
    response = client.put(
        "/api/v1/internal/library/files",
        params={"filename": "x.3mf"},
        content=b"x",
        headers={"content-type": "application/octet-stream"},
    )
    assert response.status_code == 404
    listed = client.get("/api/v1/internal/library", params={"folder_id": 1_000_000_000})
    assert listed.status_code == 404
