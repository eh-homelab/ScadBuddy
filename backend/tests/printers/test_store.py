"""The printer registry's store (#3823, plan 2026-10-16-printer-identity-native-path,
Task 2): one row per serial, the Bambuddy and LAN links, and the access code sealed so
only its own row opens it."""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from scadbuddy.core.secrets import SealError
from scadbuddy.printers import store as store_module
from scadbuddy.printers.models import LanLink, PrinterCreate, PrinterPatch
from scadbuddy.printers.reports import PrinterReportStore
from scadbuddy.printers.store import (
    PRINTER_REFERENCES,
    BambuddyLinkTakenError,
    KekMissingError,
    NoAccessCodeError,
    PrinterNotFoundError,
    PrinterReference,
    PrinterStore,
    SerialTakenError,
    SerialUnknownError,
)
from scadbuddy.spools.store import SpoolStore
from tests.conftest import PgPool
from tests.printers.conftest import RecordingBus
from tests.spools.test_store import NOW, record, tray

pytestmark = pytest.mark.requires_postgres

SERIAL = "01P00A000000001"
CODE = "12345678"
CERT = "ab" * 32


def test_create_and_lookup_by_serial(printers: PrinterStore) -> None:
    row = printers.create(PrinterCreate(serial=SERIAL, name="H2C", model="H2C"))
    assert row.preferred_path == "auto" and not row.has_access_code
    assert printers.by_serial(SERIAL) == row
    assert printers.get(row.id) == row
    assert printers.get("nope") is None
    assert [r.id for r in printers.list()] == [row.id]


def test_list_is_by_name(printers: PrinterStore) -> None:
    printers.create(PrinterCreate(serial="01P00A000000002", name="B"))
    printers.create(PrinterCreate(serial="01P00A000000003", name="A"))
    assert [r.name for r in printers.list()] == ["A", "B"]


def test_duplicate_serial_refused(printers: PrinterStore) -> None:
    first = printers.create(PrinterCreate(serial=SERIAL, name="A"))
    with pytest.raises(SerialTakenError) as raised:
        printers.create(PrinterCreate(serial=SERIAL, name="B"))
    assert raised.value.existing_id == first.id


def test_a_serial_is_upper_case_digits_and_letters() -> None:
    for bad in ("short", "01p00a000000001", "01P00A-00000001", "bambuddy-4"):
        with pytest.raises(ValidationError):
            PrinterCreate(serial=bad, name="x")


def test_patch_renames_and_sets_the_path(printers: PrinterStore) -> None:
    row = printers.create(PrinterCreate(serial=SERIAL, name="A"))
    patched = printers.patch(row.id, PrinterPatch(preferred_path="native"))
    assert (patched.name, patched.preferred_path) == ("A", "native")
    assert printers.patch(row.id, PrinterPatch(name="B")).preferred_path == "native"
    with pytest.raises(PrinterNotFoundError):
        printers.patch("nope", PrinterPatch(name="B"))


def test_lan_link_seals_and_opens(printers_with_kek: PrinterStore, pg_pool: PgPool) -> None:
    pid = printers_with_kek.create(PrinterCreate(serial=SERIAL, name="A")).id
    row = printers_with_kek.link_lan(
        pid, LanLink(host="10.0.0.9", access_code=CODE), cert_sha256=CERT
    )
    assert row.has_access_code and row.lan_host == "10.0.0.9" and row.lan_cert_sha256 == CERT
    assert CODE not in repr(row) and CODE not in row.model_dump_json()
    assert printers_with_kek.open_access_code(pid) == CODE
    with pg_pool.connection() as conn:
        stored = conn.execute("SELECT * FROM printers WHERE id = %s", (pid,)).fetchone()
    assert stored is not None
    assert CODE.encode() not in bytes(stored["access_code_sealed"])


def test_a_lan_link_repr_hides_the_code() -> None:
    assert CODE not in repr(LanLink(host="h", access_code=CODE))


@pytest.mark.parametrize("code", ["1234567x", "abc123", "12345678901234", "a b-C_d"])
def test_a_lan_link_takes_the_code_as_typed(code: str) -> None:
    """#4434: the printer decides whether a code is right, not a format."""
    assert LanLink(host="h", access_code=code).access_code == code


@pytest.mark.parametrize("code", ["", "x" * 129])
def test_a_lan_link_refuses_an_empty_or_unbounded_code(code: str) -> None:
    with pytest.raises(ValidationError):
        LanLink(host="h", access_code=code)


