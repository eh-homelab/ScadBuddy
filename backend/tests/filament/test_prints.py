"""A plate's end is booked at the printer report that shows it (#6263 Task 5):
`PrinterReportStore.advance_in` books ``print_ended`` for the job the report ends, at
the report's time; an end it cannot time is left to the run's settle."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from psycopg.types.json import Jsonb

from scadbuddy.filament import ledger, prints
from scadbuddy.filament.catalog import CatalogStore
from scadbuddy.filament.models import Material, SpoolCreate
from scadbuddy.filament.print_uses import SlotUse, record_uses, settle_uses
from scadbuddy.filament.spools import FilamentSpoolStore
from scadbuddy.printers.models import LanLink, PrinterCreate
from scadbuddy.printers.reports import PrinterReportStore
from scadbuddy.printers.store import PrinterStore
from tests.conftest import PgPool
from tests.printers.conftest import KEK

pytestmark = pytest.mark.requires_postgres

RUN = "r1"
JOB = "scadbuddy-r1-p1"


def _spool(pool: PgPool) -> UUID:
    material = next(
        r.id
        for r in CatalogStore(pool).list("materials")
        if isinstance(r, Material) and r.name == "PLA"
    )
    return (
        FilamentSpoolStore(pool)
        .create(SpoolCreate(material_id=material, grams_left=100.0), actor="test")
        .id
    )


def _printer(pool: PgPool) -> tuple[PrinterReportStore, str]:
    printers = PrinterStore(pool, KEK)
    row = printers.create(PrinterCreate(serial="01P00A000000001", name="H2C"))
    printers.link_lan(
        row.id, LanLink(host="10.0.0.9", access_code="12345678"), cert_sha256="ab" * 32
    )
    return PrinterReportStore(pool), row.id


def _start(pool: PgPool, grams: float = 40.0) -> UUID:
    spool = _spool(pool)
    started = datetime.now(UTC)
    record_uses(
        pool, RUN, JOB, [SlotUse(slot_id=0, spool_id=spool, grams=grams, plate_started_at=started)]
    )
    return spool


def _ends(pool: PgPool, spool: UUID) -> list[Any]:
    return [e for e in FilamentSpoolStore(pool).events(spool) if e.kind == "print_ended"]


def _advance(
    pool: PgPool, reports: PrinterReportStore, pid: str, state: str, at: datetime, job: str = JOB
) -> Any:
    with pool.connection() as conn, conn.transaction():
        return reports.advance_in(conn, pid, {"gcode_state": state, "subtask_name": job}, at)


def _running(pool: PgPool, reports: PrinterReportStore, pid: str) -> datetime:
    """The printer runs the job; the time the end is reported at is after it."""
    now = datetime.now(UTC)
    _advance(pool, reports, pid, "RUNNING", now + timedelta(seconds=1))
    return now + timedelta(minutes=5)


def test_a_plate_ends_at_the_report_that_shows_it(pg_pool: PgPool) -> None:
    spool = _start(pg_pool)
    reports, pid = _printer(pg_pool)
    t = _running(pg_pool, reports, pid)
    _advance(pg_pool, reports, pid, "FINISH", t)
    (end,) = _ends(pg_pool, spool)
    assert end.at == t and end.delta_g == -40
    assert end.data["job"] == JOB and end.data["ended"] == "report"
    assert end.data["basis"] == "sliced"
    started = [e for e in FilamentSpoolStore(pg_pool).events(spool) if e.kind == "print_started"]
    assert started[0].data["job"] == JOB


def test_a_failed_plate_books_no_grams(pg_pool: PgPool) -> None:
    spool = _start(pg_pool)
    reports, pid = _printer(pg_pool)
    t = _running(pg_pool, reports, pid)
    _advance(pg_pool, reports, pid, "FAILED", t)
    (end,) = _ends(pg_pool, spool)
    assert end.delta_g == 0 and end.data["basis"] == "not_applied"


def test_a_moved_on_end_is_left_to_the_settle(pg_pool: PgPool) -> None:
    spool = _start(pg_pool)
    reports, pid = _printer(pg_pool)
    t = _running(pg_pool, reports, pid)
    _advance(pg_pool, reports, pid, "RUNNING", t, job="other")
    assert _ends(pg_pool, spool) == []
    with pg_pool.connection() as conn:
        conn.execute(
            "INSERT INTO print_runs (id, output_id, idempotency_key, status, result)"
            " VALUES (%s, 'out', 'k', 'succeeded', %s)",
            (RUN, Jsonb({"lan": [{"subtask_name": JOB}]})),
        )
    assert settle_uses(pg_pool, RUN, ["done"]) is True
    (end,) = _ends(pg_pool, spool)
    assert end.delta_g == -40 and end.data["ended"] == "settle"


def test_a_plate_end_capture_that_raises_still_writes_the_report(
    pg_pool: PgPool, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _start(pg_pool)
    reports, pid = _printer(pg_pool)
    t = _running(pg_pool, reports, pid)

    def boom(*_: Any) -> None:
        raise RuntimeError("secret detail")

    monkeypatch.setattr(prints, "plate_ended", boom)
    with caplog.at_level(logging.WARNING):
        advance = _advance(pg_pool, reports, pid, "FINISH", t)
    assert advance.ended is not None and advance.ended.timed
    stored = reports.get(pid)
    assert stored is not None and stored[0]["gcode_state"] == "FINISH"
    (record,) = [r for r in caplog.records if r.message == prints.FILAMENT_CAPTURE_FAILED]
    assert record.__dict__["what"] == "plate_end" and record.__dict__["error"] == "RuntimeError"
    assert "secret detail" not in caplog.text


def test_book_end_is_once_per_use(pg_pool: PgPool) -> None:
    spool = _start(pg_pool)
    _, pid = _printer(pg_pool)
    with pg_pool.connection() as conn, conn.transaction():
        (use,) = prints.open_for_job(conn, pid, JOB)
        assert prints.open_for_run(conn, RUN) == [use]
        assert prints.book_end(conn, use, applied=True, at=None, actor="x", ended="settle")
        assert prints.book_end(conn, use, applied=True, at=None, actor="x", ended="settle") is None
        assert prints.open_for_job(conn, pid, JOB) == []
    assert len(_ends(pg_pool, spool)) == 1
    assert ledger.PRINT_RUN == "print_run"


def test_an_untimed_end_with_a_reading_before_the_settle_is_the_accepted_upper_bound(
    pg_pool: PgPool,
) -> None:
    """The filament-capture spec's accepted limit (design.md line 325): a plate end no
    report showed is booked at the settle's clock time, "an upper bound". A scale reading
    taken after the true end and before the settle is then older than the end, so the
    plate is taken off the reading once more."""
    spool = _start(pg_pool)
    reports, pid = _printer(pg_pool)
    t = _running(pg_pool, reports, pid)
    _advance(pg_pool, reports, pid, "RUNNING", t, job="other")  # moved on: untimed
    with pg_pool.connection() as conn, conn.transaction():
        ledger.record(
            conn, spool, "weighed", source="scale", absolute_g=60.0, at=t + timedelta(minutes=1)
        )
        (use,) = prints.open_for_job(conn, pid, JOB)
        assert prints.book_end(conn, use, applied=True, at=None, actor="x", ended="settle")
    spool_row = FilamentSpoolStore(pg_pool).get(spool)
    assert spool_row is not None and spool_row.grams_left == 20.0  # 60 - 40, not 60


def test_a_job_of_the_same_name_on_another_printer_is_not_matched(pg_pool: PgPool) -> None:
    """Two printers can run a job of one name: the use of a run on printer A is open for
    A's report, not for B's end of its own job J, which books nothing on A's spool."""
    spool = _start(pg_pool)
    _, pid_a = _printer(pg_pool)
    printers = PrinterStore(pg_pool, KEK)
    other = printers.create(PrinterCreate(serial="01P00A000000002", name="H2D"))
    with pg_pool.connection() as conn, conn.transaction():
        conn.execute(
            "INSERT INTO print_runs (id, output_id, idempotency_key, status, printer)"
            " VALUES (%s, 'out', 'k', 'running', %s)",
            (RUN, pid_a),
        )
        assert [u.spool_id for u in prints.open_for_job(conn, pid_a, JOB)] == [spool]
        assert prints.open_for_job(conn, other.id, JOB) == []
        prints.plate_ended(conn, other.id, JOB, "FINISH", datetime.now(UTC))
    assert _ends(pg_pool, spool) == []
    with pg_pool.connection() as conn, conn.transaction():
        prints.plate_ended(conn, pid_a, JOB, "FINISH", datetime.now(UTC))
    assert len(_ends(pg_pool, spool)) == 1


def test_a_row_written_without_data_job_is_found_by_its_ref(pg_pool: PgPool) -> None:
    """The migration backfills `data.job` once; a print worker of an older release
    that books a start after it (a rolling deploy) writes none. The lookup by run reads
    the job from the ``ref_id`` (``run/subtask/slot``) then, so the settle still
    books it, and the end it books names the job."""
    spool = _spool(pg_pool)
    with pg_pool.connection() as conn, conn.transaction():
        ledger.record(
            conn,
            spool,
            "print_started",
            source="print_estimate",
            delta_g=-5.0,
            ref_kind=ledger.PRINT_RUN,
            ref_id=f"{RUN}/{JOB}/0",
        )
        (use,) = prints.open_for_run(conn, RUN)
        assert (use.job, use.ref_id) == (JOB, f"{RUN}/{JOB}/0")
        assert prints.open_for_job(conn, str(uuid4()), JOB) == []  # the index lookup needs data.job
        event = prints.book_end(conn, use, applied=True, at=None, actor="x", ended="settle")
    assert event is not None and event.data["job"] == JOB
