"""What the print routes read of a library, whichever one serves it (plan
2026-10-17-native-print-library, Task 3). `BambuddyClient` and `NativeLibrary` both
satisfy these, structurally."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Protocol

from fastapi import status

from scadbuddy.bambuddy.models import (
    FilamentRequirements,
    Folder,
    LibraryFile,
    LibraryListRow,
    LibraryPlates,
)
from scadbuddy.core.problems import ApiError
from scadbuddy.print_library.ids import is_native_id

if TYPE_CHECKING:
    from scadbuddy.bambuddy.client import BambuddyClient


class LibraryListingSource(Protocol):
    async def folders(self) -> list[Folder]: ...
    async def library_listing(
        self, *, folder_id: int | None, every_folder: bool = False
    ) -> list[LibraryListRow]: ...


class LibraryReads(Protocol):
    async def library_file(self, file_id: int) -> LibraryFile: ...
    async def library_plates(self, file_id: int) -> LibraryPlates: ...
    def download_library_file(self, file_id: int) -> AsyncGenerator[bytes]: ...
    async def filament_requirements(
        self, file_id: int, *, plate_id: int | None = None
    ) -> FilamentRequirements: ...


def reads_for(file_id: int, client: BambuddyClient, native: LibraryReads | None) -> LibraryReads:
    """Where file ``file_id`` is read: ``native`` for a native id, Bambuddy for any other.
    A native id with no native library to read it (``library_backend=bambuddy``) is the
    404 a missing file is."""
    if not is_native_id(file_id):
        return client
    if native is None:
        raise ApiError(
            status.HTTP_404_NOT_FOUND, f"library file {file_id} not found", file_id=file_id
        )
    return native
