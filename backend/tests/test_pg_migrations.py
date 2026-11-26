"""The migration files and their ledger (#491): applied by file id in timestamp order,
out of order when an older file arrives late, and the one-time conversion of the
positional ledger from before #491."""

from __future__ import annotations

import threading
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from scadbuddy.filament.bb_sync_plan import _suppliers
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
        # The only events are the property history's back-fill of the kinds' schemas (#6949).
        assert conn.execute(
            "SELECT count(*) FROM events WHERE kind <> 'entity_property_schema.changed'"
        ).fetchone() == (0,)


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


# -- the parts migration (#7067 Task 6) ------------------------------------------------------

_P1 = "5f0e9c1a-1111-4a2b-8c3d-000000000001"
_P2 = "5f0e9c1a-2222-4a2b-8c3d-000000000002"
_BY = '{"kind": "browser"}'


def _before_parts(conn: psycopg.Connection[tuple[object, ...]]) -> None:
    """Every file before the parts migration, and one order `o1` (USD) with the items
    `oi1` and `oi2`."""
    migrate(conn, MIGRATIONS[: _index("parts_to_items")])
    conn.execute("INSERT INTO customers (id, display_name) VALUES ('c', 'C')")
    conn.execute(
        "INSERT INTO orders (id, number, customer_id, currency, created_by)"
        " VALUES ('o1', 'ORD-2026-1', 'c', 'USD', '{}')"
    )
    conn.execute(
        "INSERT INTO order_items (id, order_id, position, quantity, title, kind)"
        " VALUES ('oi1', 'o1', 0, 1, 'Keychain', 'custom'),"
        " ('oi2', 'o1', 1, 1, 'Keychain', 'custom')"
    )


def _part(
    conn: psycopg.Connection[tuple[object, ...]],
    part_id: str,
    name: str,
    *,
    on_hand: int,
    unit_cost: str,
    currency: str = "USD",
    supplier: str = "",
    link: str = "",
    pack_size: int = 10,
    pack_price_minor: int = 599,
) -> None:
    conn.execute(
        "INSERT INTO parts (id, name, supplier, link, currency, pack_size, pack_price_minor,"
        " unit_cost_minor, on_hand, low_stock, created_at, updated_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 2,"
        " '2026-11-01T10:00:00Z', '2026-11-02T10:00:00Z')",
        (part_id, name, supplier, link, currency, pack_size, pack_price_minor, unit_cost, on_hand),
    )


def _old_move(
    conn: psycopg.Connection[tuple[object, ...]],
    part_id: str,
    minute: int,
    kind: str,
    delta: int,
    unit_cost_after: str,
    amount: int | None = None,
    *,
    item: str | None = None,
    note: str = "",
) -> None:
    """A `part_stock_moves` row as `PartStore.move` wrote it, `minute` minutes after the
    part was made."""
    conn.execute(
        "INSERT INTO part_stock_moves (part_id, kind, delta, on_hand_after,"
        " unit_cost_after_minor, amount_minor, note, order_id, order_item_id, created_by,"
        " created_at) VALUES (%s, %s, %s, 0, %s, %s, %s, %s, %s, %s,"
        " '2026-11-01T10:00:00Z'::timestamptz + make_interval(mins => %s))",
        (
            part_id,
            kind,
            delta,
            unit_cost_after,
            amount,
            note,
            None if item is None else "o1",
            item,
            _BY,
            minute,
        ),
    )


def _cost_entry(
    conn: psycopg.Connection[tuple[object, ...]], item: str, part_id: str, amount: int
) -> None:
    conn.execute(
        "INSERT INTO order_cost_entries (order_id, order_item_id, kind, amount_minor, source,"
        " created_by) VALUES ('o1', %s, 'material', %s, %s, '{}')",
        (item, amount, f"part:{item}:{part_id}"),
    )


def _moves(conn: psycopg.Connection[tuple[object, ...]], item_id: str) -> list[tuple[object, ...]]:
    """The item's converted moves, oldest first: kind, qty, unit cost, amount,
    cost_unknown, order item."""
    return conn.execute(
        "SELECT kind, qty, unit_cost_minor, amount_minor, cost_unknown, order_item_id"
        " FROM stock_moves WHERE item_id = %s ORDER BY at, id",
        (item_id,),
    ).fetchall()


@pytest.mark.requires_postgres
def test_the_parts_migration_keeps_stock_cost_and_history(pg_conninfo: str) -> None:
    """#7067 Task 6: a part becomes an item with its own id, an opening lot holding its
    stock at its unit cost, and its moves; the `part:` cost entry goes (job cost reads the
    moves now, Pre-flight 5), and the item's parts row names the item."""
    with psycopg.connect(pg_conninfo) as conn:
        _before_parts(conn)
        _part(conn, _P1, "Hook", on_hand=7, unit_cost="599", pack_size=10, pack_price_minor=5990)
        _old_move(conn, _P1, 1, "add", 10, "599", 5990, note="first box")
        _old_move(conn, _P1, 2, "use", -3, "599", 1797, item="oi1")
        _cost_entry(conn, "oi1", _P1, 1797)
        conn.execute(
            "INSERT INTO order_item_parts (order_item_id, part_id, per_unit, used)"
            " VALUES ('oi1', %s, 3, 3)",
            (_P1,),
        )
        migrate(conn)
        assert conn.execute(
            "SELECT id::text, kind_id, name, low_stock, source FROM items"
        ).fetchall() == [(_P1, "hardware", "Hook", 2, "manual")]
        assert conn.execute("SELECT part_id, item_id::text FROM part_item_map").fetchall() == [
            (_P1, _P1)
        ]
        lot = conn.execute(
            "SELECT origin, received_qty, on_hand, unit_cost_minor, cost_unknown, cost_currency"
            " FROM stock_lots WHERE item_id = %s",
            (_P1,),
        ).fetchall()
        # A shop-currency lot has no `cost_currency` (R-P2-12).
        assert lot == [("opening", 7, 7, 599, False, None)]
        assert _moves(conn, _P1) == [
            ("receive", 10, 599, 5990, False, None),
            ("use", -3, 599, -1797, False, "oi1"),
        ]
        # The lot's on hand is the sum of its moves, as the stock store keeps it.
        assert conn.execute(
            "SELECT sum(m.qty) = l.on_hand FROM stock_moves m JOIN stock_lots l"
            " ON l.id = m.lot_id GROUP BY l.on_hand"
        ).fetchall() == [(True,)]
        assert conn.execute(
            "SELECT reason, actor FROM stock_moves WHERE kind = 'receive'"
        ).fetchone() == ("first box", {"kind": "browser"})
        assert conn.execute(
            "SELECT count(*) FROM order_cost_entries WHERE source LIKE 'part:%'"
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT order_item_id, item_id::text, per_unit, used FROM order_item_parts"
        ).fetchall() == [("oi1", _P1, 3, 3)]
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables"
                " WHERE table_schema = current_schema()"
            )
        }
        assert {"parts_retired", "part_stock_moves_retired"} <= tables
        assert not {"parts", "part_stock_moves"} & tables


