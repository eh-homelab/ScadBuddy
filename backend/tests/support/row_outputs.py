"""An `OutputStore` as release B builds it (#7257): its rows in Postgres and its bytes in
a local content store, for the tests that save and read outputs."""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.types.json import Jsonb

from scadbuddy.api.deps import STATE_ATTR
from scadbuddy.core.paths import DataPaths
from scadbuddy.library.output_bytes import OutputBytes
from scadbuddy.library.output_prints import OutputPrintStore
from scadbuddy.library.output_rows import OutputRow, OutputRows
from scadbuddy.library.outputs import OutputMeta, OutputStore
from scadbuddy.render.glb import BoundingBox
from scadbuddy.render.job_models import Job
from scadbuddy.store.index import Pool
from tests.support.store import local_content

#: The ``outputs`` columns a test may set with `update_row`; all but ``meta`` are JSON.
_JSON_COLUMNS = frozenset(
    {"params", "inputs", "record", "manifest", "bom", "arranged_from", "backfill"}
)


def output_store(
    tmp_path: Path,
    pool: Pool,
    *,
    paths: DataPaths | None = None,
    prints: OutputPrintStore | None = None,
) -> OutputStore:
    """Rows on ``pool``, bytes on a local content store at ``paths.output_blobs``, where
    an app built on the same ``paths`` keeps the outputs' blobs on `local` (plan D7).
    ``paths`` defaults to a data root under ``tmp_path``."""
    paths = paths if paths is not None else DataPaths(root=tmp_path / "data")
    content = local_content(paths.output_blobs, pool)
    content.spool = tmp_path / "blob-spool"
    return OutputStore(paths, rows=OutputRows(pool), bytes=OutputBytes(content), prints=prints)


def save_now(store: OutputStore, job: Job, **kwargs: Any) -> OutputMeta:
    """`OutputStore.save` from a test with no running loop. Blocking."""
    return asyncio.run(store.save(job, **kwargs))


def update_row(store: OutputStore, output_id: str, **columns: Any) -> None:
    """Set columns of the output's row by hand, as a test of what a reader makes of
    them. ``meta`` takes an `OutputMeta` or a dict."""
    assignments: list[sql.Composable] = []
    values: list[Any] = []
    for column, value in columns.items():
        if column == "meta":
            data = value.model_dump(mode="json") if isinstance(value, OutputMeta) else value
            values.append(Jsonb(data))
        elif column in _JSON_COLUMNS:
            values.append(None if value is None else Jsonb(value))
        else:
            raise ValueError(f"not a column a test sets: {column}")
        assignments.append(sql.SQL("{} = %s").format(sql.Identifier(column)))
    query = sql.SQL("UPDATE outputs SET {} WHERE output_id = %s").format(
        sql.SQL(", ").join(assignments)
    )
    with store.rows._pool.connection() as conn:
        conn.execute(query, [*values, output_id])


def copy_row(store: OutputStore, meta: OutputMeta, *, slug: str, output_id: str) -> OutputMeta:
    """``meta``'s output again as ``output_id`` of template ``slug``, naming the same
    blobs: a second template's output without rendering a second template (#1864)."""
    row = store.rows.get(meta.id)
    assert row is not None
    copied = meta.model_copy(update={"id": output_id, "slug": slug})
    store.rows.insert(dataclasses.replace(row, output_id=output_id, slug=slug, meta=copied))
    return store.get(output_id)


def output_meta(output_id: str, slug: str, *, created_at: datetime | None = None) -> OutputMeta:
    """A bare output record: no name, one 1 mm box."""
    return OutputMeta(
        id=output_id,
        slug=slug,
        job_id="j",
        created_at=created_at or datetime.now(UTC),
        bbox_mm=BoundingBox(min=(0, 0, 0), max=(1, 1, 1), size=(1, 1, 1)),
    )


def insert_row(store: OutputStore, meta: OutputMeta, **columns: Any) -> None:
    """An output's row with ``meta`` and no bytes, as a test's record of an output it
    never rendered. ``columns`` sets `OutputRow` fields (``params``, ``manifest`` ...)."""
    fields: dict[str, Any] = {
        "params": {},
        "inputs": None,
        "record": None,
        "manifest": None,
        "bom": None,
        "arranged_from": None,
        "backfill": None,
        "has_plate_cover": False,
        **columns,
    }
    store.rows.insert(
        OutputRow(output_id=meta.id, slug=meta.slug, meta=meta, copied_at=meta.created_at, **fields)
    )


def app_outputs(client: TestClient) -> OutputStore:
    """The app's `OutputStore`."""
    outputs: OutputStore = getattr(client.app.state, STATE_ATTR).outputs  # type: ignore[attr-defined]
    return outputs
