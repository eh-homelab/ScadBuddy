"""Renaming and moving Bambuddy library files, and making, changing and deleting
library folders (#2686, spec 2026-10-13 §4-§5). The mount (``tools/go/bambuddy-fs``)
and the agent call these. Every effect is Bambuddy's API.

A folder delete takes everything in it, and never lets Bambuddy's own folder delete,
which cascades to every file row in the folder, trashed ones included, reach a file
(spec §5). It walks the folder and its subfolders and refuses with 409 before anything
is deleted when a print waiting in Bambuddy's queue names any file, or when Bambuddy's
trash holds files from the folder; and with 403 when the API key cannot see every
user's trash (it lacks ``library:delete_all``), since another user's trashed file would
then be missing from that check. It moves each managed file to the top level, then
deletes the files as ``library_file_delete`` does (managed files to the trash, external
ones for good), then the folders, deepest first, each only once a fresh read finds
nothing in it: no file, trashed file or subfolder, read by a key that still sees every
user's trash. A folder with a file Bambuddy
skipped, with anything that arrived after the check, or with a subfolder kept for
either reason, is not deleted.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import status
from pydantic import BaseModel, Field, StringConstraints

from scadbuddy.bambuddy.client import LIBRARY_DELETE_ALL, BambuddyClient, client_for
from scadbuddy.bambuddy.errors import SCOPE_PROBLEM, Scope
from scadbuddy.bambuddy.models import FileUpdate, Folder, FolderCreate, FolderUpdate, LibraryFile
from scadbuddy.bambuddy.send import is_inbox
from scadbuddy.core.problems import ApiError
from scadbuddy.library.library_file_operations import (
    MAX_FILES,
    READ_CONCURRENCY,
    WHERE,
    DeletedLibraryFile,
    SkippedLibraryFile,
    delete_files,
    read_files,
    refuse_queued,
)
from scadbuddy.library.operations import PIN_TIMEOUT
from scadbuddy.operations.claims import ClaimStore
from scadbuddy.operations.kinds import OperationKind, waiting_on_bambuddy

if TYPE_CHECKING:
    from scadbuddy.core.components import Core

Name = Annotated[str, StringConstraints(min_length=1, max_length=255, pattern=r"^[^/\x00]+$")]


ADDED_AFTER_CHECK = "added to the folder after the delete was checked; the folder was kept"
FOLDER_ADDED_AFTER_CHECK = (
    "a folder made in the folder after the delete was checked; the folder was kept"
)
TRASHED_AFTER_CHECK = (
    "trashed from the folder after the delete was checked; the folder was kept, "
    "since deleting it would empty this file from Bambuddy's trash"
)
TRASH_NOT_WHOLE = (
    "Bambuddy's API key no longer sees every user's trash (it lacks "
    f"'{LIBRARY_DELETE_ALL}'); the folder was kept, since deleting it could empty "
    "another user's trashed file from it for good"
)
NOT_MOVED_OUT = "Bambuddy moves only files its API key's user added; the folder was kept"
MOVED_TO_TOP = "It was moved to the top level of the library first."


class LibraryFileUpdate(BaseModel):
    filename: Name | None = None
    #: ``0`` moves the file to the top level.
    folder_id: int | None = Field(default=None, ge=0)


class LibraryEntryUpdated(BaseModel):
    id: int
    filename: str
    folder_id: int | None = None


class LibraryFileUpload(BaseModel):
    """The kind's request: the file's bytes are held by claim (``operations/claims.py``)."""

    claim: str = Field(pattern=r"^[0-9a-f]{64}$")
    filename: Name
    folder_id: int | None = Field(default=None, ge=1)


class LibraryFolderCreate(BaseModel):
    name: Name
    parent_id: int | None = Field(default=None, ge=1)


class LibraryFolderUpdate(BaseModel):
    name: Name | None = None
    #: ``0`` moves the folder to the top level.
    parent_id: int | None = Field(default=None, ge=0)


class LibraryFolder(BaseModel):
    id: int
    name: str
    parent_id: int | None = None


class LibraryFolderDeleteResult(BaseModel):
    deleted: list[DeletedLibraryFile]
    skipped: list[SkippedLibraryFile]
    #: The folders deleted, deepest first.
    folders: list[int]


async def _refuse_partial_trash(client: BambuddyClient) -> None:
    """A folder delete trusts the trash listing to hold every file Bambuddy's cascade
    would reach, which it does only for a key that sees every user's trash."""
    if not await client.sees_every_users_trash():
        raise ApiError(
            status.HTTP_403_FORBIDDEN,
            f"Bambuddy's API key lacks the '{LIBRARY_DELETE_ALL}' permission, so it sees "
            "only its own user's trash. Deleting a folder empties every file trashed from "
            "it, other users' included, so ScadBuddy deletes folders only with a key whose "
            f"user holds '{LIBRARY_DELETE_ALL}'.",
            title="Bambuddy API key scope",
            type_=SCOPE_PROBLEM,
            required_scope=Scope.MANAGE_LIBRARY.value,
            required_permission=LIBRARY_DELETE_ALL,
        )


