"""The migration files and their ledger (#491): applied by file id in timestamp order,
out of order when an older file arrives late, and the one-time conversion of the
positional ledger from before #491."""

from __future__ import annotations

import threading
from pathlib import Path

import psycopg
import pytest

from scadbuddy.render.pg_store import (
    LEGACY_VERSIONS,
    MIGRATION_ID,
    MIGRATIONS,
    Migration,
    MigrationLedgerError,
    load_migrations,
    migrate,
)

#: The positional list as it stood before #491, in order.
_LEGACY = MIGRATIONS[: len(LEGACY_VERSIONS)]


def test_files_are_named_and_ordered_by_timestamp() -> None:
    ids = [m.id for m in MIGRATIONS]
    assert ids == sorted(ids)
    assert all(MIGRATION_ID.fullmatch(i) for i in ids)
    assert [m.id for m in _LEGACY] == list(LEGACY_VERSIONS)


def test_load_refuses_a_misnamed_file(tmp_path: Path) -> None:
    (tmp_path / "20260102T0000Z_b.sql").write_text("SELECT 2")
    (tmp_path / "20260101T2359Z_a.sql").write_text("SELECT 1")
    assert load_migrations(tmp_path) == (
        Migration("20260101T2359Z_a", "SELECT 1"),
        Migration("20260102T0000Z_b", "SELECT 2"),
    )
    (tmp_path / "2026-01-03_c.sql").write_text("SELECT 3")
    with pytest.raises(ValueError, match="is not a migration"):
        load_migrations(tmp_path)


def _ledger(conninfo: str) -> list[tuple[str, int | None]]:
    with psycopg.connect(conninfo) as conn:
        return [
            (row[0], row[1])
            for row in conn.execute("SELECT id, version FROM scadbuddy_migrations ORDER BY id")
        ]


def _columns(conninfo: str) -> set[str]:
    with psycopg.connect(conninfo) as conn:
        return {
            row[0]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_schema = current_schema() AND table_name = 'scadbuddy_migrations'"
            )
        }


def _positional_ledger(conninfo: str, versions: list[int]) -> None:
    """The ledger as it was before #491, with `versions` recorded and those of them
    that main had actually applied."""
    with psycopg.connect(conninfo) as conn:
        conn.execute(
            "CREATE TABLE scadbuddy_migrations ("
            " version integer PRIMARY KEY,"
            " applied_at timestamptz NOT NULL DEFAULT now())"
        )
        for version in versions:
            if version <= len(_LEGACY):
                conn.execute(_LEGACY[version - 1].sql.encode())
            conn.execute("INSERT INTO scadbuddy_migrations (version) VALUES (%s)", (version,))


@pytest.mark.requires_postgres
def test_fresh_database_applies_every_file_in_order(pg_conninfo: str) -> None:
    with psycopg.connect(pg_conninfo) as conn:
        assert migrate(conn) == [m.id for m in MIGRATIONS]
        assert migrate(conn) == []
    # The pre-#491 files keep their position, so an older image still reads the ledger.
    assert _ledger(pg_conninfo)[: len(LEGACY_VERSIONS)] == [
        (legacy, n) for n, legacy in enumerate(LEGACY_VERSIONS, start=1)
    ]


@pytest.mark.requires_postgres
def test_an_older_file_that_arrives_late_is_still_applied(pg_conninfo: str) -> None:
    newer = Migration("29990102T0000Z_newer", "CREATE TABLE newer (id int)")
    older = Migration("29990101T0000Z_older", "INSERT INTO newer VALUES (1)")
    with psycopg.connect(pg_conninfo) as conn:
        assert migrate(conn, (*MIGRATIONS, newer)) == [*(m.id for m in MIGRATIONS), newer.id]
        assert migrate(conn, (*MIGRATIONS, newer, older)) == [older.id]
        assert conn.execute("SELECT count(*) FROM newer").fetchone() == (1,)
        # Several unapplied files go in timestamp order, whatever order they are given in.
        d = Migration("29990104T0000Z_d", "INSERT INTO c VALUES (1)")
        c = Migration("29990103T0000Z_c", "CREATE TABLE c (id int)")
        assert migrate(conn, (*MIGRATIONS, newer, older, d, c)) == [c.id, d.id]
        # A row for a file this build does not have (a newer image ran it) is left alone.
        assert migrate(conn) == []
    assert (d.id, None) in _ledger(pg_conninfo)


