"""The agent's migration files and their ledger `ai_migrations` (#5299 D6): the API applies
them as the agent's own runner did (same ledger, lock, checksums and positional rewrite).
That both runners agree on every file is `agent/test/schema.test.ts`'s check: this suite
runs in an image holding only backend/."""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path

import psycopg
import pytest

from scadbuddy.render.agent_migrations import (
    AGENT_LEGACY_VERSIONS,
    AGENT_MIGRATION_LOCK,
    AGENT_MIGRATIONS,
    AGENT_MIGRATIONS_DIR,
    DEFAULT_LOCK_TIMEOUT_MS,
    DEFAULT_STATEMENT_TIMEOUT_MS,
    AgentMigrationChecksumError,
    AgentMigrationLedgerError,
    agent_migration_checksum,
    load_agent_migrations,
    migrate_agent,
)
from scadbuddy.render.pg_store import MIGRATION_ID, Migration

_LEGACY = AGENT_MIGRATIONS[: len(AGENT_LEGACY_VERSIONS)]


def test_the_agents_constants_are_kept() -> None:
    # "SCADAGNT" in ASCII, the key the agent's own runner took: an old agent pod and a
    # new API serialise on it during a rolling deploy.
    assert AGENT_MIGRATION_LOCK.to_bytes(8, "big") == b"SCADAGNT"
    assert (DEFAULT_LOCK_TIMEOUT_MS, DEFAULT_STATEMENT_TIMEOUT_MS) == (10_000, 60_000)


def test_files_are_named_ordered_and_the_legacy_ones_are_byte_for_byte() -> None:
    ids = [m.id for m in AGENT_MIGRATIONS]
    assert ids == sorted(ids)
    assert all(MIGRATION_ID.fullmatch(i) for i in ids)
    assert [m.id for m in _LEGACY] == [legacy for legacy, _ in AGENT_LEGACY_VERSIONS]
    # The checksums #354 recorded in every deployed ledger.
    assert [agent_migration_checksum(m) for m in _LEGACY] == [
        "6e2ec704499aed89d132836b34717b1a7ccfafb7a4a1654340ee1b7fc477b613",
        "117ed74ea5125fe163247bd88e3e99c0e75e085d22ec7b220fa99fd4e84bce80",
    ]


def test_the_checksum_is_of_the_files_bytes() -> None:
    for migration in AGENT_MIGRATIONS:
        raw = (AGENT_MIGRATIONS_DIR / f"{migration.id}.sql").read_bytes()
        assert agent_migration_checksum(migration) == hashlib.sha256(raw).hexdigest()


def test_load_keeps_the_bytes_and_refuses_a_misnamed_file(tmp_path: Path) -> None:
    (tmp_path / "20260102T0000Z_b.sql").write_bytes(b"SELECT 2;\r\n")
    (tmp_path / "20260101T2359Z_a.sql").write_text("SELECT 1")
    assert load_agent_migrations(tmp_path) == (
        Migration("20260101T2359Z_a", "SELECT 1"),
        Migration("20260102T0000Z_b", "SELECT 2;\r\n"),
    )
    (tmp_path / "2026-01-03_c.sql").write_text("SELECT 3")
    with pytest.raises(ValueError, match="is not a migration"):
        load_agent_migrations(tmp_path)


def _ledger(conninfo: str) -> list[tuple[str, int | None, str | None, str | None]]:
    with psycopg.connect(conninfo) as conn:
        return [
            (row[0], row[1], row[2], row[3])
            for row in conn.execute(
                "SELECT id, version, story, checksum FROM ai_migrations ORDER BY id"
            )
        ]


def _positional_ledger(conninfo: str, versions: list[int], *, checksums: bool = True) -> None:
    """The ledger as #354 created it (optionally with its later checksum column), with
    `versions` recorded and those of them main had applied."""
    with psycopg.connect(conninfo) as conn:
        conn.execute(
            "CREATE TABLE ai_migrations ("
            " version integer PRIMARY KEY, story text NOT NULL,"
            " applied_at timestamptz NOT NULL DEFAULT now())"
        )
        if checksums:
            conn.execute("ALTER TABLE ai_migrations ADD COLUMN checksum text")
        for version in versions:
            known = version <= len(_LEGACY)
            story = AGENT_LEGACY_VERSIONS[version - 1][1] if known else "branch"
            if known:
                conn.execute(_LEGACY[version - 1].sql.encode())
            if checksums:
                checksum = agent_migration_checksum(_LEGACY[version - 1]) if known else "x"
                conn.execute(
                    "INSERT INTO ai_migrations (version, story, checksum) VALUES (%s, %s, %s)",
                    (version, story, checksum),
                )
            else:
                conn.execute(
                    "INSERT INTO ai_migrations (version, story) VALUES (%s, %s)", (version, story)
                )


