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


@pytest.mark.requires_postgres
def test_the_print_subjects_migration_keeps_every_output_and_library_row(
    pg_conninfo: str,
) -> None:
    # #1750: both link tables become one subject-keyed `print_links`, and the library
    # file's queue items become `print_sends`. Rows written before it, by either kind,
    # survive with their first sighting; gone stays gone.
    merge = next(i for i, m in enumerate(MIGRATIONS) if m.id.endswith("_print_subjects"))
    output = "a" * 32
    with psycopg.connect(pg_conninfo) as conn:
        migrate(conn, MIGRATIONS[:merge])
        conn.execute(
            "INSERT INTO output_bambuddy_prints"
            " (output_id, archive_id, queue_item_id, plate_id, printer_id, matched_by,"
            "  first_seen)"
            " VALUES (%s, 18, 34, 1, 2, 'queue_item', '2026-10-01T10:00:00Z'),"
            "        (%s, 19, NULL, NULL, NULL, 'content_hash', '2026-10-01T11:00:00Z')",
            (output, output),
        )
        conn.execute(
            "INSERT INTO library_bambuddy_prints"
            " (queue_item_id, library_file_id, plate_id, printer_id, archive_id, name, gone,"
            "  first_seen)"
            " VALUES (51, 89, 1, 2, 40, 'cube.3mf', false, '2026-10-02T10:00:00Z'),"
            "        (52, 89, 2, 2, NULL, NULL, false, '2026-10-02T11:00:00Z'),"
            "        (53, 90, 1, 2, NULL, NULL, true, '2026-10-02T12:00:00Z')"
        )
        assert migrate(conn)[0] == MIGRATIONS[merge].id
        links = conn.execute(
            "SELECT subject, archive_id, matched_by, queue_item_id, plate_id, printer_id,"
            " name, first_seen::text FROM print_links ORDER BY archive_id"
        ).fetchall()
        sends = conn.execute(
            "SELECT subject, queue_item_id, plate_id, printer_id, gone, first_seen::text"
            " FROM print_sends ORDER BY queue_item_id"
        ).fetchall()
    assert links == [
        (f"output:{output}", 18, "queue_item", 34, 1, 2, None, "2026-10-01 10:00:00+00"),
        (f"output:{output}", 19, "content_hash", None, None, None, None, "2026-10-01 11:00:00+00"),
        ("library:89", 40, "queue_item", 51, 1, 2, "cube.3mf", "2026-10-02 10:00:00+00"),
    ]
    assert sends == [
        ("library:89", 51, 1, 2, False, "2026-10-02 10:00:00+00"),
        ("library:89", 52, 2, 2, False, "2026-10-02 11:00:00+00"),
        ("library:90", 53, 1, 2, True, "2026-10-02 12:00:00+00"),
    ]


@pytest.mark.requires_postgres
def test_a_previous_release_writing_the_old_link_tables_still_reaches_the_new_ones(
    pg_conninfo: str,
) -> None:
    # #1750: a pod of the previous release, still draining during the rollout, records
    # into the old tables. Until a later migration drops them, its writes are forwarded.
    output = "a" * 32
    with psycopg.connect(pg_conninfo) as conn:
        migrate(conn)
        conn.execute(
            "INSERT INTO output_bambuddy_prints (output_id, archive_id, matched_by)"
            " VALUES (%s, 18, 'queue_item')",
            (output,),
        )
        conn.execute(
            "INSERT INTO library_bambuddy_prints"
            " (queue_item_id, library_file_id, plate_id, printer_id)"
            " VALUES (51, 89, 1, 2), (52, 89, 1, 2)"
        )
        conn.execute(
            "UPDATE library_bambuddy_prints SET archive_id = 40, name = 'cube.3mf'"
            " WHERE queue_item_id = 51"
        )
        conn.execute("UPDATE library_bambuddy_prints SET gone = true WHERE queue_item_id = 52")
        links = conn.execute(
            "SELECT subject, archive_id, queue_item_id, name FROM print_links ORDER BY archive_id"
        ).fetchall()
        sends = conn.execute(
            "SELECT subject, queue_item_id, gone FROM print_sends ORDER BY queue_item_id"
        ).fetchall()
    assert links == [(f"output:{output}", 18, None, None), ("library:89", 40, 51, "cube.3mf")]
    assert sends == [("library:89", 51, False), ("library:89", 52, True)]
