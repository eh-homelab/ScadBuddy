"""The routes the print worker reads a native library file through (plan
2026-10-17-native-print-library, Task 7a), as it reads an output through
``api/internal.py`` (#1060): the ``scadbuddy-print`` worker holds neither the library's
rows nor the content store. Not a client API: hidden from the schema. On
``library_backend=bambuddy`` every file is the 404 of one not found."""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from scadbuddy.api.library_print import FileIdPath, _native_download
from scadbuddy.bambuddy.models import LibraryFile, LibraryPlates
from scadbuddy.core.problems import ApiError
from scadbuddy.print_library.component import PrintLibraryDep
from scadbuddy.print_library.native import NativeLibrary
from scadbuddy.print_library.remote import INTERNAL_LIBRARY

router = APIRouter(prefix=INTERNAL_LIBRARY.removeprefix("/api/v1"), include_in_schema=False)


async def _native(library: PrintLibraryDep, file_id: int) -> NativeLibrary:
    native = await library.serving_native()
    if native is None:
        raise ApiError(
            status.HTTP_404_NOT_FOUND, f"library file {file_id} not found", file_id=file_id
        )
    return native


@router.get("/{file_id}", response_model=LibraryFile)
async def library_file(file_id: FileIdPath, library: PrintLibraryDep) -> LibraryFile:
    return await (await _native(library, file_id)).library_file(file_id)


@router.get("/{file_id}/plates", response_model=LibraryPlates)
async def library_plates(file_id: FileIdPath, library: PrintLibraryDep) -> LibraryPlates:
    return await (await _native(library, file_id)).library_plates(file_id)


@router.get("/{file_id}/file", response_class=Response)
async def library_file_bytes(file_id: FileIdPath, library: PrintLibraryDep) -> Response:
    return await _native_download(await _native(library, file_id), file_id)
