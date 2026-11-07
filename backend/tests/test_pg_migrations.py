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
def test_the_old_link_tables_and_their_forwarding_are_gone(pg_conninfo: str) -> None:
    # #1772: once no pod of a release before #1771 runs, the old link tables, their
    # forwarding triggers and functions are dropped.
    with psycopg.connect(pg_conninfo) as conn:
        migrate(conn)
        tables = conn.execute(
            "SELECT table_name FROM information_schema.tables"
            " WHERE table_schema = current_schema()"
            " AND table_name IN ('output_bambuddy_prints', 'library_bambuddy_prints')"
        ).fetchall()
        functions = conn.execute(
            "SELECT proname FROM pg_proc"
            " WHERE proname IN ('print_links_forward_output', 'print_links_forward_library')"
            " AND pronamespace = current_schema()::regnamespace"
        ).fetchall()
    assert tables == []
    assert functions == []


@pytest.mark.requires_postgres
def test_the_refused_request_migration_backfills_unedited_refusals(pg_conninfo: str) -> None:
    # #3932 review: a refusal recorded before `refused_request` existed would read stale
    # after any edit, even one back to the choices refused. An unedited one takes the
    # choices it holds; an edited (stale) one keeps none, as stale already says.
    column = next(
        i for i, m in enumerate(MIGRATIONS) if m.id.endswith("_print_batch_refused_request")
    )
    refused = '{"status": "refused", "attempt": 1, "stale": false}'
    edited = '{"status": "refused", "attempt": 0, "stale": true}'
    with psycopg.connect(pg_conninfo) as conn:
        migrate(conn, MIGRATIONS[:column])
        conn.execute("INSERT INTO print_batches (id) VALUES ('b')")
        conn.execute(
            "INSERT INTO print_batch_items"
            " (batch_id, position, library_file_id, filename, request, outcome)"
            " VALUES ('b', 0, 89, 'a.3mf', '{\"copies\": 1}', %s),"
            "        ('b', 1, 91, 'b.3mf', '{\"copies\": 2}', %s),"
            "        ('b', 2, 92, 'c.3mf', '{\"copies\": 3}', NULL)",
            (refused, edited),
        )
        assert migrate(conn)[0] == MIGRATIONS[column].id
        rows = conn.execute(
            "SELECT position, refused_request FROM print_batch_items ORDER BY position"
        ).fetchall()
    assert rows == [(0, {"copies": 1}), (1, None), (2, None)]


def _index(slug: str) -> int:
    return next(i for i, m in enumerate(MIGRATIONS) if m.id.endswith(f"_{slug}"))


@pytest.mark.requires_postgres
def test_the_contract_migration_drops_only_what_the_previous_build_never_names(
    pg_conninfo: str,
) -> None:
    # #1142, expand/contract: the pending key and the old watcher's log go; the columns
    # the previous build still reads and writes stay for a later file (#5139).
    with psycopg.connect(pg_conninfo) as conn:
        migrate(conn, MIGRATIONS[: _index("drop_previous_build_columns")])
        columns = conn.execute(
            "SELECT table_name, column_name FROM information_schema.columns"
            " WHERE table_schema = current_schema() AND (table_name, column_name) IN"
            " (('print_runs', 'heartbeat_at'), ('render_jobs', 'params'),"
            "  ('workflow_definitions', 'script')) ORDER BY 1"
        ).fetchall()
        relations = conn.execute(
            "SELECT to_regclass('render_jobs_pending_key'), to_regclass('print_watches')"
        ).fetchone()
    assert columns == [
        ("print_runs", "heartbeat_at"),
        ("render_jobs", "params"),
        ("workflow_definitions", "script"),
    ]
    assert relations == (None, None)


@pytest.mark.requires_postgres
def test_the_contract_migration_ends_the_rows_an_older_build_left_unsettled(
    pg_conninfo: str,
) -> None:
    # Nothing of this build settles them: a pre-#1052 run (no execution) and a render row
    # an older build inserted (no `workflow_run_id`). Rows of this build are left alone,
    # and a row written with no inputs keeps its parameters in them.
    contract = _index("render_pending_key_and_print_watches")
    with psycopg.connect(pg_conninfo) as conn:
        migrate(conn, MIGRATIONS[:contract])
        conn.execute(
            "INSERT INTO print_runs (id, output_id, idempotency_key, status, workflow_id,"
            " workflow_run_id) VALUES ('old', 'o', 'k1', 'running', NULL, NULL),"
            " ('new', 'o', 'k2', 'running', 'print-run-new', 'r')"
        )
        conn.execute(
            "INSERT INTO render_jobs (id, slug, params, inputs, state, created_at, render_key,"
            " workflow_id, workflow_run_id) VALUES"
            " ('legacy', 'demo', '{}', '{\"params\": {}}', 'pending', now(), 'k',"
            "  'render-legacy', NULL),"
            " ('ours', 'demo', '{}', '{\"params\": {}}', 'pending', now(), 'k2', 'render-k2',"
            "  'r'),"
            " ('bare', 'demo', '{\"width\": 7}', '{}', 'done', now(), 'k3', 'render-k3', 'r')"
        )
        assert migrate(conn)[0] == MIGRATIONS[contract].id
        runs = conn.execute(
            "SELECT id, status, enqueue_attempted FROM print_runs ORDER BY id"
        ).fetchall()
        jobs = conn.execute("SELECT id, state, inputs FROM render_jobs ORDER BY id").fetchall()
    assert runs == [("new", "running", False), ("old", "failed", True)]
    assert jobs == [
        ("bare", "done", {"params": {"width": 7}, "v": 0}),
        ("legacy", "failed", {"params": {}}),
        ("ours", "pending", {"params": {}}),
    ]


