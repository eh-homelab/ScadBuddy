"""The migration files and their ledger (#491): applied by file id in timestamp order,
out of order when an older file arrives late, and the one-time conversion of the
positional ledger from before #491."""

from __future__ import annotations

import threading
from pathlib import Path

import psycopg
import pytest
from psycopg.types.json import Jsonb

from scadbuddy.render.pg_store import (
    LEGACY_VERSIONS,
    MIGRATION_ID,
    MIGRATIONS,
    Migration,
    MigrationLedgerError,
    load_migrations,
    migrate,
)

pytestmark = pytest.mark.pg_empty

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


_DOT3 = "\u2022\u2022\u2022"


def _deposit(conn: psycopg.Connection[tuple[object, ...]], tx: str, text: str) -> None:
    conn.execute(
        "INSERT INTO bank_deposits (transaction_id, item_id, amount_minor, currency,"
        " posted_on, description) VALUES (%s, 'item', 100, 'USD', '2026-11-01', %s)",
        (tx, text),
    )


@pytest.mark.requires_postgres
def test_the_remask_migration_masks_text_stored_under_the_old_rule(pg_conninfo: str) -> None:
    # #5852 review: before it, the mask kept any order-number match as written, so an
    # account number written as one could be stored. Every order number and every run of
    # four or more digits, counted across single spaces and hyphens as `mask` counts
    # them, is masked; a real order's number goes too.
    column = _index("bank_deposits_remask")
    stored = [
        "PAY ORD-2026-000123456789 THANKS",
        "PAY ORD-2026-12 3456",
        "pay ord-2026-12-34 and ORD-2026-7",
        "ACCT 1234 REF 567",
        f"REF 123 ACCT {_DOT3}",
        "ACCT 12 34 56 78 END",
        "ACCT 12-34-5 REF 1 2",
    ]
    with psycopg.connect(pg_conninfo) as conn:
        migrate(conn, MIGRATIONS[:column])
        for i, text in enumerate(stored):
            _deposit(conn, f"t{i}", text)
        assert migrate(conn)[0] == MIGRATIONS[column].id
        rows = conn.execute(
            "SELECT description FROM bank_deposits ORDER BY transaction_id"
        ).fetchall()
    assert [r[0] for r in rows] == [
        f"PAY ORD-{_DOT3} THANKS",
        f"PAY ORD-{_DOT3}",
        f"pay ORD-{_DOT3} and ORD-{_DOT3}",
        f"ACCT {_DOT3} REF 567",
        f"REF 123 ACCT {_DOT3}",
        f"ACCT {_DOT3} END",
        f"ACCT {_DOT3} REF 1 2",
    ]


