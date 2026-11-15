"""A native library file's associations (#6395): a model or another library file it
relates to, and how. The relation is said from the file's side: "file X is an
``output_of`` model Y". A listing for the target says it from there ("Y has output X");
the frontend and the agent word it, from the same row.

The rows are `library_file_associations`. A trashed file's rows stay and are hidden from
every read, so a restore shows them again; purging the file deletes them (the FKs). A
deleted model's rows are deleted by its delete (`forget_model`)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, get_args
from uuid import UUID

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from pydantic import BaseModel

from scadbuddy.bambuddy.subject import PrintSubject
from scadbuddy.orders.models import Actor

#: Every relation, the one list: the routes, the agent's tools (through the API's spec)
#: and the frontend's labels all check against it.
Relation = Literal["output_of", "variant_of", "source_of", "related_to"]
RELATIONS: tuple[str, ...] = get_args(Relation)

#: What an implied association is read from: a model made from the file (its
#: ``mesh_source``), or an output whose copy the file is (``library_files.output_id``).
ImpliedBy = Literal["mesh_source", "output"]

MAX_NOTE_CHARS = 500


class Association(BaseModel):
    """One association. ``id`` is ``None`` for an implied one, which is read live from
    what the model or the file records, is never stored, and cannot be removed."""

    id: UUID | None
    library_file_id: int
    library_file_name: str
    relation: Relation
    #: ``model:<slug>`` or ``library:<id>``.
    target: str
    #: The model's or the target file's name, when it could be read.
    target_name: str | None = None
    note: str = ""
    created_at: datetime | None = None
    created_by: Actor | None = None
    implied_by: ImpliedBy | None = None


class TargetGoneError(LookupError):
    """The file or the target file is not a live native library file."""


_SELECT = (
    "SELECT a.id, a.library_file_id, f.name AS library_file_name, a.relation, a.target,"
    " t.name AS target_file_name, a.note, a.created_at, a.created_by"
    " FROM library_file_associations a"
    " JOIN library_files f ON f.id = a.library_file_id AND f.deleted_at IS NULL"
    " LEFT JOIN library_files t ON t.id = a.target_file_id"
    " WHERE (a.target_file_id IS NULL OR t.deleted_at IS NULL)"
)


def _association(row: dict[str, Any]) -> Association:
    return Association(
        id=row["id"],
        library_file_id=row["library_file_id"],
        library_file_name=row["library_file_name"],
        relation=row["relation"],
        target=row["target"],
        target_name=row["target_file_name"],
        note=row["note"],
        created_at=row["created_at"],
        created_by=Actor.model_validate(row["created_by"]),
    )


def _lock_live(conn: Connection[DictRow], ids: list[int]) -> set[int]:
    """In a transaction of ``conn``'s: those of ``ids`` that are live files, locked ``FOR
    KEY SHARE`` so a trash (``FOR UPDATE``) waits for the commit."""
    rows = conn.execute(
        "SELECT id FROM library_files WHERE id = ANY(%s) AND deleted_at IS NULL"
        " ORDER BY id FOR KEY SHARE",
        (ids,),
    ).fetchall()
    return {int(r["id"]) for r in rows}


class AssociationStore:
    """The pool must be autocommit, as `NativeLibraryStore`'s is."""

    def __init__(self, pool: ConnectionPool[Connection[DictRow]]) -> None:
        self._pool = pool

    def add(
        self, file_id: int, target: PrintSubject, relation: Relation, note: str, by: Actor
    ) -> tuple[Association, bool]:
        """The association, and whether it is new. One already there for the file,
        target and relation keeps its id and takes ``note``. `TargetGoneError` when the
        file, or a target file, is not live."""
        target_file = target.file_id
        wanted = [file_id] if target_file is None else sorted({file_id, target_file})
        with self._pool.connection() as conn, conn.transaction():
            if _lock_live(conn, wanted) != set(wanted):
                raise TargetGoneError(target.key)
            row = conn.execute(
                "INSERT INTO library_file_associations"
                " (library_file_id, target, target_file_id, relation, note, created_by)"
                " VALUES (%s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (library_file_id, target, relation)"
                " DO UPDATE SET note = EXCLUDED.note"
                " RETURNING id, (xmax = 0) AS created",
                (
                    file_id,
                    target.key if target_file is None else PrintSubject.library(target_file).key,
                    target_file,
                    relation,
                    note,
                    Jsonb(by.model_dump(mode="json")),
                ),
            ).fetchone()
            assert row is not None
            found = conn.execute(f"{_SELECT} AND a.id = %s".encode(), (row["id"],)).fetchone()
        assert found is not None
        return _association(found), bool(row["created"])

    def get(self, id: UUID) -> Association | None:
        with self._pool.connection() as conn:
            row = conn.execute(f"{_SELECT} AND a.id = %s".encode(), (id,)).fetchone()
        return None if row is None else _association(row)

    def remove(self, id: UUID) -> bool:
        """False when there is no such row, or one of its files is in the trash: what a
        read does not list cannot be removed."""
        with self._pool.connection() as conn:
            cur = conn.execute(
                "DELETE FROM library_file_associations a WHERE a.id = %s"
                " AND EXISTS (SELECT 1 FROM library_files f"
                "  WHERE f.id = a.library_file_id AND f.deleted_at IS NULL)"
                " AND (a.target_file_id IS NULL OR EXISTS (SELECT 1 FROM library_files t"
                "  WHERE t.id = a.target_file_id AND t.deleted_at IS NULL))",
                (id,),
            )
        return cur.rowcount > 0

    def for_file(self, file_id: int) -> list[Association]:
        """Both ways: the file's own (it relates to a target), then those naming it as
        their target, oldest first."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"{_SELECT} AND (a.library_file_id = %s OR a.target_file_id = %s)"
                f" ORDER BY a.library_file_id <> %s, a.created_at, a.id".encode(),
                (file_id, file_id, file_id),
            ).fetchall()
        return [_association(r) for r in rows]

    def for_model(self, slug: str) -> list[Association]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"{_SELECT} AND a.target = %s ORDER BY a.created_at, a.id".encode(),
                (PrintSubject.model(slug).key,),
            ).fetchall()
        return [_association(r) for r in rows]

    def forget_model(self, slug: str) -> None:
        """A deleted model is no target: its rows go."""
        with self._pool.connection() as conn:
            conn.execute(
                "DELETE FROM library_file_associations WHERE target = %s",
                (PrintSubject.model(slug).key,),
            )