@pytest.mark.requires_postgres
def test_a_part_in_another_currency_opens_a_cost_unknown_lot(pg_conninfo: str) -> None:
    """D11, R-7067-11, R-P2-22: a EUR part in a USD shop opens as a foreign receive does:
    cost 0 and unknown in the shop's currency, its EUR cost kept beside; its moves are at 0
    and unknown; its pack price stays a EUR price point."""
    with psycopg.connect(pg_conninfo) as conn:
        _before_parts(conn)
        conn.execute("INSERT INTO settings (name, value) VALUES ('orders_currency', '\"USD\"')")
        _part(
            conn,
            _P1,
            "Magnet",
            on_hand=5,
            unit_cost="120.5",
            currency="EUR",
            supplier="Supermagnete",
            pack_size=20,
            pack_price_minor=2410,
        )
        _old_move(conn, _P1, 1, "add", 5, "120.5", 602)
        migrate(conn)
        assert conn.execute(
            "SELECT unit_cost_minor, cost_unknown, cost_currency, foreign_unit_cost_minor"
            " FROM stock_lots"
        ).fetchall() == [(0, True, "EUR", Decimal("120.5"))]
        assert conn.execute(
            "SELECT unit_cost_minor, amount_minor, cost_unknown FROM stock_moves"
        ).fetchall() == [(0, 0, True)]
        assert conn.execute(
            "SELECT price, currency, on_date::text, source FROM offer_price_points"
        ).fetchall() == [(Decimal("24.1"), "EUR", "2026-11-02", "manual")]


@pytest.mark.requires_postgres
def test_a_returned_use_is_linked_to_its_use(pg_conninfo: str) -> None:
    """A return reverses the newest use of its order item and part not yet reversed, so
    a fulfil, unfulfil and fulfil again leaves one use unreversed."""
    with psycopg.connect(pg_conninfo) as conn:
        _before_parts(conn)
        _part(conn, _P1, "Hook", on_hand=8, unit_cost="500")
        _old_move(conn, _P1, 1, "adjust", 10, "500")
        _old_move(conn, _P1, 2, "use", -2, "500", 1000, item="oi1")
        _old_move(conn, _P1, 3, "return", 2, "500", 1000, item="oi1")
        _old_move(conn, _P1, 4, "use", -2, "500", 1000, item="oi1")
        _old_move(conn, _P1, 5, "use", -1, "500", 500, item="oi2")
        _old_move(conn, _P1, 6, "return", 1, "500", 500, item="oi2")
        _old_move(conn, _P1, 7, "use", -1, "500", 500, item="oi2")
        migrate(conn)
        rows = conn.execute(
            "SELECT r.order_item_id, to_char(r.at AT TIME ZONE 'UTC', 'MI'), u.kind,"
            " u.order_item_id, to_char(u.at AT TIME ZONE 'UTC', 'MI')"
            " FROM stock_moves r JOIN stock_moves u ON u.id = r.reverses_move_id ORDER BY r.at"
        ).fetchall()
        # Each return names the use before it, of its own order item.
        assert rows == [("oi1", "03", "use", "oi1", "02"), ("oi2", "06", "use", "oi2", "05")]
        assert conn.execute(
            "SELECT count(*) FROM stock_moves WHERE kind = 'use'"
            " AND id NOT IN (SELECT reverses_move_id FROM stock_moves"
            "  WHERE reverses_move_id IS NOT NULL)"
        ).fetchone() == (2,)


@pytest.mark.requires_postgres
def test_a_return_after_a_dearer_add_nets_to_zero(pg_conninfo: str) -> None:
    """Review Focus 3: a return takes its use's cost, not the weighted average a later
    add made, so the order item's part cost nets to 0; an add takes what its packs cost."""
    with psycopg.connect(pg_conninfo) as conn:
        _before_parts(conn)
        _part(conn, _P1, "Hook", on_hand=20, unit_cost="580")
        _old_move(conn, _P1, 1, "add", 10, "500", 5000)
        _old_move(conn, _P1, 2, "use", -4, "500", 2000, item="oi1")
        _old_move(conn, _P1, 3, "add", 10, "600", 7000)
        _old_move(conn, _P1, 4, "return", 4, "580", 2000, item="oi1")
        migrate(conn)
        assert [(m[0], m[1], m[2]) for m in _moves(conn, _P1)] == [
            ("receive", 10, 500),
            ("use", -4, 500),
            ("receive", 10, 700),
            ("return", 4, 500),
        ]
        # Job cost's `parts` figure (Task 8 reads it so).
        assert conn.execute(
            "SELECT coalesce(sum(-amount_minor), 0) FROM stock_moves"
            " WHERE order_item_id = 'oi1' AND kind IN ('use', 'return')"
        ).fetchone() == (0,)


@pytest.mark.requires_postgres
def test_a_zero_delta_adjust_is_dropped(pg_conninfo: str) -> None:
    """A count that equalled the shelf moved nothing; `stock_moves` refuses qty 0. The
    lot's intake is the part's on hand: its first count was an adjust, not an add."""
    with psycopg.connect(pg_conninfo) as conn:
        _before_parts(conn)
        _part(conn, _P1, "Ring", on_hand=6, unit_cost="40")
        _old_move(conn, _P1, 1, "adjust", 8, "40", note="Count when the part was added")
        _old_move(conn, _P1, 2, "adjust", 0, "40", note="counted, all there")
        _old_move(conn, _P1, 3, "adjust", -2, "40", note="two lost")
        migrate(conn)
        assert [(m[0], m[1], m[3]) for m in _moves(conn, _P1)] == [
            ("adjust", 8, 320),
            ("adjust", -2, -80),
        ]
        assert conn.execute("SELECT received_qty, on_hand FROM stock_lots").fetchall() == [(6, 6)]


