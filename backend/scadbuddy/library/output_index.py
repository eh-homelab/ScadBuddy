"""Every model's saved outputs as rows (#4777, order operations spec 2026-11-07 §3.1):
``output_index``, so `GET /outputs` lists, filters by time, model, text and whether an
order names one, and pages them in one query.

The files under ``data/outputs/<slug>/<id>/`` stay each output's record and the bytes'
home. `OutputStore` writes a row when it saves, renames or deletes an output, and the
API's start runs `sync`, which adds a row for any output with none and drops the rows
whose output is gone (a deleted model takes its outputs' directories with it).
Synchronous, like `OutputStore`: callers on the loop use a thread.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool
from pydantic import ValidationError

from scadbuddy.bambuddy.project_file import cached_defaults
from scadbuddy.core.paths import DataPaths
from scadbuddy.library.outputs import META_NAME, PARAMS_NAME, OutputMeta

if TYPE_CHECKING:
    from scadbuddy.library.outputs import OutputStore

#: A page's size when the caller names none: the list spans every model.
DEFAULT_LIMIT = 50


class AfterNotListedError(LookupError):
    """``after`` names no output the query lists."""


@dataclass(frozen=True)
class OutputQuery:
    #: ``created_at >= since`` and ``< until``.
    since: datetime | None = None
    until: datetime | None = None
    #: Only these models; ``None`` is every model, an empty list none.
    slugs: Sequence[str] | None = None
    #: Matched in the output's name or its parameter text, case folded.
    q: str | None = None
    #: Whether any order item names the output; ``None`` either.
    ordered: bool | None = None
    after: str | None = None
    limit: int = DEFAULT_LIMIT


def _word(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    return json.dumps(value)


def params_text(params: Mapping[str, Any], defaults: Mapping[str, Any]) -> str:
    """The parameters that differ from ``defaults`` as ``name=value`` words, by name.
    With no defaults known (no cached schema), every parameter."""
    return " ".join(
        f"{name}={_word(value)}"
        for name, value in sorted(params.items())
        if name not in defaults or defaults[name] != value
    )


def _like(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


class OutputIndex:
    def __init__(self, pool: ConnectionPool[Connection[DictRow]], paths: DataPaths) -> None:
        self._pool = pool
        self._paths = paths

    def now(self) -> datetime:
        """The database's clock, which ``indexed_at`` is stamped with."""
        with self._pool.connection() as conn:
            row = conn.execute("SELECT clock_timestamp() AS now").fetchone()
        assert row is not None
        now: datetime = row["now"]
        return now

    def record(
        self,
        meta: OutputMeta,
        params: Mapping[str, Any],
        defaults: Mapping[str, Any] | None = None,
    ) -> None:
        """The output's row, written again if it is there."""
        if defaults is None:
            defaults = cached_defaults(self._paths, meta.slug)
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO output_index (output_id, slug, name, created_at, params_text)"
                " VALUES (%s, %s, %s, %s, %s)"
                " ON CONFLICT (output_id) DO UPDATE SET slug = EXCLUDED.slug,"
                " name = EXCLUDED.name, created_at = EXCLUDED.created_at,"
                " params_text = EXCLUDED.params_text, indexed_at = clock_timestamp()",
                (meta.id, meta.slug, meta.name, meta.created_at, params_text(params, defaults)),
            )

    def rename(self, output_id: str, name: str | None) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE output_index SET name = %s WHERE output_id = %s", (name, output_id)
            )

    def forget(self, *output_ids: str) -> None:
        with self._pool.connection() as conn:
            conn.execute("DELETE FROM output_index WHERE output_id = ANY(%s)", (list(output_ids),))

    def _filters(self, query: OutputQuery) -> tuple[str, list[Any]]:
        where: list[str] = []
        args: list[Any] = []
        if query.since is not None:
            where.append("x.created_at >= %s")
            args.append(query.since)
        if query.until is not None:
            where.append("x.created_at < %s")
            args.append(query.until)
        if query.slugs is not None:
            where.append("x.slug = ANY(%s)")
            args.append(list(query.slugs))
        if query.q:
            where.append("(x.name ILIKE %s ESCAPE '\\' OR x.params_text ILIKE %s ESCAPE '\\')")
            args += [_like(query.q), _like(query.q)]
        if query.ordered is not None:
            exists = "EXISTS (SELECT 1 FROM order_items i WHERE i.output_id = x.output_id)"
            where.append(exists if query.ordered else f"NOT {exists}")
        return (" WHERE " + " AND ".join(where)) if where else "", args

    def query(self, query: OutputQuery) -> tuple[list[str], int]:
        """The page's output ids, newest first, and how many the filters select.
        ``after`` must be an id the filters select, else `AfterNotListedError`."""
        where, args = self._filters(query)
        with self._pool.connection() as conn:
            counted = conn.execute(f"SELECT count(*) AS n FROM output_index x{where}", args)
            row = counted.fetchone()
            assert row is not None
            total: int = row["n"]
            page = where
            page_args = list(args)
            if query.after is not None:
                joined = " AND " if where else " WHERE "
                anchor = conn.execute(
                    f"SELECT x.created_at FROM output_index x{where}{joined}x.output_id = %s",
                    [*args, query.after],
                ).fetchone()
                if anchor is None:
                    raise AfterNotListedError(query.after)
                page = f"{where}{joined}(x.created_at, x.output_id) < (%s, %s)"
                page_args += [anchor["created_at"], query.after]
            rows = conn.execute(
                f"SELECT x.output_id FROM output_index x{page}"
                " ORDER BY x.created_at DESC, x.output_id DESC LIMIT %s",
                [*page_args, query.limit],
            ).fetchall()
        return [r["output_id"] for r in rows], total

    def _known(self) -> set[str]:
        with self._pool.connection() as conn:
            rows = conn.execute("SELECT output_id FROM output_index").fetchall()
        return {r["output_id"] for r in rows}

    def drop_missing(self, seen: set[str], *, before: datetime) -> int:
        """Drop the rows indexed before ``before`` whose output is not in ``seen``: a
        row a save wrote since stays, though its files were not there to be seen."""
        with self._pool.connection() as conn:
            dropped = conn.execute(
                "DELETE FROM output_index WHERE indexed_at < %s AND NOT output_id = ANY(%s)",
                (before, list(seen)),
            )
        return dropped.rowcount

    def sync(self, store: OutputStore, stop: threading.Event | None = None) -> tuple[int, int]:
        """Index every saved output with no row, then drop the rows whose output is gone:
        how many were added and dropped. Run again it changes nothing. ``stop`` ends it
        after the output in hand, without the drop."""
        started = self.now()
        known = self._known()
        seen: set[str] = set()
        defaults: dict[str, dict[str, Any]] = {}
        added = 0
        for meta_path in store.paths.outputs.glob(f"*/*/{META_NAME}"):
            if stop is not None and stop.is_set():
                return added, 0
            output_id = meta_path.parent.name
            seen.add(output_id)
            if output_id in known:
                continue
            try:
                meta = OutputMeta.model_validate_json(meta_path.read_text(encoding="utf-8"))
                params_path = meta_path.parent / PARAMS_NAME
                params = (
                    json.loads(params_path.read_text(encoding="utf-8"))
                    if params_path.is_file()
                    else {}
                )
            except (OSError, ValueError, ValidationError):
                continue  # half written or unreadable: listed once it reads
            if meta.id != output_id or not isinstance(params, dict):
                continue
            if meta.slug not in defaults:
                defaults[meta.slug] = dict(cached_defaults(self._paths, meta.slug))
            self.record(meta, params, defaults[meta.slug])
            added += 1
        return added, self.drop_missing(seen, before=started)
