# bambuddy-fs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Mount Bambuddy's library, read-write, as a FUSE filesystem that talks only to ScadBuddy's API.

**Architecture:** Two PRs.
- **PR A** adds the missing library write routes to the backend (upload, rename/move,
  mkdir, folder rename/move, folder delete with contents) as operation kinds on the
  `library` queue, with agent tools.
- **PR B** is stacked on PR A. It adds a root `go.work` workspace with two vendored
  modules: a Kiota-generated ScadBuddy client, and `bambuddy-fs` (go-fuse v2) built on
  it. It also adds a path-selecting `go` CI job and Go-client generation in the API
  diff job.

**Tech Stack:** Python 3.12, FastAPI, Temporal operations, respx (backend); TypeScript, zod, msw (agent); Go, `github.com/hanwen/go-fuse/v2`, Kiota Go (`mcr.microsoft.com/openapi/kiota`), GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-10-13-bambuddy-fs-design.md` (issue #2686). Read it before any task.

## Global Constraints

- **The mount talks only to ScadBuddy's `/api/v1`.** Nothing in `tools/go/` calls Bambuddy.
- **Kiota image, pinned:**
  `mcr.microsoft.com/openapi/kiota:1.36.1@sha256:077073656898de0f8d3339bad496f154d603c30c0cdcbcbc5feacad2ad988d9d`.
  - Run it with `-u "$(id -u):$(id -g)" -e HOME=/tmp -v "$ROOT:/work"`.
  - Never pass `-w`. The entrypoint resolves `kiota.dll` from `/app`.
  - Every Kiota path argument is absolute under `/work`.
- **Before generating, `rm -rf tools/go/scadbuddy-client/client`.** After generating,
  delete `client/.kiota.log` (it holds timestamps) and drop `descriptionHash` from
  `client/kiota-lock.json`.
- **Kiota include paths:** exactly `/api/v1/print/library`, `/api/v1/print/library/**`
  and `/api/v1/operations/**`. The `library**` glob alone misses the bare path, as
  measured.
- **Go module paths:** `github.com/eh-homelab/ScadBuddy/tools/go/scadbuddy-client` and
  `github.com/eh-homelab/ScadBuddy/tools/go/bambuddy-fs`.
- **`go.work` pins `toolchain go1.25.6`** (the version measured here), unless
  `go mod tidy` demands a newer one. In that case pin exactly what it demands, in
  `go.work` and in `go-module-ci.yml`'s `GOTOOLCHAIN`.
- **Vendoring:** a root `vendor/` from `go work vendor`, and a module-local `vendor/`
  in `tools/go/scadbuddy-client` from `GOWORK=off go mod vendor`. Both are committed.
- **Runners:** every CI job runs on `ubuntu-latest`. No `paths:` filter on `ci.yml`,
  and no job-level skip for Go: the `go` job always runs and selects inside itself.
- **New backend kinds** use `queue="library"` and `where=WHERE` from
  `library/library_file_operations.py`.
- **Bambuddy update semantics:** the update is partial, and **`0` (not null)** clears
  `folder_id`/`parent_id`.
- **Commits:**
  - Conventional titles, ending with
    `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` and
    `Claude-Session: https://claude.ai/code/session_01DEEmnnnhXLGwGF4nkbktrB`.
  - Never amend. Never commit `backend/openapi.json` or either `schema.d.ts`.
- **Backend checks:** `cd backend && uv run --frozen ruff check . && uv run --frozen
  ruff format --check . && uv run --frozen mypy && uv run --frozen pytest -n auto <paths>`.
  Postgres and Temporal are needed (see CLAUDE.md).
- **Agent checks:** `cd agent && pnpm lint && pnpm typecheck && pnpm test`.

## Review Focus

1. **Name collisions on create or rename.** Writing `a.3mf` into a folder that already
   has a file of that name replaces it: upload the new file, then delete the old one.
   Renaming onto an existing name returns `EEXIST`. Pinned in Task 8.
2. **Partial writes from editors and slicers.** A slicer opens, truncates to 0, writes,
   flushes several times and closes. The upload runs on `flush` when writes are
   unsent, exactly once, and returns its error to `close()`; `release` uploads only
   what is still unsent (spec §8: `close()` returns before `release` runs).
   Pinned in Task 8.
3. **A deleted or vanished file is still cached.** After `unlink` (or a 404 on read),
   the path and its cache entry go away, and a later `ls` does not show it. Pinned in
   Tasks 7 and 8.
4. **`rmdir` of a folder whose file a queued print names.** The folder delete answers
   409 before deleting anything, the mount returns `EBUSY`, and every file is still
   listed. Pinned in Tasks 2 and 8.
5. **A 202 from a slow operation.** A write that answers 202 is followed through
   `GET /operations/{id}` to `succeeded` or `failed`, never treated as done on the
   202. Pinned in Task 6.

---

# PR A: backend routes and agent tools

Branch: `feat/bambuddy-fs-routes` from `origin/main`, in a worktree. PR title:
`feat(library): write routes for Bambuddy library files and folders (#2686)`. Body:
`Refs #2686`.

### Task 1: BambuddyClient methods for file update and folder update/delete

**Files:**
- Modify: `backend/scadbuddy/bambuddy/client.py`
  - the `Operation` Literal (`:89-151`)
  - add methods after `annotate_library_file` (`:867-891`)
- Modify: `backend/scadbuddy/bambuddy/models.py`: add `FileUpdate` and `FolderUpdate`
  after `FolderCreate` (`:437`)
- Test: `backend/tests/bambuddy/test_client_library_writes.py` (new)

**Interfaces:**
- Produces:
  - `BambuddyClient.update_library_file(file_id: int, update: FileUpdate) -> LibraryFile`
  - `BambuddyClient.update_folder(folder_id: int, update: FolderUpdate) -> Folder`
  - `BambuddyClient.delete_folder(folder_id: int) -> None`
  - `FileUpdate(filename: str | None = None, folder_id: int | None = None)`
  - `FolderUpdate(name: str | None = None, parent_id: int | None = None)`
  - Operation literals `"library.files.update"`, `"library.folders.update"`,
    `"library.folders.delete"`

- [ ] **Step 1: Write the failing tests**

```python
"""The library write calls the mount needs (#2686): partial updates, 0 clears."""

import json

import httpx
import pytest
import respx

from scadbuddy.bambuddy.client import BambuddyClient, BambuddyConfig
from scadbuddy.bambuddy.models import FileUpdate, FolderUpdate

BASE = "https://bambuddy.test"
API = f"{BASE}/api/v1"


def client() -> BambuddyClient:
    return BambuddyClient(BambuddyConfig(base_url=BASE, api_key="s3cret"))


@respx.mock
async def test_update_library_file_sends_only_the_fields_given() -> None:
    route = respx.put(f"{API}/library/files/7").mock(
        return_value=httpx.Response(200, json={"id": 7, "filename": "b.3mf", "folder_id": 3})
    )
    async with client() as bambuddy:
        found = await bambuddy.update_library_file(7, FileUpdate(filename="b.3mf"))
    assert json.loads(route.calls.last.request.content) == {"filename": "b.3mf"}
    assert found.filename == "b.3mf"


@respx.mock
async def test_moving_a_file_to_the_top_level_sends_zero() -> None:
    route = respx.put(f"{API}/library/files/7").mock(
        return_value=httpx.Response(200, json={"id": 7, "filename": "a.3mf"})
    )
    async with client() as bambuddy:
        await bambuddy.update_library_file(7, FileUpdate(folder_id=0))
    assert json.loads(route.calls.last.request.content) == {"folder_id": 0}


@respx.mock
async def test_update_folder_and_delete_folder() -> None:
    put = respx.put(f"{API}/library/folders/4").mock(
        return_value=httpx.Response(200, json={"id": 4, "name": "New", "parent_id": None})
    )
    gone = respx.delete(f"{API}/library/folders/4").mock(return_value=httpx.Response(200, json={}))
    async with client() as bambuddy:
        folder = await bambuddy.update_folder(4, FolderUpdate(name="New"))
        await bambuddy.delete_folder(4)
    assert json.loads(put.calls.last.request.content) == {"name": "New"}
    assert folder.name == "New"
    assert gone.called


@pytest.mark.parametrize("status", [403, 404])
@respx.mock
async def test_delete_folder_maps_errors(status: int) -> None:
    respx.delete(f"{API}/library/folders/4").mock(return_value=httpx.Response(status, json={}))
    async with client() as bambuddy:
        with pytest.raises(Exception) as raised:
            await bambuddy.delete_folder(4)
    assert getattr(raised.value, "status", None) in {404, 409, 502}
```

Before running, check how existing client tests construct the client and enter it.
`grep -n "BambuddyClient(" backend/tests/bambuddy/*.py | head`. Use that exact
construction (config class name and fields) in `client()` above if it differs.

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `cd backend && uv run --frozen pytest tests/bambuddy/test_client_library_writes.py -v`
Expected: FAIL, with an `ImportError` on `FileUpdate`.

- [ ] **Step 3: Implement**

In `models.py`, after `FolderCreate`:

```python
class FileUpdate(BambuddyModel):
    """``PUT /api/v1/library/files/{id}``: a partial update. Only the fields given are
    sent, and ``folder_id=0`` moves the file to the top level (Bambuddy's sentinel;
    see `BambuddyClient.annotate_library_file`)."""

    filename: str | None = None
    folder_id: int | None = None


class FolderUpdate(BambuddyModel):
    """``PUT /api/v1/library/folders/{id}``: a partial update; ``parent_id=0`` moves the
    folder to the top level."""

    name: str | None = None
    parent_id: int | None = None
```

In `client.py`, add `"library.files.update"`, `"library.folders.update"` and
`"library.folders.delete"` to `Operation` in sorted position, then add after
`annotate_library_file`:

```python
    async def update_library_file(self, file_id: int, update: FileUpdate) -> LibraryFile:
        """``PUT /library/files/{id}``: rename and/or move. Partial, as
        :meth:`annotate_library_file` explains."""
        response = await self._send(
            "PUT",
            f"/library/files/{file_id}",
            scope=Scope.MANAGE_LIBRARY,
            operation="library.files.update",
            what=f"update library file {file_id}",
            json=update.model_dump(mode="json", exclude_none=True),
        )
        return LibraryFile.model_validate(response.json())

    async def update_folder(self, folder_id: int, update: FolderUpdate) -> Folder:
        response = await self._send(
            "PUT",
            f"/library/folders/{folder_id}",
            scope=Scope.MANAGE_LIBRARY,
            operation="library.folders.update",
            what=f"update library folder {folder_id}",
            json=update.model_dump(mode="json", exclude_none=True),
        )
        return Folder.model_validate(response.json())

    async def delete_folder(self, folder_id: int) -> None:
        """``DELETE /library/folders/{id}``. The caller empties it first (the folder
        delete kind), so what this drops is the folder's own record."""
        await self._send(
            "DELETE",
            f"/library/folders/{folder_id}",
            scope=Scope.MANAGE_LIBRARY,
            operation="library.folders.delete",
            what=f"delete library folder {folder_id}",
        )
```

Import `FileUpdate` and `FolderUpdate` where `FolderCreate` is imported. If
`lint-dashboard.sh` or `core/metrics.py` enumerates operation names
(`grep -rn '"library.folders.create"' backend/ deploy/ .github/`), add the new names
alongside it.

- [ ] **Step 4: Run tests and checks**

Run: `cd backend && uv run --frozen pytest tests/bambuddy/test_client_library_writes.py -v && uv run --frozen ruff check . && uv run --frozen mypy`
Expected: PASS.

- [ ] **Step 5: Commit**

`git commit -m "feat(bambuddy): client calls to rename, move and delete library entries (#2686)"`

### Task 2: Operation kinds and routes for file update, folder create/update/delete

**Files:**
- Create: `backend/scadbuddy/library/library_write_operations.py`
- Modify: `backend/scadbuddy/library/operations.py:295-332`: import and splat
  `library_write_kinds(core)`; mention it in `_kinds`'s docstring
- Modify: `backend/scadbuddy/api/library_print.py`: add four routes after
  `post_library_restore`
- Test: `backend/tests/api/test_library_writes.py` (new; reuse the helpers in
  `tests/api/test_library_delete.py`)

**Interfaces:**
- Consumes: the Task 1 client methods. From `library_file_operations`: `_read_files`,
  `_refuse_queued`, `LibraryDeleteResult`, `DeletedLibraryFile`,
  `SkippedLibraryFile`, `WHERE`, `MAX_FILES`. Make `_read_files` and `_refuse_queued`
  importable by dropping the leading underscore (`read_files`, `refuse_queued`) and
  updating their callers.
- Produces:
  - Request models `LibraryFileUpdate {filename?: str (1..255, no "/"), folder_id?: int >= 0}`,
    `LibraryFolderCreate {name: str (1..255, no "/"), parent_id?: int >= 1}`,
    `LibraryFolderUpdate {name?, parent_id?: int >= 0}`.
  - Result models `LibraryFolder {id, name, parent_id}` and
    `LibraryFolderDeleteResult {deleted: list[DeletedLibraryFile], skipped: list[SkippedLibraryFile], folders: list[int]}`.
  - Kinds `library_file_update`, `library_folder_create`, `library_folder_update`,
    `library_folder_delete`.
  - Routes:
    - `PATCH /api/v1/print/library/{file_id}` → `LibraryEntryUpdated {id, filename, folder_id}`
    - `POST /api/v1/print/library/folders` → `LibraryFolder`
    - `PATCH /api/v1/print/library/folders/{folder_id}` → `LibraryFolder`
    - `POST /api/v1/print/library/folders/{folder_id}/delete` → `LibraryFolderDeleteResult`
  - Subject: `LIBRARY_FILES_SUBJECT` for all of them.

- [ ] **Step 1: Write the failing tests**

```python
"""Renaming and moving library files, and making, changing and deleting folders
(#2686), through ScadBuddy's operations."""

import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from tests.api.test_library_delete import API, files, queue
from tests.api.test_send import configure
from tests.support.operations import press

pytestmark = pytest.mark.requires_postgres


def folders(*tree: dict[str, object]) -> None:
    respx.get(f"{API}/library/folders").mock(return_value=httpx.Response(200, json=list(tree)))


def folder_files(folder_id: int, *rows: tuple[int, bool]) -> None:
    respx.get(f"{API}/library/files", params={"folder_id": folder_id}).mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": i, "filename": f"file-{i}.3mf", "file_type": "3mf", "folder_id": folder_id, "is_external": ext}
                for i, ext in rows
            ],
        )
    )


@respx.mock
def test_rename_and_move_send_only_what_was_given(client: TestClient) -> None:
    configure(client)
    files((7, False))
    put = respx.put(f"{API}/library/files/7").mock(
        return_value=httpx.Response(200, json={"id": 7, "filename": "b.3mf", "folder_id": 3})
    )
    response = client.patch("/api/v1/print/library/7", json={"filename": "b.3mf", "folder_id": 3}, headers=press())
    assert response.status_code == 200, response.text
    assert response.json() == {"id": 7, "filename": "b.3mf", "folder_id": 3}
    assert json.loads(put.calls.last.request.content) == {"filename": "b.3mf", "folder_id": 3}


def test_a_filename_with_a_slash_is_refused(client: TestClient) -> None:
    configure(client)
    response = client.patch("/api/v1/print/library/7", json={"filename": "a/b.3mf"}, headers=press())
    assert response.status_code == 422


@respx.mock
def test_mkdir_and_folder_rename(client: TestClient) -> None:
    configure(client)
    made = respx.post(f"{API}/library/folders/").mock(
        return_value=httpx.Response(200, json={"id": 9, "name": "Parts", "parent_id": 2})
    )
    response = client.post("/api/v1/print/library/folders", json={"name": "Parts", "parent_id": 2}, headers=press())
    assert response.status_code == 200, response.text
    assert response.json() == {"id": 9, "name": "Parts", "parent_id": 2}
    assert json.loads(made.calls.last.request.content) == {"name": "Parts", "parent_id": 2}

    respx.put(f"{API}/library/folders/9").mock(
        return_value=httpx.Response(200, json={"id": 9, "name": "Bits", "parent_id": 2})
    )
    renamed = client.patch("/api/v1/print/library/folders/9", json={"name": "Bits"}, headers=press())
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "Bits"


@respx.mock
def test_folder_delete_takes_its_files_and_subfolders(client: TestClient) -> None:
    configure(client)
    folders({"id": 4, "name": "Top", "children": [{"id": 5, "name": "Sub", "parent_id": 4, "children": []}]})
    folder_files(4, (10, False))
    folder_files(5, (11, True))
    files((10, False), (11, True))
    queue()
    respx.post(f"{API}/library/bulk-delete").mock(return_value=httpx.Response(200, json={"deleted_files": 2}))
    gone5 = respx.delete(f"{API}/library/folders/5").mock(return_value=httpx.Response(200, json={}))
    gone4 = respx.delete(f"{API}/library/folders/4").mock(return_value=httpx.Response(200, json={}))

    response = client.post("/api/v1/print/library/folders/4/delete", headers=press())

    assert response.status_code == 200, response.text
    body = response.json()
    assert sorted((f["id"], f["trashed"]) for f in body["deleted"]) == [(10, True), (11, False)]
    assert body["folders"] == [5, 4]
    assert gone5.called and gone4.called
    assert gone5.calls.last.request is not None


@respx.mock
def test_folder_delete_refuses_before_anything_when_a_print_waits(client: TestClient) -> None:
    configure(client)
    folders({"id": 4, "name": "Top", "children": []})
    folder_files(4, (10, False))
    files((10, False))
    queue({"id": 1, "status": "pending", "library_file_id": 10})
    bulk = respx.post(f"{API}/library/bulk-delete")
    gone = respx.delete(f"{API}/library/folders/4")

    response = client.post("/api/v1/print/library/folders/4/delete", headers=press())

    assert response.status_code == 409
    assert not bulk.called and not gone.called


@respx.mock
def test_an_empty_folder_delete_drops_only_the_folder(client: TestClient) -> None:
    configure(client)
    folders({"id": 4, "name": "Top", "children": []})
    folder_files(4)
    queue()
    gone = respx.delete(f"{API}/library/folders/4").mock(return_value=httpx.Response(200, json={}))
    response = client.post("/api/v1/print/library/folders/4/delete", headers=press())
    assert response.status_code == 200, response.text
    assert response.json() == {"deleted": [], "skipped": [], "folders": [4]}
    assert gone.called
```

Check `queue()`'s item shape against `test_library_delete.py`'s 409 test and copy it
exactly. Check `folder_files`'s query parameter against `BambuddyClient.library_files`
(`client.py:787`). Fix both in these tests before running.

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `cd backend && uv run --frozen pytest tests/api/test_library_writes.py -v`
Expected: FAIL, with 404/405 on the new routes.

- [ ] **Step 3: Implement `library_write_operations.py`**

```python
"""Renaming and moving Bambuddy library files, and making, changing and deleting
library folders (#2686, spec 2026-10-13 §4-§5). The mount (``tools/go/bambuddy-fs``)
and the agent call these. Every effect is Bambuddy's API.

A folder delete takes everything in it: it walks the folder and its subfolders,
refuses with 409 before anything is deleted when a print waiting in Bambuddy's queue
names any file, deletes the files as ``library_file_delete`` does (managed files to
the trash, external ones for good), then the folders, deepest first.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import status
from pydantic import BaseModel, Field, StringConstraints

from scadbuddy.bambuddy.client import BambuddyClient, client_for
from scadbuddy.bambuddy.models import FileUpdate, Folder, FolderCreate, FolderUpdate
from scadbuddy.core.problems import ApiError
from scadbuddy.library.library_file_operations import (
    MAX_FILES,
    WHERE,
    DeletedLibraryFile,
    SkippedLibraryFile,
    read_files,
    refuse_queued,
)
from scadbuddy.operations.kinds import OperationKind, waiting_on_bambuddy

if TYPE_CHECKING:
    from scadbuddy.core.components import Core

Name = Annotated[str, StringConstraints(min_length=1, max_length=255, pattern=r"^[^/\x00]+$")]


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
    raise ApiError(status.HTTP_404_NOT_FOUND, f"library folder {folder_id} is not in Bambuddy's library")


def _deepest_first(folder: Folder) -> list[int]:
    return [f.id for _, f in sorted(folder.walk_with_depth(), key=lambda pair: -pair[0])]


async def _folder_files(client: BambuddyClient, folder: Folder) -> list[int]:
    found = await asyncio.gather(*(client.library_files(f.id) for f in folder.walk()))
    return [row.id for rows in found for row in rows]


def library_write_kinds(state: Core) -> list[OperationKind]:
    async def settings() -> Any:
        return await asyncio.to_thread(state.settings_store.load)

    async def update_check(request: dict[str, Any]) -> dict[str, Any]:
        body = LibraryFileUpdate.model_validate(request["update"])
        if body.filename is None and body.folder_id is None:
            raise ApiError(status.HTTP_422_UNPROCESSABLE_ENTITY, "give `filename`, `folder_id` or both")
        async with waiting_on_bambuddy(), client_for(await settings()) as client:
            await read_files(client, [request["file_id"]])
        return {}

    async def update_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        body = LibraryFileUpdate.model_validate(request["update"])
        async with client_for(await settings()) as client:
            found = await client.update_library_file(
                request["file_id"], FileUpdate(filename=body.filename, folder_id=body.folder_id)
            )
        return LibraryEntryUpdated(id=found.id, filename=found.filename, folder_id=found.folder_id).model_dump(mode="json")

    async def no_check(request: dict[str, Any]) -> dict[str, Any]:
        return {}

    async def create_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        body = LibraryFolderCreate.model_validate(request)
        async with client_for(await settings()) as client:
            made = await client.create_folder(FolderCreate(name=body.name, parent_id=body.parent_id))
        return LibraryFolder(id=made.id, name=made.name, parent_id=made.parent_id).model_dump(mode="json")

    async def folder_update_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        body = LibraryFolderUpdate.model_validate(request["update"])
        async with client_for(await settings()) as client:
            changed = await client.update_folder(
                request["folder_id"], FolderUpdate(name=body.name, parent_id=body.parent_id)
            )
        return LibraryFolder(id=changed.id, name=changed.name, parent_id=changed.parent_id).model_dump(mode="json")

    async def folder_delete_check(request: dict[str, Any]) -> dict[str, Any]:
        async with waiting_on_bambuddy(), client_for(await settings()) as client:
            folder = _find(await client.folders(), request["folder_id"])
            ids = await _folder_files(client, folder)
            if len(ids) > MAX_FILES:
                raise ApiError(
                    status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    f"the folder holds {len(ids)} files; ScadBuddy deletes at most {MAX_FILES} at once",
                )
            found = await read_files(client, ids)
            await refuse_queued(client, found)
        return {"files": found, "folders": _deepest_first(folder)}

    async def folder_delete_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        files: list[dict[str, Any]] = checked["files"]
        result = LibraryFolderDeleteResult(deleted=[], skipped=[], folders=[])
        async with client_for(await settings()) as client:
            if files:
                await client.bulk_delete_library_files([f["id"] for f in files])
            for entry in files:
                result.deleted.append(
                    DeletedLibraryFile(id=entry["id"], filename=entry["filename"], trashed=not entry["external"])
                )
            for folder_id in checked["folders"]:
                await client.delete_folder(folder_id)
                result.folders.append(folder_id)
        return result.model_dump(mode="json")

    def kind(name: str, check: Any, run: Any) -> OperationKind:
        return OperationKind(name, check, run, queue="library", where=WHERE)

    return [
        kind("library_file_update", update_check, update_run),
        kind("library_folder_create", no_check, create_run),
        kind("library_folder_update", no_check, folder_update_run),
        kind("library_folder_delete", folder_delete_check, folder_delete_run),
    ]
```

The folder delete reports a file Bambuddy's bulk delete skipped (because its key's
user did not add it) as skipped, the same way `delete_run` does in
`library_file_operations.py`. Copy its `count < len(files)` → `_still_listed` branch
into `folder_delete_run`. If any file was skipped, do not delete that file's folder
or its ancestors, and add a test for it:
`test_folder_delete_keeps_a_folder_whose_file_was_skipped`, with
`deleted_files: 1` of 2 and the skipped file still listed.

- [ ] **Step 4: Add the routes in `api/library_print.py`**

Follow `post_library_delete`'s shape exactly. The PATCH routes put the path id into
the request: `request={"file_id": file_id, "update": body.model_dump(mode="json", exclude_none=True)}`.
Example:

```python
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
```

The other three routes follow the same shape:
- `POST /folders`: the body is the request.
- `PATCH /folders/{folder_id}`: `{"folder_id", "update"}`.
- `POST /folders/{folder_id}/delete`: no body; request `{"folder_id": folder_id}`.

Define `FolderIdPath = Annotated[int, Path(ge=1)]`.

**Route order matters.** Declare `/folders` and `/folders/{folder_id}...` **before**
any `/{file_id}` route that could swallow `folders`. `FileIdPath` is an int, so
`folders` fails validation rather than matching, but declare them early anyway.

- [ ] **Step 5: Run the tests and checks**

Run: `cd backend && uv run --frozen pytest tests/api/test_library_writes.py tests/api/test_library_delete.py -v && uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy`
Expected: PASS.

- [ ] **Step 6: Commit**

`git commit -m "feat(library): rename, move, mkdir and folder delete routes for Bambuddy's library (#2686)"`

### Task 3: Upload into a folder (streamed, by claim)

**Files:**
- Modify: `backend/scadbuddy/library/library_write_operations.py`: add the
  `library_file_upload` kind
- Modify: `backend/scadbuddy/api/library_print.py`: route
  `POST /print/library/files`, plus a schema-only `LibraryFileUploadForm`
- Modify: `backend/scadbuddy/api/limits.py:53-62`: add
  `LIBRARY_UPLOAD_PATH = re.compile(r"^/api/v1/print/library/files$")`
- Modify: `backend/scadbuddy/main.py:1209-1230`: a `RouteLimit` for it
- Test: `backend/tests/api/test_library_writes.py` (append)

**Interfaces:**
- Consumes: `_receive` from `api/media.py` (as `api/mesh_models.py:177-235` uses
  it), `ClaimStore`, `Claimed`, `recorded`, and
  `BambuddyClient.upload_library_file(filename, content, folder_id=..., media_type=...)`.
- Produces:
  - Route `POST /api/v1/print/library/files`: multipart, with a `file` part (its
    filename is the library name) and a text `folder_id` (optional; absent means the
    top level). Answers `LibraryEntryUpdated {id, filename, folder_id}`.
  - Kind `library_file_upload`, request
    `{"claim": <sha256>, "filename": str, "folder_id": int | None}`.

- [ ] **Step 1: Write the failing tests**

```python
@respx.mock
def test_upload_into_a_folder_goes_by_claim(client: TestClient) -> None:
    configure(client)
    up = respx.post(f"{API}/library/files").mock(
        return_value=httpx.Response(200, json={"id": 50, "filename": "box.3mf"})
    )
    payload = b"PK\x03\x04" + b"\x00" * 3_000_000
    response = client.post(
        "/api/v1/print/library/files",
        files={"file": ("box.3mf", payload, "application/octet-stream")},
        data={"folder_id": "3"},
        headers=press(),
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"id": 50, "filename": "box.3mf", "folder_id": 3}
    sent = up.calls.last.request
    assert sent.url.params["folder_id"] == "3"
    assert payload in sent.content


@respx.mock
def test_a_repeated_upload_uploads_once(client: TestClient) -> None:
    configure(client)
    up = respx.post(f"{API}/library/files").mock(
        return_value=httpx.Response(200, json={"id": 50, "filename": "box.3mf"})
    )
    key = press()
    for _ in range(2):
        response = client.post(
            "/api/v1/print/library/files",
            files={"file": ("box.3mf", b"PK\x03\x04data", "application/octet-stream")},
            headers=key,
        )
        assert response.status_code == 200, response.text
    assert up.call_count == 1


def test_an_upload_needs_a_named_file(client: TestClient) -> None:
    configure(client)
    response = client.post(
        "/api/v1/print/library/files",
        files={"file": ("", b"x", "application/octet-stream")},
        headers=press(),
    )
    assert response.status_code == 422
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `cd backend && uv run --frozen pytest tests/api/test_library_writes.py -k upload -v`
Expected: FAIL (404).

- [ ] **Step 3: Implement**

- **Route.** Copy `create_from_upload`'s shape (`api/mesh_models.py:177-235`):
  - `received = await _receive(request, paths.cache, fields={"folder_id": 32})`
  - `filename = received.filenames["file"]`; refuse it with 422 if it is empty or
    contains `/`.
  - `digest = received.hashes["file"].hexdigest()`
  - `body = {"claim": digest, "filename": filename, "folder_id": int(folder_id) if folder_id else None}`
  - `recorded(...)`; only when that returns `None`, `held.append(await asyncio.to_thread(claims.hold_file, received.files["file"], digest))`
  - `finally: received.discard()`
  - `run_operation(..., claimed=Claimed(claims, held))`
  - `operation_answer(result, LibraryEntryUpdated)`

  Use the `openapi_extra` multipart schema pattern from `api/media.py:691-721` with
  a `LibraryFileUploadForm(BaseModel)` (`file: bytes`, `folder_id: int | None`) so
  the spec, and so Kiota, describe the form.
- **Kind.** Its `check` validates the request. Its `run` reads the bytes with
  `ClaimStore(state.paths.claims).get(request["claim"])` (the `meshmodel/operations.py:169-178`
  pattern, with the same 409 on `LookupError`). It then calls
  `client.upload_library_file(filename, data, folder_id=folder_id, media_type="application/octet-stream")`
  and returns `LibraryEntryUpdated(id=made.id, filename=made.filename, folder_id=folder_id)`.
  Set `run_timeout` to the upload's (`PIN_TIMEOUT` or the client's `upload_timeout`;
  use what `output_operations.py`'s upload kind uses).
- **Limit.** `RouteLimit("POST", LIBRARY_UPLOAD_PATH, MAX_DOWNLOAD_BYTES + 64 * 1024, "a library upload", "the library download cap (bambuddy/print_source.py MAX_DOWNLOAD_BYTES)")`.

- [ ] **Step 4: Run tests and checks**

Run: `cd backend && uv run --frozen pytest tests/api/test_library_writes.py -v && uv run --frozen ruff check . && uv run --frozen mypy`
Expected: PASS.

- [ ] **Step 5: Commit**

`git commit -m "feat(library): upload a file into a Bambuddy library folder (#2686)"`

### Task 4: Agent tools for the new routes

**Files:**
- Modify: `agent/src/tools/print.ts`: five `defineTool`s after `restore_library_files`
  (`:432-449`)
- Modify: `agent/src/sessions/touched.ts`: extractors near `:366-373` for the file
  tools (`type: 'bambuddy_file'`); add the folder tools to `TOUCHES_NOTHING` (`:448`)
- Test: `agent/test/libraryPrint.test.ts` (append), `agent/test/touched.test.ts` (cases
  near `:370`)

**Interfaces:**
- Consumes: the routes from Tasks 2-3. The agent's `backend/openapi.json` is
  regenerated by `pnpm test`/`typecheck`.
- Produces these tools:

| Tool | Risk | Route |
|---|---|---|
| `upload_library_file` | `write` | `POST /api/v1/print/library/files` |
| `update_library_file` | `write` | `PATCH /api/v1/print/library/{file_id}` |
| `create_library_folder` | `write` | `POST /api/v1/print/library/folders` |
| `update_library_folder` | `write` | `PATCH /api/v1/print/library/folders/{folder_id}` |
| `delete_library_folder` | `outward` | `POST /api/v1/print/library/folders/{folder_id}/delete` |

Each has `bambuddyScope: ['Manage Library']`.

- [ ] **Step 1: Write the failing tests**

```ts
describe('library writes for the mount (#2686)', () => {
  it('deleting a folder is outward and sends a key', async () => {
    let key: string | null = null
    server.use(
      http.post(`${BACKEND}/api/v1/print/library/folders/4/delete`, ({ request }) => {
        key = request.headers.get('Idempotency-Key')
        return HttpResponse.json({ deleted: [], skipped: [], folders: [4] })
      }),
    )
    expect(tool('delete_library_folder').risk).toBe('outward')
    const result = firstText(await tool('delete_library_folder').execute({ folder_id: 4 }, ctx())) as { folders: number[] }
    expect(key).toBeTruthy()
    expect(result.folders).toEqual([4])
  })

  it('renames a file with only the fields given', async () => {
    let sent: unknown
    server.use(
      http.patch(`${BACKEND}/api/v1/print/library/7`, async ({ request }) => {
        sent = await request.json()
        return HttpResponse.json({ id: 7, filename: 'b.3mf', folder_id: null })
      }),
    )
    await tool('update_library_file').execute({ file_id: 7, filename: 'b.3mf' }, ctx())
    expect(sent).toEqual({ filename: 'b.3mf' })
  })

  it('makes a folder', async () => {
    let sent: unknown
    server.use(
      http.post(`${BACKEND}/api/v1/print/library/folders`, async ({ request }) => {
        sent = await request.json()
        return HttpResponse.json({ id: 9, name: 'Parts', parent_id: 2 })
      }),
    )
    await tool('create_library_folder').execute({ name: 'Parts', parent_id: 2 }, ctx())
    expect(sent).toEqual({ name: 'Parts', parent_id: 2 })
  })
})
```

Add an `upload_library_file` test modelled on the `upload_asset` workspace-upload
test (`grep -n "upload_asset" agent/test/*.test.ts`). It should check that the
multipart body carries `folder_id` and the file part named by `filename`.

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `cd agent && pnpm test -- libraryPrint touched coverage`
Expected: FAIL. The tools are missing, and coverage names the five new operations.

- [ ] **Step 3: Implement the tools.**
  - **Delete and update tools.** Copy `delete_library_files`
    (`print.ts`, quoted in the spec research). Put the path ids in `params.path`.
  - **`upload_library_file`.** Copy `upload_asset` (`customizer.ts:462-496`), with
    input `{ folder_id?: number, filename?: string, content_base64?, workspace_path?, workspace_sha256? }`.
    Send it as
    `uploadInit(ctx, source, { name: 'file', filename: name, type: 'application/octet-stream' }, headers, folder_id === undefined ? {} : { folder_id: String(folder_id) })`.
  - **`summarize`/`title`** follow the neighbours, e.g.
    `Delete library folder ${folder_id} and everything in it`.
  - **Descriptions** state that deleting a folder takes its files and subfolders,
    sends managed files to the trash and deletes external ones for good, and refuses
    if a queued print names a file.

- [ ] **Step 4: Run the checks**

Run: `cd agent && pnpm lint && pnpm typecheck && pnpm test`
Expected: PASS. `coverage.test.ts` and `touched.test.ts` are green.

- [ ] **Step 5: Commit, push, open PR A**

`git commit -m "feat(agent): tools for Bambuddy library uploads, renames and folders (#2686)"`

Then push and run `gh pr create` with the title above, body
`Refs #2686` + the attribution footer.

---

# PR B: the Go workspace, client, mount and CI

Branch: `feat/bambuddy-fs` from PR A's branch (stacked). PR title:
`feat(bambuddy-fs): mount Bambuddy's library as a FUSE filesystem (#2686)`. Body:
`Fixes #2686`. Once PR A merges, rebase onto main.

### Task 5: Workspace, generator script, generated client

**Files:**
- Create: `go.work`, `scripts/generate-go-client.sh`,
  `tools/go/scadbuddy-client/go.mod`, `tools/go/scadbuddy-client/doc.go`
- Generated and committed: `tools/go/scadbuddy-client/client/**`,
  `tools/go/scadbuddy-client/vendor/**`, `vendor/**`, `go.work.sum`
- Modify:
  - `.dockerignore`: must NOT ignore `vendor`, `tools`, `go.work` or `go.work.sum`; the `bambuddy-fs` image (Task 11) builds from them
  - `.github/workflows/ci.yml:233-234`: the shellcheck glob adds `scripts/*.sh`
  - `CLAUDE.md`: a "Go tools" paragraph under Commands, and the #492 exception line
  - `.gitignore`: make sure nothing under `tools/go/` is ignored
    (`git check-ignore -v tools/go/scadbuddy-client/client/api/x.go` must print nothing)

**Interfaces:**
- Produces:
  - The Go package `github.com/eh-homelab/ScadBuddy/tools/go/scadbuddy-client/client`
    (`NewApiClient(adapter)`, `.Api().V1().Print().Library()...`).
  - `scripts/generate-go-client.sh [--check] [--spec <path>]`. `--spec` skips the
    export and reads a given spec, which is what CI uses with `head.json`.

- [ ] **Step 1: Write the script**

```bash
#!/usr/bin/env bash
# Generates the Go ScadBuddy client (tools/go/scadbuddy-client/client) from the
# backend's OpenAPI spec with Kiota's pinned image, then vendors (spec 2026-10-13 §6).
# Adapted from WonderForgeLabs/forge scripts/generate-kiota-clients.sh.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODULE="$ROOT/tools/go/scadbuddy-client"
OUT="$MODULE/client"
NAMESPACE="github.com/eh-homelab/ScadBuddy/tools/go/scadbuddy-client/client"
KIOTA_IMAGE="mcr.microsoft.com/openapi/kiota:1.36.1@sha256:077073656898de0f8d3339bad496f154d603c30c0cdcbcbc5feacad2ad988d9d"
SPEC_DIR="$ROOT/tools/go/.spec"   # under the /work mount; git-ignored

check=false
spec=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --check) check=true; shift ;;
    --spec) spec="$2"; shift 2 ;;
    *) echo "usage: $0 [--check] [--spec <openapi.json>]" >&2; exit 2 ;;
  esac
done

mkdir -p "$SPEC_DIR"
trap 'rm -rf "$SPEC_DIR"' EXIT
if [[ -z "$spec" ]]; then
  (cd "$ROOT/backend" && uv run --frozen python -m scadbuddy.tools.export_openapi "$SPEC_DIR/raw.json" >/dev/null)
else
  cp "$spec" "$SPEC_DIR/raw.json"
fi

# Kiota's doc comments depend on hash order when a $ref has sibling descriptions;
# dropping them makes generation reproducible (forge #1043).
jq 'walk(if type == "object" and has("$ref") and (has("description") or has("summary")) then del(.description, .summary) else . end)' \
  "$SPEC_DIR/raw.json" > "$SPEC_DIR/openapi.json"

# Delete first: a file the generator no longer emits must show up as a deletion.
rm -rf "$OUT"
docker run --rm -u "$(id -u):$(id -g)" -e HOME=/tmp -v "$ROOT:/work" "$KIOTA_IMAGE" \
  generate -d /work/tools/go/.spec/openapi.json -l Go -o /work/tools/go/scadbuddy-client/client \
  -n "$NAMESPACE" \
  --include-path '/api/v1/print/library' \
  --include-path '/api/v1/print/library/**' \
  --include-path '/api/v1/operations/**' \
  --clean-output >/dev/null

rm -f "$OUT/.kiota.log"
# The hash covers the whole spec; keeping it would churn on every unrelated API change.
jq 'del(.descriptionHash)' "$OUT/kiota-lock.json" > "$OUT/kiota-lock.json.tmp"
mv "$OUT/kiota-lock.json.tmp" "$OUT/kiota-lock.json"

gofmt -w "$OUT"
(cd "$MODULE" && GOWORK=off go mod tidy && GOWORK=off go mod vendor)
(cd "$ROOT" && go work sync && go work vendor)

if [[ "$check" == "true" ]]; then
  drift=$(cd "$ROOT" && git status --porcelain -- tools/go/scadbuddy-client vendor go.work go.work.sum)
  if [[ -n "$drift" ]]; then
    echo "The Go client is stale. Run scripts/generate-go-client.sh and commit:" >&2
    echo "$drift" >&2
    exit 1
  fi
fi
```

Add `tools/go/.spec/` to `.gitignore`.

- [ ] **Step 2: Create the module files**

`tools/go/scadbuddy-client/go.mod`:
```
module github.com/eh-homelab/ScadBuddy/tools/go/scadbuddy-client

go 1.25.0
```

`tools/go/scadbuddy-client/doc.go`:
```go
// Package scadbuddyclient holds the Kiota-generated ScadBuddy API client in ./client,
// generated by scripts/generate-go-client.sh; never edit ./client by hand.
package scadbuddyclient
```

`go.work`:
```
go 1.25.0

toolchain go1.25.6

use (
	./tools/go/scadbuddy-client
)
```

(`./tools/go/bambuddy-fs` is added in Task 6.)

- [ ] **Step 3: Generate, then run it again to prove determinism**

Run:
```
scripts/generate-go-client.sh
git add -A tools/go go.work go.work.sum vendor .gitignore
scripts/generate-go-client.sh --check
```
Expected: the second run prints nothing and exits 0. Also run `shellcheck scripts/*.sh`
and `(cd tools/go/scadbuddy-client && go build ./... && GOWORK=off go build -mod=vendor ./...)`,
both of which must succeed.

- [ ] **Step 4: Update CLAUDE.md**

- **CLAUDE.md.** Under "Generated API files (#492)", add: "The Go client
  `tools/go/scadbuddy-client/client` is the one exception: committed (a vendored Go
  module must build offline), regenerated by `scripts/generate-go-client.sh` (Kiota's
  pinned image; it deletes the folder first), checked and auto-fixed by the
  `freshness` job."
- **Commands.** Add a "Go tools (`tools/go/`)" block listing `go work vendor`, the
  script, and `cd tools/go/<m> && go vet ./... && go test -race ./...`.

- [ ] **Step 5: Commit**

`git commit -m "build(go): workspace and Kiota-generated ScadBuddy client, vendored (#2686)"`

### Task 6: `bambuddy-fs` API layer (Kiota client, 202 polling, errno mapping, streaming)

**Files:**
- Create: `tools/go/bambuddy-fs/go.mod`, `tools/go/bambuddy-fs/internal/api/api.go`,
  `.../api/errors.go`, `.../api/api_test.go`, `.../api/fake_test.go`
- Modify: `go.work` (`use ./tools/go/bambuddy-fs`); regenerate the vendor tree with
  `go work vendor`

**Interfaces:**
- Consumes: the generated client from Task 5.
- Produces (package `api`):
```go
type Folder struct{ ID int; Name string; ParentID int; Children []Folder }
type File struct{ ID int; Name string; FolderID int; Size int64; Created time.Time; External bool }
type Listing struct{ Folders []Folder; Files []File }

type Client struct{ /* base URL, Kiota adapter, http.Client */ }
func New(baseURL string, httpClient *http.Client) (*Client, error) // baseURL like http://host/api/v1
func (c *Client) List(ctx context.Context, folderID int, all bool) (Listing, error) // folderID 0 = top level
func (c *Client) Download(ctx context.Context, fileID int, w io.Writer) error // streamed
func (c *Client) Upload(ctx context.Context, folderID int, name string, r io.Reader) (File, error)
func (c *Client) UpdateFile(ctx context.Context, fileID int, name *string, folderID *int) (File, error)
func (c *Client) DeleteFiles(ctx context.Context, ids ...int) error
func (c *Client) Mkdir(ctx context.Context, parentID int, name string) (Folder, error)
func (c *Client) UpdateFolder(ctx context.Context, folderID int, name *string, parentID *int) (Folder, error)
func (c *Client) DeleteFolder(ctx context.Context, folderID int) error

