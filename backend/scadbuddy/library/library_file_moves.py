"""Copying, moving and renaming files in Bambuddy's library (#2670).

A move and a rename go through Bambuddy's API (``POST /library/files/move``,
``PUT /library/files/{id}`` with ``filename``) and keep the file's id. Bambuddy has no
copy, so a copy is the file downloaded and uploaded into the target folder, under a new id.

What ScadBuddy keeps about a file, and what each one does to it:

* Its upload rows (#316, ``output_bambuddy_uploads``) name the folder a copy sits in, and
  that folder is what tells an inbox copy (replaced and deleted by ScadBuddy) from a
  project's (never touched). A move rewrites it (:meth:`BambuddyUploadStore.relocate`),
  so a copy moved out of the inbox is not later deleted as an inbox copy. A file moved
  *into* the inbox is not made an inbox copy: its rows are dropped
  (:meth:`BambuddyUploadStore.forget_files`), so it is an ordinary file ScadBuddy never
  deletes, and the next send uploads a copy of its own. A copy of an output's copy is
  recorded as one more copy of that output, in its new folder, unless that folder is the
  inbox, for the same reason (review #2681).
* Its slices (``output_bambuddy_slices``), print choices and progress (``library:<id>``),
  the dialog's preview slices and Bambuddy's thumbnails are all keyed by the id, which a
  move and a rename keep. A copy starts with none of them, as any new file does.
* Its preview, analysis and filament slots (#2550) are cached by the file's SHA-256
  (``bambuddy/library_view.py``), which none of the three changes: a copy shares them.

#316's rule that ScadBuddy never moves a project's file on its own still holds: these
run only when a person (or their agent) asks. A move out of a project's folder takes the
file out of that project, since Bambuddy lists a project's files as those in folders
linked to it; each such file names the project it left (``left_project``).

All three run once: a repeated copy would upload a second copy, and a repeated move or
rename would answer as though it did the work again. So a copy that fails partway, or
nears its time limit, still answers: the copies made, and every other file ``skipped``.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import posixpath
from contextlib import aclosing
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from fastapi import status
from pydantic import BaseModel, Field, model_validator

from scadbuddy.bambuddy.client import THREE_MF_MEDIA_TYPE, BambuddyClient, client_for
from scadbuddy.bambuddy.commits import upload_into
from scadbuddy.bambuddy.models import Folder, LibraryFile
from scadbuddy.bambuddy.send import is_inbox
from scadbuddy.bambuddy.uploads import LibraryCopy
from scadbuddy.core.problems import ApiError
from scadbuddy.library.library_file_operations import (
    MAX_FILES,
    READ_CONCURRENCY,
    WHERE,
    SkippedLibraryFile,
)
from scadbuddy.operations.kinds import OperationKind, waiting_on_bambuddy

if TYPE_CHECKING:
    from scadbuddy.core.components import Core

logger = logging.getLogger(__name__)

#: The largest file a copy downloads; it is held in memory between download and upload.
MAX_COPY_BYTES = 256 * 1024 * 1024
#: A copy downloads and uploads every file, so it may take longer than a move. The
#: browser and the agent follow a 202 for 15 minutes (#1063).
COPY_TIMEOUT = timedelta(minutes=10)
#: When a copy stops starting files, so the run answers with what it made before
#: :data:`COPY_TIMEOUT` cuts it off: one more file's download and upload fit in the rest.
COPY_BUDGET = COPY_TIMEOUT - timedelta(minutes=3)
#: Where to look when a copy failed partway: Bambuddy may have kept the upload.
WHERE_COPIES = "the target folder in Bambuddy"
#: Bambuddy's ``FileUpdate.filename`` limit.
MAX_FILENAME = 255


class LibraryFolderTarget(BaseModel):
    """Where files go: ``folder_id``, or ``folder_path`` (folder names from the top,
    joined by ``/``), or neither for the library's top level."""

    folder_id: int | None = Field(default=None, ge=1)
    folder_path: str | None = Field(default=None, max_length=1024)

    @model_validator(mode="after")
    def _one_place(self) -> LibraryFolderTarget:
        if self.folder_id is not None and self.folder_path is not None:
            raise ValueError("name the folder once: folder_id or folder_path")
        return self


class LibraryTransferRequest(LibraryFolderTarget):
    """The library files to copy or move, by Bambuddy id, and where to."""

    file_ids: list[int] = Field(min_length=1, max_length=MAX_FILES)


class LibraryRenameRequest(BaseModel):
    file_id: int = Field(ge=1)
    #: The new name, its extension included and unchanged.
    filename: str = Field(min_length=1, max_length=MAX_FILENAME)