@pytest.mark.requires_postgres
def test_positional_ledger_is_rewritten_to_file_ids_without_rerunning(pg_conninfo: str) -> None:
    _positional_ledger(pg_conninfo, list(range(1, len(LEGACY_VERSIONS) + 1)))
    with psycopg.connect(pg_conninfo) as conn:
        # Re-running any of them would fail: their tables and columns exist.
        assert migrate(conn) == [m.id for m in MIGRATIONS[len(LEGACY_VERSIONS) :]]
        assert migrate(conn) == []
    assert _ledger(pg_conninfo)[: len(LEGACY_VERSIONS)] == [
        (legacy, n) for n, legacy in enumerate(LEGACY_VERSIONS, start=1)
    ]


@pytest.mark.requires_postgres
def test_positional_ledger_applies_what_it_had_not_run(pg_conninfo: str) -> None:
    _positional_ledger(pg_conninfo, [1, 2])
    with psycopg.connect(pg_conninfo) as conn:
        assert migrate(conn) == [m.id for m in MIGRATIONS[2:]]
        assert conn.execute("SELECT count(*) FROM events").fetchone() == (0,)


@pytest.mark.requires_postgres
def test_two_pods_convert_the_positional_ledger_once(pg_conninfo: str) -> None:
    _positional_ledger(pg_conninfo, [1, 2])
    results: list[list[str]] = []
    errors: list[BaseException] = []

    def start() -> None:
        try:
            with psycopg.connect(pg_conninfo) as conn:
                results.append(migrate(conn))
        except BaseException as exc:  # reported below
            errors.append(exc)

    threads = [threading.Thread(target=start) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert sorted(i for applied in results for i in applied) == [m.id for m in MIGRATIONS[2:]]
    assert len(_ledger(pg_conninfo)) == len(MIGRATIONS)


@pytest.mark.requires_postgres
def test_a_positional_row_main_never_had_is_refused_and_nothing_changes(
    pg_conninfo: str,
) -> None:
    _positional_ledger(pg_conninfo, [*range(1, len(LEGACY_VERSIONS) + 1), 9])
    with (
        psycopg.connect(pg_conninfo) as conn,
        pytest.raises(
            MigrationLedgerError, match=r"positional version\(s\) 9, but main only ever had 4"
        ),
    ):
        migrate(conn)
    assert "id" not in _columns(pg_conninfo)


@pytest.mark.requires_postgres
def test_the_projection_migration_backfills_inputs_of_existing_rows(pg_conninfo: str) -> None:
    # Applies every file before the projection one, inserts a row as the legacy queue
    # wrote it then (no inputs column), and then applies the rest: the real UPDATE runs.
    projection = next(
        i for i, m in enumerate(MIGRATIONS) if m.id.endswith("_render_jobs_projection")
    )
    with psycopg.connect(pg_conninfo) as conn:
        migrate(conn, MIGRATIONS[:projection])
        conn.execute(
            "INSERT INTO render_jobs (id, slug, params, state, created_at, render_key)"
            " VALUES ('old', 'demo', '{\"width\": 7}', 'done', now(), 'k')"
        )
        assert migrate(conn)[0] == MIGRATIONS[projection].id
        row = conn.execute("SELECT inputs FROM render_jobs WHERE id = 'old'").fetchone()
    assert row == ({"params": {"width": 7}},)