// Error carries the HTTP status and the problem detail; Errno maps it (spec §9).
type Error struct{ Status int; Detail string; Op string }
func Errno(err error) syscall.Errno
```

Implementation notes:
- **JSON routes** use the generated request builders, e.g.
  `c.kiota.Api().V1().Print().Library().Get(ctx, &cfg)`.
- **Writes** add `Idempotency-Key` (a random 32-hex key per call, kept across retries)
  through the request configuration's `Headers`.
- **202 handling.** Generated `Post`/`Patch` would parse a 202 body as the 200 model.
  So writes build the request with `To<Verb>RequestInformation`, then send it through
  `adapter.SendNoContent(ctx, info, nil)` with an
  `abstractions.NewResponseHandlerOption` whose handler keeps the `*http.Response`.
  The wrapper reads the status:
  - 200: decode the body with the generated model's
    `Create<Model>FromDiscriminatorValue` through `jsonserialization.NewJsonParseNode`.
  - 202: decode `{id}`, then poll `GET /operations/{id}` with the generated builder
    every 250 ms, backing off to 2 s, until `status` is `succeeded` (use `result`) or
    `failed` (return `Error{Status: error.status, Detail: error.detail}`).
  - Otherwise: `Error`.

  Check the exact helper names against the vendored
  `github.com/microsoft/kiota-abstractions-go` before writing them.
- **Download and Upload use `net/http` directly** against the same base URL. The
  generated `Get` on `/{id}/file` returns `[]byte` and would hold a whole 3MF in
  memory; Kiota's Go multipart support does not stream. The paths are
  `/print/library/{id}/file` and `/print/library/files`, the same ScadBuddy routes.
  Upload streams with `io.Pipe` + `multipart.Writer` and sends `Idempotency-Key`. Its
  202 goes through the same poll.

- [ ] **Step 1: Write the fake and the failing tests**

`fake_test.go` holds an `httptest.Server` with an in-memory library:
- A tree of folders.
- Files `{id, name, folder, data, external}`.
- A `queued map[int]bool` (a delete touching one answers 409 with
  `{"detail": "...waiting to print..."}`).
- A `slow map[string]bool` that answers 202 `{id: "op1", status: "running", ...}`
  and finishes on the second `GET /operations/op1`.

It implements every route in the Produces list with the JSON shapes from the spec
and Task 2. It records each request's `Idempotency-Key`.

`api_test.go`:
```go
func TestListMapsFoldersAndFiles(t *testing.T) {
	f := newFake(t)
	f.folder(4, "Top", 0)
	f.file(10, "a.3mf", 4, []byte("abc"), false)
	c := f.client(t)
	l, err := c.List(context.Background(), 4, true)
	if err != nil { t.Fatal(err) }
	if len(l.Files) != 1 || l.Files[0].Name != "a.3mf" || l.Files[0].Size != 3 { t.Fatalf("%+v", l.Files) }
}

