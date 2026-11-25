"""ScadBuddy's own hour counter on Postgres (#5774, spec 2026-11-17-machine-rate-actuals
§3.6, §10): `PrinterReportStore.advance_in` and `touch` move `printer_run_hours`, an
unlink clears its state, and a merge keeps the kept printer's row."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from scadbuddy.printers import run_hours
from scadbuddy.printers.models import LanLink, PrinterCreate
from scadbuddy.printers.reports import PrinterReportStore
from scadbuddy.printers.run_hours import RunHours
from scadbuddy.printers.store import PrinterStore
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_postgres

SERIAL = "01P00A000000001"
CODE = "12345678"
CERT = "ab" * 32
JOB = "scadbuddy-0a1b2c3d-p1"
# Near the database's clock, so `stale`'s future tolerance never applies.
T0 = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=1)


def _at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def _running(state: str = "RUNNING", subtask: str = JOB, percent: int = 10) -> dict[str, Any]:
    return {"gcode_state": state, "subtask_name": subtask, "mc_percent": percent}


def _linked(printers: PrinterStore, serial: str = SERIAL) -> str:
    pid = printers.create(PrinterCreate(serial=serial, name="H2C")).id
    printers.link_lan(pid, LanLink(host="10.0.0.9", access_code=CODE), cert_sha256=CERT)
    return pid


def _counter(pg_pool: PgPool, printer_id: str) -> RunHours | None:
    with pg_pool.connection() as conn:
        return run_hours.read(conn, printer_id)


def test_the_first_report_makes_the_row_and_later_reports_advance_it(
    printers_with_kek: PrinterStore, pg_pool: PgPool
) -> None:
    pid = _linked(printers_with_kek)
    reports = PrinterReportStore(pg_pool)

    reports.advance(pid, _running(), _at(0))
    assert _counter(pg_pool, pid) == RunHours(
        run_seconds=Decimal(0),
        unseen_seconds=Decimal(0),
        counted_to=_at(0),
        state="RUNNING",
        subtask=JOB,
    )

    reports.advance(pid, _running(), _at(1.5))
    reports.advance(pid, _running(), _at(1.5))  # a resend after a lost answer
    reports.advance(pid, _running("PAUSE"), _at(11.5))
    reports.advance(pid, _running("PAUSE", percent=11), _at(21.5))
    counter = _counter(pg_pool, pid)
    assert counter is not None
    assert counter.run_seconds == Decimal("11.5")
    assert (counter.state, counter.counted_to) == ("PAUSE", _at(21.5))
    # A report with nothing new to a reader is not written (#7389): the counter waits for
    # the next write, which counts the whole stretch.
    reports.advance(pid, _running("PAUSE", percent=11), _at(31.5))
    counter = _counter(pg_pool, pid)
    assert counter is not None and counter.counted_to == _at(21.5)


def test_a_long_gap_that_ends_in_another_print_is_unseen(
    printers_with_kek: PrinterStore, pg_pool: PgPool
) -> None:
    pid = _linked(printers_with_kek)
    reports = PrinterReportStore(pg_pool)
    reports.advance(pid, _running(), _at(0))
    reports.advance(pid, _running(subtask="scadbuddy-ffffffff-p1"), _at(600))
    counter = _counter(pg_pool, pid)
    assert counter is not None
    assert (counter.run_seconds, counter.unseen_seconds) == (Decimal(0), Decimal(600))


def test_a_stale_write_leaves_the_counter_alone(
    printers_with_kek: PrinterStore, pg_pool: PgPool
) -> None:
    pid = _linked(printers_with_kek)
    reports = PrinterReportStore(pg_pool)
    reports.advance(pid, _running("PAUSE"), _at(0))
    reports.advance(pid, _running(), _at(10))
    before = _counter(pg_pool, pid)

    reports.advance(pid, _running("PAUSE"), _at(5))

    assert _counter(pg_pool, pid) == before


def test_after_the_watch_clock_steps_back_the_counter_waits_for_counted_to(
    printers_with_kek: PrinterStore, pg_pool: PgPool
) -> None:
    """A row stamped by a clock an hour ahead guards nothing (`reports.stale`), so the
    next reports are written, but the counter neither moves back nor counts them."""
    pid = _linked(printers_with_kek)
    reports = PrinterReportStore(pg_pool)
    ahead = datetime.now(UTC).replace(microsecond=0) + timedelta(hours=1)
    reports.advance(pid, _running(), ahead)
    reports.advance(pid, _running(percent=11), ahead + timedelta(seconds=10))

    reports.advance(pid, _running(), _at(0))
    reports.advance(pid, _running(percent=11), _at(10))

    counter = _counter(pg_pool, pid)
    assert counter is not None
    assert (counter.run_seconds, counter.counted_to) == (Decimal(10), ahead + timedelta(seconds=10))


def test_a_late_write_while_the_watch_clock_is_ahead_counts_nothing_twice(
    printers_with_kek: PrinterStore, pg_pool: PgPool
) -> None:
    """A watch host 60 s ahead: the stored ``seen_at`` guards nothing (`reports.stale`), so
    a write 35 s late is written, but the counter neither moves back nor counts it again."""
    pid = _linked(printers_with_kek)
    reports = PrinterReportStore(pg_pool)
    base = datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=60)
    reports.advance(pid, _running(), base)
    reports.advance(pid, _running(), base + timedelta(seconds=10))

    reports.advance(pid, _running(), base - timedelta(seconds=35))  # 35 s late
    reports.advance(pid, _running(), base + timedelta(seconds=20))

    counter = _counter(pg_pool, pid)
    assert counter is not None
    assert (counter.run_seconds, counter.state) == (Decimal(20), "RUNNING")
    assert counter.counted_to == base + timedelta(seconds=20)


def test_the_counter_keeps_every_microsecond(
    printers_with_kek: PrinterStore, pg_pool: PgPool
) -> None:
    """Each write adds its gap to the stored total, so a total rounded on each write would
    drift: 50 gaps of 0.123456 s are 6.1728 s exactly."""
    pid = _linked(printers_with_kek)
    reports = PrinterReportStore(pg_pool)
    reports.advance(pid, _running(), _at(0))
    for step in range(1, 51):
        at = _at(0) + step * timedelta(microseconds=123456)
        reports.advance(pid, _running(percent=step), at)
    counter = _counter(pg_pool, pid)
    assert counter is not None and counter.run_seconds == Decimal("6.1728")


def test_a_printer_not_lan_linked_gets_no_row(
    printers_with_kek: PrinterStore, pg_pool: PgPool
) -> None:
    pid = printers_with_kek.create(PrinterCreate(serial=SERIAL, name="H2C")).id
    PrinterReportStore(pg_pool).advance(pid, _running(), _at(0))
    assert _counter(pg_pool, pid) is None


def test_a_touch_advances_the_counter(printers_with_kek: PrinterStore, pg_pool: PgPool) -> None:
    pid = _linked(printers_with_kek)
    reports = PrinterReportStore(pg_pool)
    reports.advance(pid, _running(), _at(0))

    reports.touch(pid, _at(10))
    reports.touch(pid, _at(5))  # stamped before counted_to: nothing

    counter = _counter(pg_pool, pid)
    assert counter is not None
    assert (counter.run_seconds, counter.counted_to) == (Decimal(10), _at(10))
    stored = reports.get(pid)
    assert stored is not None and stored[1] == _at(10)


def test_a_touch_with_no_row_writes_none(printers_with_kek: PrinterStore, pg_pool: PgPool) -> None:
    pid = _linked(printers_with_kek)
    PrinterReportStore(pg_pool).touch(pid, _at(0))
    assert _counter(pg_pool, pid) is None


def test_a_touch_before_the_first_report_after_the_deploy_makes_no_row(
    printers_with_kek: PrinterStore, pg_pool: PgPool
) -> None:
    """A printer that reported before the counter existed has a report row and no counter
    row: a touch makes none, and its next full report does."""
    pid = _linked(printers_with_kek)
    reports = PrinterReportStore(pg_pool)
    reports.advance(pid, _running(), _at(0))
    with pg_pool.connection() as conn:
        conn.execute("DELETE FROM printer_run_hours WHERE printer = %s", (pid,))

    reports.touch(pid, _at(10))
    assert _counter(pg_pool, pid) is None
    reports.advance(pid, _running(), _at(20))
    counter = _counter(pg_pool, pid)
    assert counter is not None and counter.counted_to == _at(20) and counter.run_seconds == 0


def test_an_unlink_keeps_the_total_and_clears_the_state_and_a_relink_counts_on(
    printers_with_kek: PrinterStore, pg_pool: PgPool
) -> None:
    pid = _linked(printers_with_kek)
    reports = PrinterReportStore(pg_pool)
    reports.advance(pid, _running(), _at(0))
    reports.advance(pid, _running(), _at(30))

    printers_with_kek.unlink_lan(pid)
    unlinked = _counter(pg_pool, pid)
    assert unlinked is not None
    assert (unlinked.run_seconds, unlinked.state) == (Decimal(30), None)

    printers_with_kek.link_lan(pid, LanLink(host="10.0.0.9", access_code=CODE), cert_sha256=CERT)
    reports.advance(pid, _running(), _at(40))  # within the gap limit, but no state to count
    reports.advance(pid, _running(percent=11), _at(50))
    counter = _counter(pg_pool, pid)
    assert counter is not None
    assert counter.run_seconds == 40 and counter.unseen_seconds == 0


def _two_rows(printers: PrinterStore) -> tuple[str, str]:
    synthetic = printers.ensure_bambuddy(4, serial=None, name="H2C", model="H2C")
    mine = printers.create(PrinterCreate(serial=SERIAL, name="Mine"))
    return synthetic.id, mine.id


def _put_counter(pg_pool: PgPool, printer_id: str, seconds: int) -> None:
    with pg_pool.connection() as conn:
        conn.execute(
            "INSERT INTO printer_run_hours (printer, run_seconds, counted_to, state)"
            " VALUES (%s, %s, %s, 'RUNNING')",
            (printer_id, seconds, T0),
        )


def test_a_merge_moves_the_gone_printers_counter_when_the_kept_has_none(
    printers: PrinterStore, pg_pool: PgPool
) -> None:
    gone, kept = _two_rows(printers)
    _put_counter(pg_pool, gone, 3600)

    printers.ensure_bambuddy(4, serial=SERIAL, name="H2C", model="H2C")

    assert printers.get(gone) is None
    counter = _counter(pg_pool, kept)
    assert counter is not None and counter.run_seconds == 3600


def test_a_merge_keeps_the_kept_printers_counter(printers: PrinterStore, pg_pool: PgPool) -> None:
    gone, kept = _two_rows(printers)
    _put_counter(pg_pool, gone, 3600)
    _put_counter(pg_pool, kept, 60)

    printers.ensure_bambuddy(4, serial=SERIAL, name="H2C", model="H2C")

    counter = _counter(pg_pool, kept)
    assert counter is not None and counter.run_seconds == 60
    assert _counter(pg_pool, gone) is None


def test_after_a_merge_a_report_before_the_moved_counter_changes_nothing(
    printers_with_kek: PrinterStore, pg_pool: PgPool
) -> None:
    """The gone printer's counter moves under a kept printer whose reports are older. A
    report before its ``counted_to`` counts nothing and keeps it; a later one counts on."""
    gone, kept = _two_rows(printers_with_kek)
    printers_with_kek.link_lan(kept, LanLink(host="10.0.0.9", access_code=CODE), cert_sha256=CERT)
    reports = PrinterReportStore(pg_pool)
    reports.advance(kept, _running(), _at(-100))
    with pg_pool.connection() as conn:  # a kept printer reported before the counter existed
        conn.execute("DELETE FROM printer_run_hours WHERE printer = %s", (kept,))
    _put_counter(pg_pool, gone, 3600)  # counted to T0

    printers_with_kek.ensure_bambuddy(4, serial=SERIAL, name="H2C", model="H2C")
    reports.advance(kept, _running(), _at(-50))
    before = _counter(pg_pool, kept)
    reports.advance(kept, _running(), _at(10))

    assert before is not None
    assert (before.run_seconds, before.counted_to) == (Decimal(3600), T0)
    after = _counter(pg_pool, kept)
    assert after is not None
    assert (after.run_seconds, after.counted_to) == (Decimal(3610), _at(10))


def test_the_reading_rounds_half_up_to_a_tenth(
    printers_with_kek: PrinterStore, pg_pool: PgPool
) -> None:
    pid = _linked(printers_with_kek)
    reports = PrinterReportStore(pg_pool)
    reports.advance(pid, _running(), _at(0))
    for step in range(1, 19):  # 180 s in 10 s ticks: 0.05 h, which rounds up to 0.1
        reports.touch(pid, _at(step * 10))
    counter = _counter(pg_pool, pid)
    assert counter is not None
    assert run_hours.hours(counter.run_seconds) == Decimal("0.1")