def test_lan_link_without_kek_refused(printers: PrinterStore) -> None:
    pid = printers.create(PrinterCreate(serial=SERIAL, name="A")).id
    with pytest.raises(KekMissingError):
        printers.link_lan(pid, LanLink(host="10.0.0.9", access_code=CODE), cert_sha256=CERT)
    with pytest.raises(KekMissingError):
        printers.open_access_code(pid)


def test_no_code_until_linked(printers_with_kek: PrinterStore) -> None:
    pid = printers_with_kek.create(PrinterCreate(serial=SERIAL, name="A")).id
    with pytest.raises(NoAccessCodeError):
        printers_with_kek.open_access_code(pid)


def test_opened_code_bound_to_row(printers_with_kek: PrinterStore, pg_pool: PgPool) -> None:
    a = printers_with_kek.create(PrinterCreate(serial=SERIAL, name="A")).id
    b = printers_with_kek.create(PrinterCreate(serial="01P00A000000002", name="B")).id
    printers_with_kek.link_lan(a, LanLink(host="10.0.0.9", access_code=CODE), cert_sha256=CERT)
    with pg_pool.connection() as conn:
        conn.execute(
            "UPDATE printers SET (access_code_sealed, access_code_dek_sealed, access_code_kek_id)"
            " = (SELECT access_code_sealed, access_code_dek_sealed, access_code_kek_id"
            " FROM printers WHERE id = %s) WHERE id = %s",
            (a, b),
        )
    with pytest.raises(SealError) as raised:
        printers_with_kek.open_access_code(b)
    assert CODE not in str(raised.value)


def test_unlink_lan_forgets_the_code(printers_with_kek: PrinterStore) -> None:
    pid = printers_with_kek.create(PrinterCreate(serial=SERIAL, name="A")).id
    printers_with_kek.link_lan(pid, LanLink(host="10.0.0.9", access_code=CODE), cert_sha256=CERT)
    row = printers_with_kek.unlink_lan(pid)
    assert (row.lan_host, row.lan_cert_sha256, row.has_access_code) == (None, None, False)
    with pytest.raises(NoAccessCodeError):
        printers_with_kek.open_access_code(pid)


def test_a_synthetic_serial_takes_no_lan_link(printers_with_kek: PrinterStore) -> None:
    row = printers_with_kek.ensure_bambuddy(4, serial=None, name="H2C", model="H2C")
    with pytest.raises(SerialUnknownError):
        printers_with_kek.link_lan(
            row.id, LanLink(host="10.0.0.9", access_code=CODE), cert_sha256=CERT
        )


def test_ensure_bambuddy_is_idempotent_and_adopts_serial(printers: PrinterStore) -> None:
    a = printers.ensure_bambuddy(4, serial=None, name="H2C", model="H2C")
    assert a.serial == "bambuddy-4" and a.preferred_path == "bambuddy"
    assert printers.ensure_bambuddy(4, serial=None, name="H2C", model="H2C").id == a.id
    b = printers.ensure_bambuddy(4, serial=SERIAL, name="H2C", model="H2C")
    assert a.id == b.id and b.serial == SERIAL
    assert len(printers.list()) == 1


def test_ensure_bambuddy_links_the_row_holding_the_serial(printers: PrinterStore) -> None:
    mine = printers.create(PrinterCreate(serial=SERIAL, name="Mine"))
    row = printers.ensure_bambuddy(4, serial=SERIAL, name="H2C", model="H2C")
    assert row.id == mine.id and row.bambuddy_printer_id == 4 and row.name == "Mine"


def test_a_synthetic_row_gives_way_to_the_row_holding_the_serial(printers: PrinterStore) -> None:
    printers.ensure_bambuddy(4, serial=None, name="H2C", model="H2C")
    mine = printers.create(PrinterCreate(serial=SERIAL, name="Mine"))
    row = printers.ensure_bambuddy(4, serial=SERIAL, name="H2C", model="H2C")
    assert row.id == mine.id and row.bambuddy_printer_id == 4
    assert [r.id for r in printers.list()] == [mine.id]


def test_a_re_added_printer_gets_a_row_of_its_own(printers: PrinterStore) -> None:
    first = printers.ensure_bambuddy(1, serial=SERIAL, name="Old", model="H2C")
    second = printers.ensure_bambuddy(2, serial=SERIAL, name="New", model="H2C")
    assert first.id != second.id and second.serial == "bambuddy-2"
    assert printers.by_bambuddy_id(1) == first


