"""Release B's migration (#7257, plan D2, D5): it drops ``output_index`` and
``output_hold_slugs`` only once the outputs are cut over, and fills the last
``params_text``. Each test migrates to the file before it, then applies it with `migrate`."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from scadbuddy.render.pg_store import MIGRATIONS, migrate

MIGRATION_ID = "20261125T1737Z_outputs_drop_output_index"


def _before() -> int:
    return next(i for i, m in enumerate(MIGRATIONS) if m.id == MIGRATION_ID)


def _exists(conn: psycopg.Connection[Any], table: str) -> bool:
    row = conn.execute("SELECT to_regclass(%s) IS NOT NULL", (table,)).fetchone()
    assert row is not None
    return bool(row[0])


def _output(conn: psycopg.Connection[Any], output_id: str, *, bytes_at: bool = True) -> None:
    conn.execute(
        "INSERT INTO outputs (output_id, slug, meta, created_at, bytes_at)"
        " VALUES (%s, 'demo', %s, now(), CASE WHEN %s THEN now() END)",
        (output_id, Jsonb({"id": output_id, "slug": "demo"}), bytes_at),
    )


def _mark(conn: psycopg.Connection[Any]) -> None:
    run = conn.execute(
        "INSERT INTO output_byte_runs (finished_at) VALUES (now()) RETURNING id"
    ).fetchone()
    assert run is not None
    conn.execute("INSERT INTO output_cutover (byte_run_id) VALUES (%s)", (run[0],))


@pytest.mark.pg_empty
def test_drop_output_index_refuses_without_cut_over(pg_conninfo: str) -> None:
    """Review Focus 1: an output and no mark, as on a `bambuddy` store. The migration
    raises with the text that names the cause and the remedy, and nothing of it stays:
    `migrate` applies the files in one transaction."""
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        migrate(conn, MIGRATIONS[: _before()])
        _output(conn, "saved")
        with pytest.raises(psycopg.errors.RaiseException) as raised:
            migrate(conn)
        text = str(raised.value)
        assert "not cut over (#7257)" in text
        assert "bambuddy" in text
        assert "Move the store to s3 or local" in text
        assert "cut_over_at" in text
        assert _exists(conn, "output_index")
        assert _exists(conn, "output_hold_slugs")
        applied = conn.execute(
            "SELECT 1 FROM scadbuddy_migrations WHERE id = %s", (MIGRATION_ID,)
        ).fetchone()
        assert applied is None


@pytest.mark.pg_empty
def test_drop_output_index_refuses_with_only_an_index_row(pg_conninfo: str) -> None:
    """An ``output_index`` row alone (no ``outputs`` row) is a database that held an
    output: no mark, no drop."""
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        migrate(conn, MIGRATIONS[: _before()])
        conn.execute(
            "INSERT INTO output_index (output_id, slug, created_at) VALUES ('old', 'demo', now())"
        )
        with pytest.raises(psycopg.errors.RaiseException, match="not cut over"):
            migrate(conn)
        assert _exists(conn, "output_index")


@pytest.mark.pg_empty
def test_drop_output_index_refuses_with_bytes_pending(pg_conninfo: str) -> None:
    """The mark is set, but a row's bytes are only in files (a phase 2 pod saved it
    during release A's roll)."""
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        migrate(conn, MIGRATIONS[: _before()])
        _mark(conn)
        _output(conn, "files", bytes_at=False)
        with pytest.raises(psycopg.errors.RaiseException) as raised:
            migrate(conn)
        assert "bytes are only in files" in str(raised.value)
        assert "copy-status" in str(raised.value)
        assert _exists(conn, "output_index")


@pytest.mark.pg_empty
def test_drop_output_index_on_new_database(pg_conninfo: str) -> None:
    """No output was ever saved (a new install, a test schema): it applies with no mark."""
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        migrate(conn, MIGRATIONS[: _before()])
        assert MIGRATION_ID in migrate(conn)
        assert not _exists(conn, "output_index")
        assert not _exists(conn, "output_hold_slugs")
        default = conn.execute(
            "SELECT column_default, is_nullable FROM information_schema.columns"
            " WHERE table_name = 'outputs' AND column_name = 'params_text'"
        ).fetchone()
        assert default == ("''::text", "NO")


@pytest.mark.pg_empty
def test_drop_output_index_fills_params_text(pg_conninfo: str) -> None:
    """D5: a row still NULL takes its index row's text (same id and slug); a row with
    text keeps it; a row with no index row, or one of another slug, gets ''. Then the
    column is NOT NULL with a default of ''."""
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        migrate(conn, MIGRATIONS[: _before()])
        _mark(conn)
        for output_id in ("null", "kept", "none", "other"):
            _output(conn, output_id)
        conn.execute("UPDATE outputs SET params_text = NULL")
        conn.execute("UPDATE outputs SET params_text = 'w=2' WHERE output_id = 'kept'")
        conn.execute(
            "INSERT INTO output_index (output_id, slug, created_at, params_text) VALUES"
            " ('null', 'demo', now(), 'w=1'), ('kept', 'demo', now(), 'w=9'),"
            " ('other', 'elsewhere', now(), 'w=3')"
        )
        assert MIGRATION_ID in migrate(conn)
        rows = conn.execute(
            "SELECT output_id, params_text FROM outputs ORDER BY output_id"
        ).fetchall()
        assert rows == [("kept", "w=2"), ("none", ""), ("null", "w=1"), ("other", "")]
        conn.execute(
            "INSERT INTO outputs (output_id, slug, meta, created_at)"
            """ VALUES ('new', 'demo', '{"id": "new"}', now())"""
        )
        assert conn.execute(
            "SELECT params_text FROM outputs WHERE output_id = 'new'"
        ).fetchone() == ("",)
        with pytest.raises(psycopg.errors.NotNullViolation):
            conn.execute("UPDATE outputs SET params_text = NULL WHERE output_id = 'new'")
