"""A copy between Bambuddy's library and ScadBuddy's own (#5355), either way: the
walk that plans it, the batch that moves the bytes, and the ``library`` queue
activities `LibraryCopy` (``workflows/library_copy.py``) runs them as.

The rules: a file whose SHA-256 the target already holds is skipped, so a rerun or a
resumed run is a no-op for what already landed; the folder path under each selected
folder is kept, and missing folders are made at the target; nothing at the target is
ever deleted or overwritten (a name already there keeps both, as each library's own
upload does); the source is only read. Each file's outcome is a row
(`LibraryCopyStore`), read by ``GET /print/library-copies/{id}``.

Bambuddy's ``file_hash`` (``GET /api/v1/library/files/{id}``, `LibraryFile.file_hash`)
and the native rows' ``sha256`` are both SHA-256 of the whole file, so the two compare
directly. The bytes go Bambuddy to ScadBuddy through `NativeLibrary.upload` (the same
path as a library upload, so `StoreFullError` is the same 507) and ScadBuddy to Bambuddy
through `send.upload_raw`, the one upload path a native file's bytes take into Bambuddy.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import tempfile
from collections.abc import Awaitable, Callable
from contextlib import aclosing
from typing import Any, BinaryIO, Protocol

from fastapi import status
from temporalio import activity
from temporalio.exceptions import ApplicationError

from scadbuddy.bambuddy.client import THREE_MF_MEDIA_TYPE, BambuddyClient, client_for
from scadbuddy.bambuddy.errors import is_transient
from scadbuddy.bambuddy.models import Folder, FolderCreate, LibraryFile, LibraryListRow
from scadbuddy.bambuddy.send import Sink, upload_raw
from scadbuddy.bambuddy.uploads import BambuddyUploadStore
from scadbuddy.core.problems import ApiError
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.print_library.component import PrintLibrary
from scadbuddy.print_library.copy_models import (
    MAX_COPY_FILES,
    LibraryCopyBatch,
    LibraryCopyBatchInput,
    LibraryCopyFinishInput,
    LibraryCopyInput,
    LibraryCopyPlan,
    LibraryCopyRequest,
)
from scadbuddy.print_library.copy_store import LibraryCopyStore, PlannedFile
from scadbuddy.print_library.ids import is_native_id
from scadbuddy.print_library.native import NativeLibrary
from scadbuddy.print_library.reads import LibraryListingSource
from scadbuddy.store.content_models import StoreFullError
from scadbuddy.store.index import Pool
from scadbuddy.store.migrate import beat_while

logger = logging.getLogger(__name__)

LIBRARY_COPY_PLAN = "library_copy_plan"
LIBRARY_COPY_BATCH = "library_copy_batch"
LIBRARY_COPY_FINISH = "library_copy_finish"

NOT_NATIVE = (
    "copying between the libraries needs ScadBuddy's own library: set library_backend to"
    " native first"
)
GONE_FROM_SOURCE = "it is no longer in the source library"
#: The batch attempt on which a file's 5xx is its own failure rather than a retry.
FILE_ATTEMPTS = 3
#: `StoreFullError`'s reason, the 507 a native upload answers (plan Ruling 11).
STORE_FULL = "the blob store has no room for it (507)"


def _flat(tree: list[Folder]) -> dict[int, Folder]:
    return {folder.id: folder for top in tree for folder in top.walk()}


def _wrong_side(request: LibraryCopyRequest) -> str | None:
    """Why the request's ids cannot be the source's or the target's (one id space per
    library, plan 2026-10-17 Ruling 1), or ``None``."""
    native_source = request.direction == "sb_to_bb"
    for file_id in request.file_ids:
        if is_native_id(file_id) != native_source:
            return f"library file {file_id} is not in the source library"
    for folder_id in request.folder_ids:
        if is_native_id(folder_id) != native_source:
            return f"library folder {folder_id} is not in the source library"
    if request.target_folder_id is not None and is_native_id(request.target_folder_id) == (
        native_source
    ):
        return f"library folder {request.target_folder_id} is not in the target library"
    return None


def check_request(request: LibraryCopyRequest) -> None:
    """The route's refusal of ids on the wrong side, before anything starts."""
    wrong = _wrong_side(request)
    if wrong is not None:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, wrong)