def test_concurrent_ensures_make_one_row(printers: PrinterStore) -> None:
    with ThreadPoolExecutor(4) as pool:
        rows = list(
            pool.map(
                lambda _: printers.ensure_bambuddy(7, serial=SERIAL, name="x", model=None),
                range(8),
            )
        )
    assert len({row.id for row in rows}) == 1


def test_link_bambuddy_refuses_a_taken_id(printers: PrinterStore) -> None:
    a = printers.create(PrinterCreate(serial=SERIAL, name="A"))
    b = printers.create(PrinterCreate(serial="01P00A000000002", name="B"))
    printers.link_bambuddy(a.id, 3)
    assert printers.link_bambuddy(a.id, 3).bambuddy_printer_id == 3
    with pytest.raises(BambuddyLinkTakenError) as raised:
        printers.link_bambuddy(b.id, 3)
    assert raised.value.existing_id == a.id
    with pytest.raises(BambuddyLinkTakenError) as again:
        printers.link_bambuddy(a.id, 5)
    assert again.value.existing_id == a.id


def test_link_bambuddy_takes_over_a_synthetic_row(printers: PrinterStore) -> None:
    synthetic = printers.ensure_bambuddy(3, serial=None, name="H2C", model=None)
    mine = printers.create(PrinterCreate(serial=SERIAL, name="Mine"))
    assert printers.link_bambuddy(mine.id, 3).bambuddy_printer_id == 3
    assert printers.get(synthetic.id) is None


def test_unlink_bambuddy_and_resolve(printers: PrinterStore) -> None:
    row = printers.create(PrinterCreate(serial=SERIAL, name="A"))
    printers.link_bambuddy(row.id, 3)
    assert printers.resolve_bambuddy_id(row.id) == 3
    assert printers.resolve_bambuddy_id(9) == 9
    assert printers.resolve_bambuddy_id(None) is None
    assert printers.resolve_bambuddy_id("nope") is None
    assert printers.unlink_bambuddy(row.id).bambuddy_printer_id is None
    assert printers.resolve_bambuddy_id(row.id) is None


def test_delete(printers: PrinterStore) -> None:
    row = printers.create(PrinterCreate(serial=SERIAL, name="A"))
    printers.delete(row.id)
    assert printers.get(row.id) is None
    with pytest.raises(PrinterNotFoundError):
        printers.delete(row.id)


# ── #4186 review ─────────────────────────────────────────────────────────────────────


def test_ids_are_uuids_and_a_malformed_id_is_not_found(printers: PrinterStore) -> None:
    """M1: `printers.id` is a uuid (spec §6.1)."""
    row = printers.create(PrinterCreate(serial=SERIAL, name="A"))
    assert str(uuid.UUID(row.id)) == row.id
    assert printers.get("nope") is None and printers.resolve_bambuddy_id("nope") is None
    with pytest.raises(PrinterNotFoundError):
        printers.delete("nope")


def test_every_reference_to_a_printer_is_merged(pg_pool: PgPool) -> None:
    """I7: every foreign key to `printers.id` is in `PRINTER_REFERENCES`, so a merge
    repoints it. A table that names a printer without one (#4180's `tray_spool_links`,
    Task 8's activity attribution) must add itself too."""
    with pg_pool.connection() as conn:
        rows = conn.execute(
            "SELECT cl.relname AS t, a.attname AS c FROM pg_constraint co"
            " JOIN pg_class cl ON cl.oid = co.conrelid"
            " JOIN pg_attribute a ON a.attrelid = co.conrelid AND a.attnum = ANY(co.conkey)"
            " WHERE co.contype = 'f' AND co.confrelid = 'printers'::regclass"
        ).fetchall()
    fks = {(row["t"], row["c"]) for row in rows}
    assert fks
    listed = {
        (ref.table, ref.column) for ref in PRINTER_REFERENCES if isinstance(ref, PrinterReference)
    }
    assert ("tray_spool_links", "printer_id") in fks  # #4190
    assert fks <= listed