class ProjectRef(BaseModel):
    id: int
    name: str | None = None


class MovedLibraryFile(BaseModel):
    id: int
    filename: str
    #: The Bambuddy project whose folder the file left, so the project no longer lists
    #: it; ``None`` when it was in no project's folder, or stays in the same project.
    left_project: ProjectRef | None = None


class CopiedLibraryFile(BaseModel):
    #: The file copied.
    source_id: int
    #: The copy, a new library file.
    id: int
    filename: str


class LibraryMoveResult(BaseModel):
    """The files Bambuddy moved, and the ones it skipped (never counted as moved)."""

    folder_id: int | None
    moved: list[MovedLibraryFile]
    skipped: list[SkippedLibraryFile]


class LibraryCopyResult(BaseModel):
    folder_id: int | None
    copied: list[CopiedLibraryFile]
    skipped: list[SkippedLibraryFile]


class LibraryRenameResult(BaseModel):
    id: int
    filename: str
    previous: str


def _unique(ids: list[int]) -> list[int]:
    return list(dict.fromkeys(ids))


def _flat(tree: list[Folder]) -> list[Folder]:
    return [folder for top in tree for folder in top.walk()]


def _paths(folders: list[Folder]) -> dict[int, str]:
    by_id = {folder.id: folder for folder in folders}
    paths: dict[int, str] = {}

    def path(folder: Folder) -> str:
        if folder.id not in paths:
            parent = by_id.get(folder.parent_id) if folder.parent_id is not None else None
            paths[folder.id] = folder.name if parent is None else f"{path(parent)}/{folder.name}"
        return paths[folder.id]

    for folder in folders:
        path(folder)
    return paths


def resolve_folder(target: LibraryFolderTarget, folders: list[Folder]) -> int | None:
    """The folder id ``target`` names, ``None`` for the top level. A path no folder has is
    the 404, and one two folders share the 409 naming both."""
    if target.folder_id is not None:
        if not any(folder.id == target.folder_id for folder in folders):
            raise ApiError(
                status.HTTP_404_NOT_FOUND,
                f"library folder {target.folder_id} is not in Bambuddy's library",
                folder_id=target.folder_id,
            )
        return target.folder_id
    if target.folder_path is None:
        return None
    wanted = "/".join(part.strip() for part in target.folder_path.split("/") if part.strip())
    if not wanted:
        return None
    found = sorted(fid for fid, path in _paths(folders).items() if path == wanted)
    if not found:
        raise ApiError(
            status.HTTP_404_NOT_FOUND,
            f"no library folder is at {wanted!r}",
            folder_path=wanted,
        )
    if len(found) > 1:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            f"{len(found)} library folders are at {wanted!r}; name one by folder_id",
            folder_ids=found,
        )
    return found[0]


async def _read_files(client: BambuddyClient, ids: list[int]) -> list[LibraryFile]:
    """Each file as Bambuddy has it; one it no longer has is the 404 naming it."""
    gate = asyncio.Semaphore(READ_CONCURRENCY)

    async def one(file_id: int) -> LibraryFile:
        async with gate:
            try:
                return await client.library_file(file_id)
            except ApiError as error:
                if error.status == status.HTTP_404_NOT_FOUND:
                    raise ApiError(
                        status.HTTP_404_NOT_FOUND,
                        f"library file {file_id} is no longer in Bambuddy's library",
                        file_id=file_id,
                    ) from None
                raise

    return list(await asyncio.gather(*(one(file_id) for file_id in ids)))


def _project_of(folder_id: int | None, folders: dict[int, Folder]) -> ProjectRef | None:
    folder = folders.get(folder_id) if folder_id is not None else None
    if folder is None or folder.project_id is None:
        return None
    return ProjectRef(id=folder.project_id, name=folder.project_name)


def _media_type(filename: str) -> str:
    if filename.lower().endswith(".3mf"):
        return THREE_MF_MEDIA_TYPE
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


async def _download(client: BambuddyClient, file_id: int) -> bytes | None:
    """The file's bytes; ``None`` past :data:`MAX_COPY_BYTES`, nothing kept."""
    data = bytearray()
    async with aclosing(client.download_library_file(file_id)) as chunks:
        async for chunk in chunks:
            data += chunk
            if len(data) > MAX_COPY_BYTES:
                return None
    return bytes(data)