@pytest.mark.requires_postgres
def test_rules_and_entities_point_at_items(pg_conninfo: str) -> None:
    """A model's rule names items (`item_id`, in its lines and their `when`), and the
    files, links and properties on `part:<p>` are on `item:<p>`."""
    with psycopg.connect(pg_conninfo) as conn:
        _before_parts(conn)
        _part(conn, _P1, "Hook", on_hand=0, unit_cost="5")
        _part(conn, _P2, "Gold hook", on_hand=0, unit_cost="9")
        conn.execute(
            "INSERT INTO model_part_rules (slug, lines, updated_by) VALUES ('tag', %s, '{}')",
            (
                Jsonb(
                    [
                        {
                            "per_unit": 2,
                            "part_id": _P1,
                            "when": [{"colour": "#FFD700", "part_id": _P2}],
                        },
                        {"per_unit": 1, "part_id": None, "when": []},
                    ]
                ),
            ),
        )
        entity = conn.execute(
            "INSERT INTO entities (kind, ref) VALUES ('part', %s) RETURNING id", (_P1,)
        ).fetchone()
        assert entity is not None
        conn.execute(
            "INSERT INTO entity_links (entity_id, url, kind, created_by)"
            " VALUES (%s, 'https://example.com/datasheet', 'datasheet', '{}')",
            (entity[0],),
        )
        conn.execute(
            "INSERT INTO entity_attachments (id, subject, entity_id, kind, filename,"
            " media_type, size_bytes, sha256, blob_key, created_by)"
            " VALUES ('a1', %s, %s, 'image', 'hook.jpg', 'image/jpeg', 1, repeat('a', 64),"
            " 'k', '{}')",
            (f"part:{_P1}", entity[0]),
        )
        migrate(conn)
        (lines,) = conn.execute("SELECT lines FROM model_part_rules").fetchone() or (None,)
        assert lines == [
            {"per_unit": 2, "item_id": _P1, "when": [{"colour": "#FFD700", "item_id": _P2}]},
            {"per_unit": 1, "item_id": None, "when": []},
        ]
        assert conn.execute(
            "SELECT e.kind, e.ref, l.url FROM entity_links l JOIN entities e ON e.id = l.entity_id"
        ).fetchall() == [("item", _P1, "https://example.com/datasheet")]
        assert conn.execute("SELECT subject FROM entity_attachments").fetchall() == [
            (f"item:{_P1}",)
        ]
        assert conn.execute("SELECT count(*) FROM entities WHERE kind = 'part'").fetchone() == (0,)


@pytest.mark.requires_postgres
def test_suppliers_are_matched_by_name_or_made(pg_conninfo: str) -> None:
    """Ruling 5: a supplier name matches an unarchived supplier in any case, else a new
    one is made; a part with no supplier gets no offer, and its link becomes a `purchase`
    link on its item. Each offer carries the part's pack and price."""
    p3 = "5f0e9c1a-3333-4a2b-8c3d-000000000003"
    p4 = "5f0e9c1a-4444-4a2b-8c3d-000000000004"
    p5 = "5f0e9c1a-5555-4a2b-8c3d-000000000005"
    with psycopg.connect(pg_conninfo) as conn:
        _before_parts(conn)
        bambu = conn.execute(
            "INSERT INTO filament_suppliers (name) VALUES ('Bambu Lab') RETURNING id"
        ).fetchone()
        assert bambu is not None
        conn.execute(
            "INSERT INTO filament_suppliers (name, archived_at) VALUES ('Amazon', now()),"
            " (' Acme ', NULL)"
        )
        _part(
            conn,
            _P1,
            "Hook",
            on_hand=0,
            unit_cost="59.9",
            supplier="bambu lab",
            link="https://eu.store.bambulab.com/hook",
        )
        _part(conn, _P2, "Ring", on_hand=0, unit_cost="1", supplier="Amazon", pack_size=100)
        _part(conn, p3, "Magnet", on_hand=0, unit_cost="1", link="https://example.com/magnet")
        _part(conn, p4, "Nut", on_hand=0, unit_cost="1", supplier=" Bambu Lab ")
        _part(conn, p5, "Bolt", on_hand=0, unit_cost="1", supplier="acme")
        migrate(conn)
        # An archived "Amazon" does not match, so a new one is made; " Acme " and
        # " Bambu Lab " match after `btrim` on both sides.
        assert conn.execute(
            "SELECT name, archived_at IS NOT NULL FROM filament_suppliers"
            " ORDER BY upper(btrim(name)), archived_at NULLS FIRST"
        ).fetchall() == [
            (" Acme ", False),
            ("Amazon", False),
            ("Amazon", True),
            ("Bambu Lab", False),
        ]
        assert conn.execute(
            "SELECT o.item_id::text, s.id = %s FROM supplier_offers o"
            " JOIN filament_suppliers s ON s.id = o.supplier_id"
            " WHERE o.item_id = ANY(%s::uuid[]) ORDER BY o.item_id",
            (bambu[0], [_P1, p4]),
        ).fetchall() == sorted([(_P1, True), (p4, True)])
        assert conn.execute(
            "SELECT s.name FROM supplier_offers o JOIN filament_suppliers s"
            " ON s.id = o.supplier_id WHERE o.item_id = %s",
            (p5,),
        ).fetchall() == [(" Acme ",)]
        # A part with no supplier has no offer.
        assert conn.execute(
            "SELECT count(*) FROM supplier_offers WHERE item_id = %s", (p3,)
        ).fetchone() == (0,)
        offers = conn.execute(
            "SELECT o.item_id::text, s.name, o.sku, o.url, o.pack_size, p.price, p.currency"
            " FROM supplier_offers o JOIN filament_suppliers s ON s.id = o.supplier_id"
            " JOIN offer_price_points p ON p.offer_id = o.id"
            " WHERE o.item_id = ANY(%s::uuid[]) ORDER BY s.name",
            ([_P1, _P2],),
        ).fetchall()
        assert offers == [
            (_P2, "Amazon", f"PART-{_P2[:8]}", None, 100, Decimal("5.99"), "USD"),
            (
                _P1,
                "Bambu Lab",
                f"PART-{_P1[:8]}",
                "https://eu.store.bambulab.com/hook",
                10,
                Decimal("5.99"),
                "USD",
            ),
        ]
        assert conn.execute(
            "SELECT e.kind, e.ref, l.kind, l.url, l.source FROM entity_links l"
            " JOIN entities e ON e.id = l.entity_id"
        ).fetchall() == [("item", p3, "purchase", "https://example.com/magnet", "parts_migration")]