def test_a_synthetic_row_merges_into_the_row_holding_the_serial(
    printers: PrinterStore, pg_pool: PgPool, bus: RecordingBus
) -> None:
    """I7: the synthetic row's dependents move to the row that keeps the printer, and
    both ids are announced so a page drops the gone one."""
    synthetic = printers.ensure_bambuddy(4, serial=None, name="H2C", model="H2C")
    reports = PrinterReportStore(pg_pool)
    with pg_pool.connection() as conn:  # a synthetic row takes no LAN link; a report is
        conn.execute(  # the dependent at hand, written as a merge would find it
            "INSERT INTO printer_reports (printer_id, report, seen_at) VALUES (%s, '{}', now())",
            (synthetic.id,),
        )
    mine = printers.create(PrinterCreate(serial=SERIAL, name="Mine"))
    bus.events.clear()

    kept = printers.ensure_bambuddy(4, serial=SERIAL, name="H2C", model="H2C")

    assert kept.id == mine.id and kept.bambuddy_printer_id == 4
    assert printers.get(synthetic.id) is None
    assert reports.get(mine.id) is not None and reports.get(synthetic.id) is None
    assert set(bus.changed()) == {synthetic.id, mine.id}


def test_a_merge_moves_the_runs_that_printed_on_the_gone_row(
    printers: PrinterStore, pg_pool: PgPool
) -> None:
    """#3823 Task 8: a run names its registry printer in ``print_runs.printer`` and in its
    activity row's ``printer`` facet; a merge repoints both, as it does every reference."""
    synthetic = printers.ensure_bambuddy(4, serial=None, name="H2C", model="H2C")
    with pg_pool.connection() as conn:
        conn.execute(
            "INSERT INTO print_runs (id, output_id, idempotency_key, status, printer)"
            " VALUES ('r1', 'o', 'k', 'running', %s)",
            (synthetic.id,),
        )
        conn.execute(
            "INSERT INTO activity (id, kind, status, title, facets, source, source_id)"
            " VALUES ('print_run:r1', 'print_run', 'running', 'Print', %s, 'print_run', 'r1')",
            (Jsonb({"subject": "o", "printer": synthetic.id}),),
        )
    mine = printers.create(PrinterCreate(serial=SERIAL, name="Mine"))

    printers.ensure_bambuddy(4, serial=SERIAL, name="H2C", model="H2C")

    with pg_pool.connection() as conn:
        run = conn.execute("SELECT printer::text AS p FROM print_runs WHERE id = 'r1'").fetchone()
        facets = conn.execute("SELECT facets FROM activity WHERE id = 'print_run:r1'").fetchone()
    assert run is not None and run["p"] == mine.id
    assert facets is not None and facets["facets"] == {"subject": "o", "printer": mine.id}


def test_deleting_a_printer_forgets_it_on_its_runs(printers: PrinterStore, pg_pool: PgPool) -> None:
    """#4442 M3: the run keeps no printer that is gone, nor does its activity facet."""
    row = printers.create(PrinterCreate(serial=SERIAL, name="Mine"))
    with pg_pool.connection() as conn:
        conn.execute(
            "INSERT INTO print_runs (id, output_id, idempotency_key, status, printer)"
            " VALUES ('r1', 'o', 'k', 'running', %s)",
            (row.id,),
        )
        conn.execute(
            "INSERT INTO activity (id, kind, status, title, facets, source, source_id)"
            " VALUES ('print_run:r1', 'print_run', 'running', 'Print', %s, 'print_run', 'r1')",
            (Jsonb({"subject": "o", "printer": row.id}),),
        )

    printers.delete(row.id)

    with pg_pool.connection() as conn:
        run = conn.execute("SELECT printer FROM print_runs WHERE id = 'r1'").fetchone()
        facets = conn.execute("SELECT facets FROM activity WHERE id = 'print_run:r1'").fetchone()
    assert run is not None and run["printer"] is None
    assert facets is not None and facets["facets"] == {"subject": "o"}


def test_link_bambuddy_takeover_merges_and_announces(
    printers: PrinterStore, bus: RecordingBus
) -> None:
    synthetic = printers.ensure_bambuddy(3, serial=None, name="H2C", model=None)
    mine = printers.create(PrinterCreate(serial=SERIAL, name="Mine"))
    bus.events.clear()
    printers.link_bambuddy(mine.id, 3)
    assert set(bus.changed()) == {synthetic.id, mine.id}


def test_a_re_added_printer_moves_the_link_when_the_old_one_is_gone(
    printers: PrinterStore,
) -> None:
    """M7: Bambuddy lists the printer under a new id only; its row moves to it."""
    first = printers.ensure_bambuddy(1, serial=SERIAL, name="Old", model="H2C")
    moved = printers.ensure_bambuddy(2, serial=SERIAL, name="New", model="H2C", listed={2})
    assert moved.id == first.id and moved.bambuddy_printer_id == 2
    assert [row.id for row in printers.list()] == [first.id]


