"""A report that changes nothing a reader reads writes nothing (#7389): `PrinterReportStore.
advance_in` answers it from the stored row, with no row written and so no commit to wait
for, until `REFRESH_EVERY` has passed since the stored ``seen_at``; then the report is
written whole. The commit of a CRUD write is a span of its own (`traced_transaction`)."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from scadbuddy.core.tracing import COMMIT_SPAN, span, traced_transaction
from scadbuddy.printers import run_hours
from scadbuddy.printers.models import LanLink, PrinterCreate
from scadbuddy.printers.reports import (
    REFRESH_EVERY,
    Advance,
    PrintEnd,
    PrinterReportStore,
    read_view,
)
from scadbuddy.printers.status import STALE_AFTER
from scadbuddy.printers.store import PrinterStore
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_postgres

JOB = "scadbuddy-0a1b2c3d-p1"
# Near the database's clock, so `stale`'s future tolerance never applies.
T0 = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=1)


def _at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def _report(state: str = "RUNNING", *, nozzle: float = 220.0, **more: Any) -> dict[str, Any]:
    return {
        "gcode_state": state,
        "subtask_name": JOB,
        "mc_percent": 10,
        "nozzle_temper": nozzle,
        "sequence_id": str(nozzle),
        "device": {
            "extruder": {"state": 32770, "info": [{"id": 0, "temp": nozzle}]},
            "bed": {"info": {"temp": 60}},
        },
        **more,
    }


def _linked(printers: PrinterStore) -> str:
    pid = printers.create(PrinterCreate(serial="01P00A000000001", name="H2C")).id
    printers.link_lan(pid, LanLink(host="10.0.0.9", access_code="12345678"), cert_sha256="ab" * 32)
    return pid


def _versions(pg_pool: PgPool, pid: str) -> tuple[str, str]:
    """The row versions of the report and its counter: a write makes new ones."""
    with pg_pool.connection() as conn:
        report = conn.execute(
            "SELECT xmin::text AS v FROM printer_reports WHERE printer_id = %s", (pid,)
        ).fetchone()
        counter = conn.execute(
            "SELECT xmin::text AS v FROM printer_run_hours WHERE printer = %s", (pid,)
        ).fetchone()
    assert report is not None and counter is not None
    return report["v"], counter["v"]


def test_the_refresh_keeps_every_reader_of_seen_at_right() -> None:
    assert REFRESH_EVERY * 3 <= STALE_AFTER
    assert REFRESH_EVERY * 3 <= run_hours.GAP_LIMIT


def test_the_read_view_leaves_out_what_no_reader_reads() -> None:
    assert read_view(_report(nozzle=220.0)) == read_view(_report(nozzle=221.5))
    assert read_view(_report()) != read_view(_report(mc_percent=11))
    assert read_view(_report()) != read_view(_report("PAUSE"))
    nozzle = {"info": [{"id": 0, "diameter": "0.4"}]}
    with_nozzle = _report()
    with_nozzle["device"] = {**with_nozzle["device"], "nozzle": nozzle}
    assert read_view(_report()) != read_view(with_nozzle)
    switched = _report()
    switched["device"] = {**switched["device"], "fila_switch": {"stat": 0}}
    assert read_view(_report()) != read_view(switched)
    assert read_view(_report()) != read_view(_report(ams={"ams": [{"id": "0", "temp": "25"}]}))


def test_an_unchanged_report_writes_nothing_until_the_refresh(
    printers_with_kek: PrinterStore, pg_pool: PgPool
) -> None:
    pid = _linked(printers_with_kek)
    reports = PrinterReportStore(pg_pool)
    assert reports.advance(pid, _report(nozzle=220.0), _at(0)) == Advance(started=JOB)
    written = _versions(pg_pool, pid)

    assert reports.advance(pid, _report(nozzle=221.0), _at(1)) == Advance()
    assert reports.advance(pid, _report(nozzle=222.0), _at(29)) == Advance()
    assert _versions(pg_pool, pid) == written
    assert reports.get(pid) == (_report(nozzle=220.0), _at(0))

    refresh = REFRESH_EVERY.total_seconds()
    assert reports.advance(pid, _report(nozzle=223.0), _at(refresh)) == Advance()
    assert reports.get(pid) == (_report(nozzle=223.0), _at(refresh))
    assert _versions(pg_pool, pid)[0] != written[0]


def test_a_change_a_reader_reads_is_written_at_once(
    printers_with_kek: PrinterStore, pg_pool: PgPool
) -> None:
    pid = _linked(printers_with_kek)
    reports = PrinterReportStore(pg_pool)
    reports.advance(pid, _report(), _at(0))
    assert reports.advance(pid, _report(mc_percent=11), _at(1)) == Advance()
    assert reports.get(pid) == (_report(mc_percent=11), _at(1))
    assert reports.advance(pid, _report("FINISH"), _at(2)) == Advance(
        ended=PrintEnd(subtask_name=JOB, timed=True)
    )
    assert reports.get(pid) == (_report("FINISH"), _at(2))


def test_skipped_reports_count_the_same_hours(
    printers_with_kek: PrinterStore, pg_pool: PgPool
) -> None:
    """The counter's state and subtask are in the read view, so a skipped report never
    changes what the row would count; the next write counts the whole stretch."""
    pid = _linked(printers_with_kek)
    reports = PrinterReportStore(pg_pool)
    for second in range(0, 25, 1):
        reports.advance(pid, _report(nozzle=200.0 + second), _at(second))
    reports.advance(pid, _report("FINISH"), _at(25))
    with pg_pool.connection() as conn:
        counter = run_hours.read(conn, pid)
    assert counter is not None
    assert (counter.run_seconds, counter.unseen_seconds) == (Decimal(25), Decimal(0))
    assert (counter.state, counter.counted_to) == ("FINISH", _at(25))


def test_a_stale_report_is_still_not_written(
    printers_with_kek: PrinterStore, pg_pool: PgPool
) -> None:
    pid = _linked(printers_with_kek)
    reports = PrinterReportStore(pg_pool)
    reports.advance(pid, _report(), _at(10))
    assert reports.advance(pid, _report("FINISH"), _at(5)) == Advance()
    assert reports.get(pid) == (_report(), _at(10))


def test_a_report_older_than_a_touch_is_not_written(
    printers_with_kek: PrinterStore, pg_pool: PgPool
) -> None:
    pid = _linked(printers_with_kek)
    reports = PrinterReportStore(pg_pool)
    reports.advance(pid, _report(), _at(0))
    reports.touch(pid, _at(10))
    assert reports.advance(pid, _report(mc_percent=11), _at(5)) == Advance()
    assert reports.get(pid) == (_report(), _at(10))


def test_a_touch_between_the_two_reads_makes_the_report_stale(
    printers_with_kek: PrinterStore, pg_pool: PgPool
) -> None:
    """A touch takes no advisory lock: one that commits after `advance_in`'s first read and
    before its ``FOR UPDATE`` read must still stop the older report, or ``seen_at`` moves
    back."""
    pid = _linked(printers_with_kek)
    reports = PrinterReportStore(pg_pool)
    reports.advance(pid, _report(), _at(0))
    answers: list[Advance] = []
    with pg_pool.connection() as touching, touching.transaction():
        touching.execute(
            "UPDATE printer_reports SET seen_at = GREATEST(seen_at, %s) WHERE printer_id = %s",
            (_at(10), pid),
        )
        late = threading.Thread(
            target=lambda: answers.append(reports.advance(pid, _report(mc_percent=11), _at(5)))
        )
        late.start()
        deadline = time.monotonic() + 10
        with pg_pool.connection() as watching:
            while time.monotonic() < deadline:
                waiting = watching.execute(
                    "SELECT 1 FROM pg_stat_activity WHERE wait_event_type = 'Lock'"
                    " AND query LIKE '%FOR UPDATE%' AND query LIKE '%printer_reports%'"
                ).fetchone()
                if waiting is not None:
                    break
                time.sleep(0.01)
            else:
                pytest.fail("the late report never waited for the row lock")
    late.join(timeout=10)
    assert answers == [Advance()]
    assert reports.get(pid) == (_report(), _at(10))


def test_a_skip_never_leaves_a_counter_gap_past_the_refresh() -> None:
    row = run_hours.RunHours(
        run_seconds=Decimal(0),
        unseen_seconds=Decimal(0),
        counted_to=T0,
        state="RUNNING",
        subtask=JOB,
    )
    assert run_hours.unchanged_by(row, T0 + REFRESH_EVERY / 2, _report(), within=REFRESH_EVERY)
    assert not run_hours.unchanged_by(row, T0 + REFRESH_EVERY, _report(), within=REFRESH_EVERY)
    assert not run_hours.unchanged_by(
        row, T0 + run_hours.GAP_LIMIT, _report(), within=run_hours.GAP_LIMIT * 2
    )


def test_the_commit_is_a_span(pg_pool: PgPool, spans: InMemorySpanExporter) -> None:
    spans.clear()
    with pg_pool.connection() as conn:
        with span("request") as request, traced_transaction(conn):
            conn.execute("SELECT 1")
        commits = [s for s in spans.get_finished_spans() if s.name == COMMIT_SPAN]
        assert len(commits) == 1
        assert commits[0].parent is not None
        assert commits[0].parent.span_id == request.get_span_context().span_id
        spans.clear()
        with conn.transaction(), traced_transaction(conn):
            conn.execute("SELECT 1")
        assert COMMIT_SPAN not in [s.name for s in spans.get_finished_spans()]
        spans.clear()
        with pytest.raises(psycopg.errors.DivisionByZero), traced_transaction(conn):
            conn.execute("SELECT 1 / 0")
        assert COMMIT_SPAN not in [s.name for s in spans.get_finished_spans()]
        assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
