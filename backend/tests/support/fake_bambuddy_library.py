"""A Bambuddy that has only a print library, in memory: the routes `BambuddyClient`'s
library methods call, and the empty print queue ``refuse_queued`` reads, each answering
the least those methods parse. CI's bambuddy-fs live mount runs ScadBuddy on the
``bambuddy`` library backend against it (`.github/scripts/bambuddy-fs-live.sh`):

    python -m tests.support.fake_bambuddy_library --port 18092
"""

from __future__ import annotations

import argparse
import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import Response


@dataclass
class _File:
    id: int
    filename: str
    folder_id: int | None
    data: bytes
    created_at: str
    trashed: bool = False

    def row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "filename": self.filename,
            "file_type": _file_type(self.filename),
            "folder_id": self.folder_id,
            "file_size": len(self.data),
            "file_hash": hashlib.sha256(self.data).hexdigest(),
            "created_at": self.created_at,
        }


@dataclass
class _Folder:
    id: int
    name: str
    parent_id: int | None


@dataclass
class _Library:
    files: dict[int, _File] = field(default_factory=dict)
    folders: dict[int, _Folder] = field(default_factory=dict)
    next_id: int = 1

    def new_id(self) -> int:
        self.next_id += 1
        return self.next_id - 1

    def file(self, file_id: int, *, trashed: bool = False) -> _File:
        found = self.files.get(file_id)
        if found is None or found.trashed != trashed:
            raise HTTPException(404, "File not found")
        return found

    def folder(self, folder_id: int) -> _Folder:
        if folder_id not in self.folders:
            raise HTTPException(404, "Folder not found")
        return self.folders[folder_id]

    def folder_row(self, folder: _Folder) -> dict[str, Any]:
        return {
            "id": folder.id,
            "name": folder.name,
            "parent_id": folder.parent_id,
            "file_count": sum(
                1 for f in self.files.values() if f.folder_id == folder.id and not f.trashed
            ),
            "children": [
                self.folder_row(child)
                for child in self.folders.values()
                if child.parent_id == folder.id
            ],
        }


def _file_type(filename: str) -> str:
    lower = filename.lower()
    if lower.endswith(".gcode.3mf"):
        return "gcode.3mf"
    return lower.rsplit(".", 1)[-1] if "." in lower else ""


def _top(value: int | None) -> int | None:
    """Bambuddy's sentinel: 0 is the top level."""
    return None if value == 0 else value


def build_app() -> FastAPI:
    app = FastAPI()
    lib = _Library()

    @app.get("/api/v1/library/folders")
    def folders() -> list[dict[str, Any]]:
        return [lib.folder_row(f) for f in lib.folders.values() if f.parent_id is None]

    @app.get("/api/v1/library/folders/{folder_id}")
    def folder(folder_id: int) -> dict[str, Any]:
        return lib.folder_row(lib.folder(folder_id))

    @app.post("/api/v1/library/folders/")
    async def create_folder(request: Request) -> dict[str, Any]:
        body = await request.json()
        parent = body.get("parent_id")
        if parent is not None:
            lib.folder(parent)
        created = _Folder(lib.new_id(), body["name"], parent)
        lib.folders[created.id] = created
        return lib.folder_row(created)

    @app.put("/api/v1/library/folders/{folder_id}")
    async def update_folder(folder_id: int, request: Request) -> dict[str, Any]:
        found = lib.folder(folder_id)
        body = await request.json()
        if "name" in body:
            found.name = body["name"]
        if "parent_id" in body:
            found.parent_id = _top(body["parent_id"])
        return lib.folder_row(found)

    @app.delete("/api/v1/library/folders/{folder_id}")
    def delete_folder(folder_id: int) -> dict[str, Any]:
        lib.folder(folder_id)
        del lib.folders[folder_id]
        return {"message": "Folder deleted"}

    @app.get("/api/v1/library/files/")
    def listing(folder_id: int | None = None, include_root: bool = True) -> list[dict[str, Any]]:
        live = [f for f in lib.files.values() if not f.trashed]
        if folder_id is not None:
            return [f.row() for f in live if f.folder_id == folder_id]
        if include_root:
            return [f.row() for f in live if f.folder_id is None]
        return [f.row() for f in live]

    @app.get("/api/v1/library/files")
    def files(folder_id: int) -> list[dict[str, Any]]:
        return [f.row() for f in lib.files.values() if f.folder_id == folder_id and not f.trashed]

    @app.post("/api/v1/library/files")
    async def upload(file: UploadFile, folder_id: int | None = None) -> dict[str, Any]:
        if folder_id is not None:
            lib.folder(folder_id)
        created = _File(
            lib.new_id(),
            file.filename or "upload",
            folder_id,
            await file.read(),
            datetime.now(UTC).replace(tzinfo=None).isoformat(),
        )
        lib.files[created.id] = created
        return created.row() | {"folder_id": None, "file_hash": None}

    @app.get("/api/v1/library/files/{file_id}")
    def library_file(file_id: int) -> dict[str, Any]:
        return lib.file(file_id).row()

    @app.get("/api/v1/library/files/{file_id}/download")
    def download(file_id: int) -> Response:
        return Response(lib.file(file_id).data, media_type="application/octet-stream")

    @app.put("/api/v1/library/files/{file_id}")
    async def update_file(file_id: int, request: Request) -> dict[str, Any]:
        found = lib.file(file_id)
        body = await request.json()
        if "filename" in body:
            found.filename = body["filename"]
        if "folder_id" in body:
            target = _top(body["folder_id"])
            if target is not None:
                lib.folder(target)
            found.folder_id = target
        return found.row()

    @app.delete("/api/v1/library/files/{file_id}")
    def delete_file(file_id: int) -> dict[str, Any]:
        lib.file(file_id).trashed = True
        return {"message": "File moved to trash", "trashed": True}

    @app.post("/api/v1/library/bulk-delete")
    async def bulk_delete(request: Request) -> dict[str, Any]:
        body = await request.json()
        deleted = 0
        for file_id in body.get("file_ids", []):
            found = lib.files.get(file_id)
            if found is not None and not found.trashed:
                found.trashed = True
                deleted += 1
        return {"deleted_files": deleted, "deleted_folders": 0}

    @app.get("/api/v1/library/trash")
    def trash(limit: int = 50, offset: int = 0) -> dict[str, Any]:
        trashed = [
            {"id": f.id, "filename": f.filename, "folder_id": f.folder_id}
            for f in lib.files.values()
            if f.trashed
        ]
        return {
            "items": trashed[offset : offset + limit],
            "total": len(trashed),
            "retention_days": 30,
        }

    @app.post("/api/v1/library/trash/{file_id}/restore")
    def restore(file_id: int) -> dict[str, Any]:
        lib.file(file_id, trashed=True).trashed = False
        return {"message": "File restored"}

    @app.get("/api/v1/auth/status")
    def auth_status() -> dict[str, Any]:
        return {"auth_enabled": False}

    @app.get("/api/v1/queue/")
    def queue() -> list[dict[str, Any]]:
        return []

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    uvicorn.run(build_app(), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