@pytest.mark.requires_postgres
def test_apply_once_in_order_recorded_by_id_with_checksums(pg_conninfo: str) -> None:
    ids = [m.id for m in AGENT_MIGRATIONS]
    with psycopg.connect(pg_conninfo) as conn:
        assert migrate_agent(conn) == ids
        assert migrate_agent(conn) == []
    rows = _ledger(pg_conninfo)
    assert [r[0] for r in rows] == ids
    assert [r[3] for r in rows] == [agent_migration_checksum(m) for m in AGENT_MIGRATIONS]
    # The pre-#491 files keep their position and story, so an older image still reads it.
    assert [(r[0], r[1], r[2]) for r in rows[: len(_LEGACY)]] == [
        (legacy, n, story) for n, (legacy, story) in enumerate(AGENT_LEGACY_VERSIONS, start=1)
    ]
    assert all(r[1] is None and r[2] is None for r in rows[len(_LEGACY) :])


@pytest.mark.requires_postgres
def test_an_older_file_that_arrives_late_is_still_applied(pg_conninfo: str) -> None:
    newer = Migration("29990102T0000Z_newer", "CREATE TABLE ai_newer (id int PRIMARY KEY)")
    older = Migration(
        "29990101T0000Z_older", "CREATE TABLE ai_older (id int); INSERT INTO ai_newer VALUES (1)"
    )
    with psycopg.connect(pg_conninfo) as conn:
        assert migrate_agent(conn, (*AGENT_MIGRATIONS, newer)) == [
            *(m.id for m in AGENT_MIGRATIONS),
            newer.id,
        ]
        assert migrate_agent(conn, (*AGENT_MIGRATIONS, newer, older)) == [older.id]
        assert conn.execute("SELECT count(*) FROM ai_newer").fetchone() == (1,)
        d = Migration("29990104T0000Z_d", "INSERT INTO ai_c VALUES (1)")
        c = Migration("29990103T0000Z_c", "CREATE TABLE ai_c (id int)")
        assert migrate_agent(conn, (*AGENT_MIGRATIONS, newer, older, d, c)) == [c.id, d.id]
        # A row for a file this build does not have (a newer image ran it) is left alone.
        assert migrate_agent(conn) == []
    assert d.id in [r[0] for r in _ledger(pg_conninfo)]


