"""The routes the print worker reads a native library file through (plan
2026-10-17-native-print-library, Task 7a), as it reads an output through
``api/internal.py`` (#1060): the ``scadbuddy-print`` worker holds neither the library's
rows nor the content store. Not a client API: hidden from the schema. On
``library_backend=bambuddy`` every file is the 404 of one not found.

It writes a native run's per-print copy here too (#3818 tier A Task 4): ``PUT /files``,
the body the file's bytes, streamed to the spool and stored as `NativeLibrary.upload`
stores a person's. Bounded by `PUT_ROUTE_LIMIT`. Like the reads, internal by path only
(README "Deploying": the API has no auth yet, so whoever reaches it reaches these)."""

from __future__ import annotations

import asyncio
import re
import tempfile
from typing import Annotated

from fastapi import APIRouter, Query, Request, Response, status

from scadbuddy.api.library_print import FileIdPath, _native_download
from scadbuddy.api.limits import RouteLimit
from scadbuddy.bambuddy.models import Folder, LibraryFile, LibraryListRow, LibraryPlates
from scadbuddy.bambuddy.print_source import MAX_DOWNLOAD_BYTES
from scadbuddy.core.problems import ApiError
from scadbuddy.print_library.component import PrintLibraryDep
from scadbuddy.print_library.native import NativeLibrary
from scadbuddy.print_library.remote import INTERNAL_LIBRARY, INTERNAL_LIBRARY_FILES

router = APIRouter(prefix=INTERNAL_LIBRARY.removeprefix("/api/v1"), include_in_schema=False)

#: For ``main.py``'s `BodySizeGate`: a copy is a 3MF laid out from one ScadBuddy held
#: whole, so the library download cap bounds it as it bounds a person's upload.
#: What a layout may add over its source (settings, hint parts, a re-zip; #4312 review
#: M5): a copy of a source at the cap still fits.
LAYOUT_HEADROOM = 16 * 1024 * 1024

PUT_ROUTE_LIMIT = RouteLimit(
    "PUT",
    re.compile(rf"^{re.escape(INTERNAL_LIBRARY_FILES)}$"),
    MAX_DOWNLOAD_BYTES + LAYOUT_HEADROOM,
    "a print's copy",
    "the library download cap (bambuddy/print_source.py MAX_DOWNLOAD_BYTES)",
)


async def _native(library: PrintLibraryDep, file_id: int | None = None) -> NativeLibrary:
    native = await library.serving_native()
    if native is None:
        if file_id is None:
            raise ApiError(status.HTTP_404_NOT_FOUND, "ScadBuddy's own library does not serve")
        raise ApiError(
            status.HTTP_404_NOT_FOUND, f"library file {file_id} not found", file_id=file_id
        )
    return native


@router.get("", response_model=list[LibraryListRow])
async def library_listing(
    library: PrintLibraryDep,
    folder_id: Annotated[int | None, Query(ge=1)] = None,
    every_folder: bool = False,
    per_print: bool = False,
) -> list[LibraryListRow]:
    native = await _native(library)
    if per_print:
        return await native.per_print_files(folder_id)
    return await native.library_listing(folder_id=folder_id, every_folder=every_folder)


@router.get("/folders", response_model=list[Folder])
async def library_folders(library: PrintLibraryDep) -> list[Folder]:
    return await (await _native(library)).folders()


@router.put("/folders/template", response_model=Folder)
async def template_folder(
    library: PrintLibraryDep, name: Annotated[str, Query(min_length=1, max_length=255)]
) -> Folder:
    """A native print's default destination (#3818 tier A Task 5): the template's
    top-level folder, as the filing names it, found or made."""
    folder = await (await _native(library)).template_folder(name)
    return Folder(id=folder.id, name=folder.name, parent_id=folder.parent_id)


@router.put("/files", response_model=LibraryFile, status_code=status.HTTP_201_CREATED)
async def upload_library_file(
    request: Request,
    library: PrintLibraryDep,
    filename: Annotated[str, Query()],
    folder_id: Annotated[int | None, Query(ge=1)] = None,
    subject: Annotated[str | None, Query(min_length=1, max_length=512)] = None,
) -> LibraryFile:
    """A per-print file (#4312 review I3): it names no output and is not filed, whatever
    the request says, and its name is checked as the public upload's is. ``subject`` is
    the print subject whose run uploads it (#4337); a print worker from before it sends
    none, and its file is then left unmarked, as every file was."""
    if not filename or "/" in filename or "\x00" in filename or len(filename) > 255:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "the file needs a name of at most 255 characters, without `/`",
        )
    native = await _native(library)
    await asyncio.to_thread(native.spool.mkdir, parents=True, exist_ok=True)
    # Streamed to a file under the spool, never held whole.
    with tempfile.TemporaryFile(dir=native.spool) as body:
        async for chunk in request.stream():
            await asyncio.to_thread(body.write, chunk)
        await asyncio.to_thread(body.seek, 0)
        row = await native.upload(filename, body, folder_id, print_subject=subject)
    return await native.library_file(row.id)


@router.get("/{file_id}", response_model=LibraryFile)
async def library_file(file_id: FileIdPath, library: PrintLibraryDep) -> LibraryFile:
    return await (await _native(library, file_id)).library_file(file_id)


@router.get("/{file_id}/plates", response_model=LibraryPlates)
async def library_plates(file_id: FileIdPath, library: PrintLibraryDep) -> LibraryPlates:
    return await (await _native(library, file_id)).library_plates(file_id)


@router.get("/{file_id}/file", response_class=Response)
async def library_file_bytes(file_id: FileIdPath, library: PrintLibraryDep) -> Response:
    return await _native_download(await _native(library, file_id), file_id)