class CopySource(LibraryListingSource, Protocol):
    """What the plan reads of the source library: `BambuddyClient` or `NativeLibrary`."""

    async def library_file(self, file_id: int) -> LibraryFile: ...


TARGET_FOLDER_GONE = "library folder {id} is not in the target library"


async def check_target_folder(request: LibraryCopyRequest, target: LibraryListingSource) -> None:
    """The 404 for a ``target_folder_id`` the target library does not have: checked at
    the start and again when the plan runs, so a folder deleted in between fails the job
    with this reason rather than each file with a wrong one."""
    if request.target_folder_id is None:
        return
    if request.target_folder_id not in _flat(await target.folders()):
        raise ApiError(
            status.HTTP_404_NOT_FOUND,
            TARGET_FOLDER_GONE.format(id=request.target_folder_id),
            folder_id=request.target_folder_id,
        )


async def plan_copy(
    request: LibraryCopyRequest, source: CopySource, target: LibraryListingSource
) -> list[PlannedFile]:
    """Every source file the request names: those of each selected folder's subtree
    with their path under it (the folder's own name first), then the files named on
    their own at the root. A folder the source does not have, or a target folder the
    target does not have, is the 404; a file the source does not have is planned
    ``failed``, so the rest still copy. More than `MAX_COPY_FILES` is the 413."""
    check_request(request)
    await check_target_folder(request, target)
    planned: list[PlannedFile] = []
    if request.folder_ids:
        folders = _flat(await source.folders())
        missing = [f for f in request.folder_ids if f not in folders]
        if missing:
            raise ApiError(
                status.HTTP_404_NOT_FOUND,
                f"library folder {missing[0]} is not in the source library",
                folder_id=missing[0],
            )
        rows = await source.library_listing(folder_id=None, every_folder=True)
        by_folder: dict[int, list[LibraryListRow]] = {}
        for row in rows:
            if row.folder_id is not None:
                by_folder.setdefault(row.folder_id, []).append(row)
        for root_id in request.folder_ids:
            for path, folder in _walk(folders[root_id], ""):
                for row in sorted(by_folder.get(folder.id, []), key=lambda r: (r.filename, r.id)):
                    planned.append(
                        PlannedFile(
                            source_id=row.id, path=path, name=row.filename, size=row.file_size
                        )
                    )
    for file_id in request.file_ids:
        try:
            found = await source.library_file(file_id)
        except ApiError as error:
            if error.status != status.HTTP_404_NOT_FOUND:
                raise
            planned.append(
                PlannedFile(source_id=file_id, path="", name=str(file_id), reason=GONE_FROM_SOURCE)
            )
            continue
        planned.append(
            PlannedFile(source_id=found.id, path="", name=found.filename, size=found.file_size)
        )
    if len(planned) > MAX_COPY_FILES:
        raise ApiError(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"the selection holds {len(planned)} files; a copy takes at most {MAX_COPY_FILES}."
            " Copy it a folder at a time",
        )
    return planned


def _walk(folder: Folder, above: str) -> list[tuple[str, Folder]]:
    path = f"{above}/{folder.name}" if above else folder.name
    found = [(path, folder)]
    for child in folder.children:
        found.extend(_walk(child, path))
    return found


class _Target:
    """Where the copies go: the folders under the root made as needed, the hash check,
    and the put. The tree is read once per batch and the folders made are remembered."""

    def __init__(self, root_id: int | None) -> None:
        self.root_id = root_id
        self._folders: dict[tuple[int | None, str], int] = {}
        self._tree_read = False

    async def _tree(self) -> list[Folder]:
        raise NotImplementedError

    async def _make_folder(self, name: str, parent_id: int | None) -> int:
        raise NotImplementedError

    async def folder_for(self, path: str) -> int | None:
        """The folder ``path`` (names joined by ``/``, ``""`` the root) names under the
        root, the oldest of its name at each level, made where missing."""
        if not self._tree_read:
            flat = _flat(await self._tree())
            for folder in sorted(flat.values(), key=lambda f: f.id):
                self._folders.setdefault((folder.parent_id, folder.name), folder.id)
            self._tree_read = True
        parent = self.root_id
        for name in [part for part in path.split("/") if part]:
            key = (parent, name)
            if key not in self._folders:
                self._folders[key] = await self._make_folder(name, parent)
            parent = self._folders[key]
        return parent

    async def has(self, digest: str, size: int | None) -> int | None:
        """The id of a live file whose bytes hash to ``digest``, or ``None``. Read, not
        locked: two copies into one target at once can both miss here and both put the
        same bytes, so the skip holds between copies, not within a race of them."""
        raise NotImplementedError