func TestA202IsFollowedToItsResult(t *testing.T) {
	f := newFake(t)
	f.slow["mkdir"] = true
	c := f.client(t)
	got, err := c.Mkdir(context.Background(), 0, "Parts")
	if err != nil { t.Fatal(err) }
	if got.Name != "Parts" || f.operationPolls < 2 { t.Fatalf("%+v polls=%d", got, f.operationPolls) }
}

func TestAQueuedFileIsEBUSY(t *testing.T) {
	f := newFake(t)
	f.file(10, "a.3mf", 0, nil, false)
	f.queued[10] = true
	err := f.client(t).DeleteFiles(context.Background(), 10)
	if Errno(err) != syscall.EBUSY { t.Fatalf("errno %v from %v", Errno(err), err) }
}

func TestErrnoMapping(t *testing.T) {
	cases := map[int]syscall.Errno{404: syscall.ENOENT, 409: syscall.EBUSY, 413: syscall.EFBIG, 401: syscall.EACCES, 403: syscall.EACCES, 500: syscall.EIO, 502: syscall.EIO}
	for status, want := range cases {
		if got := Errno(&Error{Status: status}); got != want { t.Errorf("%d: %v, want %v", status, got, want) }
	}
	if Errno(context.DeadlineExceeded) != syscall.EIO { t.Error("timeout is EIO") }
}

