"""What the print routes read of a library, whichever one serves it (plan
2026-10-17-native-print-library, Task 3). `BambuddyClient` and `NativeLibrary` both
satisfy these, structurally."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import TYPE_CHECKING, BinaryIO, Protocol

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


class Uploaded(Protocol):
    """What an upload answers: `NativeLibrary`'s row, or `RemoteLibrary`'s `LibraryFile`."""

    @property
    def id(self) -> int: ...


class LibraryWrites(Protocol):
    """A per-print file's upload: it names no output and is not filed (#4312 review I3),
    so nothing written through it can pass for an output's filed copy."""

    async def upload(self, filename: str, data: BinaryIO, folder_id: int | None) -> Uploaded: ...


class NativeCopies(LibraryReads, LibraryListingSource, LibraryWrites, Protocol):
    """What a native run's per-print copy needs of the native library (#3818 tier A Tasks
    4, 5): its reads, its folders (the destination's check), a folder's files (an earlier
    attempt's upload, found by its bytes), the upload, and the template's folder the
    filing uses. `NativeLibrary` in the API's process, `RemoteLibrary` on the print
    worker."""

    async def template_folder(self, name: str) -> Uploaded: ...

    async def per_print_files(self, folder_id: int | None) -> list[LibraryListRow]:
        """The live files directly in ``folder_id`` that name no output and are not filed:
        per-print copies and slices, and files a person uploaded."""
        ...


def _no_client(message: str) -> ApiError:
    """A Bambuddy read reached with no client, as a native run (#3818 tier A, plan
    2026-10-18 D3) passes none: a caller's mistake, the 500 the print path's misuse guards
    raise (`print_source._misuse`), which its activities raise as non-retryable."""
    return ApiError(status.HTTP_500_INTERNAL_SERVER_ERROR, message)


def reads_for(
    file_id: int, client: BambuddyClient | None, native: LibraryReads | None
) -> LibraryReads:
    """Where file ``file_id`` is read: ``native`` for a native id, Bambuddy for any other.
    A native id with no native library to read it (``library_backend=bambuddy``) is the
    404 a missing file is."""
    if not is_native_id(file_id):
        if client is None:
            raise _no_client(f"library file {file_id} is Bambuddy's, and a native run has none")
        return client
    if native is None:
        raise ApiError(
            status.HTTP_404_NOT_FOUND, f"library file {file_id} not found", file_id=file_id
        )
    return native


def served_reads(
    file_id: int, client: BambuddyClient | None, native: LibraryReads | None
) -> LibraryReads:
    """Where the API reads file ``file_id``, ``native`` being the library it serves: all of
    it on ``native``, where a Bambuddy-range id is its 404 (one id space at a time), and
    :func:`reads_for` on ``bambuddy``. Not for the print worker, whose ``RemoteLibrary``
    does not say which library the API serves: it takes :func:`reads_for`."""
    if native is not None:
        return native
    if client is None:
        raise _no_client(f"library file {file_id} is read from Bambuddy, which needs a client")
    return reads_for(file_id, client, None)


def check_served(file_id: int, native: object | None) -> None:
    """The 404 for an id of the library that does not serve (one id space at a time): a
    Bambuddy-range id on ``native``, a native id on ``bambuddy`` (``native`` None)."""
    if is_native_id(file_id) != (native is not None):
        raise ApiError(
            status.HTTP_404_NOT_FOUND, f"library file {file_id} not found", file_id=file_id
        )


def check_folder_served(folder_id: int | None, native: object | None) -> None:
    """`check_served` for a folder: the other library's folder is a 404, as the native
    write routes answer a missing one, never an empty listing."""
    if folder_id is not None and is_native_id(folder_id) != (native is not None):
        raise ApiError(
            status.HTTP_404_NOT_FOUND,
            f"library folder {folder_id} not found",
            folder_id=folder_id,
        )


def reads_of(files: LibraryReads | None, client: BambuddyClient | None) -> LibraryReads:
    """``files`` when given, else Bambuddy's ``client``, one of which a read needs."""
    if files is not None:
        return files
    if client is None:
        raise _no_client("a library read needs its file's library or a Bambuddy client")
    return client


#: The native library a kind reads a native file from (#3827), asked when the kind runs:
#: the kinds are built before the settings say which library serves.
NativeReads = Callable[[], Awaitable[LibraryReads | None]]


async def no_native() -> LibraryReads | None:
    return None