class _NativeTarget(_Target):
    def __init__(self, native: NativeLibrary, root_id: int | None) -> None:
        super().__init__(root_id)
        self.native = native

    async def _tree(self) -> list[Folder]:
        return await self.native.folders()

    async def _make_folder(self, name: str, parent_id: int | None) -> int:
        return (await self.native.create_folder(name, parent_id)).id

    async def has(self, digest: str, size: int | None) -> int | None:
        found = await asyncio.to_thread(self.native.rows.files_by_sha256, [digest])
        return found[0].id if found else None

    async def put(self, name: str, spool: BinaryIO, folder_id: int | None) -> int:
        return (await self.native.upload(name, spool, folder_id)).id


class _BambuddyTarget(_Target):
    """Bambuddy lists no hash: the whole library's rows are read once per batch, and
    only a row of the source file's size has its ``file_hash`` read (`same_bytes`'s
    narrowing, `send.py`), each at most once."""

    def __init__(self, client: BambuddyClient, uploads: BambuddyUploadStore, root_id: int | None):
        super().__init__(root_id)
        self.client = client
        self.uploads = uploads
        self._rows: dict[int, LibraryListRow] | None = None
        self._hashes: dict[int, str | None] = {}

    async def _tree(self) -> list[Folder]:
        return await self.client.folders()

    async def _make_folder(self, name: str, parent_id: int | None) -> int:
        return (await self.client.create_folder(FolderCreate(name=name, parent_id=parent_id))).id

    async def _listed(self) -> dict[int, LibraryListRow]:
        if self._rows is None:
            root, every = await asyncio.gather(
                self.client.library_listing(folder_id=None),
                self.client.library_listing(folder_id=None, every_folder=True),
            )
            self._rows = {row.id: row for row in [*root, *every]}
        return self._rows

    async def has(self, digest: str, size: int | None) -> int | None:
        rows = await self._listed()
        for row in sorted(rows.values(), key=lambda r: r.id):
            if size is not None and row.file_size is not None and row.file_size != size:
                continue
            if row.id not in self._hashes:
                try:
                    self._hashes[row.id] = (await self.client.library_file(row.id)).file_hash
                except ApiError as error:
                    if error.status != status.HTTP_404_NOT_FOUND:
                        raise  # unread, not absent: the batch is retried
                    self._hashes[row.id] = None
            if self._hashes[row.id] == digest:
                return row.id
        return None

    async def put(
        self,
        name: str,
        media_type: str,
        folder_id: int | None,
        write: Callable[[Sink], Awaitable[None]],
        *,
        digest: str,
        size: int | None,
    ) -> int:
        uploaded = await upload_raw(
            self.client, self.uploads, folder_id, filename=name, media_type=media_type, write=write
        )
        rows = await self._listed()
        rows[uploaded.id] = LibraryListRow(
            id=uploaded.id, filename=uploaded.filename, file_type="", file_size=size
        )
        self._hashes[uploaded.id] = digest
        return uploaded.id


def _reason(error: BaseException) -> str:
    if isinstance(error, ApiError):
        return f"HTTP {error.status}: {error.detail}"
    return f"{type(error).__name__}: {error}"


async def _spool(chunks: Any, spool: BinaryIO) -> str:
    """``chunks`` written to ``spool``, their SHA-256 answered."""
    digest = hashlib.sha256()
    async with aclosing(chunks) as stream:
        async for chunk in stream:
            digest.update(chunk)
            await asyncio.to_thread(spool.write, chunk)
    await asyncio.to_thread(spool.seek, 0)
    return digest.hexdigest()