@pytest.mark.requires_postgres
def test_the_remask_migration_masks_the_notes_a_confirmed_deposit_copied(
    pg_conninfo: str,
) -> None:
    # #5852 security review: "Record this deposit as payment" copies the description
    # into the payment's notes (`record_deposit`), so that copy is masked too: the
    # payment the deposit row names, and a `bank_transfer` payment whose reference is a
    # deposit's transaction id. A payment typed by hand keeps its notes.
    column = _index("bank_deposits_remask")
    leaked = "PAY ORD-2026-000123456789"
    with psycopg.connect(pg_conninfo) as conn:
        migrate(conn, MIGRATIONS[:column])
        conn.execute("INSERT INTO customers (id, display_name) VALUES ('c', 'C')")
        conn.execute(
            "INSERT INTO orders (id, number, customer_id, currency, created_by)"
            " VALUES ('o', 'ORD-2026-1', 'c', 'USD', '{}')"
        )
        _deposit(conn, "t-linked", leaked)
        _deposit(conn, "t-ref", leaked)
        conn.execute(
            "INSERT INTO order_payments (id, order_id, amount_minor, method, reference, notes)"
            " VALUES ('linked', 'o', 100, 'bank_transfer', 'other', %(n)s),"
            "        ('ref', 'o', 100, 'bank_transfer', 't-ref', %(n)s),"
            "        ('cash', 'o', 100, 'cash', 't-ref', 'TILL 12345678'),"
            "        ('typed', 'o', 100, 'bank_transfer', 'cheque 9', 'CHEQUE 12345678')",
            {"n": leaked},
        )
        conn.execute(
            "UPDATE bank_deposits SET payment_id = 'linked' WHERE transaction_id = 't-linked'"
        )
        assert migrate(conn)[0] == MIGRATIONS[column].id
        notes: dict[object, object] = dict(
            conn.execute("SELECT id, notes FROM order_payments").fetchall()
        )
    assert notes == {
        "linked": f"PAY ORD-{_DOT3}",
        "ref": f"PAY ORD-{_DOT3}",
        "cash": "TILL 12345678",
        "typed": "CHEQUE 12345678",
    }


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
def test_the_sliced_output_backfill_marks_only_the_outputs_a_slice_names(
    pg_conninfo: str,
) -> None:
    # #5298 Task 32: the print worker's read URL needs the output mark. The backfill marks
    # each `slice` blob a native slice names, keeps its other meta, and leaves the
    # slicer's inputs (named by no slice) unmarked.
    backfill = _index("store_blobs_sliced_output_mark")
    with psycopg.connect(pg_conninfo) as conn:
        migrate(conn, MIGRATIONS[:backfill])
        conn.execute(
            "INSERT INTO store_blobs (key, sha256, kind, backend, backend_id, size, meta)"
            " VALUES ('slice-out', 'a', 'slice', 'local', 'a', 1, '{\"swept\": true}'),"
            " ('slice-in', 'b', 'slice', 'local', 'b', 1, '{}')"
        )
        conn.execute(
            "INSERT INTO native_slices"
            " (workflow_id, subject, library_file_id, plate_id, preset_key, status,"
            "  sliced_blob_key) VALUES ('w', 'output:a', 1, 1, 'k', 'completed', 'slice-out')"
        )
        assert migrate(conn)[0] == MIGRATIONS[backfill].id
        rows = conn.execute("SELECT key, meta FROM store_blobs ORDER BY key").fetchall()
    assert rows == [("slice-in", {}), ("slice-out", {"swept": True, "output": "sliced"})]


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
        # #5704's later backfill names the revision a production attempt printed.
        assert conn.execute(
            "SELECT purpose, revision_id FROM order_item_fulfilments"
        ).fetchone() == ("production", rev_id)


@pytest.mark.requires_postgres
def test_the_printed_revision_backfill_names_the_newest_revision_before_each_attempt(
    pg_conninfo: str,
) -> None:
    # #5704: a production attempt from before the file names the item's newest revision
    # written no later than it; a test print keeps its own, and an item with no revision
    # names none.
    backfill = _index("order_attempt_printed_revision")
    with psycopg.connect(pg_conninfo) as conn:
        migrate(conn, MIGRATIONS[:backfill])
        conn.execute("INSERT INTO customers (id, display_name) VALUES ('c', 'C')")
        conn.execute(
            "INSERT INTO orders (id, number, customer_id, currency, created_by)"
            " VALUES ('o', 'ORD-2026-1', 'c', 'USD', '{}')"
        )
        conn.execute(
            "INSERT INTO order_items (id, order_id, position, quantity, title, kind, slug,"
            " commit, inputs, template_name, render_key) VALUES"
            " ('model', 'o', 0, 1, 'Demo', 'model', 'demo', %s, '{\"params\": {}}', 'Demo', 'k')",
            ("a" * 40,),
        )
        conn.execute(
            "INSERT INTO order_items (id, order_id, position, quantity, title, kind)"
            " VALUES ('custom', 'o', 1, 1, 'B', 'custom')"
        )
        for rev, number, at in (("r1", 1, "1 hour"), ("r2", 2, "0 hours")):
            conn.execute(
                "INSERT INTO order_item_revisions (id, item_id, number, slug, commit, inputs,"
                " template_name, render_key, created_at, created_by) VALUES"
                " (%s, 'model', %s, 'demo', %s, '{}', 'Demo', 'k', now() - %s::interval, '{}')",
                (rev, number, "a" * 40, at),
            )
        conn.execute(
            "INSERT INTO order_item_fulfilments"
            " (id, order_item_id, attempt, quantity, outcome, created_by, created_at,"
            "  purpose, revision_id) VALUES"
            " ('first', 'model', 1, 1, 'succeeded', '{}', now() - interval '2 hours',"
            "  'production', NULL),"
            " ('one', 'model', 2, 1, 'succeeded', '{}', now() - interval '30 minutes',"
            "  'production', NULL),"
            " ('two', 'model', 3, 1, 'succeeded', '{}', now(), 'production', NULL),"
            " ('test', 'model', 4, 1, 'succeeded', '{}', now(), 'test', 'r1'),"
            " ('none', 'custom', 1, 1, 'succeeded', '{}', now(), 'production', NULL)"
        )
        assert migrate(conn)[0] == MIGRATIONS[backfill].id
        rows: dict[object, object] = dict(
            conn.execute("SELECT id, revision_id FROM order_item_fulfilments").fetchall()
        )
    assert rows == {"first": None, "one": "r1", "two": "r2", "test": "r1", "none": None}


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