func TestDownloadStreamsAndUploadSendsAKey(t *testing.T) {
	f := newFake(t)
	big := bytes.Repeat([]byte("x"), 5<<20)
	f.file(10, "big.3mf", 0, big, false)
	c := f.client(t)
	var got bytes.Buffer
	if err := c.Download(context.Background(), 10, &got); err != nil { t.Fatal(err) }
	if !bytes.Equal(got.Bytes(), big) { t.Fatal("download differs") }
	if _, err := c.Upload(context.Background(), 0, "new.3mf", bytes.NewReader([]byte("PK"))); err != nil { t.Fatal(err) }
	if f.lastKey == "" { t.Fatal("no Idempotency-Key on upload") }
}
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `cd tools/go/bambuddy-fs && go test ./internal/api/...`
Expected: FAIL (undefined: New).

- [ ] **Step 3: Implement `api.go` and `errors.go` as specified above**

`errors.go`:
```go
func Errno(err error) syscall.Errno {
	if err == nil { return 0 }
	var e *Error
	if errors.As(err, &e) {
		switch {
		case e.Status == 404: return syscall.ENOENT
		case e.Status == 409: return syscall.EBUSY
		case e.Status == 413: return syscall.EFBIG
		case e.Status == 401 || e.Status == 403: return syscall.EACCES
		}
	}
	return syscall.EIO
}
```