def _find(tree: list[Folder], folder_id: int) -> Folder:
    for top in tree:
        for folder in top.walk():
            if folder.id == folder_id:
                return folder
    raise ApiError(
        status.HTTP_404_NOT_FOUND, f"library folder {folder_id} is not in Bambuddy's library"
    )


def _deepest_first(folder: Folder) -> list[Folder]:
    return [f for _, f in sorted(folder.walk_with_depth(), key=lambda pair: -pair[0])]


def _by_id(tree: list[Folder]) -> dict[int, Folder]:
    return {folder.id: folder for top in tree for folder in top.walk()}


async def _to_top_level(
    client: BambuddyClient, files: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[SkippedLibraryFile]]:
    """Moves each managed file out of the folder before it is trashed.

    Bambuddy v1.2.5.5 trashes a file by setting ``deleted_at`` only, and its folder
    delete (``backend/app/api/routes/library.py`` ``delete_folder``, lines 1419-1485)
    removes from disk every file whose ``folder_id`` is in the folder, trashed or not,
    then drops the rows by ``LibraryFile.folder_id``'s ``ondelete="CASCADE"``
    (``backend/app/models/library.py`` line 104). A file trashed while still in the
    folder would be gone for good. Moved to the top level first, Undo restores it there.

    A file Bambuddy will not move (its key's user did not add it: ``update_file``'s 403;
    or any other refusal, such as a 422) or no longer has (404) is skipped, and keeps its
    folder; the files moved before or after it are still moved and deleted."""
    gate = asyncio.Semaphore(READ_CONCURRENCY)

    async def one(entry: dict[str, Any]) -> SkippedLibraryFile | None:
        async with gate:
            try:
                await client.update_library_file(entry["id"], FileUpdate(folder_id=0))
            except ApiError as error:
                upstream = error.extensions.get("bambuddy_status")
                if upstream == status.HTTP_403_FORBIDDEN:
                    reason = NOT_MOVED_OUT
                elif error.status == status.HTTP_404_NOT_FOUND:
                    reason = "it is no longer in Bambuddy's library"
                else:
                    # Some files may have moved already: report this one rather than
                    # abort with no account of where they went.
                    reason = (
                        f"Bambuddy refused to move it to the top level "
                        f"(HTTP {upstream or error.status}: {error.detail}); it stayed in "
                        "its folder, and the folder was kept"
                    )
                return SkippedLibraryFile(id=entry["id"], filename=entry["filename"], reason=reason)
        return None

    outcomes = await asyncio.gather(*(one(entry) for entry in files))
    moved = [entry for entry, out in zip(files, outcomes, strict=True) if out is None]
    return moved, [out for out in outcomes if out is not None]


def _moved_first(
    skipped: list[SkippedLibraryFile], moved: list[dict[str, Any]]
) -> list[SkippedLibraryFile]:
    """A moved file the delete then skipped sits at the top level now; say so."""
    ids = {entry["id"] for entry in moved}
    return [
        entry.model_copy(update={"reason": f"{entry.reason}. {MOVED_TO_TOP}"})
        if entry.id in ids
        else entry
        for entry in skipped
    ]