@pytest.mark.requires_postgres
def test_the_deposits_tax_migration(pg_conninfo: str) -> None:
    with psycopg.connect(pg_conninfo) as conn:
        migrate(conn, MIGRATIONS[: _index("order_deposits_tax")])
        customer_id = conn.execute(
            "INSERT INTO customers (display_name) VALUES ('Ada') RETURNING id"
        ).fetchone()[0]  # type: ignore[index]
        order_id = conn.execute(
            "INSERT INTO orders (number, customer_id, currency, created_by)"
            " VALUES ('2026-9002', %s, 'USD', '{}') RETURNING id",
            (customer_id,),
        ).fetchone()[0]  # type: ignore[index]
        conn.execute(
            "INSERT INTO order_charges (order_id, kind, label, amount_minor, position)"
            " VALUES (%s, 'tax', 'VAT', 100, 0)",
            (order_id,),
        )
        assert migrate(conn)[0] == MIGRATIONS[_index("order_deposits_tax")].id
        # the old charge is typed, with no option, and the order has no deposit
        assert conn.execute("SELECT auto, shipping_option_id FROM order_charges").fetchall() == [
            (False, None)
        ]
        assert conn.execute(
            "SELECT deposit_minor, deposit_waived_reason, balance_waived_reason FROM orders"
        ).fetchall() == [(None, None, None)]
        charge = (
            "INSERT INTO order_charges"
            " (order_id, kind, label, amount_minor, position, auto, shipping_option_id)"
            " VALUES (%s, %s, 'x', 1, 1, %s, %s)"
        )
        # an auto charge must be tax
        with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
            conn.execute(charge, (order_id, "fee", True, None))
        # an option is a shipping charge's
        with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
            conn.execute(charge, (order_id, "fee", False, "pickup"))
        conn.execute(charge, (order_id, "shipping", False, "pickup"))
        conn.execute(charge, (order_id, "tax", True, None))
        # two auto rows on one order are refused
        with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
            conn.execute(charge, (order_id, "tax", True, None))
        update = "UPDATE orders SET {column} = %s WHERE id = %s"
        with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
            conn.execute(update.format(column="deposit_minor"), (0, order_id))
        for column in ("deposit_waived_reason", "balance_waived_reason"):
            for reason in ("", "x" * 501):
                with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
                    conn.execute(update.format(column=column), (reason, order_id))
        conn.execute(
            "UPDATE orders SET deposit_minor = 1, deposit_waived_reason = %s,"
            " balance_waived_reason = %s WHERE id = %s",
            ("x" * 500, "y", order_id),
        )


