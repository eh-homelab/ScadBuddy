"""The native library as the print worker reads it (plan 2026-10-17-native-print-library,
Task 7a): the ``scadbuddy-print`` worker holds neither the library's rows' pool nor the
content store, so it asks the API's cluster-internal routes (``api/internal_library.py``)
for a native file, as `RemoteOutputs` asks for an output (#1060), and writes a native
run's per-print copy there too (#3818 tier A Task 4)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from typing import BinaryIO

import httpx
from pydantic import TypeAdapter

from scadbuddy.bambuddy.models import (
    FilamentRequirements,
    Folder,
    LibraryFile,
    LibraryListRow,
    LibraryPlates,
)
from scadbuddy.bambuddy.output_reader import JSON_MEDIA_TYPE, RESTART_DELAYS, _answer

#: The API's internal routes the remote library calls (``api/internal_library.py``).
INTERNAL_LIBRARY = "/api/v1/internal/library"
#: The bytes route's media type.
OCTET_STREAM = "application/octet-stream"
#: Where `RemoteLibrary.upload` PUTs a file (``api/internal_library.py``).
INTERNAL_LIBRARY_FILES = f"{INTERNAL_LIBRARY}/files"
#: What an upload's body is read in.
UPLOAD_CHUNK = 1024 * 1024

_ROWS = TypeAdapter(list[LibraryListRow])
_FOLDERS = TypeAdapter(list[Folder])


class RemoteLibrary:
    """`LibraryReads` through the API at ``base_url`` (``SCADBUDDY_API_INTERNAL_URL``). A
    problem the API answers is the `ApiError` it raised there (`output_reader._problem`);
    anything else raises as it is, for the activity's retry policy."""

    def __init__(
        self,
        base_url: str,
        http: httpx.AsyncClient,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.http = http
        self._sleep = sleep

    async def aclose(self) -> None:
        await self.http.aclose()

    def _url(self, file_id: int, rest: str = "") -> str:
        return f"{self.base_url}{INTERNAL_LIBRARY}/{file_id}{rest}"

    async def _get(self, url: str, params: dict[str, str] | None = None) -> httpx.Response:
        """One GET, retried on a transport error (the API restarting) for about
        `RESTART_DELAYS`, as `RemoteOutputs` does."""
        for delay in RESTART_DELAYS:
            try:
                return await self.http.get(url, params=params)
            except httpx.TransportError:
                await self._sleep(delay)
        return await self.http.get(url, params=params)

    async def library_listing(
        self, *, folder_id: int | None, every_folder: bool = False
    ) -> list[LibraryListRow]:
        """The files directly in ``folder_id`` (the top level for ``None``), or in every
        folder: what finds an upload an earlier attempt left unrecorded."""
        params = {"every_folder": "true"} if every_folder else {}
        if folder_id is not None:
            params["folder_id"] = str(folder_id)
        response = await self._get(f"{self.base_url}{INTERNAL_LIBRARY}", params)
        return _ROWS.validate_json(_answer(response, JSON_MEDIA_TYPE).content)

    async def folders(self) -> list[Folder]:
        """The folder tree, as `NativeLibrary.folders` answers it."""
        response = await self._get(f"{self.base_url}{INTERNAL_LIBRARY}/folders")
        return _FOLDERS.validate_json(_answer(response, JSON_MEDIA_TYPE).content)

    async def template_folder(self, name: str) -> Folder:
        """The top-level folder ``name``, found or made (`NativeLibrary.template_folder`):
        idempotent, so a transport error is retried as a read is."""
        url = f"{self.base_url}{INTERNAL_LIBRARY}/folders/template"
        for delay in RESTART_DELAYS:
            try:
                response = await self.http.put(url, params={"name": name})
                break
            except httpx.TransportError:
                await self._sleep(delay)
        else:
            response = await self.http.put(url, params={"name": name})
        return Folder.model_validate_json(_answer(response, JSON_MEDIA_TYPE).content)

    async def per_print_files(self, folder_id: int | None) -> list[LibraryListRow]:
        params = {"per_print": "true"}
        if folder_id is not None:
            params["folder_id"] = str(folder_id)
        response = await self._get(f"{self.base_url}{INTERNAL_LIBRARY}", params)
        return _ROWS.validate_json(_answer(response, JSON_MEDIA_TYPE).content)

    async def upload(
        self, filename: str, data: BinaryIO, folder_id: int | None, *, print_subject: str
    ) -> LibraryFile:
        """``data`` uploaded as a new native file, streamed, marked with the
        ``print_subject`` whose run uploads it (#4337). Never retried here (a second PUT
        would be a second file): the caller finds an earlier attempt's upload by its
        bytes (`copies.left_unrecorded`), and the activity's retry policy retries."""
        params = {"filename": filename, "subject": print_subject}
        if folder_id is not None:
            params["folder_id"] = str(folder_id)

        async def body() -> AsyncIterator[bytes]:
            while chunk := await asyncio.to_thread(data.read, UPLOAD_CHUNK):
                yield chunk

        response = await self.http.put(
            f"{self.base_url}{INTERNAL_LIBRARY_FILES}",
            params=params,
            content=body(),
            headers={"content-type": OCTET_STREAM},
        )
        return LibraryFile.model_validate_json(_answer(response, JSON_MEDIA_TYPE).content)

    async def library_file(self, file_id: int) -> LibraryFile:
        response = await self._get(self._url(file_id))
        return LibraryFile.model_validate_json(_answer(response, JSON_MEDIA_TYPE).content)

    async def library_plates(self, file_id: int) -> LibraryPlates:
        response = await self._get(self._url(file_id, "/plates"))
        return LibraryPlates.model_validate_json(_answer(response, JSON_MEDIA_TYPE).content)

    async def download_library_file(self, file_id: int) -> AsyncGenerator[bytes]:
        """The file's bytes, streamed. Unlike the other reads it is not retried on a
        transport error (a stream cut part-way cannot resume): the error is the
        activity's, whose retry policy reads the file again."""
        async with self.http.stream("GET", self._url(file_id, "/file")) as response:
            if not response.is_success:
                await response.aread()
            _answer(response, OCTET_STREAM)
            async for chunk in response.aiter_bytes():
                yield chunk

    async def filament_requirements(
        self, file_id: int, *, plate_id: int | None = None
    ) -> FilamentRequirements:
        """Empty, as the native library's own are (Ruling 6): the slots are read from the
        file itself. The file is read, so a missing one is its 404."""
        file = await self.library_file(file_id)
        return FilamentRequirements(file_id=file.id, filename=file.filename, plate_id=plate_id)