@pytest.mark.requires_postgres
def test_a_supplier_the_parts_migration_made_stays_out_of_bambuddy(pg_conninfo: str) -> None:
    """R-P2-8, D16: a supplier made from a part's supplier name has only an item offer,
    so the Bambuddy sync plans no create for it."""
    with psycopg.connect(pg_conninfo, row_factory=dict_row) as conn:
        _before_parts(conn)  # type: ignore[arg-type]
        conn.execute("INSERT INTO filament_suppliers (name, bb_supplier_id) VALUES ('Linked', 7)")
        _part(conn, _P1, "Hook", on_hand=0, unit_cost="1", supplier="Bolt Depot")  # type: ignore[arg-type]
        migrate(conn)
        assert conn.execute("SELECT count(*) AS n FROM supplier_offers").fetchone() == {"n": 1}
        # A supplier Bambuddy knows is planned; the migration's is not.
        assert [item.name for item in _suppliers(conn)] == ["Linked"]


@pytest.mark.requires_postgres
def test_a_removed_part_cost_entry_converts_at_zero(pg_conninfo: str) -> None:
    """R-P2-21: a costed use whose `part:` entry a person removed cost the order nothing,
    so it converts at 0, and so does its return; the reason says why. A use whose entry is
    there keeps its cost."""
    with psycopg.connect(pg_conninfo) as conn:
        _before_parts(conn)
        conn.execute(
            "INSERT INTO order_items (id, order_id, position, quantity, title, kind)"
            " VALUES ('oi3', 'o1', 2, 1, 'Keychain', 'custom')"
        )
        _part(conn, _P1, "Hook", on_hand=3, unit_cost="500")
        _old_move(conn, _P1, 1, "add", 10, "500", 5000)
        # oi1: costed, its entry removed, never returned.
        _old_move(conn, _P1, 2, "use", -2, "500", 1000, item="oi1", note="fulfil")
        # oi2: costed, its entry removed, then unfulfilled (the return found no entry).
        _old_move(conn, _P1, 3, "use", -1, "500", 500, item="oi2")
        _old_move(conn, _P1, 4, "return", 1, "500", None, item="oi2")
        # oi3: costed, its entry still there.
        _old_move(conn, _P1, 5, "use", -4, "500", 2000, item="oi3")
        _cost_entry(conn, "oi3", _P1, 2000)
        _old_move(conn, _P1, 6, "use", -1, "500", 500, item="oi2")
        _cost_entry(conn, "oi2", _P1, 500)
        migrate(conn)
        rows = conn.execute(
            "SELECT order_item_id, kind, unit_cost_minor, amount_minor, cost_unknown,"
            " reason LIKE '%%cost entry was removed%%' FROM stock_moves"
            " WHERE order_item_id IS NOT NULL ORDER BY at"
        ).fetchall()
        assert rows == [
            ("oi1", "use", 0, 0, False, True),
            ("oi2", "use", 0, 0, False, True),
            ("oi2", "return", 0, 0, False, True),
            ("oi3", "use", 500, -2000, False, False),
            ("oi2", "use", 500, -500, False, False),
        ]
        assert conn.execute(
            "SELECT reason FROM stock_moves WHERE order_item_id = 'oi1'"
        ).fetchone() == (
            "fulfil (Cost 0: its part cost entry was removed from the order before parts"
            " became items, #7067.)",
        )


@pytest.mark.requires_postgres
def test_a_null_amount_foreign_use_and_a_rounds_to_zero_use(pg_conninfo: str) -> None:
    """R-P2-23: a use with no amount is unknown when its cost is a whole minor unit or
    more, or when its part is in another currency; one that rounds to 0 is a known 0."""
    p3 = "5f0e9c1a-3333-4a2b-8c3d-000000000003"
    with psycopg.connect(pg_conninfo) as conn:
        _before_parts(conn)
        _part(conn, _P1, "Magnet", on_hand=-1, unit_cost="0.1", currency="EUR")
        _part(conn, _P2, "Washer", on_hand=-1, unit_cost="0.4")
        _part(conn, p3, "Ring", on_hand=-1, unit_cost="0.6")
        _old_move(conn, _P1, 1, "use", -1, "0.1", None, item="oi1")
        _old_move(conn, _P2, 2, "use", -1, "0.4", None, item="oi1")
        _old_move(conn, p3, 3, "use", -1, "0.6", None, item="oi1")
        migrate(conn)
        assert conn.execute(
            "SELECT item_id::text, unit_cost_minor, cost_unknown FROM stock_moves ORDER BY at"
        ).fetchall() == [
            (_P1, 0, True),
            (_P2, Decimal("0.4"), False),
            (p3, Decimal("0.6"), True),
        ]


@pytest.mark.requires_postgres
def test_a_non_uuid_part_id_gets_a_new_item_id(pg_conninfo: str) -> None:
    """A part id that is no uuid's text maps to a new uuid, and its order parts, rule
    lines and entity follow the map; an upper-case uuid keeps its value."""
    upper = _P2.upper()
    with psycopg.connect(pg_conninfo) as conn:
        _before_parts(conn)
        _part(conn, "legacy-1", "Hook", on_hand=1, unit_cost="5")
        _part(conn, upper, "Ring", on_hand=1, unit_cost="5")
        conn.execute(
            "INSERT INTO order_item_parts (order_item_id, part_id, per_unit)"
            " VALUES ('oi1', 'legacy-1', 2), ('oi1', %s, 1)",
            (upper,),
        )
        conn.execute(
            "INSERT INTO model_part_rules (slug, lines, updated_by) VALUES ('tag', %s, '{}')",
            (
                Jsonb(
                    [
                        {
                            "per_unit": 1,
                            "part_id": "legacy-1",
                            "when": [{"colour": "#000000", "part_id": upper}],
                        }
                    ]
                ),
            ),
        )
        conn.execute(
            "INSERT INTO entities (kind, ref) VALUES ('part', 'legacy-1'), ('part', %s)", (upper,)
        )
        migrate(conn)
        mapped: dict[str, str] = dict(
            conn.execute("SELECT part_id, item_id::text FROM part_item_map").fetchall()
        )
        new = mapped["legacy-1"]
        assert new not in ("legacy-1", _P2)
        assert mapped[upper] == _P2
        assert conn.execute("SELECT id::text FROM items ORDER BY name").fetchall() == [
            (new,),
            (_P2,),
        ]
        assert sorted(
            conn.execute("SELECT item_id::text, per_unit FROM order_item_parts").fetchall()
        ) == sorted([(new, 2), (_P2, 1)])
        (lines,) = conn.execute("SELECT lines FROM model_part_rules").fetchone() or (None,)
        assert lines == [
            {"per_unit": 1, "item_id": new, "when": [{"colour": "#000000", "item_id": _P2}]}
        ]
        assert sorted(conn.execute("SELECT kind, ref FROM entities").fetchall()) == sorted(
            [("item", new), ("item", _P2)]
        )