def test_a_report_after_unlink_is_not_written(
    printers_with_kek: PrinterStore, pg_pool: PgPool
) -> None:
    """M2: an attempt still running after the unlink writes nothing."""
    pid = printers_with_kek.create(PrinterCreate(serial=SERIAL, name="A")).id
    printers_with_kek.link_lan(pid, LanLink(host="10.0.0.9", access_code=CODE), cert_sha256=CERT)
    reports = PrinterReportStore(pg_pool)
    reports.put(pid, {"mc_percent": 1}, datetime.now(UTC))
    printers_with_kek.unlink_lan(pid)
    reports.put(pid, {"mc_percent": 2}, datetime.now(UTC))
    assert reports.get(pid) is None


def test_ensure_takes_no_lock_when_nothing_changes(printers: PrinterStore, pg_pool: PgPool) -> None:
    """M13: a strip read for a known printer waits on no writer."""
    printers.ensure_bambuddy(4, serial=SERIAL, name="H2C", model=None)
    with pg_pool.connection() as conn, conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('scadbuddy:printers', 0))")
        with ThreadPoolExecutor(1) as pool:
            found = pool.submit(
                printers.ensure_bambuddy, 4, serial=SERIAL, name="H2C", model=None
            ).result(timeout=5)
    assert found.bambuddy_printer_id == 4


# ── #4186 re-review ──────────────────────────────────────────────────────────────────


def _two_rows(printers: PrinterStore) -> tuple[str, str]:
    synthetic = printers.ensure_bambuddy(4, serial=None, name="H2C", model="H2C")
    mine = printers.create(PrinterCreate(serial=SERIAL, name="Mine"))
    return synthetic.id, mine.id


