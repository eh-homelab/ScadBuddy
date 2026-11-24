"""Each template's derived row and the copy runs' ledger (#7317, model records spec
2026-11-25 §5.1): the tables ``templates`` and ``template_copy_runs``.

``model.json`` in git stays each template's record. A row is only ever written from the
template's files, so any row can be rebuilt from the tree and dropping the table loses
nothing (spec §7.1). Synchronous, like `OutputIndex`: callers on the loop use a thread.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

#: The most of a ``model.json`` that is not JSON that a row keeps (spec §5.1).
META_TEXT_MAX = 64 * 1024

#: A copy run older than this is deleted when the next run starts (plan decision 8).
RUN_KEPT = "30 days"

_COLUMNS = (
    "template_id",
    "origin",
    "meta",
    "meta_error",
    "meta_text",
    "name",
    "version",
    "updated_at",
    "has_readme",
    "inputs_version",
    "colour_params",
    "files",
    "cover",
    "files_stamp",
    "refreshed_head",
)

_UPSERT = (
    f"INSERT INTO templates ({', '.join(_COLUMNS)})"
    f" VALUES ({', '.join(['%s'] * len(_COLUMNS))})"
    " ON CONFLICT (template_id) DO UPDATE SET "
    + ", ".join(f"{column} = EXCLUDED.{column}" for column in _COLUMNS[1:])
    + ", refreshed_at = clock_timestamp()"
)

#: Mine first, then the built-ins, each in byte order, as Python's `sorted` of the ids.
_ORDER = " ORDER BY origin = 'builtin', template_id COLLATE \"C\""


@dataclass(frozen=True)
class TemplateRow:
    """One ``templates`` row, every column but ``refreshed_at``."""

    template_id: str
    origin: Literal["mine", "builtin"]
    meta: dict[str, Any] | None
    meta_error: str | None
    meta_text: str | None
    name: str
    version: str | None
    updated_at: datetime
    has_readme: bool
    inputs_version: int
    colour_params: list[str] | None
    files: dict[str, int]
    cover: dict[str, str] | None
    files_stamp: str
    refreshed_head: str | None


def _values(row: TemplateRow) -> tuple[Any, ...]:
    return (
        row.template_id,
        row.origin,
        None if row.meta is None else Jsonb(row.meta),
        row.meta_error,
        row.meta_text,
        row.name,
        row.version,
        row.updated_at,
        row.has_readme,
        row.inputs_version,
        row.colour_params,
        Jsonb(row.files),
        None if row.cover is None else Jsonb(row.cover),
        row.files_stamp,
        row.refreshed_head,
    )


def _row_of(found: DictRow) -> TemplateRow:
    return TemplateRow(**{column: found[column] for column in _COLUMNS})


def write_rows(conn: Connection[Any], rows: Sequence[TemplateRow], gone: Sequence[str]) -> None:
    """Write ``rows`` and drop the rows of ``gone`` on the caller's connection, in the
    caller's transaction. It reads nothing, so a connection of any row factory runs it."""
    if rows:
        with conn.cursor() as cur:
            cur.executemany(_UPSERT, [_values(row) for row in rows])
    if gone:
        conn.execute("DELETE FROM templates WHERE template_id = ANY(%s)", (list(gone),))


class TemplateRows:
    def __init__(self, pool: ConnectionPool[Connection[DictRow]]) -> None:
        self._pool = pool
        self._copied = False

    def put(self, rows: Sequence[TemplateRow], gone: Sequence[str]) -> None:
        """`write_rows` in a transaction of its own."""
        with self._pool.connection() as conn, conn.transaction():
            write_rows(conn, rows, gone)

    def get(self, template_id: str) -> TemplateRow | None:
        with self._pool.connection() as conn:
            found = conn.execute(
                f"SELECT {', '.join(_COLUMNS)} FROM templates WHERE template_id = %s",
                (template_id,),
            ).fetchone()
        return None if found is None else _row_of(found)

    def all(self) -> list[TemplateRow]:
        with self._pool.connection() as conn:
            found = conn.execute(f"SELECT {', '.join(_COLUMNS)} FROM templates{_ORDER}").fetchall()
        return [_row_of(row) for row in found]

    def ids(self) -> list[str]:
        with self._pool.connection() as conn:
            found = conn.execute(f"SELECT template_id FROM templates{_ORDER}").fetchall()
        return [row["template_id"] for row in found]

    def stamps(self) -> dict[str, tuple[str, str | None]]:
        """Each row's ``files_stamp`` and ``version``, by template id."""
        with self._pool.connection() as conn:
            found = conn.execute("SELECT template_id, files_stamp, version FROM templates")
            return {row["template_id"]: (row["files_stamp"], row["version"]) for row in found}

    def start_run(self) -> int:
        """A new copy run's id. The runs that started more than `RUN_KEPT` ago go."""
        with self._pool.connection() as conn, conn.transaction():
            conn.execute(
                "DELETE FROM template_copy_runs"
                " WHERE started_at < clock_timestamp() - %s::interval",
                (RUN_KEPT,),
            )
            found = conn.execute(
                "INSERT INTO template_copy_runs DEFAULT VALUES RETURNING id"
            ).fetchone()
        assert found is not None
        run_id: int = found["id"]
        return run_id

    def finish_run(
        self,
        run_id: int,
        *,
        added: int,
        refreshed: int,
        dropped: int,
        unreadable: list[dict[str, str]],
    ) -> None:
        """The run saw every folder: it sets ``finished_at``."""
        self._end(run_id, True, added, refreshed, dropped, unreadable)

    def end_incomplete_run(
        self,
        run_id: int,
        *,
        added: int,
        refreshed: int,
        dropped: int,
        unreadable: list[dict[str, str]],
    ) -> None:
        """The run did not see every folder: ``incomplete``, and no ``finished_at``."""
        self._end(run_id, False, added, refreshed, dropped, unreadable)

    def _end(
        self,
        run_id: int,
        finished: bool,
        added: int,
        refreshed: int,
        dropped: int,
        unreadable: list[dict[str, str]],
    ) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE template_copy_runs SET"
                " finished_at = CASE WHEN %s THEN clock_timestamp() END,"
                " incomplete = NOT %s, added = %s, refreshed = %s, dropped = %s,"
                " unreadable = %s WHERE id = %s",
                (finished, finished, added, refreshed, dropped, Jsonb(unreadable), run_id),
            )

    def last_run(self) -> dict[str, Any] | None:
        """The newest run's row, or None before the first run."""
        with self._pool.connection() as conn:
            found = conn.execute(
                "SELECT id, started_at, finished_at, incomplete, added, refreshed, dropped,"
                " unreadable FROM template_copy_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return None if found is None else dict(found)

    def copied(self) -> bool:
        """Whether a copy run has finished. Once true, it stays true for this object."""
        if self._copied:
            return True
        with self._pool.connection() as conn:
            found = conn.execute(
                "SELECT EXISTS (SELECT 1 FROM template_copy_runs"
                " WHERE finished_at IS NOT NULL) AS copied"
            ).fetchone()
        self._copied = bool(found and found["copied"])
        return self._copied