def _contract_columns(conn: psycopg.Connection[tuple[object, ...]]) -> list[tuple[str, str]]:
    rows = conn.execute(
        "SELECT table_name, column_name FROM information_schema.columns"
        " WHERE table_schema = current_schema() AND (table_name, column_name) IN"
        " (('print_runs', 'heartbeat_at'), ('render_jobs', 'params'),"
        "  ('workflow_definitions', 'script')) ORDER BY 1"
    ).fetchall()
    return [(str(table), str(column)) for table, column in rows]


@pytest.mark.requires_postgres
def test_the_second_contract_step_drops_the_columns_the_previous_build_read(
    pg_conninfo: str,
) -> None:
    # #5139: once no pod of the build before #5124 runs, nothing names them.
    with psycopg.connect(pg_conninfo) as conn:
        migrate(conn)
        assert _contract_columns(conn) == []


@pytest.mark.requires_postgres
def test_a_flow_version_with_no_commit_keeps_the_script_column_without_failing(
    pg_conninfo: str,
) -> None:
    # Its script is the only copy, and migrations run at the API's start: a raise would be
    # an outage, so the column stays, with a notice, and the rest of the step applies.
    drop = _index("workflow_definitions_drop_script")
    with psycopg.connect(pg_conninfo) as conn:
        migrate(conn, MIGRATIONS[:drop])
        conn.execute(
            "INSERT INTO workflow_definitions (id, name, version, script, created_by)"
            " VALUES ('old', 'swap', 1, 'x', '{}')"
        )
        notices: list[str] = []
        conn.add_notice_handler(lambda diag: notices.append(diag.message_primary or ""))
        assert migrate(conn) == [m.id for m in MIGRATIONS[drop:]]
        assert _contract_columns(conn) == [("workflow_definitions", "script")]
        script = conn.execute("SELECT script FROM workflow_definitions").fetchone()
    assert script == ("x",)
    assert any("workflow_definitions.script kept" in n for n in notices), notices


@pytest.mark.requires_postgres
def test_the_archive_copied_backfill_marks_every_attempt_from_before_it(
    pg_conninfo: str,
) -> None:
    # #5276: the first backfill could not tell an attempt already copied from an archive
    # with no seconds or grams from one never copied, and marked it not copied, so its
    # first read after the deploy copied the times again over a correction. Every attempt
    # naming archives from before that file is taken as copied; one recorded since is
    # left for its first copy.
    backfill = _index("order_attempt_archive_copied_backfill")
    with psycopg.connect(pg_conninfo) as conn:
        migrate(conn, MIGRATIONS[:backfill])
        conn.execute("INSERT INTO customers (id, display_name) VALUES ('c', 'C')")
        conn.execute(
            "INSERT INTO orders (id, number, customer_id, currency, created_by)"
            " VALUES ('o', 'ORD-2026-1', 'c', 'USD', '{}')"
        )
        conn.execute(
            "INSERT INTO order_items (id, order_id, position, quantity, title, kind)"
            " VALUES ('i', 'o', 0, 1, 'Bracket', 'custom')"
        )
        conn.execute(
            "INSERT INTO order_item_fulfilments"
            " (id, order_item_id, attempt, quantity, outcome, created_by, archive_ids,"
            "  archive_copied, created_at) VALUES"
            " ('before', 'i', 1, 1, 'succeeded', '{}', '{81}', false,"
            "  now() - interval '1 hour'),"
            " ('none', 'i', 2, 1, 'succeeded', '{}', '{}', false, now() - interval '1 hour'),"
            " ('since', 'i', 3, 1, 'succeeded', '{}', '{82}', false, now())"
        )
        assert migrate(conn)[0] == MIGRATIONS[backfill].id
        rows = conn.execute(
            "SELECT id, archive_copied FROM order_item_fulfilments ORDER BY attempt"
        ).fetchall()
    assert rows == [("before", True), ("none", False), ("since", False)]