@pytest.mark.requires_postgres
def test_the_storefront_drafts_migration_keeps_every_order_a_back_office_one(
    pg_conninfo: str,
) -> None:
    # #5282: an order made before it is a back-office order, never submitted through the
    # storefront, and it passes the new customer-and-number check.
    with psycopg.connect(pg_conninfo) as conn:
        migrate(conn, MIGRATIONS[: _index("storefront_drafts")])
        conn.execute("INSERT INTO customers (id, display_name) VALUES ('c', 'C')")
        conn.execute(
            "INSERT INTO orders (id, number, customer_id, currency, created_by)"
            " VALUES ('o', 'ORD-2026-1', 'c', 'USD', '{}')"
        )
        assert migrate(conn)[0] == MIGRATIONS[_index("storefront_drafts")].id
        assert conn.execute(
            "SELECT source, submitted_at, storefront_visitor_id FROM orders"
        ).fetchall() == [("back_office", None, None)]
        conn.execute("UPDATE orders SET notes = 'still valid' WHERE id = 'o'")
        with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
            conn.execute("UPDATE orders SET customer_id = NULL WHERE id = 'o'")
        conn.execute("INSERT INTO storefront_visitors (id, token_sha256) VALUES ('v', 'h')")
        conn.execute(
            "INSERT INTO orders (id, currency, created_by, source, storefront_visitor_id)"
            " VALUES ('d', 'USD', '{}', 'storefront', 'v')"
        )
        with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
            conn.execute("UPDATE orders SET storefront_visitor_id = 'v' WHERE id = 'o'")
        assert conn.execute(
            "SELECT to_regclass('orders_storefront_visitor') IS NOT NULL"
        ).fetchone() == (True,)
        conn.execute("DELETE FROM storefront_visitors")
        assert conn.execute(
            "SELECT storefront_visitor_id FROM orders WHERE id = 'd'"
        ).fetchone() == (None,)


@pytest.mark.requires_postgres
def test_the_old_spool_tables_are_dropped(pg_conninfo: str) -> None:
    """#6348: `native_spools`, `native_spool_uses` and the provider overlay's tables are
    gone once every migration has run, and the filament table that carried their data stays."""
    old = [
        "native_spool_uses",
        "native_spools",
        "spool_record_links",
        "spool_provider_records",
        "spool_provider_refreshes",
        "tray_spool_links",
        "physical_spools",
    ]
    with psycopg.connect(pg_conninfo) as conn:
        migrate(conn)
        present = {
            row[0]
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables"
                " WHERE table_schema = current_schema() AND table_name = ANY(%s)",
                ([*old, "filament_spools"],),
            )
        }
    assert present == {"filament_spools"}


@pytest.mark.requires_postgres
@pytest.mark.parametrize(
    ("before", "after"),
    [
        (
            {"spools": "bb_to_sb", "tags": "off"},
            {"spools": "bb_to_sb", "tags": "bb_to_sb"},
        ),
        ({"tags": "sb_to_bb"}, {"tags": "two_way"}),
        ({"spools": "bb_to_sb", "tags": "two_way"}, {"spools": "bb_to_sb", "tags": "two_way"}),
        (
            {"spools": "off"},
            {"spools": "off", "weights": "off", "tags": "off", "presets": "off"},
        ),
        (
            {"spools": "off", "tags": "off", "weights": "bb_to_sb"},
            {"spools": "off", "weights": "off", "tags": "off", "presets": "off"},
        ),
        (
            {"spools": "off", "tags": "sb_to_bb"},
            {"spools": "off", "weights": "off", "tags": "sb_to_bb", "presets": "off"},
        ),
        (None, None),
    ],
)
def test_the_tags_migration_rewrites_the_old_values(
    pg_conninfo: str, before: dict[str, str] | None, after: dict[str, str] | None
) -> None:
    """#6265 Decision 4: #6264's ``off`` (the pull binds BB's tags, nothing is pushed) is
    now ``bb_to_sb``, its ``sb_to_bb`` (pull and push) ``two_way``; anything else stays.
    A row whose ``spools`` is ``off`` skipped the whole pull before #6265, so it pulls
    nothing after: ``weights``, ``presets`` and ``tags`` are ``off``, but a ``tags``
    ``sb_to_bb`` (the push) stays. A second run changes nothing."""
    at = _index("filament_sync_tags_values")
    with psycopg.connect(pg_conninfo) as conn:
        migrate(conn, MIGRATIONS[:at])
        if before is not None:
            conn.execute(
                "INSERT INTO settings (name, value) VALUES ('filament_sync', %s)",
                (Jsonb(before),),
            )
        assert MIGRATIONS[at].id in migrate(conn)
        row = conn.execute("SELECT value FROM settings WHERE name = 'filament_sync'").fetchone()
        assert (None if row is None else row[0]) == after
        conn.execute(MIGRATIONS[at].sql.encode("utf-8"))
        again = conn.execute("SELECT value FROM settings WHERE name = 'filament_sync'").fetchone()
    assert (None if again is None else again[0]) == after