ScadBuddy maps a Bambuddy 403 to a 409 that carries `required_scope` (see
`test_a_key_without_manage_library_names_the_scope`). So in `Error`'s decoder, a 409
with `required_scope` in the body sets `Status = 403`, so that it maps to `EACCES`.
Add that as a case in `TestErrnoMapping`, decoding a body.

- [ ] **Step 4: Run the tests and vet**

Run: `cd tools/go/bambuddy-fs && go vet ./... && go test -race ./...`, then from the
root `go work vendor && git status --porcelain vendor`.
Expected: PASS. Vendor changes are staged with the commit.

- [ ] **Step 5: Commit**

`git commit -m "feat(bambuddy-fs): ScadBuddy API layer over the Kiota client (#2686)"`

### Task 7: Read-only FUSE tree (naming, inodes, cache)

**Files:**
- Create: `tools/go/bambuddy-fs/internal/fsys/names.go`, `names_test.go`, `tree.go`,
  `cache.go`, `mount_test.go`
- Modify: `tools/go/bambuddy-fs/go.mod` (`github.com/hanwen/go-fuse/v2 v2.12.0`),
  then `go work vendor`

**Interfaces:**
- Consumes: the `api.Client` methods (through an interface, so tests can use the fake).
- Produces:
```go
type Library interface {
	List(ctx context.Context, folderID int, all bool) (api.Listing, error)
	Download(ctx context.Context, fileID int, w io.Writer) error
	// write methods are added in Task 8
}
type Options struct{ All, ReadOnly bool; TTL time.Duration; CacheDir string }
func NewRoot(lib Library, opts Options) fs.InodeEmbedder
func Names(files []api.File) map[string]api.File // display name → file (spec §3)
```

- [ ] **Step 1: Write the failing tests**

`names_test.go`:
```go
func TestDuplicateNamesGetTheirID(t *testing.T) {
	t0 := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	files := []api.File{
		{ID: 12, Name: "a.3mf", Created: t0.Add(time.Hour)},
		{ID: 11, Name: "a.3mf", Created: t0},
		{ID: 13, Name: "b.stl", Created: t0},
	}
	got := Names(files)
	if got["a.3mf"].ID != 11 { t.Fatalf("oldest keeps the plain name: %+v", got) }
	if got["a (id 12).3mf"].ID != 12 { t.Fatalf("later one is suffixed: %+v", got) }
	if got["b.stl"].ID != 13 { t.Fatal("unique name kept") }
}

func TestANameWithoutExtensionIsSuffixedAtTheEnd(t *testing.T) {
	got := Names([]api.File{{ID: 1, Name: "README"}, {ID: 2, Name: "README"}})
	if _, ok := got["README (id 2)"]; !ok { t.Fatalf("%v", got) }
}
```