@pytest.mark.requires_postgres
def test_step_7_keeps_a_non_part_cost_entry(pg_conninfo: str) -> None:
    """Only the `part:` entry a converted use replaces goes; other entries stay."""
    with psycopg.connect(pg_conninfo) as conn:
        _before_parts(conn)
        _part(conn, _P1, "Hook", on_hand=-2, unit_cost="500")
        _old_move(conn, _P1, 1, "use", -2, "500", 1000, item="oi1")
        _cost_entry(conn, "oi1", _P1, 1000)
        conn.execute(
            "INSERT INTO order_cost_entries (order_id, order_item_id, kind, amount_minor, source,"
            " created_by) VALUES ('o1', 'oi1', 'material', 300, 'manual:glue', '{}'),"
            " ('o1', 'oi1', 'other', 200, NULL, '{}'),"
            " ('o1', 'oi2', 'material', 400, %s, '{}')",
            (f"part:oi2:{_P2}",),
        )
        migrate(conn)
        assert sorted(
            conn.execute("SELECT coalesce(source, ''), amount_minor FROM order_cost_entries")
        ) == sorted([("manual:glue", 300), ("", 200), (f"part:oi2:{_P2}", 400)])


@pytest.mark.requires_postgres
def test_a_return_pairs_per_part_and_an_unpaired_return_converts(pg_conninfo: str) -> None:
    """Two parts used and returned on one order item each pair with their own use at their
    own cost; a return with no use converts at its own cost and reverses nothing."""
    with psycopg.connect(pg_conninfo) as conn:
        _before_parts(conn)
        _part(conn, _P1, "Hook", on_hand=1, unit_cost="510")
        _part(conn, _P2, "Ring", on_hand=0, unit_cost="900")
        _old_move(conn, _P1, 1, "use", -2, "500", 1000, item="oi1")
        _old_move(conn, _P2, 2, "use", -3, "900", 2700, item="oi1")
        _old_move(conn, _P2, 3, "return", 3, "900", 2700, item="oi1")
        _old_move(conn, _P1, 4, "return", 2, "505", 1000, item="oi1")
        _old_move(conn, _P1, 5, "return", 1, "510", None, item="oi2")
        migrate(conn)
        rows = conn.execute(
            "SELECT r.item_id::text, r.order_item_id, r.unit_cost_minor, u.item_id::text,"
            " u.unit_cost_minor FROM stock_moves r LEFT JOIN stock_moves u"
            " ON u.id = r.reverses_move_id WHERE r.kind = 'return' ORDER BY r.at"
        ).fetchall()
        assert rows == [
            (_P2, "oi1", 900, _P2, 900),
            (_P1, "oi1", 500, _P1, 500),
            (_P1, "oi2", 510, None, None),
        ]


@pytest.mark.requires_postgres
def test_an_orphan_part_entity_raises(pg_conninfo: str) -> None:
    """R-P2-24: a part entity whose ref names no part stops the migration, and nothing of
    it is applied."""
    with psycopg.connect(pg_conninfo) as conn:
        _before_parts(conn)
        _part(conn, _P1, "Hook", on_hand=0, unit_cost="5")
        conn.execute("INSERT INTO entities (kind, ref) VALUES ('part', 'gone')")
        conn.commit()
        with pytest.raises(psycopg.errors.RaiseException, match="part entity gone names no part"):
            migrate(conn)
        conn.rollback()
        assert conn.execute("SELECT count(*) FROM parts").fetchone() == (1,)
        assert conn.execute("SELECT to_regclass('part_item_map')").fetchone() == (None,)


# -- the K1 class migration (#6949 K1 Task 7, design 2026-11-25 §6.3, §8) ---------------------

#: Each kind with its ancestors, itself at depth 0.
_CHAIN = (
    "WITH RECURSIVE chain (kind_id, anc_id, depth) AS ("
    " SELECT id, id, 0 FROM item_kinds"
    " UNION ALL SELECT c.kind_id, k.parent_id, c.depth + 1 FROM chain c"
    " JOIN item_kinds k ON k.id = c.anc_id WHERE k.parent_id IS NOT NULL)"
)

#: Each kind's class, read root first (a child only answers what its parent left open).
_RESOLVED = (
    _CHAIN + ", resolved AS (SELECT c.kind_id,"
    " (array_agg(a.nature ORDER BY c.depth DESC)"
    "  FILTER (WHERE a.nature IS NOT NULL))[1] AS nature,"
    " (array_agg(a.role ORDER BY c.depth DESC) FILTER (WHERE a.role IS NOT NULL))[1] AS role,"
    " (array_agg(a.counting ORDER BY c.depth DESC)"
    "  FILTER (WHERE a.counting IS NOT NULL))[1] AS counting,"
    " (array_agg(a.billing ORDER BY c.depth DESC)"
    "  FILTER (WHERE a.billing IS NOT NULL))[1] AS billing,"
    " count(a.special_type) AS specials,"
    " (array_agg(a.steps -> 'life' ->> 'handler' ORDER BY c.depth)"
    "  FILTER (WHERE a.steps -> 'life' ? 'handler'))[1] AS life"
    " FROM chain c JOIN item_kinds a ON a.id = c.anc_id GROUP BY c.kind_id)"
)

_REPORT_RULES = {
    "seeded_kinds",
    "person_kinds",
    "kind_life",
    "items_on_roots",
    "item_life",
    "unit_life",
    "part_lots",
    "model_codes",
    "kind_schemas",
    "tracking_derived",
}


def _before_inventory_class(conn: psycopg.Connection[tuple[object, ...]]) -> None:
    """Every file before the K1 class migration."""
    migrate(conn, MIGRATIONS[: _index("inventory_class")])