def _table_columns(conn: psycopg.Connection[tuple[object, ...]], table: str) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema = current_schema() AND table_name = %s",
            (table,),
        )
    }


@pytest.mark.requires_postgres
def test_the_bb_sync_migration_adds_the_sync_columns_and_tables(pg_conninfo: str) -> None:
    """#6265 Task 3: the links' hashes and seen values, the BB ids of suppliers, locations
    and spool types, the create intents and the push errors."""
    with psycopg.connect(pg_conninfo) as conn:
        migrate(conn)
        assert {
            "pushed_fields",
            "bb_seen",
            "bb_seen_at",
            "bb_k_profiles",
            "bb_seeded",
            "pushed_hash",
        } <= (_table_columns(conn, "filament_bb_links"))
        for table, bb_id in (
            ("filament_suppliers", "bb_supplier_id"),
            ("filament_locations", "bb_location_id"),
        ):
            assert {bb_id, "bb_pushed", "bb_seen", "bb_seen_at"} <= _table_columns(conn, table)
        assert {"bb_pushed", "bb_seen", "bb_seen_at"} <= _table_columns(
            conn, "filament_insert_types"
        )
        assert {"bb_filament_id", "bb_pushed", "bb_seen", "bb_seen_at"} <= _table_columns(
            conn, "filament_spool_types"
        )
        assert _table_columns(conn, "filament_bb_creates") == {
            "kind",
            "sb_id",
            "started_at",
            "sent",
        }
        assert _table_columns(conn, "filament_bb_push_errors") == {
            "kind",
            "sb_id",
            "error",
            "attempts",
            "at",
            "last_pass_id",
        }
        with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
            conn.execute(
                "INSERT INTO filament_bb_creates (kind, sb_id) VALUES ('tray', gen_random_uuid())"
            )


@pytest.mark.requires_postgres
def test_one_sync_state_row(pg_conninfo: str) -> None:
    """#6265 Decision 12, F6: the state row exists from the start, with no instance yet,
    and there is never a second one."""
    with psycopg.connect(pg_conninfo) as conn:
        migrate(conn)
        rows = conn.execute(
            "SELECT id, instance, last_pass_at, last_ok_at, last_error, counts"
            " FROM filament_bb_sync_state"
        ).fetchall()
        assert rows == [(True, None, None, None, None, {})]
        with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
            conn.execute("INSERT INTO filament_bb_sync_state (id) VALUES (false)")
        with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
            conn.execute("INSERT INTO filament_bb_sync_state DEFAULT VALUES")


@pytest.mark.requires_postgres
def test_the_compatibility_view_and_its_trigger_are_dropped(pg_conninfo: str) -> None:
    """#6960: once every replica runs #6950's build, the `order_attachments` view, the
    trigger that filled `entity_id` and its function are gone."""
    with psycopg.connect(pg_conninfo) as conn:
        migrate(conn, MIGRATIONS[: _index("entities") + 1])
        assert conn.execute("SELECT to_regclass('order_attachments')").fetchone() == (
            "order_attachments",
        )
        migrate(conn)
        assert conn.execute("SELECT to_regclass('order_attachments')").fetchone() == (None,)
        assert conn.execute(
            "SELECT count(*) FROM pg_trigger WHERE tgname = 'entity_attachments_fill_entity'"
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT to_regprocedure('entity_attachments_fill_entity()')"
        ).fetchone() == (None,)
        # Nothing fills `entity_id` any more: a write that names none is refused.
        with pytest.raises(psycopg.errors.NotNullViolation), conn.transaction():
            conn.execute(
                "INSERT INTO entity_attachments (id, subject, kind, filename, media_type,"
                " size_bytes, sha256, blob_key, created_by) VALUES ('x', 'supplier:s', 'image',"
                " 'a.png', 'image/png', 1, repeat('a', 64), 'attachment-x', '{}')"
            )


