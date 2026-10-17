"""`library_folders` and `library_files`: the native print library's rows (plan
2026-10-17-native-print-library, Task 2). The bytes live in the blob store under each
file's `blob_key`; nothing here touches them."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from pydantic import BaseModel

from scadbuddy.bambuddy.models import LibraryPlate


class UnsetType(Enum):
    UNSET = 0


#: "Not given", as against `None` (the top level) for a folder id.
Unset = UnsetType.UNSET


class FolderCycleError(Exception):
    """A folder cannot move under itself or one of its descendants."""


class NativeFolder(BaseModel):
    id: int
    name: str
    parent_id: int | None
    created_at: datetime


class NativeFile(BaseModel):
    id: int
    folder_id: int | None
    name: str
    size: int
    sha256: str
    blob_key: str
    media_type: str
    plates: list[LibraryPlate]
    has_thumbnail: bool
    output_id: str | None
    filed: bool
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None

    @property
    def file_type(self) -> str:
        lower = self.name.lower()
        if lower.endswith(".gcode.3mf"):
            return "gcode.3mf"
        return lower.rsplit(".", 1)[-1] if "." in lower else ""


_FILE_COLUMNS = (
    "id, folder_id, name, size, sha256, blob_key, media_type, plates, has_thumbnail,"
    " output_id, filed, created_at, updated_at, deleted_at"
)
_FOLDER_COLUMNS = "id, name, parent_id, created_at"
_FOLDER_TREE_LOCK = "SELECT pg_advisory_xact_lock(hashtextextended('scadbuddy:library:tree', 0))"


class NativeLibraryStore:
    """The pool must be autocommit, as `BlobRefs`' is; a multi-statement write opens its
    own transaction."""

    def __init__(self, pool: ConnectionPool[Connection[DictRow]]) -> None:
        self._pool = pool

    def _files(self, sql: str, params: tuple[Any, ...] = ()) -> list[NativeFile]:
        with self._pool.connection() as conn:
            rows = conn.execute(sql.encode(), params).fetchall()
        return [NativeFile.model_validate(r) for r in rows]

    def _folders(self, sql: str, params: tuple[Any, ...] = ()) -> list[NativeFolder]:
        with self._pool.connection() as conn:
            rows = conn.execute(sql.encode(), params).fetchall()
        return [NativeFolder.model_validate(r) for r in rows]

    # files

    def next_file_id(self) -> int:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT nextval(pg_get_serial_sequence('library_files', 'id')) AS id"
            ).fetchone()
        assert row is not None
        return int(row["id"])

    def insert_file(self, row: NativeFile) -> NativeFile:
        (out,) = self._files(
            f"INSERT INTO library_files (id, folder_id, name, size, sha256, blob_key, media_type,"
            f" plates, has_thumbnail, output_id, filed, created_at, updated_at, deleted_at)"
            f" VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            f" RETURNING {_FILE_COLUMNS}",
            (
                row.id,
                row.folder_id,
                row.name,
                row.size,
                row.sha256,
                row.blob_key,
                row.media_type,
                Jsonb([p.model_dump(mode="json") for p in row.plates]),
                row.has_thumbnail,
                row.output_id,
                row.filed,
                row.created_at,
                row.updated_at,
                row.deleted_at,
            ),
        )
        return out

    def file(self, id: int, *, trashed: bool = False) -> NativeFile | None:
        state = "IS NOT NULL" if trashed else "IS NULL"
        found = self._files(
            f"SELECT {_FILE_COLUMNS} FROM library_files WHERE id = %s AND deleted_at {state}",
            (id,),
        )
        return found[0] if found else None

    def files(self, folder_id: int | None, *, every_folder: bool = False) -> list[NativeFile]:
        where = "deleted_at IS NULL"
        params: tuple[Any, ...] = ()
        if not every_folder:
            if folder_id is None:
                where += " AND folder_id IS NULL"
            else:
                where += " AND folder_id = %s"
                params = (folder_id,)
        return self._files(
            f"SELECT {_FILE_COLUMNS} FROM library_files WHERE {where} ORDER BY id", params
        )

    def files_by_ids(self, ids: list[int]) -> list[NativeFile]:
        return self._files(
            f"SELECT {_FILE_COLUMNS} FROM library_files WHERE id = ANY(%s) AND deleted_at IS NULL"
            f" ORDER BY id",
            (list(ids),),
        )

    def update_file(
        self,
        id: int,
        *,
        name: str | None,
        folder_id: int | UnsetType | None,
        filed: bool | None,
    ) -> NativeFile | None:
        sets = ["updated_at = now()"]
        params: list[Any] = []
        if name is not None:
            sets.append("name = %s")
            params.append(name)
        if not isinstance(folder_id, UnsetType):
            sets.append("folder_id = %s")
            params.append(folder_id)
        if filed is not None:
            sets.append("filed = %s")
            params.append(filed)
        found = self._files(
            f"UPDATE library_files SET {', '.join(sets)} WHERE id = %s AND deleted_at IS NULL"
            f" RETURNING {_FILE_COLUMNS}",
            (*params, id),
        )
        return found[0] if found else None

    def trash(self, ids: list[int]) -> list[NativeFile]:
        return self._files(
            f"UPDATE library_files SET deleted_at = now(), updated_at = now()"
            f" WHERE id = ANY(%s) AND deleted_at IS NULL RETURNING {_FILE_COLUMNS}",
            (list(ids),),
        )

    def restore(self, ids: list[int]) -> list[int]:
        """A file whose folder is gone comes back with `folder_id` NULL, by the FK."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                "UPDATE library_files SET deleted_at = NULL, updated_at = now()"
                " WHERE id = ANY(%s) AND deleted_at IS NOT NULL RETURNING id",
                (list(ids),),
            ).fetchall()
        return sorted(int(r["id"]) for r in rows)

    def trashed_before(self, cutoff: datetime) -> list[NativeFile]:
        return self._files(
            f"SELECT {_FILE_COLUMNS} FROM library_files"
            f" WHERE deleted_at IS NOT NULL AND deleted_at < %s ORDER BY deleted_at, id",
            (cutoff,),
        )

    def purge_trashed(self, id: int, cutoff: datetime) -> NativeFile | None:
        """Delete the row only if it is still trashed and past ``cutoff``; ``None`` when a
        restore got there first, so a restore between a sweep's list and its purge loses
        nothing."""
        found = self._files(
            f"DELETE FROM library_files WHERE id = %s AND deleted_at IS NOT NULL"
            f" AND deleted_at < %s RETURNING {_FILE_COLUMNS}",
            (id, cutoff),
        )
        return found[0] if found else None

    def filed_for_output(self, output_id: str) -> list[NativeFile]:
        return self._files(
            f"SELECT {_FILE_COLUMNS} FROM library_files"
            f" WHERE output_id = %s AND filed AND deleted_at IS NULL ORDER BY id",
            (output_id,),
        )

    def live_file_keys(self) -> set[str]:
        """Every `blob_key`, trashed rows included."""
        with self._pool.connection() as conn:
            rows = conn.execute("SELECT blob_key FROM library_files").fetchall()
        return {r["blob_key"] for r in rows}

    # folders

    def folders(self) -> list[NativeFolder]:
        return self._folders(f"SELECT {_FOLDER_COLUMNS} FROM library_folders ORDER BY id")

    def folder(self, id: int) -> NativeFolder | None:
        found = self._folders(f"SELECT {_FOLDER_COLUMNS} FROM library_folders WHERE id = %s", (id,))
        return found[0] if found else None

    def create_folder(self, name: str, parent_id: int | None) -> NativeFolder:
        (out,) = self._folders(
            f"INSERT INTO library_folders (name, parent_id) VALUES (%s, %s)"
            f" RETURNING {_FOLDER_COLUMNS}",
            (name, parent_id),
        )
        return out

    def update_folder(
        self, id: int, *, name: str | None, parent_id: int | UnsetType | None
    ) -> NativeFolder | None:
        with self._pool.connection() as conn, conn.transaction():
            conn.execute(_FOLDER_TREE_LOCK)
            if isinstance(parent_id, int):
                cyclic = conn.execute(
                    "WITH RECURSIVE sub AS ("
                    " SELECT id FROM library_folders WHERE id = %s"
                    " UNION ALL SELECT f.id FROM library_folders f"
                    " JOIN sub ON f.parent_id = sub.id)"
                    " SELECT 1 FROM sub WHERE id = %s",
                    (id, parent_id),
                ).fetchone()
                if cyclic is not None:
                    raise FolderCycleError(id)
            sets = ["name = COALESCE(%s, name)"]
            params: list[Any] = [name]
            if not isinstance(parent_id, UnsetType):
                sets.append("parent_id = %s")
                params.append(parent_id)
            row = conn.execute(
                f"UPDATE library_folders SET {', '.join(sets)} WHERE id = %s"
                f" RETURNING {_FOLDER_COLUMNS}".encode(),
                (*params, id),
            ).fetchone()
        return NativeFolder.model_validate(row) if row else None

    def delete_folder(self, id: int) -> bool:
        with self._pool.connection() as conn:
            cur = conn.execute("DELETE FROM library_folders WHERE id = %s", (id,))
        return cur.rowcount > 0

    def template_folder(self, name: str) -> NativeFolder:
        """The oldest top-level folder called ``name``, or a new one, once even when
        several callers race."""
        with self._pool.connection() as conn, conn.transaction():
            conn.execute(
                "SELECT pg_advisory_xact_lock("
                "hashtextextended('scadbuddy:library:folder:' || %s, 0))",
                (name,),
            )
            row = conn.execute(
                f"SELECT {_FOLDER_COLUMNS} FROM library_folders"
                f" WHERE parent_id IS NULL AND name = %s ORDER BY id LIMIT 1".encode(),
                (name,),
            ).fetchone()
            if row is None:
                row = conn.execute(
                    f"INSERT INTO library_folders (name, parent_id) VALUES (%s, NULL)"
                    f" RETURNING {_FOLDER_COLUMNS}".encode(),
                    (name,),
                ).fetchone()
        assert row is not None
        return NativeFolder.model_validate(row)