@pytest.mark.requires_postgres
def test_the_revisions_migration_gives_every_model_item_revision_one(pg_conninfo: str) -> None:
    # Storefront phase 1 (#5280): a model item's reference and colours become its
    # revision 1, which its quotes name; a custom item has none.
    colours = '[{"slot": 0, "colour": "#ff0000", "filament_id": null}]'
    with psycopg.connect(pg_conninfo) as conn:
        migrate(conn, MIGRATIONS[: _index("order_item_revisions")])
        conn.execute("INSERT INTO customers (id, display_name) VALUES ('c', 'C')")
        conn.execute(
            "INSERT INTO orders (id, number, customer_id, currency, created_by)"
            " VALUES ('o', 'ORD-2026-1', 'c', 'USD', '{\"kind\": \"browser\"}')"
        )
        conn.execute(
            "INSERT INTO order_items (id, order_id, position, quantity, title, kind, slug,"
            " commit, inputs, template_name, render_key, colours) VALUES"
            " ('model', 'o', 0, 1, 'Demo', 'model', 'demo', %s, '{\"params\": {}}', 'Demo',"
            "  'k', %s)",
            ("a" * 40, colours),
        )
        conn.execute(
            "INSERT INTO order_items (id, order_id, position, quantity, title, kind)"
            " VALUES ('custom', 'o', 1, 1, 'Bracket', 'custom')"
        )
        for item in ("model", "custom"):
            conn.execute(
                "INSERT INTO order_item_quotes (order_item_id, source, plates, lines,"
                " unit_cost_minor, unit_price_minor, quantity, total_minor, created_by)"
                " VALUES (%s, 'manual', '[]', '[]', 1, 1, 1, 1, '{}')",
                (item,),
            )
        conn.execute(
            "INSERT INTO order_item_fulfilments (order_item_id, attempt, quantity, outcome,"
            " created_by) VALUES ('model', 1, 1, 'succeeded', '{}')"
        )
        assert migrate(conn)[0] == MIGRATIONS[_index("order_item_revisions")].id
        rev = conn.execute(
            "SELECT id, item_id, number, slug, colours, sent_at, answer FROM order_item_revisions"
        ).fetchall()
        assert len(rev) == 1
        rev_id, item_id, number, slug, rev_colours, sent_at, answer = rev[0]
        assert (item_id, number, slug) == ("model", 1, "demo")
        assert rev_colours == [{"slot": 0, "colour": "#ff0000", "filament_id": None}]
        assert sent_at is None and answer is None
        assert conn.execute(
            "SELECT id, current_revision_id, approval FROM order_items ORDER BY position"
        ).fetchall() == [("model", rev_id, "together"), ("custom", None, "together")]
        assert dict(
            conn.execute("SELECT order_item_id, revision_id FROM order_item_quotes").fetchall()
        ) == {"model": rev_id, "custom": None}
        assert conn.execute(
            "SELECT purpose, revision_id FROM order_item_fulfilments"
        ).fetchone() == ("production", None)


@pytest.mark.requires_postgres
def test_the_payments_seam_migration(pg_conninfo: str) -> None:
    with psycopg.connect(pg_conninfo) as conn:
        migrate(conn, MIGRATIONS[: _index("payments_seam")])
        customer_id = conn.execute(
            "INSERT INTO customers (display_name) VALUES ('Ada') RETURNING id"
        ).fetchone()[0]  # type: ignore[index]
        order_id = conn.execute(
            "INSERT INTO orders (number, customer_id, currency, created_by)"
            " VALUES ('2026-9001', %s, 'USD', '{}') RETURNING id",
            (customer_id,),
        ).fetchone()[0]  # type: ignore[index]
        conn.execute(
            "INSERT INTO order_payments (order_id, amount_minor) VALUES (%s, 100)", (order_id,)
        )
        assert migrate(conn)[0] == MIGRATIONS[_index("payments_seam")].id
        cols = {
            r[0]
            for r in conn.execute(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_name = 'order_payments'"
            )
        }
        assert {"provider", "payment_request_id"} <= cols
        # the old payment row survives with no provider
        assert conn.execute("SELECT provider FROM order_payments").fetchall() == [(None,)]
        insert = (
            "INSERT INTO payment_requests"
            " (order_id, kind, amount_minor, currency, created_by{extra})"
            " VALUES (%s, %s, %s, 'USD', '{{}}'{more})"
        )
        with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
            conn.execute(insert.format(extra="", more=""), (order_id, "balance", 0))
        with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
            conn.execute(
                insert.format(extra=", hosted_url", more=", 'https://x'"),
                (order_id, "deposit", 5),
            )
        with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
            conn.execute(
                "INSERT INTO payment_requests"
                " (order_id, kind, amount_minor, currency, created_by, status)"
                " VALUES (%s, 'deposit', 5, 'USD', '{}', 'paid')",
                (order_id,),
            )
        conn.execute(
            "INSERT INTO payment_events (provider, event_id, kind) VALUES ('f', 'e1', 'paid')"
        )
        with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
            conn.execute(
                "INSERT INTO payment_events (provider, event_id, kind) VALUES ('f', 'e1', 'paid')"
            )
        cred = (
            "INSERT INTO payment_credentials"
            " (provider, account_label, secret_sealed, dek_sealed, kek_id, is_default)"
            " VALUES (%s, 'x', '\\x00', '\\x00', 'k', true)"
        )
        conn.execute(cred, ("one",))
        with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
            conn.execute(cred, ("two",))
