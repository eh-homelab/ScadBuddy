"""`RemoteLibrary`: the print worker's reads of a native file, through the API's internal
routes (plan 2026-10-17-native-print-library, Task 7a). The routes themselves run in
`tests/api/test_library_native.py`."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx
import pytest

from scadbuddy.core.problems import PROBLEM_MEDIA_TYPE, ApiError
from scadbuddy.core.settings import Settings
from scadbuddy.print_library.remote import INTERNAL_LIBRARY, RemoteLibrary
from scadbuddy.worker import build_print_deps
from tests.conftest import UNUSED_TEMPORAL_ADDRESS

BASE = "http://scadbuddy:8080"
FILE_ID = 1_000_000_001
FILE = {
    "id": FILE_ID,
    "filename": "bracket.3mf",
    "file_type": "3mf",
    "file_size": 6,
    "file_hash": "a" * 64,
}


def _api(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == f"{INTERNAL_LIBRARY}/{FILE_ID}":
        return httpx.Response(200, json=FILE)
    if path == f"{INTERNAL_LIBRARY}/{FILE_ID}/plates":
        return httpx.Response(200, json={"file_id": FILE_ID, "plates": [{"index": 1}]})
    if path == f"{INTERNAL_LIBRARY}/{FILE_ID}/file":
        return httpx.Response(
            200, content=b"3mf!!!", headers={"content-type": "application/octet-stream"}
        )
    return httpx.Response(
        404,
        json={
            "title": "Not Found",
            "status": 404,
            "detail": "library file 7 not found",
            "file_id": 7,
        },
        headers={"content-type": PROBLEM_MEDIA_TYPE},
    )


def _remote() -> RemoteLibrary:
    return RemoteLibrary(BASE, httpx.AsyncClient(transport=httpx.MockTransport(_api)))


async def test_remote_library_reads_through_the_internal_routes() -> None:
    remote = _remote()
    try:
        file = await remote.library_file(FILE_ID)
        plates = await remote.library_plates(FILE_ID)
        data = b"".join([chunk async for chunk in remote.download_library_file(FILE_ID)])
        needs = await remote.filament_requirements(FILE_ID, plate_id=2)
    finally:
        await remote.aclose()

    assert (file.id, file.filename) == (FILE_ID, "bracket.3mf")
    assert [plate.index for plate in plates.plates] == [1]
    assert data == b"3mf!!!"
    assert (needs.file_id, needs.plate_id, needs.filaments) == (FILE_ID, 2, [])


@pytest.mark.parametrize("read", ["file", "plates", "bytes", "filaments"])
async def test_a_missing_file_is_the_apis_own_problem(read: str) -> None:
    remote = _remote()
    calls: dict[str, Callable[[], Awaitable[object]]] = {
        "file": lambda: remote.library_file(7),
        "plates": lambda: remote.library_plates(7),
        "bytes": lambda: _drain(remote, 7),
        "filaments": lambda: remote.filament_requirements(7),
    }
    try:
        with pytest.raises(ApiError) as raised:
            await calls[read]()
    finally:
        await remote.aclose()
    assert raised.value.status == 404
    assert raised.value.extensions == {"file_id": 7}


async def _drain(remote: RemoteLibrary, file_id: int) -> bytes:
    return b"".join([chunk async for chunk in remote.download_library_file(file_id)])


async def test_a_page_that_is_not_the_apis_is_an_error_to_retry() -> None:
    def spa(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>", headers={"content-type": "text/html"})

    remote = RemoteLibrary(BASE, httpx.AsyncClient(transport=httpx.MockTransport(spa)))
    try:
        with pytest.raises(httpx.HTTPError):
            await remote.library_file(FILE_ID)
        with pytest.raises(httpx.HTTPError):
            await _drain(remote, FILE_ID)
    finally:
        await remote.aclose()


async def test_the_api_restarting_is_waited_out() -> None:
    answers = iter([httpx.ConnectError("refused"), None])

    def restarting(request: httpx.Request) -> httpx.Response:
        error = next(answers)
        if error is not None:
            raise error
        return _api(request)

    async def no_wait(delay: float) -> None:
        pass

    remote = RemoteLibrary(
        BASE, httpx.AsyncClient(transport=httpx.MockTransport(restarting)), sleep=no_wait
    )
    try:
        assert (await remote.library_file(FILE_ID)).id == FILE_ID
    finally:
        await remote.aclose()


@pytest.mark.requires_postgres
async def test_the_print_worker_reads_native_files_remotely_and_closes_it(
    pg_conninfo: str, tmp_path: Path
) -> None:
    settings = Settings(
        database_url=pg_conninfo,
        temporal_address=UNUSED_TEMPORAL_ADDRESS,
        data_dir=tmp_path / "data",
        api_internal_url=BASE,
    )
    deps = await asyncio.to_thread(build_print_deps, settings)
    library = deps.library
    assert isinstance(library, RemoteLibrary)
    assert library.base_url == BASE

    await deps.aclose()

    assert library.http.is_closed