`mount_test.go` uses a real mount and skips without `/dev/fuse`:
```go
func mount(t *testing.T, lib Library, opts Options) string {
	t.Helper()
	if _, err := os.Stat("/dev/fuse"); err != nil { t.Skip("no /dev/fuse") }
	dir := t.TempDir()
	opts.CacheDir = t.TempDir()
	server, err := fs.Mount(dir, NewRoot(lib, opts), &fs.Options{})
	if err != nil { t.Skipf("cannot mount here: %v", err) }
	t.Cleanup(func() { _ = server.Unmount() })
	return dir
}

func TestReadOnlyTree(t *testing.T) {
	lib := newFakeLibrary() // in-package fake implementing Library
	lib.folder(4, "Top", 0)
	lib.file(10, "a.3mf", 4, []byte("hello"))
	lib.file(11, "a.3mf", 4, []byte("world"))
	dir := mount(t, lib, Options{TTL: 0})
	entries, err := os.ReadDir(filepath.Join(dir, "Top"))
	if err != nil { t.Fatal(err) }
	if len(entries) != 2 { t.Fatalf("%v", entries) }
	data, err := os.ReadFile(filepath.Join(dir, "Top", "a.3mf"))
	if err != nil || string(data) != "hello" { t.Fatalf("%q %v", data, err) }
	info, _ := os.Stat(filepath.Join(dir, "Top", "a (id 11).3mf"))
	if info.Size() != 5 { t.Fatalf("size %d", info.Size()) }
}

func TestAFileGoneFromTheLibraryIsENOENT(t *testing.T) {
	lib := newFakeLibrary()
	lib.file(10, "a.3mf", 0, []byte("x"))
	dir := mount(t, lib, Options{TTL: 0})
	lib.remove(10)
	if _, err := os.Stat(filepath.Join(dir, "a.3mf")); !errors.Is(err, os.ErrNotExist) { t.Fatalf("%v", err) }
}
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `cd tools/go/bambuddy-fs && go test ./internal/fsys/...`
Expected: FAIL (undefined: Names).

- [ ] **Step 3: Implement**

- **`Names`.** Sort a name's files by `(Created, ID)`. The first keeps the name; the
  rest become `stem + " (id N)" + ext`, where `ext` is everything from the first `.`
  of the base name that is not at index 0 (so `a.gcode.3mf` → `a (id N).gcode.3mf`).
- **`tree.go`.**
  - A directory node per folder (`fs.Inode` stable ID: `uint64(folderID)<<1`) and a
    file node per file (`uint64(fileID)<<1 | 1`).
  - `Lookup`/`Readdir` call `List` with `opts.All`, and cache the listing per folder
    for `TTL`.
  - Set `EntryTimeout`/`AttrTimeout` to `TTL` in the mount options (Task 9).
  - The top level is folder 0: `List(ctx, 0, all)`, whose `Folders` field is the tree.
    A folder's subdirectories are its `Children`.
  - `Getattr`: files `0444` (`0644` unless ReadOnly once Task 8 lands), dirs `0555`
    (`0755`); `Size`; `Mtime=Ctime=Created`.
- **`cache.go`.**
  - `Open` downloads to `<CacheDir>/<id>.part` and renames it to `<id>` once the size
    matches `Size`. It reuses an existing `<id>` whose size matches.
  - `Read` uses `ReadAt` on the cached file.
  - A 404 from Download drops the cache entry and the inode (`NotifyEntry` on the
    parent) and returns `ENOENT`.
- **Errors.** Every error returned to FUSE goes through `api.Errno`.

- [ ] **Step 4: Run the tests**

Run: `cd tools/go/bambuddy-fs && go vet ./... && go test -race ./...`
Expected: PASS. Mount tests skip without `/dev/fuse`; this machine has it, so they run.

- [ ] **Step 5: Commit**

`git commit -m "feat(bambuddy-fs): read-only FUSE tree of the library (#2686)"`

### Task 8: Writes (create/write/release, replace, unlink, mkdir, rename, rmdir, --ro)

**Files:**
- Modify: `tools/go/bambuddy-fs/internal/fsys/tree.go`; create
  `tools/go/bambuddy-fs/internal/fsys/write.go`, `write_test.go`
- Modify: `Library` (add the write methods with the `api.Client` signatures)

**Interfaces:**
- Produces: `Library` gains `Upload`, `UpdateFile`, `DeleteFiles`, `Mkdir`,
  `UpdateFolder` and `DeleteFolder`, with exactly Task 6's signatures.

- [ ] **Step 1: Write the failing mount tests**

```go
func TestWriteUploadsOnceOnClose(t *testing.T) {
	lib := newFakeLibrary()
	dir := mount(t, lib, Options{})
	f, err := os.Create(filepath.Join(dir, "new.3mf"))
	if err != nil { t.Fatal(err) }
	for i := 0; i < 3; i++ { _, _ = f.Write([]byte("abc")); _ = f.Sync() }
	if err := f.Close(); err != nil { t.Fatal(err) }
	if lib.uploads != 1 { t.Fatalf("uploads %d", lib.uploads) }
	data, _ := os.ReadFile(filepath.Join(dir, "new.3mf"))
	if string(data) != "abcabcabc" { t.Fatalf("%q", data) }
}

func TestOverwritingANameReplacesTheFile(t *testing.T) {
	lib := newFakeLibrary()
	lib.file(10, "a.3mf", 0, []byte("old"))
	dir := mount(t, lib, Options{})
	if err := os.WriteFile(filepath.Join(dir, "a.3mf"), []byte("new"), 0o644); err != nil { t.Fatal(err) }
	if !lib.deleted[10] { t.Fatal("old file not deleted after the upload") }
	data, _ := os.ReadFile(filepath.Join(dir, "a.3mf"))
	if string(data) != "new" { t.Fatalf("%q", data) }
}

func TestRenameOntoAnExistingNameIsEEXIST(t *testing.T) {
	lib := newFakeLibrary()
	lib.file(10, "a.3mf", 0, nil)
	lib.file(11, "b.3mf", 0, nil)
	dir := mount(t, lib, Options{})
	err := os.Rename(filepath.Join(dir, "a.3mf"), filepath.Join(dir, "b.3mf"))
	if !errors.Is(err, syscall.EEXIST) { t.Fatalf("%v", err) }
}

func TestMoveAndMkdir(t *testing.T) {
	lib := newFakeLibrary()
	lib.file(10, "a.3mf", 0, nil)
	dir := mount(t, lib, Options{})
	if err := os.Mkdir(filepath.Join(dir, "Parts"), 0o755); err != nil { t.Fatal(err) }
	if err := os.Rename(filepath.Join(dir, "a.3mf"), filepath.Join(dir, "Parts", "c.3mf")); err != nil { t.Fatal(err) }
	if lib.files[10].Name != "c.3mf" || lib.files[10].FolderID == 0 { t.Fatalf("%+v", lib.files[10]) }
}

func TestUnlinkAndRmdirOfAFullFolder(t *testing.T) {
	lib := newFakeLibrary()
	lib.folder(4, "Top", 0)
	lib.file(10, "a.3mf", 4, nil)
	dir := mount(t, lib, Options{})
	if err := os.Remove(filepath.Join(dir, "Top")); err != nil { t.Fatalf("rmdir of a full folder: %v", err) }
	if !lib.folderDeleted[4] { t.Fatal("folder delete not called") }
	if _, err := os.Stat(filepath.Join(dir, "Top")); !errors.Is(err, os.ErrNotExist) { t.Fatal("still listed") }
}

func TestRmdirRefusedByAQueuedPrintIsEBUSYAndKeepsEverything(t *testing.T) {
	lib := newFakeLibrary()
	lib.folder(4, "Top", 0)
	lib.file(10, "a.3mf", 4, nil)
	lib.busy[10] = true
	dir := mount(t, lib, Options{})
	err := os.Remove(filepath.Join(dir, "Top"))
	if !errors.Is(err, syscall.EBUSY) { t.Fatalf("%v", err) }
	if _, err := os.Stat(filepath.Join(dir, "Top", "a.3mf")); err != nil { t.Fatal("file gone after a refused rmdir") }
}

func TestReadOnlyRefusesWrites(t *testing.T) {
	lib := newFakeLibrary()
	lib.file(10, "a.3mf", 0, nil)
	dir := mount(t, lib, Options{ReadOnly: true})
	if err := os.WriteFile(filepath.Join(dir, "x.3mf"), nil, 0o644); !errors.Is(err, syscall.EROFS) { t.Fatalf("%v", err) }
	if err := os.Remove(filepath.Join(dir, "a.3mf")); !errors.Is(err, syscall.EROFS) { t.Fatalf("%v", err) }
}

func TestChmodIsENOTSUP(t *testing.T) {
	lib := newFakeLibrary()
	lib.file(10, "a.3mf", 0, nil)
	dir := mount(t, lib, Options{})
	if err := os.Chmod(filepath.Join(dir, "a.3mf"), 0o600); !errors.Is(err, syscall.ENOTSUP) { t.Fatalf("%v", err) }
}
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `cd tools/go/bambuddy-fs && go test ./internal/fsys/ -run 'Write|Overwrit|Rename|Move|Unlink|Rmdir|ReadOnly|Chmod' -v`
Expected: FAIL.

- [ ] **Step 3: Implement `write.go`**

- **`Create`** returns a handle on a temp file under `CacheDir`. `Write`/`Flush`
  touch only the temp file until `Flush` finds unsent writes: it uploads once (Upload
  with the parent folder ID and name) and returns the error to `close()`. `Release`
  uploads only what is still unsent. A mount test makes `Upload` fail and checks that
  `f.Close()` returns the error. If the name existed before, it then calls `DeleteFiles(oldID)`. It then
  invalidates the parent's listing (`NotifyEntry`) and moves the temp file into the
  cache as `<newID>`.
- **`Open` with `O_TRUNC|O_WRONLY` or `O_RDWR`** on an existing file starts the same
  temp-file handle, seeded with the cached content unless truncating. `Setattr` with
  a size is allowed only on a write handle; anything else is `ENOTSUP`.
- **`Unlink`** → `DeleteFiles(id)`. **`Mkdir`** → `Mkdir`. **`Rmdir`** →
  `DeleteFolder`. **`Rename`**: refuse if the target name exists (`EEXIST`), then call
  `UpdateFile`/`UpdateFolder` with the new name and/or parent. Folder 0 is the top
  level, so send `0`. Each success invalidates the affected parents' listings.
- **`ReadOnly`**: every one of these returns `EROFS`. Getattr modes drop the write
  bits.
- **Errors** map through `api.Errno`. Log each failure with the operation and
  `Error.Detail` (`log/slog`).

- [ ] **Step 4: Run the tests**

Run: `cd tools/go/bambuddy-fs && go vet ./... && go test -race ./...`
Expected: PASS.

- [ ] **Step 5: Commit**

`git commit -m "feat(bambuddy-fs): writes, renames, folder deletes and --ro (#2686)"`

### Task 9: The CLI

**Files:**
- Create: `tools/go/bambuddy-fs/main.go`, `tools/go/bambuddy-fs/main_test.go`,
  `tools/go/bambuddy-fs/README.md`
- Modify: root `README.md`: a short "Mounting the library (bambuddy-fs)" section
  linking to it

**Interfaces:**
- Consumes: `api.New`, `fsys.NewRoot`, `fsys.Options`.
- Produces:
  `bambuddy-fs mount --api <url> [--ro] [--all] [--allow-other] [--attr-ttl 5s] [--cache-dir <dir>] <mountpoint>`. `--allow-other` sets go-fuse's `MountOptions.AllowOther`.
  The `--cache-dir` default is `$XDG_CACHE_HOME/bambuddy-fs` (or
  `~/.cache/bambuddy-fs`). It unmounts on SIGINT/SIGTERM.