@pytest.mark.requires_postgres
def test_two_pods_starting_together_apply_each_file_once(pg_conninfo: str) -> None:
    results: list[list[str]] = []
    errors: list[BaseException] = []

    def start() -> None:
        try:
            with psycopg.connect(pg_conninfo) as conn:
                results.append(migrate_agent(conn))
        except BaseException as exc:  # reported below
            errors.append(exc)

    threads = [threading.Thread(target=start) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert sorted(i for applied in results for i in applied) == [m.id for m in AGENT_MIGRATIONS]


@pytest.mark.requires_postgres
def test_an_applied_file_whose_sql_changed_is_refused(pg_conninfo: str) -> None:
    first = AGENT_MIGRATIONS[0]
    edited = (Migration(first.id, f"{first.sql}\n-- edited"), *AGENT_MIGRATIONS[1:])
    with psycopg.connect(pg_conninfo) as conn:
        migrate_agent(conn)
        with pytest.raises(
            AgentMigrationChecksumError,
            match=f"ai migration {first.id} was applied with different SQL",
        ):
            migrate_agent(conn, edited)
        # The original still passes.
        assert migrate_agent(conn) == []


@pytest.mark.requires_postgres
def test_positional_ledger_is_rewritten_to_file_ids_without_rerunning(pg_conninfo: str) -> None:
    _positional_ledger(pg_conninfo, list(range(1, len(_LEGACY) + 1)))
    with psycopg.connect(pg_conninfo) as conn:
        assert migrate_agent(conn) == [m.id for m in AGENT_MIGRATIONS[len(_LEGACY) :]]
        assert migrate_agent(conn) == []
    rows = [r for r in _ledger(pg_conninfo) if r[1] is not None]
    assert [(r[0], r[1], r[3]) for r in sorted(rows, key=lambda r: r[1] or 0)] == [
        (m.id, n, agent_migration_checksum(m)) for n, m in enumerate(_LEGACY, start=1)
    ]


@pytest.mark.requires_postgres
def test_two_pods_convert_the_positional_ledger_once(pg_conninfo: str) -> None:
    _positional_ledger(pg_conninfo, [1, 2])
    results: list[list[str]] = []
    errors: list[BaseException] = []

    def start() -> None:
        try:
            with psycopg.connect(pg_conninfo) as conn:
                results.append(migrate_agent(conn))
        except BaseException as exc:  # reported below
            errors.append(exc)

    threads = [threading.Thread(target=start) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert sorted(i for applied in results for i in applied) == [m.id for m in AGENT_MIGRATIONS[2:]]
    assert len(_ledger(pg_conninfo)) == len(AGENT_MIGRATIONS)


@pytest.mark.requires_postgres
def test_positional_ledger_applies_what_it_had_not_run_and_adopts_rows_without_a_checksum(
    pg_conninfo: str,
) -> None:
    # #354's ledger before the checksum column, with only entry 1 applied.
    _positional_ledger(pg_conninfo, [1], checksums=False)
    with psycopg.connect(pg_conninfo) as conn:
        assert migrate_agent(conn) == [m.id for m in AGENT_MIGRATIONS[1:]]
    rows = {r[0]: r for r in _ledger(pg_conninfo)}
    assert rows[_LEGACY[1].id][1:3] == (2, "#300")
    assert rows[_LEGACY[0].id][3] == agent_migration_checksum(_LEGACY[0])
    edited = (
        Migration(_LEGACY[0].id, _LEGACY[0].sql.replace("text", "varchar")),
        *AGENT_MIGRATIONS[1:],
    )
    with psycopg.connect(pg_conninfo) as conn, pytest.raises(AgentMigrationChecksumError):
        migrate_agent(conn, edited)


@pytest.mark.requires_postgres
def test_a_positional_row_main_never_had_is_refused_and_nothing_changes(
    pg_conninfo: str,
) -> None:
    _positional_ledger(pg_conninfo, [1, 2, 3])
    with (
        psycopg.connect(pg_conninfo) as conn,
        pytest.raises(
            AgentMigrationLedgerError, match=r"positional version\(s\) 3, but main only ever had 2"
        ),
    ):
        migrate_agent(conn)
    with psycopg.connect(pg_conninfo) as conn:
        columns = {
            row[0]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_schema = current_schema() AND table_name = 'ai_migrations'"
            )
        }
    assert "id" not in columns


@pytest.mark.requires_postgres
def test_a_held_lock_gives_up_after_lock_timeout(pg_conninfo: str) -> None:
    # The test's own key, as the agent's test did (#1249): nothing else holds it.
    key = AGENT_MIGRATION_LOCK + 1249
    with psycopg.connect(pg_conninfo, autocommit=True) as holder:
        holder.execute("SELECT pg_advisory_lock(%s)", (key,))
        with (
            psycopg.connect(pg_conninfo) as conn,
            pytest.raises(psycopg.errors.LockNotAvailable),
        ):
            migrate_agent(conn, lock_timeout_ms=200, lock_key=key)
        holder.execute("SELECT pg_advisory_unlock(%s)", (key,))
    with psycopg.connect(pg_conninfo) as conn:
        assert migrate_agent(conn, lock_key=key) == [m.id for m in AGENT_MIGRATIONS]


@pytest.mark.requires_postgres
def test_the_timeouts_are_local_to_the_migration(pg_conninfo: str) -> None:
    probe = Migration(
        "29990101T0000Z_probe",
        "CREATE TABLE ai_probe AS SELECT current_setting('lock_timeout') AS lock,"
        " current_setting('statement_timeout') AS statement",
    )
    with psycopg.connect(pg_conninfo) as conn:
        migrate_agent(conn, (probe,), lock_timeout_ms=1234, statement_timeout_ms=5678)
        assert conn.execute("SELECT lock, statement FROM ai_probe").fetchone() == (
            "1234ms",
            "5678ms",
        )
        assert conn.execute("SHOW lock_timeout").fetchone() == ("0",)