@pytest.mark.requires_postgres
def test_the_entities_migration_keeps_order_attachments_for_the_previous_build(
    pg_conninfo: str,
) -> None:
    """#6950: the previous build's replicas read and write `order_attachments` during the
    roll. The view keeps that name, and an insert through it, which names no
    `entity_id`, gets its subject's entity."""
    with psycopg.connect(pg_conninfo) as conn:
        migrate(conn, MIGRATIONS[: _index("entities")])
        conn.execute("INSERT INTO customers (id, display_name) VALUES ('c', 'C')")
        conn.execute(
            "INSERT INTO orders (id, number, customer_id, currency, created_by)"
            " VALUES ('o', 'ORD-2026-1', 'c', 'USD', '{}')"
        )
        old = (
            "INSERT INTO order_attachments (id, order_id, subject, kind, role, filename,"
            " media_type, size_bytes, sha256, blob_key, caption, created_by)"
            " VALUES (%s, 'o', 'order:o', 'image', 'other', 'a.png', 'image/png', 1,"
            " repeat('a', 64), 'attachment-a', '', '{}')"
        )
        conn.execute(old, ("before",))
        # Only up to `entities`: a later migration drops the view (#6960,
        # `test_the_compatibility_view_and_its_trigger_are_dropped`).
        assert (
            migrate(conn, MIGRATIONS[: _index("entities") + 1])[0]
            == MIGRATIONS[_index("entities")].id
        )

        conn.execute(old, ("during",))
        conn.execute(
            "INSERT INTO order_attachments (id, order_id, subject, kind, filename, media_type,"
            " size_bytes, sha256, blob_key, created_by)"
            " VALUES ('item', 'o', 'order_item:i9', 'image', 'b.png', 'image/png', 1,"
            " repeat('b', 64), 'attachment-b', '{}')"
        )
        rows = conn.execute(
            "SELECT a.id, e.kind, e.ref FROM entity_attachments a"
            " JOIN entities e ON e.id = a.entity_id ORDER BY a.id"
        ).fetchall()
        assert rows == [
            ("before", "order", "o"),
            ("during", "order", "o"),
            ("item", "order_item", "i9"),
        ]
        assert conn.execute("SELECT count(*) FROM entities WHERE kind = 'order'").fetchone() == (1,)
        # Another entity's file is not the previous build's to read.
        conn.execute("INSERT INTO entities (kind, ref) VALUES ('supplier', 's')")
        conn.execute(
            "INSERT INTO entity_attachments (id, subject, entity_id, kind, filename,"
            " media_type, size_bytes, sha256, blob_key, created_by)"
            " SELECT 'supplier', 'supplier:s', id, 'image', 'c.png', 'image/png', 1,"
            " repeat('c', 64), 'attachment-c', '{}' FROM entities WHERE kind = 'supplier'"
        )
        assert conn.execute(
            "SELECT id, order_id, subject, attempt_item_id, fulfilment_id"
            " FROM order_attachments ORDER BY id"
        ).fetchall() == [
            ("before", "o", "order:o", None, None),
            ("during", "o", "order:o", None, None),
            ("item", "o", "order_item:i9", None, None),
        ]
        conn.execute("UPDATE order_attachments SET caption = 'x' WHERE id = 'during'")
        conn.execute("DELETE FROM order_attachments WHERE id = 'item'")
        assert conn.execute(
            "SELECT id, caption FROM entity_attachments WHERE id IN ('during', 'item')"
        ).fetchall() == [("during", "x")]
        # The previous build's claim release moves a file's subject and names no entity.
        conn.execute("UPDATE order_attachments SET subject = 'order_item:i9' WHERE id = 'during'")
        assert conn.execute(
            "SELECT e.kind, e.ref FROM entity_attachments a JOIN entities e"
            " ON e.id = a.entity_id WHERE a.id = 'during'"
        ).fetchone() == ("order_item", "i9")


