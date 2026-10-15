"""``/api/v1/print/library/…`` — printing a file already in Bambuddy's library (#313).

The same dialog as an output's (``printing.py``): its choices, filament step and run,
over :class:`~scadbuddy.bambuddy.print_source.LibrarySource`, which prints a copy laid
out for the printer as an output's is (#1752) and never changes the file itself; a
sliced file is queued as it is (#2671). The images are proxied so the API key never
reaches the browser.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncGenerator, AsyncIterable, AsyncIterator
from functools import partial
from typing import Annotated, BinaryIO

from fastapi import APIRouter, Path, Query, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from scadbuddy.activity.component import ACTIVITY, ActivityComponent
from scadbuddy.api.components import component_dep
from scadbuddy.api.deps import (
    DownloadsDep,
    OutputsDep,
    PathsDep,
    PrintLinksDep,
    PrintProgressDep,
    PrintRunsDep,
    ReadBudgetDep,
    SettingsStoreDep,
    StateDep,
    UploadsDep,
)
from scadbuddy.api.jobs import GLB_MEDIA_TYPE
from scadbuddy.api.media import receive_upload
from scadbuddy.api.operations import (
    OPERATION_RESPONSES,
    Claimed,
    IdempotencyKey,
    operation_answer,
    recorded,
    run_operation,
)
from scadbuddy.api.outputs import OutputPlate, live_outputs_for_files, read_plain_files
from scadbuddy.api.printing import PRINT_RUN_PROBLEMS, accept_run, failed_before_queueing
from scadbuddy.api.prints import MEDIA_RESPONSES, _proxy
from scadbuddy.bambuddy.choices import ChoicesView, choices_for
from scadbuddy.bambuddy.client import client_for
from scadbuddy.bambuddy.filaments import FilamentOptions
from scadbuddy.bambuddy.filaments3mf import LocalFilaments
from scadbuddy.bambuddy.library_listing import LibraryListing, list_library
from scadbuddy.bambuddy.library_view import (
    SPOOL_DIRNAME,
    NotViewableError,
    library_filaments,
    library_preview,
)
from scadbuddy.bambuddy.preview import (
    TOOLPATH_HEADERS,
    PreviewStarted,
    SlicePreview,
    read_preview,
    read_toolpath,
)
from scadbuddy.bambuddy.print_run import (
    PrintCheck,
    PrintRunRequest,
    check_for_library,
    filament_options_for_library,
)
from scadbuddy.bambuddy.print_source import LibrarySource
from scadbuddy.bambuddy.progress import QUEUE_PATH, PrintProgress, from_failed_run, library_progress
from scadbuddy.bambuddy.projects import AttachResult, ProjectAttach
from scadbuddy.bambuddy.runs import PrintRun
from scadbuddy.bambuddy.subject import PrintSubject, library_slug
from scadbuddy.core.paths import DataPaths
from scadbuddy.core.problems import ApiError
from scadbuddy.core.tracing import span
from scadbuddy.library.library_file_moves import (
    LibraryCopyResult,
    LibraryMoveResult,
    LibraryRenameRequest,
    LibraryRenameResult,
    LibraryTransferRequest,
)
from scadbuddy.library.library_file_operations import (
    LibraryDeleteResult,
    LibraryFilesRequest,
    LibraryRestoreResult,
)
from scadbuddy.library.library_write_operations import (
    LibraryEntryUpdated,
    LibraryFileUpdate,
    LibraryFolder,
    LibraryFolderCreate,
    LibraryFolderDeleteResult,
    LibraryFolderUpdate,
)
from scadbuddy.library.settings_store import ModelPrintChoices, SettingsStore
from scadbuddy.operations.claims import ClaimStore, Held
from scadbuddy.operations.component import OperationsDep
from scadbuddy.rack.component import RackUsageDep
from scadbuddy.render.geometry import NoSuchPlateError
from scadbuddy.render.read_budget import ReadBudget
from scadbuddy.store.download_cache import DownloadCache, Fill, TooLargeError, is_sha256
from scadbuddy.workflows.component import FollowsDep
from scadbuddy.workflows.print_models import SourceSpec

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/print/library", tags=["print"])

#: The operations record's subject for a library delete, restore, copy, move or rename.
LIBRARY_FILES_SUBJECT = "library-files"

FileIdPath = Annotated[int, Path(ge=1)]
FolderIdPath = Annotated[int, Path(ge=1)]


@router.get("", response_model=LibraryListing, summary="Bambuddy's library, one folder at a time")
async def get_library(
    store: SettingsStoreDep,
    uploads: UploadsDep,
    outputs: OutputsDep,
    folder_id: Annotated[int | None, Query()] = None,
    show_all: Annotated[bool, Query(alias="all")] = False,
    file_id: Annotated[int | None, Query(ge=1)] = None,
) -> LibraryListing:
    """The folder tree and one folder's files (the root's without ``folder_id``).
    Without ``all`` only unsliced 3MFs; with it every file, each flagged ``printable``.
    A file ScadBuddy uploaded names its ``output_id`` (#1864), while that output still
    exists: the upload row outlives a deleted output, and a file whose output is gone is
    any other library file (#2138). ``file_id`` in place of ``folder_id`` lists the
    folder that file is in, which a deep link to a file opens (#2165); a file Bambuddy
    no longer has is its 404."""
    async with client_for(store.load()) as client:
        if file_id is not None and folder_id is None:
            folder_id = (await client.library_file(file_id)).folder_id
        listing = await list_library(client, folder_id=folder_id, show_all=show_all)
    made = await live_outputs_for_files(uploads, outputs, (entry.id for entry in listing.files))
    for entry in listing.files:
        entry.output_id = made.get(entry.id)
    return listing


@router.post(
    "/delete",
    response_model=LibraryDeleteResult,
    summary="Delete library files through Bambuddy, to its trash",
    responses=OPERATION_RESPONSES,
)
async def post_library_delete(
    body: LibraryFilesRequest,
    response: Response,
    ops: OperationsDep,
    idempotency_key: IdempotencyKey = None,
) -> LibraryDeleteResult | JSONResponse:
    """Moves each file to Bambuddy's trash (#2167), one file with ``DELETE``, several with
    Bambuddy's bulk delete. An external file skips the trash and is gone for good
    (``trashed: false``). A file Bambuddy skipped is listed in ``skipped``, never in
    ``deleted``. A file a print waiting in Bambuddy's queue names is a 409, and one
    Bambuddy no longer has a 404, both before anything is deleted."""
    result = await run_operation(
        ops,
        response,
        kind=ops.kinds["library_file_delete"],
        subject=LIBRARY_FILES_SUBJECT,
        request=body,
        idempotency_key=idempotency_key,
    )
    return operation_answer(result, LibraryDeleteResult)


@router.post(
    "/restore",
    response_model=LibraryRestoreResult,
    summary="Restore library files from Bambuddy's trash",
    responses=OPERATION_RESPONSES,
)
async def post_library_restore(
    body: LibraryFilesRequest,
    response: Response,
    ops: OperationsDep,
    idempotency_key: IdempotencyKey = None,
) -> LibraryRestoreResult | JSONResponse:
    """A delete's Undo (#2167): each file back from Bambuddy's trash, under its own id. A
    file not in the trash (external, or emptied from it) is listed in ``skipped``."""
    result = await run_operation(
        ops,
        response,
        kind=ops.kinds["library_file_restore"],
        subject=LIBRARY_FILES_SUBJECT,
        request=body,
        idempotency_key=idempotency_key,
    )
    return operation_answer(result, LibraryRestoreResult)


@router.post(
    "/move",
    response_model=LibraryMoveResult,
    summary="Move library files to another folder through Bambuddy",
    responses=OPERATION_RESPONSES,
)
async def post_library_move(
    body: LibraryTransferRequest,
    response: Response,
    ops: OperationsDep,
    idempotency_key: IdempotencyKey = None,
) -> LibraryMoveResult | JSONResponse:
    """Moves each file into ``folder_id`` or ``folder_path`` (the top level with neither)
    under its own id (#2670), so everything ScadBuddy keys by the id stays with it, and
    its upload rows (#316) follow it to the new folder; moved into the inbox, it stops being
    recorded, so no send or output delete removes it. A file that leaves a project's
    folder names that project in ``left_project``: the project no longer lists it. A file
    Bambuddy skipped is in ``skipped``. A folder or file Bambuddy does not have is a 404,
    and a path two folders share a 409, both before anything moves."""
    result = await run_operation(
        ops,
        response,
        kind=ops.kinds["library_file_move"],
        subject=LIBRARY_FILES_SUBJECT,
        request=body,
        idempotency_key=idempotency_key,
    )
    return operation_answer(result, LibraryMoveResult)


@router.post(
    "/copy",
    response_model=LibraryCopyResult,
    summary="Copy library files into a folder",
    responses=OPERATION_RESPONSES,
)
async def post_library_copy(
    body: LibraryTransferRequest,
    response: Response,
    ops: OperationsDep,
    idempotency_key: IdempotencyKey = None,
) -> LibraryCopyResult | JSONResponse:
    """Copies each file into ``folder_id`` or ``folder_path`` (the top level with neither)
    as a new library file (#2670). Bambuddy has no copy, so each is downloaded and
    uploaded, its notes copied after; a copy of an output's copy is recorded as one more
    copy of that output, unless it is in the inbox. A file too large to copy, gone by then,
    or that Bambuddy failed on is in ``skipped``, with any after a failure; the copies made
    before it are still answered."""
    result = await run_operation(
        ops,
        response,
        kind=ops.kinds["library_file_copy"],
        subject=LIBRARY_FILES_SUBJECT,
        request=body,
        idempotency_key=idempotency_key,
    )
    return operation_answer(result, LibraryCopyResult)


@router.post(
    "/rename",
    response_model=LibraryRenameResult,
    summary="Rename a library file through Bambuddy",
    responses=OPERATION_RESPONSES,
)
async def post_library_rename(
    body: LibraryRenameRequest,
    response: Response,
    ops: OperationsDep,
    idempotency_key: IdempotencyKey = None,
) -> LibraryRenameResult | JSONResponse:
    """Renames the file under its own id (#2670). The new name keeps the file's
    extension (``.3mf``, ``.gcode.3mf``), or the rename is a 422; Bambuddy refuses a name a
    printer's SD card would not take."""
    result = await run_operation(
        ops,
        response,
        kind=ops.kinds["library_file_rename"],
        subject=LIBRARY_FILES_SUBJECT,
        request=body,
        idempotency_key=idempotency_key,
    )
    return operation_answer(result, LibraryRenameResult)


@router.patch(
    "/folders/{folder_id}",
    response_model=LibraryFolder,
    summary="Rename or move a library folder",
    responses=OPERATION_RESPONSES,
)
async def patch_library_folder(
    folder_id: FolderIdPath,
    body: LibraryFolderUpdate,
    response: Response,
    ops: OperationsDep,
    idempotency_key: IdempotencyKey = None,
) -> LibraryFolder | JSONResponse:
    """A partial update through Bambuddy: ``name``, ``parent_id`` (``0`` is the top
    level), or both (#2686)."""
    result = await run_operation(
        ops,
        response,
        kind=ops.kinds["library_folder_update"],
        subject=LIBRARY_FILES_SUBJECT,
        request={"folder_id": folder_id, "update": body.model_dump(mode="json", exclude_none=True)},
        idempotency_key=idempotency_key,
    )
    return operation_answer(result, LibraryFolder)


@router.post(
    "/folders/{folder_id}/delete",
    response_model=LibraryFolderDeleteResult,
    summary="Delete a library folder and everything in it",
    responses=OPERATION_RESPONSES,
)
async def post_library_folder_delete(
    folder_id: FolderIdPath,
    response: Response,
    ops: OperationsDep,
    idempotency_key: IdempotencyKey = None,
) -> LibraryFolderDeleteResult | JSONResponse:
    """Deletes the folder's files as ``/delete`` does, then its folders, deepest first
    (#2686). A file a print waiting in Bambuddy's queue names is a 409 before anything
    is deleted, as are files of the folder in Bambuddy's trash; a key without
    ``library:delete_all`` (which sees only its own user's trash) is a 403. A folder
    holding a file Bambuddy skipped, and its ancestors, are kept."""
    result = await run_operation(
        ops,
        response,
        kind=ops.kinds["library_folder_delete"],
        subject=LIBRARY_FILES_SUBJECT,
        request={"folder_id": folder_id},
        idempotency_key=idempotency_key,
    )
    return operation_answer(result, LibraryFolderDeleteResult)


@router.post(
    "/folders",
    response_model=LibraryFolder,
    summary="Make a library folder",
    responses=OPERATION_RESPONSES,
)
async def post_library_folder(
    body: LibraryFolderCreate,
    response: Response,
    ops: OperationsDep,
    idempotency_key: IdempotencyKey = None,
) -> LibraryFolder | JSONResponse:
    """A new folder through Bambuddy, at the top level without ``parent_id`` (#2686)."""
    result = await run_operation(
        ops,
        response,
        kind=ops.kinds["library_folder_create"],
        subject=LIBRARY_FILES_SUBJECT,
        request=body,
        idempotency_key=idempotency_key,
    )
    return operation_answer(result, LibraryFolder)


class LibraryFileUploadForm(BaseModel):
    """The multipart body of ``POST /print/library/files`` (schema only)."""

    file: bytes = Field(description="The file; its filename is the library name")
    folder_id: int | None = Field(default=None, description="The folder; absent is the top level")


@router.post(
    "/files",
    response_model=LibraryEntryUpdated,
    summary="Upload a file into a library folder",
    responses=OPERATION_RESPONSES,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {"$ref": "#/components/schemas/LibraryFileUploadForm"}
                }
            },
        }
    },
)
async def post_library_file(
    request: Request,
    response: Response,
    ops: OperationsDep,
    paths: PathsDep,
    idempotency_key: IdempotencyKey = None,
) -> LibraryEntryUpdated | JSONResponse:
    """Streams the ``file`` part to disk and uploads it through Bambuddy, to the folder
    in ``folder_id`` or the top level; its filename is the library name. The body may be
    as large as the library download cap (#2686)."""
    received = await receive_upload(request, paths.cache, fields={"folder_id": 32})
    try:
        path = received.files.get("file")
        if path is None or received.sizes["file"] == 0:
            raise ApiError(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "send the file as the `file` part"
            )
        filename = received.filenames["file"]
        if not filename or "/" in filename or "\x00" in filename or len(filename) > 255:
            raise ApiError(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "the file needs a name of at most 255 characters, without `/`",
            )
        try:
            raw = received.fields.get("folder_id", bytearray()).decode("ascii").strip()
            folder_id = int(raw) if raw else None
        except ValueError:
            raise ApiError(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "`folder_id` is a folder's number"
            ) from None
        if folder_id is not None and folder_id < 1:
            raise ApiError(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "`folder_id` is a folder's number"
            )
        digest = received.hashes["file"].hexdigest()
        body = {"claim": digest, "filename": filename, "folder_id": folder_id}
        kind = ops.kinds["library_file_upload"]
        claims = ClaimStore(paths.claims)
        held: list[Held] = []
        if (
            await recorded(
                ops,
                kind=kind,
                subject=LIBRARY_FILES_SUBJECT,
                request=body,
                idempotency_key=idempotency_key,
            )
            is None
        ):
            held.append(await asyncio.to_thread(claims.hold_file, path, digest))
        result = await run_operation(
            ops,
            response,
            kind=kind,
            subject=LIBRARY_FILES_SUBJECT,
            request=body,
            idempotency_key=idempotency_key,
            claimed=Claimed(claims, held),
        )
        return operation_answer(result, LibraryEntryUpdated)
    finally:
        received.discard()


@router.patch(
    "/{file_id}",
    response_model=LibraryEntryUpdated,
    summary="Rename or move a library file",
    responses=OPERATION_RESPONSES,
)
async def patch_library_file(
    file_id: FileIdPath,
    body: LibraryFileUpdate,
    response: Response,
    ops: OperationsDep,
    idempotency_key: IdempotencyKey = None,
) -> LibraryEntryUpdated | JSONResponse:
    """A partial update through Bambuddy: ``filename``, ``folder_id`` (``0`` is the top
    level), or both (#2686)."""
    result = await run_operation(
        ops,
        response,
        kind=ops.kinds["library_file_update"],
        subject=LIBRARY_FILES_SUBJECT,
        request={"file_id": file_id, "update": body.model_dump(mode="json", exclude_none=True)},
        idempotency_key=idempotency_key,
    )
    return operation_answer(result, LibraryEntryUpdated)


@router.get(
    "/{file_id}/plates", response_model=list[OutputPlate], summary="The library file's plates"
)
async def get_library_plates(file_id: FileIdPath, store: SettingsStoreDep) -> list[OutputPlate]:
    """What the dialog offers as ``plate_id``; empty when Bambuddy reads none (an STL,
    or a 3MF with no plate metadata), which prints as plate 1."""
    async with client_for(store.load()) as client:
        plates = await client.library_plates(file_id)
    return [
        OutputPlate(index=plate.index, has_thumbnail=plate.has_thumbnail, name=plate.name or None)
        for plate in plates.plates
    ]


class LibraryFileObject(BaseModel):
    """One object of a library file, as Arrange reads it from the 3MF (#1863)."""

    #: What `POST /outputs/arrange` names it by, with `library_file_id`.
    part: str
    name: str
    #: How many build items place it this way up.
    count: int
    #: Its filaments' colours, `#RRGGBB`.
    colours: list[str]
    #: Width, depth and height, mm.
    size: tuple[float, float, float]
    notes: list[str]


class LibraryFileObjects(BaseModel):
    file_id: int
    filename: str
    objects: list[LibraryFileObject]


@router.get(
    "/{file_id}/objects",
    response_model=LibraryFileObjects,
    summary="The objects Arrange reads from a library file",
)
async def get_library_objects(
    file_id: FileIdPath, store: SettingsStoreDep, state: StateDep, budget: ReadBudgetDep
) -> LibraryFileObjects:
    """Each object the file's 3MF places, with its count (#1863): what the Arrange
    dialog lists for a file ScadBuddy did not make. One that cannot be arranged is the
    arrange's own 422 (code `library_file_not_arrangeable`), saying why. The read spends
    the read budgets in effect, each overridable for this request (#2087)."""
    async with client_for(store.load()) as client:
        found = (await read_plain_files(client, [file_id], state, budget))[file_id]
    return LibraryFileObjects(
        file_id=file_id,
        filename=found.filename,
        objects=[
            LibraryFileObject(
                part=obj.part,
                name=obj.file,
                count=obj.count,
                colours=obj.colours,
                size=obj.bbox.size,
                notes=obj.notes,
            )
            for obj in found.objects
        ],
    )


@router.get(
    "/{file_id}/preview.glb",
    response_class=Response,
    responses={200: {"content": {GLB_MEDIA_TYPE: {}}}},
    summary="One plate of the library file as a preview mesh",
)
async def get_library_preview(
    file_id: FileIdPath,
    store: SettingsStoreDep,
    paths: PathsDep,
    budget: ReadBudgetDep,
    downloads: DownloadsDep,
    plate: Annotated[int, Query(ge=1)] = 1,
) -> Response:
    """Plate ``plate`` of the file, read from the 3MF a print of it slices (#1753), as an
    output's ``preview.glb`` is read from its own: its parts in their colours, where the
    file places them. A file ScadBuddy cannot read a mesh from (sliced, not a 3MF or STL,
    damaged, past a read budget, #2087) is a 422 saying why; a plate it lacks is a 404."""
    async with client_for(store.load()) as client:
        try:
            data = await library_preview(client, paths.cache, file_id, plate, budget, downloads)
        except NotViewableError as error:
            raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
        except NoSuchPlateError as error:
            raise ApiError(status.HTTP_404_NOT_FOUND, str(error)) from None
    return Response(data, media_type=GLB_MEDIA_TYPE)


#: The largest kept file served from the download cache. A hit is copied whole into the
#: request's own file before its first byte is sent, so past this a cached file would
#: start later than Bambuddy's stream of it does; it is streamed from Bambuddy instead.
CACHED_COPY_MAX_BYTES = 256 * 1024 * 1024


@router.get(
    "/{file_id}/file",
    response_class=StreamingResponse,
    responses=MEDIA_RESPONSES,
    summary="The library file itself",
)
async def get_library_file(
    file_id: FileIdPath,
    request: Request,
    store: SettingsStoreDep,
    paths: PathsDep,
    downloads: DownloadsDep,
) -> Response:
    """The file as Bambuddy's library holds it: what a print of it was made from. Always
    a download: a library file can be any type (an SVG, an HTML page), and served on
    ScadBuddy's origin under the type Bambuddy names it could run script there.

    #2788: a whole-file ask for a file Bambuddy states a hash for is served from the
    shared download cache when it is there, and streamed from Bambuddy while it is
    kept there when it is not, so the first byte never waits on the whole download. A
    ranged ask streams from Bambuddy as before."""
    sha: str | None = None
    keep = False
    if request.headers.get("range") is None and downloads is not None:
        sha = await _cacheable_hash(store, downloads, file_id)
        kept_size = None if sha is None else await asyncio.to_thread(downloads.size, sha)
        # One kept but too large to copy before its first byte is not kept again.
        keep = sha is not None and (kept_size is None or kept_size <= CACHED_COPY_MAX_BYTES)
        if sha is not None and kept_size is not None and kept_size <= CACHED_COPY_MAX_BYTES:
            copy = await asyncio.to_thread(
                downloads.private_copy, sha, paths.cache / SPOOL_DIRNAME, what="library_file"
            )
            if copy is not None:
                size = await asyncio.to_thread(lambda: os.fstat(copy.fileno()).st_size)
                return StreamingResponse(
                    _read_closing(copy),
                    media_type="application/octet-stream",
                    headers={
                        "content-disposition": "attachment",
                        "x-content-type-options": "nosniff",
                        "content-length": str(size),
                        # The bytes are whole here; a resumed download is a ranged ask,
                        # which Bambuddy answers.
                        "accept-ranges": "bytes",
                        # The hash names these bytes, so it is their validator. A miss
                        # keeps Bambuddy's own instead, on purpose: a resume is a ranged
                        # ask Bambuddy answers, and only its validator can match there,
                        # so a resume of a hit restarts whole rather than risk mixed bytes.
                        "etag": f'"{sha}"',
                    },
                )
    response = await _proxy(
        store,
        request,
        f"/library/files/{file_id}/download",
        operation="library.download",
        what="download the library file",
    )
    response.headers["content-type"] = "application/octet-stream"
    response.headers["content-disposition"] = "attachment"
    # A body Bambuddy encoded is not the bytes the hash names; it is sent, never kept.
    if (
        keep
        and sha is not None
        and downloads is not None
        and response.status_code == 200
        and "content-encoding" not in response.headers
    ):
        assert isinstance(response, StreamingResponse)
        response.body_iterator = _kept_as_sent(downloads, sha, response.body_iterator)
    return response


async def _read_closing(copy: BinaryIO) -> AsyncIterator[bytes]:
    """``copy``'s bytes, closing it however the response ends. It has no name on disk,
    so a dropped connection leaves nothing behind."""
    try:
        while chunk := await asyncio.to_thread(copy.read, 1 << 20):
            yield chunk
    finally:
        await asyncio.to_thread(copy.close)


async def _cacheable_hash(
    store: SettingsStore, downloads: DownloadCache, file_id: int
) -> str | None:
    """The hash library file ``file_id`` is kept under in the download cache; None for
    one streamed from Bambuddy as before: no hash stated (#2160), larger than the
    cache keeps by its stated size, or a read of the file that failed. That read is only
    for the cache, so its failure is a miss and the download is still tried.

    It is one more call to Bambuddy before every whole-file download, hit or miss; its
    own span says so, and whether it found a hash to keep the file under."""
    with span("library.file_hash", attributes={"scadbuddy.library.file_id": file_id}) as current:
        try:
            async with client_for(store.load()) as client:
                file = await client.library_file(file_id)
        except ApiError:
            logger.info(
                "could not read a library file's hash", extra={"file_id": file_id}, exc_info=True
            )
            current.set_attribute("scadbuddy.cache.cacheable", False)
            return None
        sha = (file.file_hash or "").lower()
        cacheable = is_sha256(sha) and (file.file_size or 0) <= downloads.max_entry_bytes
        current.set_attribute("scadbuddy.cache.cacheable", cacheable)
        return sha if cacheable else None


async def _kept_as_sent(
    downloads: DownloadCache, sha: str, chunks: AsyncIterable[str | bytes | memoryview]
) -> AsyncGenerator[bytes | str | memoryview]:
    """``chunks``, sent as they come, also written into the download cache under
    ``sha``. Kept only when the bytes are that hash and all of them were sent; past
    the cache's limit, out of room or on a failing volume the copy stops and the
    response goes on."""
    try:
        fill_cm = downloads.fill(sha)
        fill: Fill | None = await asyncio.to_thread(fill_cm.__enter__)
    except OSError:
        logger.warning("could not start keeping a library file", extra={"sha256": sha})
        fill = None
    try:
        async for chunk in chunks:
            if fill is not None:
                # Every chunk is written, a memoryview or str too: one left out would
                # leave the fill short of its hash, and nothing would be kept.
                data = chunk.encode() if isinstance(chunk, str) else bytes(chunk)
                try:
                    await asyncio.to_thread(fill.write, data)
                except (TooLargeError, OSError):
                    # Cleared first and closed here, not awaited: a cancel inside an
                    # awaited close left `fill` set, and the `finally` closed it again
                    # while the thread was still in it.
                    fill = None
                    with contextlib.suppress(OSError):
                        fill_cm.__exit__(None, None, None)
            yield chunk
        if fill is not None:
            try:
                await asyncio.to_thread(fill.commit)
            except OSError:
                logger.warning("could not keep a library file", extra={"sha256": sha})
    finally:
        # First, and not awaited: a browser gone is a cancelled scope, where every await
        # raises again and nothing after it runs, which left the partial on disk. A
        # cancel during a `to_thread` write or commit leaves that thread running while
        # this closes and removes the partial under it: its I/O error is discarded with
        # the thread and nothing is kept, which is the outcome wanted.
        if fill is not None:
            with contextlib.suppress(OSError):
                fill_cm.__exit__(None, None, None)
        # The proxied body closes Bambuddy's response when it is closed.
        close = getattr(chunks, "aclose", None)
        if close is not None:
            await close()


@router.get(
    "/{file_id}/thumbnail",
    response_class=StreamingResponse,
    responses=MEDIA_RESPONSES,
    summary="The library file's thumbnail",
)
async def get_library_thumbnail(
    file_id: FileIdPath, request: Request, store: SettingsStoreDep
) -> Response:
    return await _proxy(
        store,
        request,
        f"/library/files/{file_id}/thumbnail",
        operation="library.thumbnail",
        what="show the file's thumbnail",
    )


@router.get(
    "/{file_id}/plates/{index}/thumbnail",
    response_class=StreamingResponse,
    responses=MEDIA_RESPONSES,
    summary="One plate's image of a library file",
)
async def get_library_plate_thumbnail(
    file_id: FileIdPath,
    index: Annotated[int, Path(ge=1)],
    request: Request,
    store: SettingsStoreDep,
) -> Response:
    return await _proxy(
        store,
        request,
        f"/library/files/{file_id}/plate-thumbnail/{index}",
        operation="library.plate_thumbnail",
        what="show the plate image",
    )


@router.get(
    "/{file_id}/choices",
    response_model=ChoicesView,
    summary="What the print dialog offers for this library file",
)
async def get_library_choices(
    file_id: FileIdPath,
    store: SettingsStoreDep,
    uploads: UploadsDep,
    rack: RackUsageDep,
    paths: PathsDep,
    budget: ReadBudgetDep,
    downloads: DownloadsDep,
    printer_id: Annotated[int | None, Query()] = None,
) -> ChoicesView:
    """As ``/print/outputs/{id}/choices``; ``model_choices`` is what this file last
    printed with, so the dialog reopens on it. The file's filament slots are read from
    the file itself, the fetch its preview shares, and from Bambuddy only for a file
    ScadBuddy cannot read them from (#2550)."""
    settings = store.load()
    remembered = store.library_choices(file_id)
    async with client_for(settings) as client:
        # Loaded alongside the printers and presets (#2504).
        source = LibrarySource.load(
            client,
            file_id,
            uploads=uploads,
            settings=settings,
            filaments=partial(
                library_filaments, client, paths.cache, budget=budget, downloads=downloads
            ),
        )
        return await choices_for(
            client,
            source,
            settings,
            remembered=remembered,
            printer_id=printer_id,
            cache=paths.cache,
            rack=rack,
        )


@router.put(
    "/{file_id}/choices",
    response_model=ModelPrintChoices,
    summary="Remember this library file's choices",
)
def put_library_choices(
    file_id: FileIdPath, body: ModelPrintChoices, store: SettingsStoreDep
) -> ModelPrintChoices:
    """Replaces this file's entry whole; an empty body forgets it. Needs no Bambuddy."""
    return store.set_library_choices(file_id, body)


@router.get(
    "/{file_id}/filaments",
    response_model=FilamentOptions,
    summary="Spools that can print this library file",
)
async def get_library_filaments(
    file_id: FileIdPath,
    store: SettingsStoreDep,
    uploads: UploadsDep,
    paths: PathsDep,
    budget: ReadBudgetDep,
    downloads: DownloadsDep,
    printer_id: Annotated[int | None, Query()] = None,
    plate_id: Annotated[int, Query(ge=1)] = 1,
    all_plates: Annotated[bool, Query()] = False,
) -> FilamentOptions:
    settings = store.load()
    async with client_for(settings) as client:
        return await filament_options_for_library(
            client,
            uploads,
            settings,
            file_id,
            printer_id=printer_id,
            plate_id=plate_id,
            all_plates=all_plates,
            filaments=partial(
                library_filaments, client, paths.cache, budget=budget, downloads=downloads
            ),
        )


@router.post(
    "/{file_id}/run",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=PrintRun,
    responses={
        status.HTTP_200_OK: {
            "model": PrintRun,
            "description": "A repeat of a run in flight, or one that succeeded (or failed "
            "after it tried to queue) within the last ten minutes: that run, and no new print.",
        },
        **PRINT_RUN_PROBLEMS,
    },
    summary="Slice this library file with the dialog's choices and queue it, in the background",
)
async def post_library_run(
    file_id: FileIdPath,
    body: PrintRunRequest,
    response: Response,
    runs: PrintRunsDep,
    activity: Annotated[ActivityComponent, component_dep(ACTIVITY)],
    store: SettingsStoreDep,
    paths: PathsDep,
    budget: ReadBudgetDep,
    downloads: DownloadsDep,
) -> PrintRun:
    """As ``/print/outputs/{id}/run`` (202, then follow ``GET /print/runs/{id}``; a
    repeat of the same request is its run, #742), on the file as it stands in Bambuddy.
    A sliced file is queued as it is, with no slice (#2671), and never refused: one
    sliced for another printer model or a nozzle the printer lacks queues with a
    warning that says so. A file deleted in Bambuddy is its 404, before the 202. The
    run's ``subject`` is ``library:<file id>``, and so is its ``output_id``, which older
    clients read. The file's filament slots go with the run, read from the file as
    ``/choices`` read them (#2550)."""

    async def prepare(source: SourceSpec) -> SourceSpec:
        filaments = await _run_filaments(store, paths, budget, file_id, downloads)
        return source.model_copy(update={"filaments": filaments})

    return await accept_run(
        runs,
        response,
        subject=PrintSubject.library(file_id),
        slug=library_slug(PrintSubject.library(file_id)),
        request=body,
        source=SourceSpec(kind="library", file_id=file_id),
        activity=activity,
        prepare=prepare,
    )


async def _run_filaments(
    store: SettingsStore,
    paths: DataPaths,
    budget: ReadBudget,
    file_id: int,
    downloads: DownloadCache | None,
) -> LocalFilaments | None:
    """The slots a run of library file ``file_id`` reads (#2550): from the cache the
    dialog filled, or a download of the file that fills it. Only for a file Bambuddy
    states a hash for: one with none is never cached, so reading it here would download
    it a second time beside the run's own, and the run asks Bambuddy instead, as it
    does for a file this cannot read. Never refuses the run: the run's own reads say
    why a file cannot print."""
    try:
        async with client_for(store.load()) as client:
            file = await client.library_file(file_id)
            if not file.file_hash:
                return None
            return await library_filaments(client, paths.cache, file, budget, downloads)
    except ApiError:
        return None


@router.get(
    "/{file_id}/progress",
    response_model=PrintProgress | None,
    summary="How the last print of this library file is going",
)
async def get_library_progress(
    file_id: FileIdPath,
    uploads: UploadsDep,
    links: PrintLinksDep,
    store: SettingsStoreDep,
    observer: PrintProgressDep,
    follows: FollowsDep,
    state: StateDep,
) -> PrintProgress | None:
    """As ``/print/outputs/{id}/progress`` (#1751): the file's newest run, its queue
    items read off Bambuddy until it is ``settled``. ``null`` when ScadBuddy never
    queued the file. A newest run that failed before it queued anything is that failure
    (``route: "run"``). Changes are published on ``print:library:<file id>``."""
    subject = PrintSubject.library(file_id)
    key = subject.run_subject
    failed = await failed_before_queueing(state, key)
    progress: PrintProgress | None = None
    async with client_for(store.load()) as client:
        if failed is not None:
            progress = from_failed_run(failed, bambuddy_url=client.config.web_url(QUEUE_PATH))
        else:
            progress = await library_progress(client, subject, links, uploads=uploads)
    observer.observe_subject(key, library_slug(subject), progress)
    # As an output's: a print someone is looking at that is still moving is followed.
    if progress is not None and not progress.settled:
        follows.ensure(key)
    return progress


@router.post(
    "/{file_id}/project",
    response_model=AttachResult,
    summary="File this library file's print under its project",
    responses=OPERATION_RESPONSES,
)
async def post_library_attach_project(
    file_id: FileIdPath,
    body: ProjectAttach,
    response: Response,
    ops: OperationsDep,
    idempotency_key: IdempotencyKey = None,
) -> AttachResult | JSONResponse:
    """As ``/print/outputs/{id}/project`` (#1751): the queue entries named, else those
    of the file's newest run, and whatever archives they have produced, filed under the
    project."""
    result = await run_operation(
        ops,
        response,
        kind=ops.kinds["attach_project"],
        subject=PrintSubject.library(file_id).run_subject,
        request={
            "library_file_id": file_id,
            "body": body.model_dump(mode="json", exclude_unset=True),
        },
        idempotency_key=idempotency_key,
    )
    return operation_answer(result, AttachResult)


@router.post(
    "/{file_id}/preview-slice",
    response_model=PreviewStarted,
    summary="Slice the dialog's choices in the background, without queueing",
    responses=OPERATION_RESPONSES,
)
async def post_library_preview_slice(
    file_id: FileIdPath,
    body: PrintRunRequest,
    response: Response,
    ops: OperationsDep,
    idempotency_key: IdempotencyKey = None,
) -> PreviewStarted | JSONResponse:
    """As ``/print/outputs/{id}/preview-slice``, for a file in Bambuddy's library (#2169)."""
    result = await run_operation(
        ops,
        response,
        kind=ops.kinds["preview_slice"],
        subject=library_slug(PrintSubject.library(file_id)),
        request={"library_file_id": file_id, "request": body.model_dump(mode="json")},
        idempotency_key=idempotency_key,
    )
    return operation_answer(result, PreviewStarted)


@router.get(
    "/{file_id}/preview-slices/{job_id}",
    response_model=SlicePreview,
    summary="How a background slice stands, and what it came to",
)
async def get_library_preview_slice(
    file_id: FileIdPath, job_id: int, store: SettingsStoreDep, uploads: UploadsDep
) -> SlicePreview:
    """As ``/print/outputs/{id}/preview-slices/{job}``, for a file in Bambuddy's library."""
    async with client_for(store.load()) as client:
        return await read_preview(
            client, uploads, job_id, PrintSubject.library(file_id).run_subject
        )


@router.get(
    "/{file_id}/preview-slices/{job_id}/toolpath",
    response_class=Response,
    responses={
        200: {
            "content": {"application/octet-stream": {}},
            "description": "The toolpath, as `scadbuddy.bambuddy.toolpath` encodes it",
        }
    },
    summary="The toolpath of a finished background slice",
)
async def get_library_preview_toolpath(
    file_id: FileIdPath, job_id: int, store: SettingsStoreDep, uploads: UploadsDep
) -> Response:
    """As ``/print/outputs/{id}/preview-slices/{job}/toolpath``, for a library file's."""
    async with client_for(store.load()) as client:
        body = await read_toolpath(
            client, uploads, job_id, PrintSubject.library(file_id).run_subject
        )
    return Response(body, media_type="application/octet-stream", headers=TOOLPATH_HEADERS)


@router.post(
    "/{file_id}/check",
    response_model=PrintCheck,
    summary="What the run would refuse for the dialog's choices, before Print",
)
async def post_library_check(
    file_id: FileIdPath,
    body: PrintRunRequest,
    store: SettingsStoreDep,
    uploads: UploadsDep,
    rack: RackUsageDep,
    paths: PathsDep,
    budget: ReadBudgetDep,
    downloads: DownloadsDep,
) -> PrintCheck:
    """As ``/print/outputs/{id}/check``, on the file as it stands in Bambuddy (#755, #760);
    its slots read from the file itself, as ``/choices`` reads them (#2550)."""
    settings = store.load()
    async with client_for(settings) as client:
        return await check_for_library(
            client,
            uploads,
            settings,
            file_id,
            body,
            rack=rack,
            filaments=partial(
                library_filaments, client, paths.cache, budget=budget, downloads=downloads
            ),
        )