- [ ] **Step 1: Write the failing test**

```go
func TestParseFlags(t *testing.T) {
	cfg, err := parse([]string{"mount", "--api", "http://h/api/v1", "--ro", "--attr-ttl", "2s", "/mnt/x"})
	if err != nil { t.Fatal(err) }
	if cfg.API != "http://h/api/v1" || !cfg.ReadOnly || cfg.TTL != 2*time.Second || cfg.Mountpoint != "/mnt/x" { t.Fatalf("%+v", cfg) }
	if _, err := parse([]string{"mount", "/mnt/x"}); err == nil { t.Fatal("--api is required") }
}
```

- [ ] **Step 2: Run the test and confirm it fails**

Run: `cd tools/go/bambuddy-fs && go test . -run TestParseFlags`
Expected: FAIL.

- [ ] **Step 3: Implement**

- **`parse([]string) (config, error)`** uses `flag.NewFlagSet("mount", flag.ContinueOnError)`.
- **`main`:**
  - `fs.Mount(mountpoint, root, &fs.Options{EntryTimeout: &ttl, AttrTimeout: &ttl, MountOptions: fuse.MountOptions{FsName: "bambuddy", Name: "bambuddy-fs"}})`
  - `signal.NotifyContext`, then `server.Unmount()` on a signal, then
    `server.Wait()`.
- **README.md**:
  - install: `go install github.com/eh-homelab/ScadBuddy/tools/go/bambuddy-fs@latest`,
    or build from the workspace
  - needs `fuse3`
  - the flags
  - running the container image (spec §7.1), with its `docker run` line
  - the delete semantics, verbatim from spec §5 (managed files to the trash, external
    files gone for good; `rmdir` takes the folder's contents; 409 is `EBUSY`)
  - the errno table from spec §9

- [ ] **Step 4: Build and try it against a local ScadBuddy, if one is running**

Run:
```
cd tools/go/bambuddy-fs
go build -o /tmp/bambuddy-fs .
/tmp/bambuddy-fs mount --api http://localhost:8000/api/v1 --ro /tmp/bfs & sleep 2
ls /tmp/bfs
fusermount3 -u /tmp/bfs
```
Expected: the folder tree lists. Skip this step if no backend is running, and say so
in the task report.

- [ ] **Step 5: Commit**

`git commit -m "feat(bambuddy-fs): the mount command (#2686)"`

### Task 10: CI: `go-module-ci.yml`, the `go` job, freshness client generation, Dependabot

**Files:**
- Create: `.github/workflows/go-module-ci.yml`, `.github/scripts/select-go.sh`,
  `.github/scripts/select-go.test.sh`
- Modify:
  - `.github/workflows/ci.yml`:
    - a new `go` job
    - `summary.needs` and its asserts (`:1643-1752`)
    - `dispatch-review.needs` (`:1768`)
    - the `freshness` job (`:1406-1640`): permissions and the new steps
  - `.github/dependabot.yml`: a gomod entry

**Interfaces:**
- Consumes: `scripts/generate-go-client.sh [--check] [--spec <path>]` from Task 5.
- Produces:
  - `select-go.sh`, which reads changed paths on stdin and prints `true` or `false`.
  - The `go` job, displayed as "Go modules", which is always required in CI Summary.

- [ ] **Step 1: Write the failing selector test**

`.github/scripts/select-go.test.sh`, in the style of
`.github/scripts/select-models.test.sh`:
```bash
#!/usr/bin/env bash
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
fail=0
expect() { local want="$1"; shift; got=$(printf '%s\n' "$@" | "$here/select-go.sh"); [[ "$got" == "$want" ]] || { echo "FAIL: $* -> $got, want $want"; fail=1; }; }
expect true  tools/go/bambuddy-fs/main.go
expect true  go.work
expect true  go.work.sum
expect true  vendor/modules.txt
expect true  scripts/generate-go-client.sh
expect true  .github/workflows/go-module-ci.yml
expect true  tools/go/bambuddy-fs/Dockerfile
expect false Dockerfile
expect false backend/scadbuddy/api/library_print.py
expect false README.md
expect false toolsy/go.mod
exit "$fail"
```

Add it wherever `.github/scripts/*.test.sh` are run in the lint job (`grep -n "test.sh" .github/workflows/ci.yml`).

- [ ] **Step 2: Run the test and confirm it fails**

Run: `bash .github/scripts/select-go.test.sh`
Expected: FAIL (no such file).

- [ ] **Step 3: Write `select-go.sh`**

```bash
#!/usr/bin/env bash
# Prints true when any changed path on stdin is Go's (spec 2026-10-13 §7).
set -euo pipefail
if grep -qE '^(go\.work(\.sum)?|vendor/|tools/go/|scripts/generate-go-client\.sh$|\.github/workflows/go-module-ci\.yml$)'; then
  echo true
else
  echo false
fi
```

- [ ] **Step 4: Write `go-module-ci.yml`**

Copy forge's `go-module-ci.yml` (quoted in the design conversation;
`WonderForgeLabs/forge` `.github/workflows/go-module-ci.yml` at `origin/main`) with
these changes:
- **Inputs:** only `run` (string `"true"`/`"false"`). There is no `runner-label` and
  no `modules` input: the job discovers `tools/go/*/go.mod` itself.
- **`runs-on: ubuntu-latest`**, `timeout-minutes: 20`, `permissions: contents: read`.
- **A step "No Go changes"**, `if: inputs.run != 'true'`, writes `No Go changes in
  this PR; nothing to check.` to `$GITHUB_STEP_SUMMARY`.
- **Every other step** has `if: inputs.run == 'true'`:
  - checkout
  - `actions/setup-go@v6` with `go-version-file: go.work`
  - `sudo apt-get install -y fuse3`
  - forge's module loop (gofmt, vet, build, `test -race`, `GOWORK=off go build -mod=vendor`)
  - `go work vendor` drift via `git status --porcelain -- vendor go.work go.work.sum 'tools/go/*/go.mod' 'tools/go/*/go.sum'`
- **`GOTOOLCHAIN`** is `go1.25.6+auto`, matching `go.work`'s `toolchain`.
- **No coverage upload.**

- [ ] **Step 5: Add the `go` job to `ci.yml`**

```yaml
  go-select:
    name: Select Go work
    runs-on: ubuntu-latest
    timeout-minutes: 5
    outputs:
      run: ${{ steps.select.outputs.run }}
    steps:
      - uses: actions/checkout@v7
        with:
          fetch-depth: 2
      - name: Did this change touch Go?
        id: select
        env:
          EVENT_NAME: ${{ github.event_name }}
          QUEUE_BASE: ${{ github.event.merge_group.base_sha }}
        run: |
          set -euo pipefail
          run=true
          if [ "$EVENT_NAME" = "pull_request" ] \
            && git rev-parse -q --verify 'HEAD^2' >/dev/null \
            && git diff --name-only 'HEAD^1' HEAD >"$RUNNER_TEMP/changed.txt"; then
            run=$(.github/scripts/select-go.sh <"$RUNNER_TEMP/changed.txt")
          elif [ "$EVENT_NAME" = "merge_group" ] \
            && git fetch -q --depth=1 origin "$QUEUE_BASE" \
            && git diff --name-only "$QUEUE_BASE" HEAD >"$RUNNER_TEMP/changed.txt"; then
            run=$(.github/scripts/select-go.sh <"$RUNNER_TEMP/changed.txt")
          fi
          echo "run=$run" | tee -a "$GITHUB_OUTPUT"

  go:
    name: Go modules
    needs: [go-select]
    uses: ./.github/workflows/go-module-ci.yml
    with:
      run: ${{ needs.go-select.outputs.run }}
```

In `summary`:
- add `go-select, go` to `needs`
- add `GO_SELECT_RESULT: ${{ needs.go-select.result }}` and
  `GO_RESULT: ${{ needs.go.result }}`
- add `assert "Select Go work" "$GO_SELECT_RESULT" true` and
  `assert "Go modules" "$GO_RESULT" true`

Add `go` to `dispatch-review.needs`.

- [ ] **Step 6: Add client generation to `freshness`**

The changes:

- **Permissions.** The job's `permissions` become
  `contents: write, pull-requests: write`. The app token step already exists; it
  asks for `permission-contents: write`, and the `RUNNER_APP` installation must hold
  `contents: write` on this repo (check before enabling: it only commented until now).