@pytest.mark.requires_postgres
def test_the_attempt_media_migration_backfills_the_archive_copies(pg_conninfo: str) -> None:
    """#7231 (spec 2026-11-05 §4.2): a copy `copy_in` wrote gets its archive and medium
    from its caption and its name, on an attempt and on an item (before #5268). A caption
    a person edited, or a name of neither medium, stays null. Of two rows on one key only
    the oldest is set, so the unique index can be built."""
    attempt = "Attempt 1, Bambuddy archive"
    rows = [
        ("photo", "order_item_fulfilment:f", "finish_20261101.jpg", f"{attempt} 81", 5),
        ("lapse", "order_item_fulfilment:f", "timelapse-81.mp4", f"{attempt} 81", 5),
        ("item", "order_item:i", "finish_20261020.jpg", "Attempt 2, Bambuddy archive 70", 5),
        ("edited", "order_item_fulfilment:f", "finish_20261102.jpg", "Front view", 5),
        ("other", "order_item_fulfilment:f", "done.jpg", f"{attempt} 81", 5),
        ("first", "order_item_fulfilment:f", "finish_20261103.jpg", f"{attempt} 82", 4),
        ("second", "order_item_fulfilment:f", "finish_20261103.jpg", f"{attempt} 82", 3),
    ]
    with psycopg.connect(pg_conninfo) as conn:
        migrate(conn, MIGRATIONS[: _index("attempt_media_provenance")])
        conn.execute("INSERT INTO customers (id, display_name) VALUES ('c', 'C')")
        conn.execute(
            "INSERT INTO orders (id, number, customer_id, currency, created_by)"
            " VALUES ('o', 'ORD-2026-1', 'c', 'USD', '{}')"
        )
        conn.execute(
            "INSERT INTO entities (kind, ref) VALUES"
            " ('order_item_fulfilment', 'f'), ('order_item', 'i')"
        )
        for row_id, subject, filename, caption, hours in rows:
            conn.execute(
                "INSERT INTO entity_attachments (id, order_id, subject, entity_id, kind,"
                " filename, media_type, size_bytes, sha256, blob_key, caption, created_by,"
                " created_at, attempt_item_id)"
                " SELECT %s, 'o', %s, id, 'image', %s, 'image/jpeg', 1, repeat('a', 64),"
                " 'attachment-a', %s, '{}', now() - make_interval(hours => %s),"
                " CASE WHEN kind = 'order_item_fulfilment' THEN 'i' END"
                " FROM entities WHERE kind || ':' || ref = %s",
                (row_id, subject, filename, caption, hours, subject),
            )
        migrate(conn)
        got = conn.execute(
            "SELECT id, archive_id, archive_media FROM entity_attachments ORDER BY id"
        ).fetchall()
        assert got == [
            ("edited", None, None),
            ("first", 82, "finished_photo"),
            ("item", 70, "finished_photo"),
            ("lapse", 81, "timelapse"),
            ("other", None, None),
            ("photo", 81, "finished_photo"),
            ("second", None, None),
        ]
        # Both set or both null, and only the two media.
        with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
            conn.execute("UPDATE entity_attachments SET archive_id = 9 WHERE id = 'edited'")
        with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
            conn.execute(
                "UPDATE entity_attachments SET archive_id = 9, archive_media = 'video'"
                " WHERE id = 'edited'"
            )
        # One row per (subject, archive, medium, name).
        with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
            conn.execute(
                "UPDATE entity_attachments SET archive_id = 82,"
                " archive_media = 'finished_photo' WHERE id = 'second'"
            )
        assert conn.execute("SELECT media_checked_at FROM order_item_fulfilments").fetchall() == []
        # #7366: the scan was dropped (spec 2026-11-05 §9), and so is its stamp.
        assert "timelapse_scanned_at" not in _table_columns(conn, "order_item_fulfilments")