def test_a_repoint_hook_runs_inside_the_merge_transaction(
    printers: PrinterStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N2: a reference with a key of its own (#4180's ``tray_spool_links``) registers a
    hook, called with the merge's connection while both rows still exist."""
    gone, kept = _two_rows(printers)
    calls: list[tuple[str, str, bool, bool]] = []

    def hook(conn: Any, merged: str, into: str) -> None:
        in_tx = conn.info.transaction_status == psycopg.pq.TransactionStatus.INTRANS
        both = conn.execute(
            "SELECT count(*) AS n FROM printers WHERE id IN (%s, %s)", (merged, into)
        ).fetchone()["n"]
        calls.append((merged, into, in_tx, both == 2))

    monkeypatch.setattr(store_module, "PRINTER_REFERENCES", (*PRINTER_REFERENCES, hook))
    printers.ensure_bambuddy(4, serial=SERIAL, name="H2C", model="H2C")
    assert calls == [(gone, kept, True, True)]


def test_a_failing_repoint_hook_undoes_the_merge(
    printers: PrinterStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    gone, kept = _two_rows(printers)

    def hook(conn: Any, merged: str, into: str) -> None:
        raise RuntimeError("conflict")

    monkeypatch.setattr(store_module, "PRINTER_REFERENCES", (*PRINTER_REFERENCES, hook))
    with pytest.raises(RuntimeError):
        printers.ensure_bambuddy(4, serial=SERIAL, name="H2C", model="H2C")
    assert printers.get(gone) is not None
    assert printers.require(kept).bambuddy_printer_id is None


def test_the_rows_remember_which_bambuddy_printers_were_active(printers: PrinterStore) -> None:
    """N4: what Bambuddy last listed as active, for when it is down."""
    a = printers.ensure_bambuddy(1, serial=None, name="A", model=None)
    b = printers.ensure_bambuddy(2, serial=None, name="B", model=None)
    printers.mark_bambuddy_active({1})
    assert printers.require(a.id).bambuddy_active is True
    assert printers.require(b.id).bambuddy_active is False
    printers.mark_bambuddy_active({1, 2})
    assert printers.require(b.id).bambuddy_active is True


def test_an_unlinked_serial_is_not_linked_again_by_ensure(printers: PrinterStore) -> None:
    """The unlink is remembered (`bambuddy_unlinked_id`): the Bambuddy printer gets its
    synthetic row instead, and a later link by the user clears the memory."""
    row = printers.create(PrinterCreate(serial=SERIAL, name="Mine"))
    printers.link_bambuddy(row.id, 1)
    printers.unlink_bambuddy(row.id)
    for _ in range(2):
        other = printers.ensure_bambuddy(1, serial=SERIAL, name="P1", model=None)
        assert other.id != row.id and other.serial == "bambuddy-1"
        assert printers.require(row.id).bambuddy_printer_id is None
    relinked = printers.link_bambuddy(row.id, 1)
    assert relinked.bambuddy_printer_id == 1 and relinked.bambuddy_unlinked_id is None
    assert printers.get(other.id) is None  # the synthetic row merged into it


def test_an_unlink_racing_an_ensure_stays_unlinked(printers: PrinterStore) -> None:
    for attempt in range(8):
        serial = f"01P00A00000010{attempt}"
        row = printers.create(PrinterCreate(serial=serial, name=f"R{attempt}"))
        printers.link_bambuddy(row.id, 100 + attempt)
        barrier = threading.Barrier(2)

        def unlink(row_id: str = row.id, barrier: threading.Barrier = barrier) -> None:
            barrier.wait()
            printers.unlink_bambuddy(row_id)

        def ensure(
            bid: int = 100 + attempt, serial: str = serial, barrier: threading.Barrier = barrier
        ) -> None:
            barrier.wait()
            printers.ensure_bambuddy(bid, serial=serial, name="P", model=None)

        with ThreadPoolExecutor(2) as pool:
            for done in [pool.submit(unlink), pool.submit(ensure)]:
                done.result(timeout=10)
        printers.ensure_bambuddy(100 + attempt, serial=serial, name="P", model=None)
        assert printers.require(row.id).bambuddy_printer_id is None


# ── #4190: tray links follow their printer ──────────────────────────────────────────


async def test_a_merge_repoints_the_tray_links(printers: PrinterStore, pg_pool: PgPool) -> None:
    """The synthetic row's tray links move to the row it merges into, in the merge's
    transaction; a tray both link keeps the kept row's (`repoint_tray_links`)."""
    spools = SpoolStore(pg_pool)
    synthetic = printers.ensure_bambuddy(4, serial=None, name="H2C", model=None)
    mine = printers.create(PrinterCreate(serial=SERIAL, name="Mine"))
    await spools.replace_records("bambuddy", [record("1"), record("2"), record("3")], NOW)
    await spools.link(uuid.UUID(synthetic.id), tray(0, 0), provider="bambuddy", record_id="1")
    await spools.link(uuid.UUID(synthetic.id), tray(0, 1), provider="bambuddy", record_id="2")
    await spools.link(uuid.UUID(mine.id), tray(0, 1), provider="bambuddy", record_id="3")

    printers.ensure_bambuddy(4, serial=SERIAL, name="H2C", model=None)

    assert printers.get(synthetic.id) is None
    with pg_pool.connection() as conn:
        links = conn.execute(
            "SELECT printer_id::text AS p, tray_id FROM tray_spool_links ORDER BY tray_id"
        ).fetchall()
    assert [(row["p"], row["tray_id"]) for row in links] == [(mine.id, 0), (mine.id, 1)]
    overlay = await spools.overlay(
        uuid.UUID(mine.id), [tray(0, 0), tray(0, 1)], now=NOW, providers=["bambuddy"]
    )
    assert [t.records["bambuddy"].record_id for t in overlay.trays] == ["1", "3"]


def _count(pool: PgPool, table: str) -> int:
    with pool.connection() as conn:
        found = conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()
    assert found is not None
    return int(found["n"])


async def test_deleting_a_printer_deletes_its_tray_links(
    printers: PrinterStore, pg_pool: PgPool
) -> None:
    """#4190: ``ON DELETE CASCADE``; the spools themselves stay."""
    spools = SpoolStore(pg_pool)
    row = printers.create(PrinterCreate(serial=SERIAL, name="Mine"))
    await spools.replace_records("bambuddy", [record("1")], NOW)
    await spools.link(uuid.UUID(row.id), tray(0, 0), provider="bambuddy", record_id="1")

    printers.delete(row.id)

    assert _count(pg_pool, "tray_spool_links") == 0
    assert _count(pg_pool, "physical_spools") == 1


def test_a_tray_link_names_a_registered_printer(pg_pool: PgPool) -> None:
    with pg_pool.connection() as conn, pytest.raises(psycopg.errors.ForeignKeyViolation):
        conn.execute(
            "INSERT INTO physical_spools (id, material) VALUES (gen_random_uuid(), 'PLA')"
            " RETURNING id"
        )
        conn.execute(
            "INSERT INTO tray_spool_links (printer_id, ams_id, tray_id, physical_spool_id,"
            " fingerprint) SELECT gen_random_uuid(), 0, 0, id, 'f' FROM physical_spools"
        )
