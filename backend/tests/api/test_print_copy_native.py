"""The print worker's writes of a native per-print copy (#3818 tier A Task 4): through
the API's hidden internal route, as its reads are (`RemoteLibrary`, plan
2026-10-17-native-print-library Task 7a)."""

from __future__ import annotations

import io
from collections.abc import Iterator
from functools import partial
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR
from scadbuddy.api.internal_library import PUT_ROUTE_LIMIT
from scadbuddy.bambuddy.print_source import MAX_DOWNLOAD_BYTES
from scadbuddy.bambuddy.uploads import LibraryCopy, SlicedCopy
from scadbuddy.core.problems import ApiError
from scadbuddy.core.settings import Settings
from scadbuddy.library.settings_store import S3Settings
from scadbuddy.main import create_app
from scadbuddy.print_library.ids import is_native_id
from scadbuddy.print_library.remote import RemoteLibrary
from tests.api.test_library_native import _native, _native_settings, _save, _three_mf
from tests.api.test_library_native import widget as widget  # the fixture, shared
from tests.support.operations import press

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


@pytest.mark.parametrize("filename", ["a/b.3mf", "a\x00.3mf", "x" * 256 + ".3mf"])
def test_the_internal_write_checks_the_name_as_the_public_upload_does(
    native_client: TestClient, filename: str
) -> None:
    """#4312 review M4: no `/`, no NUL, at most 255 characters (`api/library_print.py`)."""
    response = native_client.put(
        "/api/v1/internal/library/files",
        params={"filename": filename},
        content=b"x",
        headers={"content-type": "application/octet-stream"},
    )
    assert response.status_code == 422, response.text


def test_the_internal_write_never_names_an_output_or_files_one(
    native_client: TestClient,
) -> None:
    """#4312 review I3: the route takes no `output_id` or `filed`, so nothing that reaches
    it can make a row the filing would take for an output's filed copy."""
    response = native_client.put(
        "/api/v1/internal/library/files",
        params={"filename": "x.3mf", "output_id": "0123456789abcdef", "filed": "true"},
        content=b"x",
        headers={"content-type": "application/octet-stream"},
    )
    assert response.status_code == 201, response.text
    row = native_client.portal.call(_native(native_client)._file, response.json()["id"])  # type: ignore[union-attr]
    assert (row.output_id, row.filed) == (None, False)


def test_the_write_limit_has_room_over_the_source_cap() -> None:
    """#4312 review M5: a laid-out copy of a source at the cap can be a little larger."""
    assert isinstance(PUT_ROUTE_LIMIT.limit, int)
    assert PUT_ROUTE_LIMIT.limit > MAX_DOWNLOAD_BYTES


@pytest.mark.requires_git
@pytest.mark.parametrize("delete_inbox_copies", [True, False])
def test_an_output_delete_trashes_its_native_per_print_copies(
    native_client: TestClient, widget: str, tmp_path: Path, delete_inbox_copies: bool
) -> None:
    """#4312 review I1: a native run's per-print copy and its slice name no output (Ruling
    6), so the delete reaches them through their upload rows, before it forgets the rows.
    With ``delete_inbox_copies`` they go to the trash as Bambuddy's inbox copies are
    deleted; without, they stay as files of the library, as Bambuddy's do. Either way no
    file is left that nothing names and nobody can see."""
    saved = _save(native_client, widget, "One").json()
    native = _native(native_client)
    state = getattr(native_client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    payload = _three_mf(tmp_path / "copy.3mf").read_bytes()
    portal = native_client.portal
    assert portal is not None
    copy = portal.call(native.upload, "copy.3mf", io.BytesIO(payload), None)
    sliced = portal.call(native.upload, "copy.plate1.sliced.3mf", io.BytesIO(b"g"), None)
    portal.call(
        state.uploads.record,
        saved["id"],
        LibraryCopy(id=copy.id, folder_id=None, target_key="t"),
    )
    portal.call(state.uploads.record_sliced, saved["id"], copy.id, SlicedCopy(id=sliced.id))

    deleted = native_client.delete(
        f"/api/v1/outputs/{saved['id']}",
        params={"delete_inbox_copies": str(delete_inbox_copies).lower()},
        headers=press(),
    )

    assert deleted.status_code == 204, deleted.text
    live = {f.id for f in native.rows.files(None, every_folder=True)}
    trashed = {copy.id, sliced.id} - live
    assert trashed == ({copy.id, sliced.id} if delete_inbox_copies else set())
    assert portal.call(partial(state.uploads.for_output, saved["id"], native=True)) == []