async def _copy_bb_to_sb(
    client: BambuddyClient, target: _NativeTarget, row: PlannedFile
) -> tuple[str, int | None, int | None, str | None]:
    """One Bambuddy file into the native library: (status, target id, target folder,
    reason). Only the source's own 404s (the read, the download) mean the file is gone;
    a 404 from the target (a folder deleted since the plan) keeps its own reason."""
    try:
        source = await client.library_file(row.source_id)
    except ApiError as error:
        if error.status == status.HTTP_404_NOT_FOUND:
            return "failed", None, None, GONE_FROM_SOURCE
        raise
    folder_id = await target.folder_for(row.path)
    if source.file_hash is not None:
        held = await target.has(source.file_hash, source.file_size)
        if held is not None:
            return "skipped_same_hash", held, folder_id, None
    with await asyncio.to_thread(tempfile.TemporaryFile, "w+b") as spool:
        try:
            digest = await _spool(client.download_library_file(source.id), spool)
        except ApiError as error:
            if error.status == status.HTTP_404_NOT_FOUND:
                return "failed", None, folder_id, GONE_FROM_SOURCE
            raise
        if source.file_hash is not None and digest != source.file_hash:
            return "failed", None, folder_id, f"sha256 mismatch: Bambuddy served {digest}"
        if source.file_hash is None:
            held = await target.has(digest, source.file_size)
            if held is not None:
                return "skipped_same_hash", held, folder_id, None
        try:
            copied = await target.put(source.filename, spool, folder_id)
        except StoreFullError as error:
            return "failed", None, folder_id, f"{STORE_FULL}: {error}"
    return "copied", copied, folder_id, None


def _media_type(name: str, native_type: str) -> str:
    lower = name.lower()
    if lower.endswith(".3mf"):
        return THREE_MF_MEDIA_TYPE
    if lower.endswith(".stl"):
        return "model/stl"
    return native_type or "application/octet-stream"


async def _copy_sb_to_bb(
    native: NativeLibrary, target: _BambuddyTarget, row: PlannedFile
) -> tuple[str, int | None, int | None, str | None]:
    """One native file into Bambuddy's library."""
    found = await asyncio.to_thread(native.rows.file, row.source_id)
    if found is None:
        return "failed", None, None, GONE_FROM_SOURCE
    folder_id = await target.folder_for(row.path)
    held = await target.has(found.sha256, found.size)
    if held is not None:
        return "skipped_same_hash", held, folder_id, None

    async def write(out: Sink) -> None:
        async with aclosing(native.download_library_file(found.id)) as chunks:
            async for chunk in chunks:
                await asyncio.to_thread(out.write, chunk)

    copied = await target.put(
        found.name,
        _media_type(found.name, found.media_type),
        folder_id,
        write,
        digest=found.sha256,
        size=found.size,
    )
    return "copied", copied, folder_id, None


async def copy_batch(
    store: LibraryCopyStore,
    input: LibraryCopyBatchInput,
    *,
    client: BambuddyClient,
    native: NativeLibrary,
    uploads: BambuddyUploadStore,
    heartbeat: Callable[[str], None],
    attempt: int = 1,
) -> LibraryCopyBatch:
    """Up to ``input.limit`` of the job's pending files, each recorded as it ends. One
    file's failure (gone from the source, refused by the target, the store full) is its
    row's ``reason`` and never stops the others. A library that does not answer
    (`is_transient`) always raises, and the activity is retried until it does. A 5xx
    the library answered for the file raises too, since it may still be the library;
    on the `FILE_ATTEMPTS`th ``attempt`` it is the file's own failure, recorded as its
    reason, and the batch ends there, so one file a library always 500s on (a 3MF its
    upload refuses, bytes gone from its disk) cannot hold the job on that file forever,
    while the rows after it wait for the next batch, which starts at attempt 1 again.
    ``attempt`` is the activity's, so only that first row can spend it."""
    request = input.request
    rows = await asyncio.to_thread(store.pending, input.copy_id, limit=input.limit)
    target: _NativeTarget | _BambuddyTarget = (
        _NativeTarget(native, request.target_folder_id)
        if request.direction == "bb_to_sb"
        else _BambuddyTarget(client, uploads, request.target_folder_id)
    )
    copied = skipped = failed = 0
    ended_early = False
    for row in rows:
        beating = asyncio.create_task(beat_while(f"{row.path}/{row.name}", heartbeat))
        try:
            if isinstance(target, _NativeTarget):
                outcome = await _copy_bb_to_sb(client, target, row)
            else:
                outcome = await _copy_sb_to_bb(native, target, row)
        except ApiError as error:
            # A 404 here is the target's (the source's own are answered above): a
            # folder deleted since the plan, say. The row says which.
            if error.status >= 500 and error.status != status.HTTP_507_INSUFFICIENT_STORAGE:
                # A library not answering at all (a restart, a gateway's 502/503/504) is
                # never this file's failure: the batch is retried until it answers.
                if is_transient(error) or attempt < FILE_ATTEMPTS:
                    raise
                ended_early = True  # this file spent the batch's tries; the rest wait
            outcome = ("failed", None, None, _reason(error))
        except (StoreFullError, ValueError, OSError) as error:
            logger.warning("a library copy failed on one file", exc_info=True)
            outcome = ("failed", None, None, _reason(error))
        finally:
            beating.cancel()
            await asyncio.gather(beating, return_exceptions=True)
        result, target_id, folder_id, reason = outcome
        await asyncio.to_thread(
            store.record,
            input.copy_id,
            row.source_id,
            result,  # type: ignore[arg-type]
            reason=reason,
            target_id=target_id,
            target_folder_id=folder_id,
        )
        if result == "copied":
            copied += 1
        elif result == "skipped_same_hash":
            skipped += 1
        else:
            failed += 1
        if ended_early:
            break
    counts = await asyncio.to_thread(store.counts, input.copy_id)
    return LibraryCopyBatch(copied=copied, skipped=skipped, failed=failed, remaining=counts.pending)