def _item(
    conn: psycopg.Connection[tuple[object, ...]],
    kind: str,
    name: str,
    *,
    life_hours: int | None = None,
    life_months: int | None = None,
    compatible: list[str] | None = None,
    minute: int = 0,
) -> str:
    row = conn.execute(
        "INSERT INTO items (kind_id, name, life_hours, life_months, compatible_with, created_at)"
        " VALUES (%s, %s, %s, %s, %s,"
        " '2026-11-01T10:00:00Z'::timestamptz + make_interval(mins => %s)) RETURNING id::text",
        (kind, name, life_hours, life_months, compatible or [], minute),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _report(conn: psycopg.Connection[tuple[object, ...]], rule: str) -> tuple[int, list[dict]]:  # type: ignore[type-arg]
    row = conn.execute(
        "SELECT count, detail FROM inventory_k1_report WHERE rule = %s", (rule,)
    ).fetchone()
    assert row is not None, rule
    return row[0], row[1]  # type: ignore[return-value]


@pytest.mark.requires_postgres
def test_inventory_class_places_the_builtin_kinds(pg_conninfo: str) -> None:
    """§8 built-in kinds: by `kinds.yaml`. A kind's class columns hold its own keys only
    (C22), so `printer` sets none and its chain gives physical, durable, unit; its special
    type stays NULL until K6 (R-K1-7)."""
    with psycopg.connect(pg_conninfo) as conn:
        _before_inventory_class(conn)
        migrate(conn)
        assert conn.execute(
            "SELECT parent_id, nature, role, counting, billing, special_type,"
            " steps -> 'life' ->> 'handler' FROM item_kinds WHERE id = 'printer'"
        ).fetchone() == ("durable", None, None, None, None, None, "life.hours")
        assert conn.execute(
            _RESOLVED + " SELECT nature, role, counting, billing FROM resolved"
            " WHERE kind_id = 'printer'"
        ).fetchone() == ("physical", "durable", "unit", None)
        assert conn.execute("SELECT name FROM item_kinds WHERE id = 'spool_part'").fetchone() == (
            "Spool insert",
        )
        assert (
            conn.execute(
                "SELECT parent_id, counting FROM item_kinds WHERE id IN ('hardware', 'ams_part')"
            ).fetchall()
            == [("consumable", "lot")] * 2
        )
        new = [
            "durable",
            "general_consumable",
            "warranty",
            "service_plan",
            "labour",
            "flat_service",
        ]
        assert conn.execute(
            "SELECT count(*) FROM item_kinds WHERE id = ANY(%s) AND builtin", (new,)
        ).fetchone() == (6,)
        # R-K1-6: the old columns are derived from the class and `steps.life`.
        assert dict(
            conn.execute(
                "SELECT id, tracking || '/' || life_basis FROM item_kinds"
                " WHERE id IN ('filament', 'durable', 'consumable', 'service', 'printer',"
                " 'build_plate', 'equipment', 'labour')"
            ).fetchall()
        ) == {
            "filament": "filament/none",
            "durable": "unit/none",
            "consumable": "lot/none",
            "service": "none/none",
            "printer": "unit/hours",
            "build_plate": "unit/none",
            "equipment": "unit/months",
            "labour": "none/none",
        }
        assert conn.execute("SELECT count(*) FROM relation_types WHERE builtin").fetchone() == (5,)
        assert {r[0] for r in conn.execute("SELECT rule FROM inventory_k1_report")} == (
            _REPORT_RULES
        )
        for table in ("relations", "handler_config_history", "inventory_operations"):
            assert conn.execute("SELECT to_regclass(%s)", (table,)).fetchone() != (None,)
        # A unit may end (a service term).
        item = _item(conn, "warranty", "Care")
        conn.execute(
            "INSERT INTO stock_units (item_id, status, cost_minor, origin, created_by)"
            " VALUES (%s, 'ended', 0, 'manual', '{}')",
            (item,),
        )


_TRACKINGS = ("lot", "unit", "filament", "none")
_BASES = ("none", "hours", "months")
_PARENTS = (None, "part", "consumable", "equipment")


@pytest.mark.requires_postgres
def test_every_phase_2_combination_is_placed(pg_conninfo: str) -> None:
    """§8 and R-K1-9: a person kind of each `tracking`, `life_basis` and parent gets the
    class of its tracking. One whose rule conflicts with its parent's class, or that is a
    root, moves under the root of its class; a life basis on a kind that is not durable is
    dropped; an item's `life_hours` on a kind that is not durable is dropped."""
    combos = [(t, b, p) for t in _TRACKINGS for b in _BASES for p in _PARENTS]
    items: dict[str, str] = {}
    with psycopg.connect(pg_conninfo) as conn:
        _before_inventory_class(conn)
        for n, (tracking, basis, parent) in enumerate(combos):
            kind = f"k_{tracking}_{basis}_{parent or 'root'}"
            conn.execute(
                "INSERT INTO item_kinds (id, name, parent_id, tracking, life_basis)"
                " VALUES (%s, %s, %s, %s, %s)",
                (kind, kind, parent, tracking, basis),
            )
            items[_item(conn, kind, kind, life_hours=100 if n % 3 == 0 else None)] = kind
        migrate(conn)

        conflicts = conn.execute(
            _CHAIN + " SELECT c.kind_id FROM chain c JOIN item_kinds k ON k.id = c.kind_id"
            " JOIN item_kinds a ON a.id = c.anc_id WHERE c.depth > 0 AND ("
            " k.nature <> a.nature OR k.role <> a.role OR k.counting <> a.counting"
            " OR k.billing <> a.billing)"
        ).fetchall()
        assert conflicts == []
        rows = conn.execute(
            _RESOLVED + " SELECT kind_id, nature, role, counting, billing, specials"
            " FROM resolved WHERE kind_id LIKE 'k\\_%'"
        ).fetchall()
        assert len(rows) == len(combos)
        for kind, nature, role, counting, billing, specials in rows:
            assert specials <= 1, kind
            if nature == "service":
                assert (role, counting, billing) == (None, None, "flat"), kind
            else:
                assert nature == "physical" and role and counting and billing is None, kind
        assert {r[0] for r in conn.execute("SELECT rule FROM inventory_k1_report")} == (
            _REPORT_RULES
        )

        role_of = {
            "lot": "consumable", "unit": "durable", "filament": "consumable", "none": None
        }  # fmt: skip
        stays = {("lot", "consumable"), ("unit", "part"), ("unit", "equipment")}
        moved = {f"k_{t}_{b}_{p or 'root'}" for t, b, p in combos if (t, p) not in stays}
        _, detail = _report(conn, "person_kinds")
        assert {d["kind"] for d in detail} == moved
        assert all("old_parent" in d for d in detail)
        _, detail = _report(conn, "kind_life")
        assert {d["kind"] for d in detail} == {
            f"k_{t}_{b}_{p or 'root'}" for t, b, p in combos if b != "none" and t != "unit"
        }
        _, detail = _report(conn, "item_life")
        assert {d["item"] for d in detail} == {
            item
            for item, kind in items.items()
            if role_of[kind.split("_")[1]] != "durable"
            and conn.execute("SELECT life_hours FROM items WHERE id = %s", (item,)).fetchone()
            != (None,)
        }
        # A durable person kind's basis is its `steps.life` handler, even where its parent's
        # chain names the same handler (`equipment` gives `life.months`): §8.
        assert dict(
            conn.execute(
                "SELECT id, steps -> 'life' FROM item_kinds WHERE id IN"
                " ('k_unit_hours_root', 'k_unit_months_part', 'k_unit_months_equipment')"
            ).fetchall()
        ) == {
            "k_unit_hours_root": {
                "handler": "life.hours",
                "params": {"source": "own_run_hours"},
            },
            "k_unit_months_part": {"handler": "life.months", "params": {}},
            "k_unit_months_equipment": {"handler": "life.months", "params": {}},
        }
        assert conn.execute(
            "SELECT tracking, life_basis FROM item_kinds WHERE id = 'k_filament_hours_root'"
        ).fetchone() == ("filament", "none")


@pytest.mark.requires_postgres
def test_items_on_roots_move(pg_conninfo: str) -> None:
    """§8: a root leaves its class open, so its items move to the seeded leaf."""
    with psycopg.connect(pg_conninfo) as conn:
        _before_inventory_class(conn)
        tape = _item(conn, "consumable", "Tape")
        repair = _item(conn, "service", "Repair")
        migrate(conn)
        assert dict(
            conn.execute(
                "SELECT id::text, kind_id FROM items WHERE id = ANY(%s::uuid[])", ([tape, repair],)
            ).fetchall()
        ) == {tape: "general_consumable", repair: "flat_service"}
        assert _report(conn, "items_on_roots")[0] == 2


@pytest.mark.requires_postgres
def test_part_lots_become_units(pg_conninfo: str) -> None:
    """R-K1-10: `part` becomes durable, so its lots become whole units at the lot's cost,
    with an `adjust` move; the fraction stays on the lot and is named."""
    with psycopg.connect(pg_conninfo) as conn:
        _before_inventory_class(conn)
        item = _item(conn, "part", "Fan")
        lot = conn.execute(
            "INSERT INTO stock_lots (item_id, received_qty, on_hand, unit_cost_minor, origin,"
            " created_by) VALUES (%s, 2.5, 2.5, 300, 'manual', '{}') RETURNING id::text",
            (item,),
        ).fetchone()
        assert lot is not None
        migrate(conn)
        assert (
            conn.execute(
                "SELECT cost_minor, origin, status FROM stock_units WHERE item_id = %s", (item,)
            ).fetchall()
            == [(300, "opening", "in_stock")] * 2
        )
        assert conn.execute(
            "SELECT kind, qty, unit_cost_minor, amount_minor FROM stock_moves"
            " WHERE lot_id = %s::uuid",
            (lot[0],),
        ).fetchall() == [("adjust", -2, 300, -600)]
        assert conn.execute(
            "SELECT count(*) FROM stock_moves WHERE kind = 'receive' AND qty = 1"
            " AND unit_cost_minor = 300 AND unit_id IS NOT NULL AND item_id = %s",
            (item,),
        ).fetchone() == (2,)
        assert conn.execute(
            "SELECT on_hand FROM stock_lots WHERE id = %s::uuid", (lot[0],)
        ).fetchone() == (Decimal("0.5"),)
        count, detail = _report(conn, "part_lots")
        assert count == 1
        assert detail == [{"lot": lot[0], "item": item, "units": 2, "left": 0.5}]


@pytest.mark.requires_postgres
def test_model_codes_become_kinds(pg_conninfo: str) -> None:
    """§8 and R-K1-11: each distinct model code is a kind under `printer`, named as the
    oldest item spells it; each item is `compatible_with` its codes' kinds, and a printer
    item with one code takes that kind."""
    with psycopg.connect(pg_conninfo) as conn:
        _before_inventory_class(conn)
        one = _item(conn, "nozzle", "Hardened", compatible=["H2C"], minute=1)
        two = _item(conn, "nozzle", "Brass", compatible=["h2c", " X1C "], minute=2)
        printer = _item(conn, "printer", "Mine", compatible=["H2C"], minute=3)
        both = _item(conn, "printer", "Spare", compatible=["H2C", "X1C"], minute=4)
        migrate(conn)
        assert conn.execute(
            "SELECT id, name, parent_id FROM item_kinds WHERE id LIKE 'printer\\_%' ORDER BY id"
        ).fetchall() == [
            ("printer_h2c", "H2C", "printer"),
            ("printer_x1c", "X1C", "printer"),
        ]
        rels = conn.execute(
            "SELECT f.ref, t.ref FROM relations r JOIN entities f ON f.id = r.from_entity"
            " JOIN entities t ON t.id = r.to_entity"
            " WHERE r.type_id = 'compatible_with' AND f.kind = 'item' AND t.kind = 'item_kind'"
        ).fetchall()
        assert {r for r in rels if r[0] in (one, two)} == {
            (one, "printer_h2c"),
            (two, "printer_h2c"),
            (two, "printer_x1c"),
        }
        assert dict(
            conn.execute(
                "SELECT id::text, kind_id FROM items WHERE id = ANY(%s::uuid[])", ([printer, both],)
            ).fetchall()
        ) == {printer: "printer_h2c", both: "printer"}
        assert conn.execute(
            "SELECT d ->> 'stays' FROM inventory_k1_report, jsonb_array_elements(detail) AS d"
            " WHERE rule = 'model_codes' AND d ? 'stays'"
        ).fetchall() == [(both,)]


@pytest.mark.requires_postgres
def test_item_life_moves_into_steps(pg_conninfo: str) -> None:
    """§4.5: an item's life moves into `steps.life`, as parameters of the handler it
    resolves to, or with the handler it needs on a durable item."""
    with psycopg.connect(pg_conninfo) as conn:
        _before_inventory_class(conn)
        nozzle = _item(conn, "nozzle", "Hardened", life_hours=1500)
        tool = _item(conn, "tool", "Driver", life_hours=50)
        migrate(conn)
        assert dict(
            conn.execute(
                "SELECT id::text, steps FROM items WHERE id = ANY(%s::uuid[])", ([nozzle, tool],)
            ).fetchall()
        ) == {
            nozzle: {"life": {"params": {"hours": 1500}}},
            tool: {
                "life": {
                    "handler": "life.hours",
                    "params": {"hours": 50, "source": "own_run_hours"},
                }
            },
        }


@pytest.mark.requires_postgres
def test_a_durable_kind_under_nozzle_gets_its_basis_handler(pg_conninfo: str) -> None:
    """§8: `hours` gives `life.hours` with `source: own_run_hours` and no `hours`, also
    under a parent that names `life.hours` itself."""
    with psycopg.connect(pg_conninfo) as conn:
        _before_inventory_class(conn)
        conn.execute(
            "INSERT INTO item_kinds (id, name, parent_id, tracking, life_basis)"
            " VALUES ('cht', 'CHT', 'nozzle', 'unit', 'hours')"
        )
        migrate(conn)
        assert conn.execute("SELECT steps FROM item_kinds WHERE id = 'cht'").fetchone() == (
            {"life": {"handler": "life.hours", "params": {"source": "own_run_hours"}}},
        )


@pytest.mark.requires_postgres
def test_a_person_kind_with_a_seeded_id_is_named_with_its_old_values(pg_conninfo: str) -> None:
    """A person's kind that has a seeded id becomes the built-in kind; the report keeps
    its old name, parent, tracking, life basis and schema."""
    schema = {"type": "object", "properties": {"rate": {"type": "number"}}}
    with psycopg.connect(pg_conninfo) as conn:
        _before_inventory_class(conn)
        conn.execute(
            "INSERT INTO item_kinds (id, name, parent_id, tracking, attribute_schema)"
            " VALUES ('labour', 'My labour', 'service', 'none', %s),"
            " ('durable', 'Durables', 'part', 'lot', DEFAULT)",
            (Jsonb(schema),),
        )
        _item(conn, "labour", "Assembly hour")
        migrate(conn)
        assert conn.execute(
            "SELECT name, parent_id, billing, builtin FROM item_kinds WHERE id = 'labour'"
        ).fetchone() == ("Labour", "service", "hourly", True)
        _, detail = _report(conn, "seeded_kinds")
        taken = {d["kind"]: d for d in detail if d["was"] != "new"}
        assert taken == {
            "labour": {
                "kind": "labour",
                "was": "a person's kind",
                "name": "My labour",
                "parent_id": "service",
                "tracking": "none",
                "life_basis": "none",
                "attribute_schema": schema,
            },
            "durable": {
                "kind": "durable",
                "was": "a person's kind",
                "name": "Durables",
                "parent_id": "part",
                "tracking": "lot",
                "life_basis": "none",
                "attribute_schema": {"type": "object", "properties": {}},
            },
        }


@pytest.mark.requires_postgres
def test_a_person_kind_with_a_seeded_id_and_other_stock_raises(pg_conninfo: str) -> None:
    """Its items would change meaning under the seeded class, so the migration stops and
    nothing of it is applied."""
    with psycopg.connect(pg_conninfo) as conn:
        _before_inventory_class(conn)
        conn.execute(
            "INSERT INTO item_kinds (id, name, tracking) VALUES ('general_consumable', 'G', 'unit')"
        )
        _item(conn, "general_consumable", "Fan")
        conn.commit()
        with pytest.raises(
            psycopg.errors.RaiseException, match="general_consumable has 1 items tracked by unit"
        ):
            migrate(conn)
        conn.rollback()
        assert conn.execute("SELECT to_regclass('inventory_k1_report')").fetchone() == (None,)


@pytest.mark.requires_postgres
def test_a_moved_kind_keeps_its_old_chains_schema(pg_conninfo: str) -> None:
    """§8 keeps every row's meaning: a kind whose parent changed keeps the properties its
    old chain gave it, in its own schema, and the report names it."""
    with psycopg.connect(pg_conninfo) as conn:
        _before_inventory_class(conn)
        conn.execute(
            "UPDATE item_kinds SET attribute_schema = %s WHERE id = 'part'",
            (Jsonb({"type": "object", "properties": {"mpn": {"type": "string"}}}),),
        )
        conn.execute(
            "INSERT INTO item_kinds (id, name, parent_id, tracking)"
            " VALUES ('foam', 'Foam', 'spool_part', 'lot')"
        )
        migrate(conn)
        props = dict(
            conn.execute(
                "SELECT id, attribute_schema -> 'properties' FROM item_kinds"
                " WHERE id IN ('foam', 'hardware', 'ams_part')"
            ).fetchall()
        )
        assert set(props["foam"]) == {"empty_weight_g", "role"}
        assert props["foam"]["role"]["enum"] == ["shell", "addon"]
        assert props["hardware"] == props["ams_part"] == {"mpn": {"type": "string"}}
        _, detail = _report(conn, "kind_schemas")
        assert {d["kind"]: d["keys"] for d in detail} == {
            "foam": ["empty_weight_g", "role"],
            "hardware": ["mpn"],
            "ams_part": ["mpn"],
        }


@pytest.mark.requires_postgres
def test_property_history_backfills_existing_rows(pg_conninfo: str) -> None:
    """#6949 (design §4.8): each property that exists before the history is one `set` event
    at its own `updated_at`, past the history subscriber's starting cursor."""
    history = _index("property_history_events")
    with psycopg.connect(pg_conninfo) as conn:
        migrate(conn, MIGRATIONS[:history])
        before = conn.execute(
            "INSERT INTO events (event_id, kind, at, payload) VALUES ('e0', 'job.progress',"
            ' now(), \'{"id": "e0", "at": "2026-11-25T00:00:00Z", "kind": "job.progress"}\')'
            " RETURNING seq"
        ).fetchone()
        assert before is not None
        entity = conn.execute(
            "INSERT INTO entities (kind, ref) VALUES ('item', 'i1') RETURNING id"
        ).fetchone()
        assert entity is not None
        conn.execute(
            "INSERT INTO entity_properties (entity_id, key, value, updated_at, updated_by)"
            " VALUES (%s, 'b', '2', '2026-11-01T00:00:00Z', '{\"kind\": \"browser\"}'),"
            " (%s, 'a', '\"x\"', '2026-11-02T00:00:00Z', '{\"kind\": \"browser\"}')",
            (entity[0], entity[0]),
        )
        assert migrate(conn)[0] == MIGRATIONS[history].id
        rows = conn.execute(
            "SELECT e.seq, e.payload, p.updated_at FROM events e JOIN entity_properties p"
            " ON p.entity_id = (e.payload->>'entity_id')::uuid AND p.key = e.payload->>'key'"
            " WHERE e.kind = 'entity_property.changed' ORDER BY e.seq"
        ).fetchall()
        cursor = conn.execute("SELECT seq FROM event_subscribers WHERE name = 'history'").fetchone()
        changed_at = [
            conn.execute("SELECT %s::timestamptz", (p["changed_at"],)).fetchone()
            for _, p, _ in rows
        ]
    assert [(p["key"], p["op"], p["old"], p["new"], p["ref"]) for _, p, _ in rows] == [
        ("a", "set", None, "x", "i1"),
        ("b", "set", None, 2, "i1"),
    ]
    assert [p["actor"] for _, p, _ in rows] == [{"kind": "browser"}] * 2
    assert changed_at == [(updated_at,) for _, _, updated_at in rows]
    assert cursor == before
    assert all(seq > before[0] for seq, _, _ in rows)
