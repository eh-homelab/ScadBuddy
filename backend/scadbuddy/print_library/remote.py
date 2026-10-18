"""The native library as the print worker reads it (plan 2026-10-17-native-print-library,
Task 7a): the ``scadbuddy-print`` worker holds neither the library's rows' pool nor the
content store, so it asks the API's cluster-internal routes (``api/internal_library.py``)
for a native file, as `RemoteOutputs` asks for an output (#1060)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable

import httpx

from scadbuddy.bambuddy.models import FilamentRequirements, LibraryFile, LibraryPlates
from scadbuddy.bambuddy.output_reader import JSON_MEDIA_TYPE, RESTART_DELAYS, _answer

#: The API's internal routes the remote library calls (``api/internal_library.py``).
INTERNAL_LIBRARY = "/api/v1/internal/library"
#: The bytes route's media type.
OCTET_STREAM = "application/octet-stream"


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

    async def _get(self, url: str) -> httpx.Response:
        """One GET, retried on a transport error (the API restarting) for about
        `RESTART_DELAYS`, as `RemoteOutputs` does."""
        for delay in RESTART_DELAYS:
            try:
                return await self.http.get(url)
            except httpx.TransportError:
                await self._sleep(delay)
        return await self.http.get(url)

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
