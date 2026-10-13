"""Renaming and moving Bambuddy library files, and making, changing and deleting
library folders (#2686, spec 2026-10-13 §4-§5). The mount (``tools/go/bambuddy-fs``)
and the agent call these. Every effect is Bambuddy's API.

A folder delete takes everything in it: it walks the folder and its subfolders,
refuses with 409 before anything is deleted when a print waiting in Bambuddy's queue
names any file, deletes the files as ``library_file_delete`` does (managed files to
the trash, external ones for good), then the folders, deepest first. A folder with a
file Bambuddy skipped, or with a subfolder kept for that reason, is not deleted.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import status
from pydantic import BaseModel, Field, StringConstraints

from scadbuddy.bambuddy.client import client_for
from scadbuddy.bambuddy.models import FileUpdate, Folder, FolderCreate, FolderUpdate
from scadbuddy.core.problems import ApiError
from scadbuddy.library.library_file_operations import (
    MAX_FILES,
    WHERE,
    DeletedLibraryFile,
    SkippedLibraryFile,
    delete_files,
    read_files,
    refuse_queued,
)
from scadbuddy.operations.kinds import OperationKind, waiting_on_bambuddy

if TYPE_CHECKING:
    from scadbuddy.core.components import Core

Name = Annotated[str, StringConstraints(min_length=1, max_length=255, pattern=r"^[^/\x00]+$")]


ADDED_AFTER_CHECK = "added to the folder after the delete was checked; the folder was kept"


class LibraryFileUpdate(BaseModel):
    filename: Name | None = None
    #: ``0`` moves the file to the top level.
    folder_id: int | None = Field(default=None, ge=0)


class LibraryEntryUpdated(BaseModel):
    id: int
    filename: str
    folder_id: int | None = None


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
        async with client_for(await settings()) as client:
            found = await client.update_library_file(
                request["file_id"], FileUpdate(filename=body.filename, folder_id=body.folder_id)
            )
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
            listed = await asyncio.gather(*(client.library_files(f.id) for f in order))
            ids = [row.id for rows in listed for row in rows]
            if len(ids) > MAX_FILES:
                raise ApiError(
                    status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    f"the folder holds {len(ids)} files; ScadBuddy deletes at most "
                    f"{MAX_FILES} at once",
                )
            found = await read_files(client, ids)
            await refuse_queued(client, found)
        tree = [
            {"id": f.id, "children": [c.id for c in f.children], "files": [row.id for row in rows]}
            for f, rows in zip(order, listed, strict=True)
        ]
        return {"files": found, "tree": tree}

    async def folder_delete_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        files: list[dict[str, Any]] = checked["files"]
        async with client_for(await settings()) as client:
            gone = await delete_files(client, files) if files else None
            skipped = {entry.id for entry in gone.skipped} if gone else set()
            result = LibraryFolderDeleteResult(
                deleted=gone.deleted if gone else [],
                skipped=gone.skipped if gone else [],
                folders=[],
            )
            kept: set[int] = set()
            checked_ids = {entry["id"] for entry in files}
            for node in checked["tree"]:
                if skipped.intersection(node["files"]) or kept.intersection(node["children"]):
                    kept.add(node["id"])
                    continue
                # Bambuddy's folder delete may take whatever the folder holds, so a file
                # that arrived after the check keeps the folder (and its ancestors).
                left = await client.library_files(node["id"])
                if left:
                    kept.add(node["id"])
                    result.skipped.extend(
                        SkippedLibraryFile(
                            id=row.id, filename=row.filename, reason=ADDED_AFTER_CHECK
                        )
                        for row in left
                        if row.id not in checked_ids
                    )
                    continue
                await client.delete_folder(node["id"])
                result.folders.append(node["id"])
        return result.model_dump(mode="json")

    def kind(name: str, check: Any, run: Any) -> OperationKind:
        return OperationKind(name, check, run, queue="library", where=WHERE)

    return [
        kind("library_file_update", update_check, update_run),
        kind("library_folder_create", no_check, create_run),
        kind("library_folder_update", no_check, folder_update_run),
        kind("library_folder_delete", folder_delete_check, folder_delete_run),
    ]
