"""Deleting files from Bambuddy's library, and putting them back (#2167).

Every delete goes through Bambuddy's API (``DELETE /library/files/{id}`` for one file,
``POST /library/bulk-delete`` for several), which moves a file to Bambuddy's trash. An
external file skips the trash and cannot be restored. Undo is
``POST /library/trash/{id}/restore``. ScadBuddy never touches Bambuddy's storage.

What a delete leaves alone, and why:

* An output's library copy may be deleted: the next send reads a recorded copy before
  reusing it and uploads a fresh one when the copy is gone (``send._still_there``).
  The record stays, so an Undo brings the copy back into use.
* The print choices and progress remembered under ``library:<id>`` stay too. Undo
  restores the same id, so they are still right once the file is back.
* A file that a print waiting in Bambuddy's queue names is refused. Bambuddy would
  fail that print at the printer once the file is in the trash. A print already
  running works from the printer's own copy, and a ScadBuddy print of a library file
  queues a laid-out copy (#1752), so neither of those blocks the delete.

Both kinds run once: a bulk delete repeated after a crash would count the files
already trashed as skipped, and a repeated restore would answer 404.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from fastapi import status
from pydantic import BaseModel, Field

from scadbuddy.bambuddy.client import BambuddyClient, client_for
from scadbuddy.bambuddy.stages import stage_of
from scadbuddy.core.problems import ApiError
from scadbuddy.operations.kinds import OperationKind, waiting_on_bambuddy

if TYPE_CHECKING:
    from scadbuddy.core.components import Core

#: The most files one request deletes or restores; the page's selection is far smaller.
MAX_FILES = 500
#: How many file reads a delete's check has in flight at once.
READ_CONCURRENCY = 8
#: Where to look when a run may have done its work before failing.
WHERE = "Bambuddy's library and its trash"


class LibraryFilesRequest(BaseModel):
    """The library files to delete or restore, by Bambuddy id."""

    file_ids: list[int] = Field(min_length=1, max_length=MAX_FILES)


class DeletedLibraryFile(BaseModel):
    id: int
    filename: str
    #: False for an external file: Bambuddy dropped it for good, so it cannot be restored.
    trashed: bool


class SkippedLibraryFile(BaseModel):
    id: int
    filename: str | None = None
    #: Why it was not deleted or restored, in a sentence.
    reason: str


class LibraryDeleteResult(BaseModel):
    """The files Bambuddy deleted, and the ones it skipped (never counted as deleted)."""

    deleted: list[DeletedLibraryFile] = Field(default_factory=list)
    skipped: list[SkippedLibraryFile] = Field(default_factory=list)


class LibraryRestoreResult(BaseModel):
    restored: list[int] = Field(default_factory=list)
    skipped: list[SkippedLibraryFile] = Field(default_factory=list)


#: Why Bambuddy's bulk delete skips a file: its key's user did not add it.
NOT_OWNED = "Bambuddy deletes only files its API key's user added"


def _unique(ids: list[int]) -> list[int]:
    return list(dict.fromkeys(ids))


async def _read_files(client: BambuddyClient, ids: list[int]) -> list[dict[str, Any]]:
    """Each file's name and whether it is external; a file Bambuddy no longer has is
    the 404 naming it."""
    gate = asyncio.Semaphore(READ_CONCURRENCY)

    async def one(file_id: int) -> dict[str, Any]:
        async with gate:
            try:
                found = await client.library_file(file_id)
            except ApiError as error:
                if error.status == status.HTTP_404_NOT_FOUND:
                    raise ApiError(
                        status.HTTP_404_NOT_FOUND,
                        f"library file {file_id} is no longer in Bambuddy's library",
                        file_id=file_id,
                    ) from None
                raise
        return {"id": found.id, "filename": found.filename, "external": found.is_external}

    return list(await asyncio.gather(*(one(file_id) for file_id in ids)))


async def _refuse_queued(client: BambuddyClient, files: list[dict[str, Any]]) -> None:
    """A file a waiting print names would fail that print at the printer."""
    names: dict[int, str] = {entry["id"]: entry["filename"] for entry in files}
    waiting = sorted(
        {
            item.library_file_id
            for item in await client.queue(status="pending")
            if item.library_file_id is not None
            and item.library_file_id in names
            and stage_of(item.status) == "queued"
        }
    )
    if waiting:
        listed = ", ".join(names[file_id] for file_id in waiting)
        raise ApiError(
            status.HTTP_409_CONFLICT,
            f"{listed} {'is' if len(waiting) == 1 else 'are'} waiting to print in Bambuddy's "
            "queue. Remove the print from the queue, or let it print, then delete.",
            file_ids=waiting,
        )


async def _still_listed(client: BambuddyClient, file_id: int) -> bool:
    try:
        await client.library_file(file_id)
    except ApiError as error:
        if error.status == status.HTTP_404_NOT_FOUND:
            return False
        raise
    return True


def library_file_kinds(state: Core) -> list[OperationKind]:
    """The delete and the restore, bound to this process's settings store (read at each
    call, so a test's replaced store is the one used)."""

    async def delete_check(request: dict[str, Any]) -> dict[str, Any]:
        ids = _unique(LibraryFilesRequest.model_validate(request).file_ids)
        settings = await asyncio.to_thread(state.settings_store.load)
        async with waiting_on_bambuddy(), client_for(settings) as client:
            files = await _read_files(client, ids)
            await _refuse_queued(client, files)
        return {"files": files}

    async def delete_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        files: list[dict[str, Any]] = checked["files"]
        result = LibraryDeleteResult()
        async with client_for(await asyncio.to_thread(state.settings_store.load)) as client:
            if len(files) == 1:
                [only] = files
                trashed = await client.delete_library_file(only["id"])
                result.deleted.append(
                    DeletedLibraryFile(
                        id=only["id"],
                        filename=only["filename"],
                        trashed=trashed and not only["external"],
                    )
                )
            else:
                count = await client.bulk_delete_library_files([entry["id"] for entry in files])
                # Bambuddy says only how many; any it skipped is still listed.
                listed = (
                    await asyncio.gather(*(_still_listed(client, entry["id"]) for entry in files))
                    if count < len(files)
                    else [False] * len(files)
                )
                for entry, kept in zip(files, listed, strict=True):
                    if kept:
                        result.skipped.append(
                            SkippedLibraryFile(
                                id=entry["id"], filename=entry["filename"], reason=NOT_OWNED
                            )
                        )
                    else:
                        result.deleted.append(
                            DeletedLibraryFile(
                                id=entry["id"],
                                filename=entry["filename"],
                                trashed=not entry["external"],
                            )
                        )
        return result.model_dump(mode="json")

    async def restore_check(request: dict[str, Any]) -> dict[str, Any]:
        LibraryFilesRequest.model_validate(request)
        return {}

    async def restore_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        result = LibraryRestoreResult()
        async with client_for(await asyncio.to_thread(state.settings_store.load)) as client:
            for file_id in _unique(LibraryFilesRequest.model_validate(request).file_ids):
                try:
                    await client.restore_library_file(file_id)
                except ApiError as error:
                    if error.status != status.HTTP_404_NOT_FOUND:
                        raise
                    result.skipped.append(
                        SkippedLibraryFile(
                            id=file_id,
                            reason="it is not in Bambuddy's trash: an external file, or one "
                            "already emptied from the trash",
                        )
                    )
                else:
                    result.restored.append(file_id)
        return result.model_dump(mode="json")

    return [
        OperationKind(
            "library_file_delete", delete_check, delete_run, queue="library", where=WHERE
        ),
        OperationKind(
            "library_file_restore", restore_check, restore_run, queue="library", where=WHERE
        ),
    ]
