"""`library_folders` and `library_files`: the native print library's rows (plan
2026-10-17-native-print-library, Task 2). The bytes live in the blob store under each
file's `blob_key`; nothing here touches them."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

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


class FolderGoneError(LookupError):
    """The folder a file was to go in is gone (deleted since its check)."""


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
    #: The print subject whose run put it here, as its per-print copy or a slice (#4337);
    #: ``None`` for every other file, and once a person moved or restored it.
    print_subject: str | None = None

    @property
    def file_type(self) -> str:
        return file_type_of(self.name)


def file_type_of(name: str) -> str:
    """Bambuddy's `file_type`: the extension, ``"gcode.3mf"`` for a sliced file."""
    lower = name.lower()
    if lower.endswith(".gcode.3mf"):
        return "gcode.3mf"
    return lower.rsplit(".", 1)[-1] if "." in lower else ""


_FILE_COLUMNS = (
    "id, folder_id, name, size, sha256, blob_key, media_type, plates, has_thumbnail,"
    " output_id, filed, created_at, updated_at, deleted_at, print_subject"
)
_FOLDER_COLUMNS = "id, name, parent_id, created_at"
_FOLDER_TREE_LOCK = "SELECT pg_advisory_xact_lock(hashtextextended('scadbuddy:library:tree', 0))"


def _insert_file(conn: Connection[DictRow], row: NativeFile) -> NativeFile:
    out = conn.execute(
        f"INSERT INTO library_files (id, folder_id, name, size, sha256, blob_key, media_type,"
        f" plates, has_thumbnail, output_id, filed, created_at, updated_at, deleted_at,"
        f" print_subject)"
        f" VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
        f" RETURNING {_FILE_COLUMNS}".encode(),
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
            row.print_subject,
        ),
    ).fetchone()
    assert out is not None
    return NativeFile.model_validate(out)


def _lock_folder_names(conn: Connection[DictRow], folder_id: int | None) -> bool:
    """In a transaction of ``conn``'s: one writer of a folder's names at a time, taken by
    every write that puts a name in a folder (an insert, a move, a rename, a put), after
    the folder's ``FOR KEY SHARE`` and before the write, so a holder waits on nothing
    else. Holding it, `put_for_output`'s pick sees every name committed in the folder,
    and no writer lands one between that pick and its insert. A row with no folder is
    never put, so the top level takes none. A folder gone (the KEY SHARE found no row)
    takes none either, and is False: an insert or move then fails its FK, the 404."""
    if folder_id is None:
        return True
    live = conn.execute(
        "SELECT id FROM library_folders WHERE id = %s FOR KEY SHARE", (folder_id,)
    ).fetchone()
    if live is None:
        return False
    conn.execute(
        "SELECT pg_advisory_xact_lock("
        "hashtextextended('scadbuddy:library:folder-names:' || %s, 0))",
        (str(folder_id),),
    )
    return True


def _update_file(
    conn: Connection[DictRow], id: int, sets: list[str], params: list[Any]
) -> NativeFile | None:
    row = conn.execute(
        f"UPDATE library_files SET {', '.join(sets)} WHERE id = %s AND deleted_at IS NULL"
        f" RETURNING {_FILE_COLUMNS}".encode(),
        (*params, id),
    ).fetchone()
    return None if row is None else NativeFile.model_validate(row)


def _rows_of_output(
    conn: Connection[DictRow],
    output_id: str,
    *,
    which: Literal["every", "filed", "live filed"] = "every",
) -> list[NativeFile]:
    """The rows naming the output, by id. ``every``: filed, moved or trashed, since a
    move clears ``filed`` and keeps ``output_id`` until `forget_output`. ``filed``: only
    its filed copies, trashed or not; a moved copy or one put in a folder of the user's
    choosing (#4507) is not one. ``live filed``: its filed copies not in the trash."""
    where = "output_id = %s"
    if which != "every":
        where += " AND filed"
    if which == "live filed":
        where += " AND deleted_at IS NULL"
    rows = conn.execute(
        f"SELECT {_FILE_COLUMNS} FROM library_files WHERE {where} ORDER BY id".encode(),
        (output_id,),
    ).fetchall()
    return [NativeFile.model_validate(r) for r in rows]


