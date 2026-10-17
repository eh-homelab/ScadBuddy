"""What the print routes read of a library, whichever one serves it (plan
2026-10-17-native-print-library, Task 3). `BambuddyClient` and `NativeLibrary` both
satisfy these, structurally."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Protocol

from scadbuddy.bambuddy.models import (
    FilamentRequirements,
    Folder,
    LibraryFile,
    LibraryListRow,
    LibraryPlates,
)


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