- **After the merge (#3053).** A `go-client` job runs `scripts/generate-go-client.sh
  --check` on every non-PR run (push to main, schedule); `CI Summary` requires it
  there. Two PRs that each regenerate cleanly can still land a stale client.
- **Determinism and freshness step.** After "Diff the API against the base", add:

```yaml
      - name: Set up Go for the client
        if: ${{ steps.detect.outputs.backend == 'true' }}
        uses: actions/setup-go@v6
        with:
          go-version-file: go.work

      - name: Go client is deterministic and fresh
        id: goclient
        if: ${{ steps.detect.outputs.backend == 'true' }}
        env:
          LINES: ${{ steps.diff.outputs.changed_lines }}
          BASE_SHA: ${{ github.event.pull_request.base.sha }}
        run: |
          set -euo pipefail
          git fetch --no-tags --depth=1 origin "$BASE_SHA"
          # `--quiet`, not `| head -1`: under pipefail a SIGPIPE'd git fails the step.
          if [ "${LINES:-0}" = "0" ] \
            && git diff --quiet "$BASE_SHA" HEAD -- tools/go/scadbuddy-client scripts/generate-go-client.sh; then
            echo "state=unchanged" | tee -a "$GITHUB_OUTPUT"; exit 0
          fi
          scripts/generate-go-client.sh --spec "$RUNNER_TEMP/head.json"
          cp -a tools/go/scadbuddy-client/client "$RUNNER_TEMP/client-1"
          scripts/generate-go-client.sh --spec "$RUNNER_TEMP/head.json"
          if ! diff -r "$RUNNER_TEMP/client-1" tools/go/scadbuddy-client/client >/dev/null; then
            echo "::error::Two generations of the Go client differ; Kiota output is not a function of the spec."
            exit 1
          fi
          if [ -z "$(git status --porcelain -- tools/go/scadbuddy-client vendor go.work go.work.sum)" ]; then
            echo "state=fresh" | tee -a "$GITHUB_OUTPUT"
          else
            git diff --stat -- tools/go/scadbuddy-client vendor > "$RUNNER_TEMP/client.stat" || true
            echo "state=stale" | tee -a "$GITHUB_OUTPUT"
          fi

      - name: Push the regenerated Go client
        id: goclient-push
        if: ${{ steps.goclient.outputs.state == 'stale' && env.CAN_COMMENT == 'true' }}
        env:
          GH_TOKEN: ${{ steps.app-token.outputs.token }}
          HEAD_REF: ${{ github.event.pull_request.head.ref }}
          REPO: ${{ github.repository }}
        run: |
          set -euo pipefail
          git fetch --no-tags --depth=1 "https://x-access-token:${GH_TOKEN}@github.com/${REPO}.git" "$HEAD_REF"
          git worktree add -B regen "$RUNNER_TEMP/head" FETCH_HEAD
          (cd "$RUNNER_TEMP/head" && scripts/generate-go-client.sh --spec "$RUNNER_TEMP/head.json")
          cd "$RUNNER_TEMP/head"
          git add -A tools/go/scadbuddy-client vendor go.work go.work.sum
          git -c user.name="scadbuddy-ci" -c user.email="ci@scadbuddy.invalid" \
            commit -m "chore: regenerate Kiota client" -m "Generated by the freshness job from this PR's API spec."
          git push "https://x-access-token:${GH_TOKEN}@github.com/${REPO}.git" "HEAD:${HEAD_REF}"
          echo "sha=$(git rev-parse --short HEAD)" | tee -a "$GITHUB_OUTPUT"
```

  - **Regenerating from `head.json` on the head ref.** `head.json` is exported from
    the merge ref, which is the API that will land. Generating from it on the head
    ref is the intended result.
  - **Pushing.** The push uses the app token, so `pull_request` workflows run on the
    new commit. Remove the token from the worktree's remote config afterwards; it is
    never written there, because the push names the URL.
  - **Failing.** A final step fails the job when `state == 'stale'`, with `::error::`.
    It says either "regenerated in <sha>; the next run verifies" or, without a token,
    "run scripts/generate-go-client.sh and commit".
- **Diff comment.** Extend the "Post the API diff on the PR" body with a Go client
  section after the API diff, written from `steps.goclient.outputs.state`, the push
  step's `sha` and `$RUNNER_TEMP/client.stat`:
  - "### Go client"
  - then "Up to date.", "Unchanged by this PR.", or "Regenerated in `<sha>`" with the
    stat in a `<details>`.

  The step's `if` is unchanged. Pass the new values through `env`.

- [ ] **Step 7: Add Dependabot**

Add to `.github/dependabot.yml`, copying the uv entry's conventions:
```yaml
  - package-ecosystem: "gomod"
    directories:
      - "/tools/go/scadbuddy-client"
      - "/tools/go/bambuddy-fs"
    schedule:
      interval: "weekly"
      day: "monday"
      time: "04:00"
    open-pull-requests-limit: 10
    reviewers:
      - "ElanHasson"
    commit-message:
      prefix: "deps"
      include: "scope"
    labels: ["dependencies", "gomod"]
    groups:
      kiota:
        patterns: ["github.com/microsoft/kiota-*"]
      go-fuse:
        patterns: ["github.com/hanwen/go-fuse/*"]
```

A Dependabot gomod bump does not run `go work vendor` itself. Task 11's
`vendor-autofix.yml` (from gooey) commits it.

- [ ] **Step 8: Lint and run the script tests**

Run:
```
actionlint
shellcheck .github/scripts/*.sh scripts/*.sh
bash .github/scripts/select-go.test.sh
```
Expected: clean, and the selector test passes.

- [ ] **Step 9: Commit, push, open PR B**

`git commit -m "ci(go): Go module checks, Go client freshness in the API diff job, Dependabot (#2686)"`

Push and open PR B: base is PR A's branch, title as above, body `Fixes #2686` plus a
summary and the attribution footer. Watch CI. The `freshness` job should report
"Up to date" for the Go client, and "Go modules" should run, because this PR touches
`tools/go/**`.

### Task 11: The container image and vendor autofix (forge's and gooey's patterns)

**Files:**
- Create:
  - `tools/go/bambuddy-fs/Dockerfile`
  - `.github/workflows/build-go-image.yml` (from forge's)
  - `.github/workflows/vendor-autofix.yml` (from gooey's)
- Modify:
  - `.github/workflows/go-module-ci.yml`: gooey's gofmt step, plus image build and
    smoke steps
  - `.github/workflows/ci.yml:218-222`: hadolint also covers `tools/go/*/Dockerfile`
  - `tools/go/bambuddy-fs/README.md`: a "Container image" section

**Interfaces:**
- Consumes: the workspace vendor tree (Task 5) and `main.go` (Task 9).
- Produces: the image `ghcr.io/eh-homelab/scadbuddy-bambuddy-fs`.

Read spec §7.1 and §7.2 first. The sources to copy, from local checkouts:
- `git -C ~/repos/WonderForgeLabs/forge show origin/main:.github/workflows/build-go-image.yml`
- `git -C ~/repos/WonderForgeLabs/forge show origin/main:tools/go/envctl/Dockerfile`
- `git -C ~/repos/WonderForgeLabs/gooey show origin/main:.github/workflows/vendor-autofix.yml`
- `git -C ~/repos/WonderForgeLabs/gooey show origin/main:.github/workflows/ci.yml`
  (the "gofmt" step, lines 88-112)

- [ ] **Step 1: The Dockerfile**

```dockerfile
# bambuddy-fs (#2686, spec 2026-10-13 §7.1): the library as a FUSE filesystem.
# Built with the repo root as context (it needs the workspace and the root vendor/):
#   docker build -f tools/go/bambuddy-fs/Dockerfile .
# Published by .github/workflows/build-go-image.yml as ghcr.io/eh-homelab/scadbuddy-bambuddy-fs.
FROM --platform=$BUILDPLATFORM golang:1.25.6-bookworm AS build
ARG TARGETOS
ARG TARGETARCH
WORKDIR /src
COPY go.work go.work.sum ./
COPY vendor/ vendor/
COPY tools/go/ tools/go/
RUN CGO_ENABLED=0 GOOS=$TARGETOS GOARCH=$TARGETARCH GOFLAGS=-mod=vendor \
    go build -trimpath -ldflags="-s -w" -o /out/bambuddy-fs ./tools/go/bambuddy-fs

# Not distroless: FUSE needs fusermount3, and mounting needs root in a container.
FROM debian:bookworm-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends fuse3 ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /out/bambuddy-fs /usr/local/bin/bambuddy-fs
ENTRYPOINT ["bambuddy-fs"]
CMD ["mount", "--help"]
```

- The golang tag must equal `go.work`'s `toolchain`.
- `hadolint tools/go/bambuddy-fs/Dockerfile` must pass with `.hadolint.yaml`.
- For apt pins and `DL3008`, follow the root Dockerfile's existing practice.

Run:
```
docker build -f tools/go/bambuddy-fs/Dockerfile -t scadbuddy-bambuddy-fs:dev .
docker run --rm scadbuddy-bambuddy-fs:dev --help
docker buildx build --platform linux/arm64 -f tools/go/bambuddy-fs/Dockerfile .
```
Expected: `--help` exits 0 (make `main` do so), and the arm64 build succeeds.

- [ ] **Step 2: `build-go-image.yml`**

Copy forge's file, with these changes:
- `runs-on: ubuntu-latest` everywhere; drop the runner-pool overflow step and its
  sparse checkout.
- **Triggers:** push to main with `paths: [tools/go/**, go.work, go.work.sum, vendor/**, .github/workflows/build-go-image.yml]`,
  `tags: ["v*"]`, and `workflow_dispatch` with `commit_sha`.
- **Discovery** stays the same: every `tools/go/*/main.go` needs a `Dockerfile`.
- **Build:**
  - `docker/setup-qemu-action` + `docker/setup-buildx-action`
  - `docker/build-push-action` with `context: .`, `file: ${{ matrix.module }}/Dockerfile`,
    `platforms: linux/amd64,linux/arm64`, `push: true`
  - `cache-from: type=gha,scope=go-<name>`, `cache-to: type=gha,scope=go-<name>,mode=min`
- **Tags:** `ghcr.io/eh-homelab/scadbuddy-<name>:sha-<sha>` and `:latest`, plus
  `:<version>` (the tag without its `v`) on a `v*` tag.
- `permissions: contents: read, packages: write`.
- Pin action versions to the ones `build-image.yml` uses.

- [ ] **Step 3: Lint and smoke in CI**

- **`go-module-ci.yml`.** Replace forge's per-module gofmt with gooey's whole-tree
  gofmt step. Lower its "too few files" floor from 100 to 5, since this tree is
  small. After the module loop, add:

```bash
for d in tools/go/*/; do
  [ -f "$d/main.go" ] || continue
  name=$(basename "$d")
  docker build -f "$d/Dockerfile" -t "go-$name:ci" .
  docker run --rm "go-$name:ci" --help
done
```

  All of these steps carry `if: inputs.run == 'true'`.
- **`ci.yml` hadolint.** `hadolint/hadolint-action` takes one `dockerfile`. Add a
  second step with `dockerfile: tools/go/bambuddy-fs/Dockerfile`, or switch to a
  `recursive: true` run, if that keeps the root's config.

- [ ] **Step 4: `vendor-autofix.yml`**

Copy gooey's file, with these changes:
- `runs-on: ubuntu-latest`
- `actions/setup-go@v6` with `go-version-file: go.work`
- the run step is `go work vendor && (cd tools/go/scadbuddy-client && GOWORK=off go mod vendor)`
- `git add -A -- vendor tools/go/scadbuddy-client/vendor go.work.sum '*go.sum'`
- **Push** with an app token from `actions/create-github-app-token@v3`
  (`client-id: ${{ secrets.RUNNER_APP_ID }}`, `private-key: ${{ secrets.RUNNER_APP_PRIVATE_KEY }}`,
  as `ci.yml:1437` does), passed to checkout's `token`. CI then re-runs on the
  pushed head. Replace gooey's closing `::notice::` accordingly.

Guard on `github.event.pull_request.user.login == 'dependabot[bot]'` (the PR's author;
`github.actor` is whoever triggered the run) and head repo == this repo, and keep
its `pull_request_target` trigger with gooey's `paths`. It checks out the PR head by
SHA and runs only `go`, so no PR code executes.

- [ ] **Step 5: Lint and commit**

Run `actionlint`, `hadolint tools/go/bambuddy-fs/Dockerfile` and
`shellcheck .github/scripts/*.sh scripts/*.sh`.

`git commit -m "build(bambuddy-fs): publish Go CLIs as images; gofmt annotations; vendor autofix (#2686)"`
