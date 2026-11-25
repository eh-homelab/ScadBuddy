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
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool
from pydantic import ValidationError

from scadbuddy.bambuddy.project_file import cached_defaults
from scadbuddy.core.paths import DataPaths
from scadbuddy.core.tracing import span
from scadbuddy.library.output_query import DEFAULT_LIMIT as DEFAULT_LIMIT
from scadbuddy.library.output_query import AfterNotListedError as AfterNotListedError
from scadbuddy.library.output_query import OutputQuery as OutputQuery
from scadbuddy.library.output_query import params_text, query_ids, saved_by_storefront_run
from scadbuddy.library.outputs import (
    BACK_OFFICE,
    INPUTS_NAME,
    META_NAME,
    PARAMS_NAME,
    OutputMeta,
)

if TYPE_CHECKING:
    from scadbuddy.library.outputs import OutputStore


def _inputs(directory: Path) -> dict[str, Any]:
    """The output's ``inputs.json``, or none when it is missing or unreadable."""
    try:
        loaded = json.loads((directory / INPUTS_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


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
        """The output's row, written again if it is there. The output's ``outputs`` row
        of the same slug takes the same ``params_text`` in the same transaction (#7257):
        so a row the copy wrote gets the text that the list on ``outputs`` matches."""
        if defaults is None:
            defaults = cached_defaults(self._paths, meta.slug)
        text = params_text(params, defaults)
        with self._pool.connection() as conn, conn.transaction():
            conn.execute(
                "UPDATE outputs SET params_text = %s WHERE output_id = %s AND slug = %s",
                (text, meta.id, meta.slug),
            )
            conn.execute(
                "INSERT INTO output_index"
                " (output_id, slug, name, created_at, params_text, source)"
                " VALUES (%s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (output_id) DO UPDATE SET slug = EXCLUDED.slug,"
                " name = EXCLUDED.name, created_at = EXCLUDED.created_at,"
                " params_text = EXCLUDED.params_text, source = EXCLUDED.source,"
                " indexed_at = clock_timestamp()",
                (
                    meta.id,
                    meta.slug,
                    meta.name,
                    meta.created_at,
                    text,
                    meta.source,
                ),
            )

    def rename(self, output_id: str, name: str | None) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE output_index SET name = %s WHERE output_id = %s", (name, output_id)
            )

    def forget(self, *output_ids: str) -> None:
        with self._pool.connection() as conn:
            conn.execute("DELETE FROM output_index WHERE output_id = ANY(%s)", (list(output_ids),))

    def forget_slug(self, slug: str) -> None:
        """Drop every row of ``slug``'s outputs: its outputs' directory is gone."""
        with self._pool.connection() as conn:
            conn.execute("DELETE FROM output_index WHERE slug = %s", (slug,))

    def query(self, query: OutputQuery) -> tuple[list[str], int]:
        """The page's output ids, newest first, and how many the filters select.
        ``after`` must be an id the filters select, else `AfterNotListedError`."""
        with self._pool.connection() as conn:
            return query_ids(conn, "output_index", query)

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
        # It globs every output's meta.json on the NFS share: one span (#7245).
        with span("output_index.sync") as current:
            added, dropped = self._sync(store, stop)
            current.set_attribute("scadbuddy.added", added)
            current.set_attribute("scadbuddy.dropped", dropped)
            return added, dropped

    def _sync(self, store: OutputStore, stop: threading.Event | None) -> tuple[int, int]:
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
            if meta.source == BACK_OFFICE and saved_by_storefront_run(
                meta, _inputs(meta_path.parent)
            ):
                # Saved before #6371 by the storefront slice run: the migration dropped
                # its row, so it is marked here, once, and indexed as the storefront's.
                try:
                    meta = store.mark_storefront(output_id)
                except (OSError, ValueError, ValidationError):
                    continue
            if meta.slug not in defaults:
                defaults[meta.slug] = dict(cached_defaults(self._paths, meta.slug))
            self.record(meta, params, defaults[meta.slug])
            added += 1
        return added, self.drop_missing(seen, before=started)