def _live_copy_in_folder(
    conn: Connection[DictRow], output_id: str, folder_id: int
) -> NativeFile | None:
    """The output's first live row in ``folder_id``, whoever put it there."""
    row = conn.execute(
        f"SELECT {_FILE_COLUMNS} FROM library_files WHERE output_id = %s"
        f" AND folder_id = %s AND deleted_at IS NULL ORDER BY id LIMIT 1".encode(),
        (output_id, folder_id),
    ).fetchone()
    return None if row is None else NativeFile.model_validate(row)


def _lock_output(conn: Connection[DictRow], output_id: str) -> None:
    """In a transaction of ``conn``'s: one writer of an output's copies at a time."""
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended('scadbuddy:library:output:' || %s, 0))",
        (output_id,),
    )


def unique_name(stem: str, taken: set[str]) -> str:
    """``<stem>.3mf``, or ``<stem> (2).3mf`` and on, unlike every casefolded name in
    ``taken``."""
    name, number = f"{stem}.3mf", 2
    while name.casefold() in taken:
        name, number = f"{stem} ({number}).3mf", number + 1
    return name


def _template_folder(conn: Connection[DictRow], name: str) -> NativeFolder:
    """In a transaction of ``conn``'s."""
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended('scadbuddy:library:folder:' || %s, 0))",
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
        """An upload or a copy, under its folder's names lock (`_lock_folder_names`). It
        keeps the name it is given, as a Bambuddy upload does."""
        with self._pool.connection() as conn, conn.transaction():
            _lock_folder_names(conn, row.folder_id)
            return _insert_file(conn, row)

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
        """A rename, a move or both, under the names lock of the folder the file ends up
        in (`_lock_folder_names`); the row is locked first, as `trash` and
        `delete_folder_tree` take it. Like an upload, it keeps the name it is given. A
        per-print file a person moves to another folder is theirs (#4337, case 4), as
        Bambuddy's inbox copy once moved is: its ``print_subject`` is cleared."""
        sets = ["updated_at = now()"]
        params: list[Any] = []
        if name is not None:
            sets.append("name = %s")
            params.append(name)
        if not isinstance(folder_id, UnsetType):
            # Every SET reads the row as it was: this compares the folder it leaves.
            sets.append(
                "print_subject = CASE WHEN folder_id IS NOT DISTINCT FROM %s THEN print_subject END"
            )
            params.append(folder_id)
            sets.append("folder_id = %s")
            params.append(folder_id)
        if filed is not None:
            sets.append("filed = %s")
            params.append(filed)
        with self._pool.connection() as conn, conn.transaction():
            current = conn.execute(
                "SELECT folder_id FROM library_files WHERE id = %s AND deleted_at IS NULL"
                " FOR UPDATE",
                (id,),
            ).fetchone()
            if current is None:
                return None
            if name is not None or not isinstance(folder_id, UnsetType):
                target = current["folder_id"] if isinstance(folder_id, UnsetType) else folder_id
                _lock_folder_names(conn, target)
            return _update_file(conn, id, sets, params)

    def trash(self, ids: list[int], *, per_print: bool = False) -> list[NativeFile]:
        """The rows are locked by id first, as `delete_folder_tree` locks them: an UPDATE
        alone locks them in the order its scan meets them, and would deadlock with it.
        ``per_print`` trashes only those still marked (#4337): a file a person made theirs
        since the caller read it (a move, a restore) stays."""
        only = " AND print_subject IS NOT NULL" if per_print else ""
        with self._pool.connection() as conn, conn.transaction():
            conn.execute(
                "SELECT id FROM library_files WHERE id = ANY(%s) AND deleted_at IS NULL"
                " ORDER BY id FOR UPDATE",
                (list(ids),),
            )
            rows = conn.execute(
                f"UPDATE library_files SET deleted_at = now(), updated_at = now()"
                f" WHERE id = ANY(%s) AND deleted_at IS NULL{only}"
                f" RETURNING {_FILE_COLUMNS}".encode(),
                (list(ids),),
            ).fetchall()
        return [NativeFile.model_validate(r) for r in rows]

    def restore(self, ids: list[int]) -> list[int]:
        """A file whose folder is gone comes back with `folder_id` NULL, by the FK. A
        per-print file a person restores is theirs (#4337): its ``print_subject`` is
        cleared, or the sweep that trashed it would trash it again."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                "UPDATE library_files SET deleted_at = NULL, updated_at = now(),"
                " print_subject = NULL"
                " WHERE id = ANY(%s) AND deleted_at IS NOT NULL RETURNING id",
                (list(ids),),
            ).fetchall()
        return sorted(int(r["id"]) for r in rows)

    def per_print(self) -> list[NativeFile]:
        """Every live file a run put here and nobody has made theirs (#4337): what
        `sweep.sweep_print_copies` judges."""
        return self._files(
            f"SELECT {_FILE_COLUMNS} FROM library_files"
            f" WHERE print_subject IS NOT NULL AND deleted_at IS NULL ORDER BY id"
        )

    def existing(self, ids: list[int]) -> set[int]:
        """Those of ``ids`` whose row is still there, live or trashed."""
        if not ids:
            return set()
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT id FROM library_files WHERE id = ANY(%s)", (list(ids),)
            ).fetchall()
        return {int(r["id"]) for r in rows}

    def release(self, ids: list[int]) -> None:
        """These files are a person's now (#4337): no sweep trashes them."""
        if not ids:
            return
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE library_files SET print_subject = NULL WHERE id = ANY(%s)", (list(ids),)
            )

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
        with self._pool.connection() as conn:
            return _rows_of_output(conn, output_id, which="live filed")

    def rows_for_output(self, output_id: str) -> list[NativeFile]:
        """Every row the output has, live or not; only the tests read it."""
        with self._pool.connection() as conn:
            return _rows_of_output(conn, output_id)

    def filed_rows_for_output(self, output_id: str) -> list[NativeFile]:
        with self._pool.connection() as conn:
            return _rows_of_output(conn, output_id, which="filed")

    def live_copy_in_folder(self, output_id: str, folder_id: int) -> NativeFile | None:
        """The output's live copy in ``folder_id``, if any; no lock, so a hint only."""
        with self._pool.connection() as conn:
            return _live_copy_in_folder(conn, output_id, folder_id)

    def file_for_output(self, row: NativeFile, folder: str) -> tuple[NativeFile, bool]:
        """Insert ``row``, an output's filed copy, in the top-level folder ``folder``
        (found or made), unless the output already has a filed copy, trashed or not: that
        row and False, or ``row`` as inserted and True. One short transaction on one
        connection, under an advisory lock per output, so overlapping attempts file one
        and a filing never holds a connection while it waits on the pool for another."""
        with self._pool.connection() as conn, conn.transaction():
            _lock_output(conn, row.output_id or "")
            existing = _rows_of_output(conn, row.output_id or "", which="filed")
            if existing:
                return existing[0], False
            folder_id = _template_folder(conn, folder).id
            return _insert_file(conn, row.model_copy(update={"folder_id": folder_id})), True

    def put_for_output(self, row: NativeFile, folder_id: int, stem: str) -> tuple[NativeFile, bool]:
        """Insert ``row``, an output's copy, in ``folder_id`` under a name made from
        ``stem`` unique in the folder, unless a live row of the output is already there:
        that row and False, or ``row`` as inserted and True. Under the same per-output lock
        as `file_for_output`, so overlapping calls put one copy (#4507). The folder is
        locked ``FOR KEY SHARE`` first, so it is live until the commit, and one deleted
        since the caller's check is `FolderGoneError`; a delete waits for the commit and
        then trashes this copy with the folder's other files.

        The name is picked and inserted under the folder's names lock, which every writer
        of a name into the folder takes (`_lock_folder_names`: an upload, a copy, a move, a
        rename, another put), so no write running beside this one can take the name it
        picks (#4512). An upload, copy, move or rename keeps the name it is given, so one
        made after this put commits may repeat the name, as it may repeat any name already
        there: nothing relies on unique names (no constraint; rows are found by id)."""
        with self._pool.connection() as conn, conn.transaction():
            _lock_output(conn, row.output_id or "")
            there = _live_copy_in_folder(conn, row.output_id or "", folder_id)
            if there is not None:
                return there, False
            # The name pick and the insert under the folder's lock too: the output's lock
            # alone lets two outputs of one stem both take `Sign.3mf`.
            if not _lock_folder_names(conn, folder_id):
                raise FolderGoneError(folder_id)
            taken = conn.execute(
                "SELECT name FROM library_files WHERE folder_id = %s AND deleted_at IS NULL",
                (folder_id,),
            ).fetchall()
            name = unique_name(stem, {r["name"].casefold() for r in taken})
            placed = row.model_copy(update={"folder_id": folder_id, "name": name})
            return _insert_file(conn, placed), True

    def forget_output(self, output_id: str) -> None:
        """A deleted output is named by no row: a file moved out of its folder stays, with
        no ``output_id`` to point at nothing."""
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE library_files SET output_id = NULL WHERE output_id = %s", (output_id,)
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
        """Under the tree lock, so a folder made under one being deleted waits for the
        delete and then fails its FK, never the delete (``parent_id`` is RESTRICT)."""
        with self._pool.connection() as conn, conn.transaction():
            conn.execute(_FOLDER_TREE_LOCK)
            row = conn.execute(
                f"INSERT INTO library_folders (name, parent_id) VALUES (%s, %s)"
                f" RETURNING {_FOLDER_COLUMNS}".encode(),
                (name, parent_id),
            ).fetchone()
        return NativeFolder.model_validate(row)

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

    def delete_folder_tree(self, id: int) -> tuple[list[NativeFile], list[int]] | None:
        """Trash every live file in the folder and its subfolders, then delete the
        folders deepest first, in one transaction; ``None`` when the folder is gone.

        The subtree's folder rows are locked ``FOR UPDATE`` before the files are
        trashed, and an insert or move into a folder takes ``FOR KEY SHARE`` on it for its
        FK check, so a file arriving mid-delete waits and then fails its FK: no live file
        is left in a deleted folder for ``ON DELETE SET NULL`` to drop at the top level.
        The subtree's live file rows are locked first, by id, as a move takes them (its
        row, then its target folder's KEY SHARE): taking the folders first would deadlock
        with a move between two folders of the subtree."""
        with self._pool.connection() as conn, conn.transaction():
            conn.execute(_FOLDER_TREE_LOCK)
            tree = conn.execute(
                "WITH RECURSIVE sub AS ("
                " SELECT id, 0 AS depth FROM library_folders WHERE id = %s"
                " UNION ALL SELECT f.id, sub.depth + 1 FROM library_folders f"
                " JOIN sub ON f.parent_id = sub.id)"
                " SELECT id, depth FROM sub ORDER BY depth DESC, id",
                (id,),
            ).fetchall()
            if not tree:
                return None
            order = [int(r["id"]) for r in tree]
            conn.execute(
                "SELECT id FROM library_files WHERE folder_id = ANY(%s) AND deleted_at IS NULL"
                " ORDER BY id FOR UPDATE",
                (order,),
            )
            conn.execute(
                "SELECT id FROM library_folders WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
                (order,),
            )
            rows = conn.execute(
                f"UPDATE library_files SET deleted_at = now(), updated_at = now()"
                f" WHERE folder_id = ANY(%s) AND deleted_at IS NULL"
                f" RETURNING {_FILE_COLUMNS}".encode(),
                (order,),
            ).fetchall()
            for folder_id in order:
                conn.execute("DELETE FROM library_folders WHERE id = %s", (folder_id,))
        files = sorted((NativeFile.model_validate(r) for r in rows), key=lambda f: f.id)
        return files, order

    def delete_folder(self, id: int) -> bool:
        with self._pool.connection() as conn:
            cur = conn.execute("DELETE FROM library_folders WHERE id = %s", (id,))
        return cur.rowcount > 0

    def template_folder(self, name: str) -> NativeFolder:
        """The oldest top-level folder called ``name``, or a new one, once even when
        several callers race."""
        with self._pool.connection() as conn, conn.transaction():
            return _template_folder(conn, name)