def _extension(filename: str, file_type: str | None) -> str:
    """The part of ``filename`` a rename keeps: Bambuddy's ``file_type`` when the name
    ends with it (``gcode.3mf`` for a sliced file), else the last suffix."""
    if file_type and filename.lower().endswith(f".{file_type.lower()}"):
        return filename[-(len(file_type) + 1) :]
    return posixpath.splitext(filename)[1]


async def _not_moved_to(
    client: BambuddyClient, ids: list[int], folder_id: int | None
) -> dict[int, str]:
    """Why each of ``ids`` is not in ``folder_id`` after a move, read back from Bambuddy;
    the ones that are there are left out."""

    async def one(file_id: int) -> tuple[int, str | None]:
        try:
            found = await client.library_file(file_id)
        except ApiError as error:
            if error.status != status.HTTP_404_NOT_FOUND:
                raise
            return file_id, "it is no longer in Bambuddy's library"
        if found.folder_id != folder_id:
            return file_id, "Bambuddy did not move it, and gave no reason"
        return file_id, None

    read = await asyncio.gather(*(one(file_id) for file_id in ids))
    return {file_id: reason for file_id, reason in read if reason is not None}


def library_file_move_kinds(state: Core) -> list[OperationKind]:
    """Copy, move and rename, bound to this process's settings store and upload rows
    (read at each call, so a test's replaced store is the one used)."""

    async def _target(client: BambuddyClient, request: LibraryFolderTarget) -> dict[str, Any]:
        folders = _flat(await client.folders())
        return {"folder_id": resolve_folder(request, folders), "folders": folders}

    async def move_check(request: dict[str, Any]) -> dict[str, Any]:
        body = LibraryTransferRequest.model_validate(request)
        settings = await asyncio.to_thread(state.settings_store.load)
        async with waiting_on_bambuddy(), client_for(settings) as client:
            target = await _target(client, body)
            files = await _read_files(client, _unique(body.file_ids))
        folders = {folder.id: folder for folder in target["folders"]}
        going_to = _project_of(target["folder_id"], folders)
        moving = []
        for file in files:
            left = _project_of(file.folder_id, folders)
            if left is not None and going_to is not None and left.id == going_to.id:
                left = None
            moving.append(
                MovedLibraryFile(id=file.id, filename=file.filename, left_project=left).model_dump(
                    mode="json"
                )
            )
        return {"folder_id": target["folder_id"], "files": moving}

    async def move_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        folder_id: int | None = checked["folder_id"]
        files = [MovedLibraryFile.model_validate(entry) for entry in checked["files"]]
        settings = await asyncio.to_thread(state.settings_store.load)
        result = LibraryMoveResult(folder_id=folder_id, moved=[], skipped=[])
        async with client_for(settings) as client:
            answer = await client.move_library_files([file.id for file in files], folder_id)
            reasons = {skip.file_id: skip.reason or skip.code for skip in answer.skipped_reasons}
            unreported = [file for file in files if file.id not in reasons]
            gone: dict[int, str] = {}
            if answer.moved < len(unreported):
                # Bambuddy passes over an id it no longer has without a word: what it did
                # not count as moved is read back, never assumed to be where it was sent.
                gone = await _not_moved_to(client, [file.id for file in unreported], folder_id)
        for file in files:
            reason = (
                f"Bambuddy did not move it: {reasons[file.id] or 'no reason given'}"
                if file.id in reasons
                else gone.get(file.id)
            )
            if reason is not None:
                result.skipped.append(
                    SkippedLibraryFile(id=file.id, filename=file.filename, reason=reason)
                )
            else:
                result.moved.append(file)
        moved = [file.id for file in result.moved]
        if is_inbox(folder_id, settings):
            # A recorded copy in the inbox is one ScadBuddy replaces and deletes; a file a
            # person put there is theirs, so it stops being recorded instead.
            await state.uploads.forget_files(moved)
        else:
            await state.uploads.relocate(moved, folder_id)
        return result.model_dump(mode="json")

    async def copy_check(request: dict[str, Any]) -> dict[str, Any]:
        body = LibraryTransferRequest.model_validate(request)
        settings = await asyncio.to_thread(state.settings_store.load)
        async with waiting_on_bambuddy(), client_for(settings) as client:
            target = await _target(client, body)
            files = await _read_files(client, _unique(body.file_ids))
        return {
            "folder_id": target["folder_id"],
            "files": [{"id": file.id, "filename": file.filename} for file in files],
        }

    async def copy_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        """Each file in turn. A failure stops the run but not the answer: the copies made
        so far are reported (and recorded), the file that failed and those after it are
        ``skipped``, so the one run never fails having made copies nobody is told of."""
        folder_id: int | None = checked["folder_id"]
        settings = await asyncio.to_thread(state.settings_store.load)
        # A copy into the inbox stays an ordinary file: a recorded inbox copy is one the
        # next send replaces and an output's delete removes (review #2681).
        record = not is_inbox(folder_id, settings)
        result = LibraryCopyResult(folder_id=folder_id, copied=[], skipped=[])
        loop = asyncio.get_running_loop()
        deadline = loop.time() + COPY_BUDGET.total_seconds()
        stopped: str | None = None
        async with client_for(settings) as client:
            for entry in checked["files"]:
                if stopped is None and loop.time() >= deadline:
                    stopped = "not tried: the copy ran out of time. Copy it again"
                if stopped is not None:
                    result.skipped.append(
                        SkippedLibraryFile(
                            id=entry["id"], filename=entry["filename"], reason=stopped
                        )
                    )
                    continue
                try:
                    copied = await _copy_one(client, entry, folder_id, record)
                except ApiError as error:
                    if error.status == status.HTTP_404_NOT_FOUND:
                        reason = "it is no longer in Bambuddy's library"
                    else:
                        reason = (
                            f"Bambuddy failed while copying it ({error.detail}). Check "
                            f"{WHERE_COPIES} before copying it again"
                        )
                        stopped = f"not tried after {entry['filename']} failed"
                    result.skipped.append(
                        SkippedLibraryFile(
                            id=entry["id"], filename=entry["filename"], reason=reason
                        )
                    )
                    continue
                if isinstance(copied, SkippedLibraryFile):
                    result.skipped.append(copied)
                else:
                    result.copied.append(copied)
        return result.model_dump(mode="json")

    async def _copy_one(
        client: BambuddyClient, entry: dict[str, Any], folder_id: int | None, record: bool
    ) -> CopiedLibraryFile | SkippedLibraryFile:
        source = await client.library_file(entry["id"])
        content = await _download(client, source.id)
        if content is None:
            return SkippedLibraryFile(
                id=source.id,
                filename=source.filename,
                reason=f"it is larger than the {MAX_COPY_BYTES // 1024**2} MiB ScadBuddy copies",
            )
        copy = await upload_into(
            client,
            source.filename,
            content,
            folder_id=folder_id,
            media_type=_media_type(source.filename),
        )
        if record:
            for output_id, target_key in await state.uploads.recorded_as(source.id):
                await state.uploads.record(
                    output_id, LibraryCopy(id=copy.id, folder_id=folder_id, target_key=target_key)
                )
        if source.notes:
            # The notes carry the "Edit in ScadBuddy" link of an output's copy. The copy is
            # made by now, so a failure here is logged, not reported as no copy.
            try:
                await client.annotate_library_file(copy.id, source.notes)
            except ApiError as error:
                logger.warning(
                    "could not copy a library file's notes to its copy",
                    extra={"library_file_id": copy.id, "status": error.status},
                )
        return CopiedLibraryFile(source_id=source.id, id=copy.id, filename=copy.filename)

    async def rename_check(request: dict[str, Any]) -> dict[str, Any]:
        body = LibraryRenameRequest.model_validate(request)
        name = body.filename.strip()
        if not name or "/" in name or "\\" in name:
            raise ApiError(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"{body.filename!r} is not a file name: it is empty or names a folder",
            )
        settings = await asyncio.to_thread(state.settings_store.load)
        async with waiting_on_bambuddy(), client_for(settings) as client:
            [file] = await _read_files(client, [body.file_id])
        keep = _extension(file.filename, file.file_type)
        if keep and not name.lower().endswith(keep.lower()):
            raise ApiError(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"a rename keeps the file's type: {name!r} has to end in {keep!r}",
                extension=keep,
            )
        return {"file_id": file.id, "filename": name, "previous": file.filename}

    async def rename_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        async with client_for(await asyncio.to_thread(state.settings_store.load)) as client:
            renamed = await client.rename_library_file(checked["file_id"], checked["filename"])
        return LibraryRenameResult(
            id=renamed.id, filename=renamed.filename, previous=checked["previous"]
        ).model_dump(mode="json")

    return [
        OperationKind("library_file_move", move_check, move_run, queue="library", where=WHERE),
        OperationKind(
            "library_file_copy",
            copy_check,
            copy_run,
            queue="library",
            where=WHERE,
            run_timeout=COPY_TIMEOUT,
        ),
        OperationKind(
            "library_file_rename", rename_check, rename_run, queue="library", where=WHERE
        ),
    ]