def library_copy_activities(
    pool_of: Callable[[], Pool],
    settings_of: Callable[[], StoredSettings],
    library: PrintLibrary,
    uploads_of: Callable[[], BambuddyUploadStore],
) -> list[Callable[..., Any]]:
    """The three activities, in the API process (its ``library`` worker, which holds the
    native library). Each reads the settings when it runs. A plan that cannot run (no
    native library, a folder the source lacks, too many files) records the failure on
    the job's row and ends the workflow with a non-retryable error."""

    async def _native() -> NativeLibrary:
        native = await library.serving_native()
        if native is None:
            raise ApiError(status.HTTP_409_CONFLICT, NOT_NATIVE)
        return native

    @activity.defn(name=LIBRARY_COPY_PLAN)
    async def library_copy_plan(input: LibraryCopyInput) -> LibraryCopyPlan:
        store = LibraryCopyStore(pool_of())
        try:
            native = await _native()
            settings = await asyncio.to_thread(settings_of)
            async with client_for(settings) as client:
                bb_to_sb = input.request.direction == "bb_to_sb"
                planned = await plan_copy(
                    input.request,
                    client if bb_to_sb else native,
                    native if bb_to_sb else client,
                )
        except ApiError as error:
            if error.status >= 500:
                raise  # a library down at plan time: the plan is retried, not failed
            await asyncio.to_thread(store.finish, input.copy_id, "failed", error.detail)
            raise ApplicationError(error.detail, non_retryable=True) from error
        total = await asyncio.to_thread(store.plan, input.copy_id, planned)
        return LibraryCopyPlan(total=total)

    @activity.defn(name=LIBRARY_COPY_BATCH)
    async def library_copy_batch(input: LibraryCopyBatchInput) -> LibraryCopyBatch:
        store = LibraryCopyStore(pool_of())
        try:
            native = await _native()
            settings = await asyncio.to_thread(settings_of)
        except ApiError as error:
            await asyncio.to_thread(store.finish, input.copy_id, "failed", error.detail)
            raise ApplicationError(error.detail, non_retryable=True) from error
        async with client_for(settings) as client:
            return await copy_batch(
                store,
                input,
                client=client,
                native=native,
                uploads=uploads_of(),
                heartbeat=activity.heartbeat,
                attempt=activity.info().attempt,
            )

    @activity.defn(name=LIBRARY_COPY_FINISH)
    async def library_copy_finish(input: LibraryCopyFinishInput) -> None:
        store = LibraryCopyStore(pool_of())
        await asyncio.to_thread(
            store.finish, input.copy_id, "failed" if input.error else "done", input.error
        )

    return [library_copy_plan, library_copy_batch, library_copy_finish]