def library_write_kinds(state: Core) -> list[OperationKind]:
    async def settings() -> Any:
        return await asyncio.to_thread(state.settings_store.load)

    async def update_check(request: dict[str, Any]) -> dict[str, Any]:
        body = LibraryFileUpdate.model_validate(request["update"])
        if body.filename is None and body.folder_id is None:
            raise ApiError(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "give `filename`, `folder_id` or both"
            )
        async with waiting_on_bambuddy(), client_for(await settings()) as client:
            await read_files(client, [request["file_id"]])
        return {}

    async def update_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        body = LibraryFileUpdate.model_validate(request["update"])
        stored = await settings()
        async with client_for(stored) as client:
            found = await client.update_library_file(
                request["file_id"], FileUpdate(filename=body.filename, folder_id=body.folder_id)
            )
        if body.folder_id is not None:
            # As ``library_file_move`` does (#2670): a file moved into the inbox stops
            # being recorded as an output's copy; moved elsewhere, its rows follow it.
            if is_inbox(found.folder_id, stored):
                await state.uploads.forget_files([found.id])
            else:
                await state.uploads.relocate([found.id], found.folder_id)
        return LibraryEntryUpdated(
            id=found.id, filename=found.filename, folder_id=found.folder_id
        ).model_dump(mode="json")

    async def no_check(request: dict[str, Any]) -> dict[str, Any]:
        return {}

    async def create_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        body = LibraryFolderCreate.model_validate(request)
        async with client_for(await settings()) as client:
            made = await client.create_folder(
                FolderCreate(name=body.name, parent_id=body.parent_id)
            )
        return LibraryFolder(id=made.id, name=made.name, parent_id=made.parent_id).model_dump(
            mode="json"
        )

    async def folder_update_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        body = LibraryFolderUpdate.model_validate(request["update"])
        async with client_for(await settings()) as client:
            changed = await client.update_folder(
                request["folder_id"], FolderUpdate(name=body.name, parent_id=body.parent_id)
            )
        return LibraryFolder(
            id=changed.id, name=changed.name, parent_id=changed.parent_id
        ).model_dump(mode="json")

    async def folder_delete_check(request: dict[str, Any]) -> dict[str, Any]:
        async with waiting_on_bambuddy(), client_for(await settings()) as client:
            folder = _find(await client.folders(), request["folder_id"])
            order = _deepest_first(folder)
            gate = asyncio.Semaphore(READ_CONCURRENCY)

            async def listed_in(folder_id: int) -> list[LibraryFile]:
                async with gate:
                    return await client.library_files(folder_id)

            listed = await asyncio.gather(*(listed_in(f.id) for f in order))
            ids = [row.id for rows in listed for row in rows]
            if len(ids) > MAX_FILES:
                raise ApiError(
                    status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    f"the folder holds {len(ids)} files; ScadBuddy deletes at most "
                    f"{MAX_FILES} at once",
                )
            found = await read_files(client, ids)
            await refuse_queued(client, found)
            subtree = {f.id for f in order}
            await _refuse_partial_trash(client)
            trashed = [t.id for t in await client.trashed_library_files() if t.folder_id in subtree]
            if trashed:
                # Bambuddy's folder delete would empty them from its trash for good, and
                # they cannot be moved out first: v1.2.5.5's ``PUT /library/files/{id}``
                # reads ``LibraryFile.active()`` (``routes/library.py`` line 5092), which
                # leaves out trashed rows, so it answers 404 for one.
                raise ApiError(
                    status.HTTP_409_CONFLICT,
                    f"{len(trashed)} {'file' if len(trashed) == 1 else 'files'} in Bambuddy's "
                    "trash still belong to this folder. Restore or empty "
                    f"{'it' if len(trashed) == 1 else 'them'} first, then delete the folder.",
                    file_ids=trashed,
                )
        tree = [
            {"id": f.id, "children": [c.id for c in f.children], "files": [row.id for row in rows]}
            for f, rows in zip(order, listed, strict=True)
        ]
        return {"files": found, "tree": tree}

    async def folder_delete_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        files: list[dict[str, Any]] = checked["files"]
        async with client_for(await settings()) as client:
            moved, not_moved = await _to_top_level(
                client, [entry for entry in files if not entry["external"]]
            )
            doomed = moved + [entry for entry in files if entry["external"]]
            gone = await delete_files(client, doomed) if doomed else None
            result = LibraryFolderDeleteResult(
                deleted=gone.deleted if gone else [],
                skipped=not_moved + _moved_first(gone.skipped if gone else [], moved),
                folders=[],
            )
            skipped = {entry.id for entry in result.skipped}
            kept: set[int] = set()
            checked_ids = {entry["id"] for entry in files}
            for node in checked["tree"]:
                if skipped.intersection(node["files"]) or kept.intersection(node["children"]):
                    kept.add(node["id"])
                    continue
                # Bambuddy's folder delete takes whatever the folder holds, its trash and
                # its subfolders included, so anything that arrived after the check keeps
                # the folder (and its ancestors). These re-reads per node, just before
                # its delete, ARE the guard against that (a folder made or a file trashed
                # in the subtree mid-run): never hoist or cache them. The trash cannot be
                # read for one folder: Bambuddy v1.2.5.5's ``GET /library/trash``
                # (``routes/library_trash.py`` ``list_trash``, lines 88-150) takes only
                # ``limit`` and ``offset``.
                now = _by_id(await client.folders()).get(node["id"])
                if now is None:
                    continue
                left = await client.library_files(node["id"])
                if not await client.sees_every_users_trash():
                    kept.add(node["id"])
                    result.skipped.append(
                        SkippedLibraryFile(id=now.id, filename=now.name, reason=TRASH_NOT_WHOLE)
                    )
                    continue
                in_trash = [
                    t for t in await client.trashed_library_files() if t.folder_id == node["id"]
                ]
                if left or in_trash or now.children:
                    kept.add(node["id"])
                    result.skipped.extend(
                        SkippedLibraryFile(
                            id=c.id, filename=c.name, reason=FOLDER_ADDED_AFTER_CHECK
                        )
                        for c in now.children
                        if c.id not in node["children"]
                    )
                    result.skipped.extend(
                        SkippedLibraryFile(
                            id=row.id, filename=row.filename, reason=ADDED_AFTER_CHECK
                        )
                        for row in left
                        if row.id not in checked_ids
                    )
                    result.skipped.extend(
                        SkippedLibraryFile(id=t.id, filename=t.filename, reason=TRASHED_AFTER_CHECK)
                        for t in in_trash
                    )
                    continue
                await client.delete_folder(node["id"])
                result.folders.append(node["id"])
        return result.model_dump(mode="json")

    async def upload_check(request: dict[str, Any]) -> dict[str, Any]:
        LibraryFileUpload.model_validate(request)
        return {}

    async def upload_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        body = LibraryFileUpload.model_validate(request)
        try:
            # Streamed from the claim, never read whole: an upload may be 256 MB, and
            # this worker runs inside the API process.
            held = await asyncio.to_thread(ClaimStore(state.paths.claims).open, body.claim)
        except LookupError:
            raise ApiError(
                status.HTTP_409_CONFLICT,
                # Not "send it again": a re-send with the same key replays this answer.
                "this request's upload is no longer held; upload the file again",
            ) from None
        with held:
            async with client_for(await settings()) as client:
                made = await client.upload_library_file(
                    body.filename,
                    held,
                    folder_id=body.folder_id,
                    media_type="application/octet-stream",
                )
        return LibraryEntryUpdated(
            id=made.id, filename=made.filename, folder_id=made.folder_id
        ).model_dump(mode="json")

    def kind(
        name: str, check: Any, run: Any, run_timeout: timedelta | None = None
    ) -> OperationKind:
        return OperationKind(
            name, check, run, queue="library", where=WHERE, run_timeout=run_timeout
        )

    return [
        kind("library_file_update", update_check, update_run),
        kind("library_folder_create", no_check, create_run),
        kind("library_folder_update", no_check, folder_update_run),
        kind("library_folder_delete", folder_delete_check, folder_delete_run),
        # An upload of up to the download cap must not time out into a second upload.
        kind("library_file_upload", upload_check, upload_run, run_timeout=PIN_TIMEOUT),
    ]
