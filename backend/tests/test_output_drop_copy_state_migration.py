"""The migration that drops the retired copies' state (#7887, plan 2026-11-25 D11): on a
database with release B's schema, a cut-over mark and its runs, it drops the run tables,
the mark and the copy-only columns of ``outputs``, and keeps the rows."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from scadbuddy.render.pg_store import MIGRATIONS, migrate

MIGRATION_ID = "20261126T1043Z_outputs_drop_copy_state"
DROPPED_TABLES = ("output_cutover", "output_byte_runs", "output_copy_runs")
DROPPED_COLUMNS = ("copied_at", "files_stamp", "bytes_stamp", "damaged")


def _before() -> int:
    return next(i for i, m in enumerate(MIGRATIONS) if m.id == MIGRATION_ID)


def _exists(conn: psycopg.Connection[Any], table: str) -> bool:
    row = conn.execute("SELECT to_regclass(%s) IS NOT NULL", (table,)).fetchone()
    assert row is not None
    return bool(row[0])


def _columns(conn: psycopg.Connection[Any]) -> set[str]:
    found = conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'outputs'"
    ).fetchall()
    return {str(row[0]) for row in found}


@pytest.mark.pg_empty
def test_drop_copy_state_on_release_b_schema(pg_conninfo: str) -> None:
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        migrate(conn, MIGRATIONS[: _before()])
        conn.execute("INSERT INTO output_copy_runs (finished_at) VALUES (now())")
        run = conn.execute(
            "INSERT INTO output_byte_runs (finished_at) VALUES (now()) RETURNING id"
        ).fetchone()
        assert run is not None
        conn.execute("INSERT INTO output_cutover (byte_run_id) VALUES (%s)", (run[0],))
        conn.execute(
            "INSERT INTO outputs (output_id, slug, meta, created_at, bytes_at, files_stamp,"
            " bytes_stamp, damaged) VALUES ('o1', 'demo', %s, now(), now(), 's', 'b', %s)",
            (Jsonb({"id": "o1", "slug": "demo"}), Jsonb([{"role": "model", "error": "x"}])),
        )
        assert set(DROPPED_COLUMNS) <= _columns(conn)

        assert MIGRATION_ID in migrate(conn)

        for table in DROPPED_TABLES:
            assert not _exists(conn, table), table
        assert not set(DROPPED_COLUMNS) & _columns(conn)
        row = conn.execute("SELECT output_id, bytes_at FROM outputs").fetchone()
        assert row is not None and row[0] == "o1" and row[1] is not None
