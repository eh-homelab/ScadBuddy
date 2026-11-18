"""Bambuddy's usage rows booked as print ends (#6263, spec §8.2): a row on a linked spool
is one ``print_ended`` at BB's ``created_at``, booked once, with the archive of its print
when one ended in the window, and counted once beside BB's reading and SB's own plate."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest
import respx

from scadbuddy.bambuddy.models import Archive, SpoolUsage
from scadbuddy.bambuddy.models import Spool as BbSpool
from scadbuddy.filament import bambuddy_usage, capture, ledger
from scadbuddy.filament.bambuddy_pull import pull, read_and_pull
from scadbuddy.filament.bambuddy_usage import book_usage
from scadbuddy.filament.catalog import CatalogStore
from scadbuddy.filament.component import Filament
from scadbuddy.filament.inventory import FilamentInventory
from scadbuddy.filament.locations import LocationStore
from scadbuddy.filament.presets import PoolSpoolPresets
from scadbuddy.filament.spools import FilamentSpoolStore
from scadbuddy.filament.types import SpoolTypeStore
from scadbuddy.library.settings_store import StoredSettings
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_postgres

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
T0 = datetime(2026, 11, 14, 10, 0, tzinfo=UTC)
T1 = datetime(2026, 11, 14, 11, 0, tzinfo=UTC)
BB = "https://bambuddy.test"
JOB = "scadbuddy-ac13ac3c-p1"


def _filament(pool: PgPool) -> Filament:
    return Filament(
        catalog=CatalogStore(pool),
        types=SpoolTypeStore(pool),
        spools=FilamentSpoolStore(pool),
        locations=LocationStore(pool),
        inventory=FilamentInventory(pool),
        presets=PoolSpoolPresets(pool),
    )


def _naive(when: datetime) -> str:
    """A Bambuddy time: naive UTC."""
    return when.replace(tzinfo=None).isoformat()


def bb(**over: Any) -> BbSpool:
    body: dict[str, Any] = {
        "id": 7,
        "material": "PLA",
        "label_weight": 1000,
        "weight_used": 100.0,
        "updated_at": _naive(T0),
    } | over
    return BbSpool.model_validate(body)


def usage(**over: Any) -> SpoolUsage:
    body: dict[str, Any] = {
        "id": 457,
        "spool_id": 7,
        "printer_id": 1,
        "print_name": JOB,
        "weight_used": 75.1,
        "percent_used": 7.5,
        "status": "completed",
        "created_at": _naive(T1),
    } | over
    return SpoolUsage.model_validate(body)


def archive(completed_at: str, **over: Any) -> Archive:
    return Archive.model_validate({"id": 248, "printer_id": 1, "completed_at": completed_at} | over)


def _linked(f: Filament) -> UUID:
    (made,) = pull(f, [bb()], now=NOW).created_ids
    return made


def _book(f: Filament, rows: list[SpoolUsage], archives: list[Archive] | None = None) -> Any:
    with f.spools.transaction() as conn:
        return book_usage(conn, rows, archives or [], actor="bambuddy")


def _usage_events(f: Filament, spool: UUID) -> list[Any]:
    return [e for e in f.spools.events(spool) if e.ref_kind == ledger.BB_USAGE]


def _left(f: Filament, spool: UUID) -> float | None:
    got = f.spools.get(spool)
    assert got is not None
    return got.grams_left


def test_a_usage_row_of_a_print_sb_did_not_start_is_a_print_end(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    spool = _linked(f)
    ended = _naive(T1 + timedelta(seconds=0.9))
    report = _book(f, [usage()], [archive(ended + "+00:00")])
    assert (report.booked, report.unlinked) == (1, 0)
    (event,) = _usage_events(f, spool)
    assert (event.kind, event.source, event.delta_g) == ("print_ended", "print_actual", -75.1)
    assert (event.ref_kind, event.ref_id, event.at) == (ledger.BB_USAGE, "457", T1)
    assert event.data["bb_archive_id"] == 248
    assert event.data["job"] == JOB
    assert _left(f, spool) == pytest.approx(900 - 75.1)


def test_a_naive_archive_time_matches(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    spool = _linked(f)
    _book(f, [usage()], [archive(_naive(T1 + timedelta(seconds=0.9)))])
    (event,) = _usage_events(f, spool)
    assert event.data["bb_archive_id"] == 248


def test_an_archive_outside_the_window_or_of_another_printer_is_not_matched(
    pg_pool: PgPool,
) -> None:
    f = _filament(pg_pool)
    spool = _linked(f)
    archives = [
        archive(_naive(T1 + timedelta(seconds=31)), id=1),
        archive(_naive(T1 - timedelta(seconds=1)), id=2),
        archive(_naive(T1 + timedelta(seconds=1)), id=3, printer_id=2),
    ]
    _book(f, [usage()], archives)
    (event,) = _usage_events(f, spool)
    assert event.data["bb_archive_id"] is None


def test_a_usage_row_is_booked_once(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    spool = _linked(f)
    assert _book(f, [usage()]).booked == 1
    assert _book(f, [usage()]).booked == 0
    assert len(_usage_events(f, spool)) == 1
    assert _left(f, spool) == pytest.approx(900 - 75.1)


def test_a_usage_row_before_the_link_is_not_booked(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    spool = _linked(f)
    report = _book(
        f, [usage(created_at=_naive(T0)), usage(id=1, created_at=_naive(T0 - timedelta(hours=1)))]
    )
    assert report.booked == 0
    assert _usage_events(f, spool) == []
    assert _left(f, spool) == 900


def test_a_usage_row_of_an_unlinked_bb_spool_is_counted_unlinked(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    _linked(f)
    report = _book(f, [usage(spool_id=99)])
    assert (report.booked, report.unlinked) == (0, 1)


def test_a_usage_row_without_a_name_is_booked(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    spool = _linked(f)
    assert _book(f, [usage(print_name=None)]).booked == 1
    (event,) = _usage_events(f, spool)
    assert "job" in event.data
    assert event.data["job"] is None
    assert _left(f, spool) == pytest.approx(900 - 75.1)


def test_a_usage_row_names_the_sb_printer(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    spool = _linked(f)
    with pg_pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO printers (id, serial, name, bambuddy_printer_id)"
            " VALUES (gen_random_uuid(), 'TEST00000000001', 'H2C', 1) RETURNING id"
        ).fetchone()
    assert row is not None
    _book(f, [usage(), usage(id=458, printer_id=5, spool_id=7)])
    by_id = {e.ref_id: e.printer_id for e in _usage_events(f, spool)}
    assert by_id == {"457": row["id"], "458": None}


def test_bb_reading_and_usage_of_one_write_count_once(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    spool = _linked(f)
    pull(f, [bb(weight_used=175.1, updated_at=_naive(T1))], now=NOW)
    _book(f, [usage()])
    assert _left(f, spool) == pytest.approx(1000 - 175.1)


def test_an_sb_plate_and_its_usage_row_count_once(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    spool = _linked(f)
    plate = T1 + timedelta(seconds=1)
    ref = f"run-1/{JOB}/0"
    plate_event: dict[str, Any] = {
        "delta_g": -70.0,
        "ref_kind": ledger.PRINT_RUN,
        "ref_id": ref,
        "at": plate,
        "data": {"job": JOB},
    }
    with f.spools.transaction() as conn:
        ledger.record(conn, spool, "print_started", source="print_estimate", **plate_event)
        ledger.record(conn, spool, "print_ended", source="print_actual", **plate_event)
    pull(f, [bb(weight_used=175.1, updated_at=_naive(T1))], now=NOW)
    _book(f, [usage()])
    assert _left(f, spool) == pytest.approx(1000 - 175.1)


async def test_a_pull_books_bambuddys_usage_rows(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    spool = _linked(f)
    settings = StoredSettings(bambuddy_url=BB, bambuddy_api_key="k")
    ended = _naive(T1 + timedelta(seconds=0.9))
    rows = [usage().model_dump(mode="json"), usage(id=9, spool_id=99).model_dump(mode="json")]
    with respx.mock() as router:
        router.get(f"{BB}/api/v1/inventory/spools").respond(json=[bb().model_dump(mode="json")])
        router.get(f"{BB}/api/v1/inventory/usage").respond(json=rows)
        router.get(f"{BB}/api/v1/inventory/catalog").respond(json=[])
        router.get(f"{BB}/api/v1/archives/").respond(
            json=[{"id": 248, "printer_id": 1, "completed_at": ended}]
        )
        report = await read_and_pull(f, settings)
        paths = [call.request.url.path for call in router.calls]
    assert paths[:2] == ["/api/v1/inventory/spools", "/api/v1/inventory/usage"]
    assert (report.usage_booked, report.usage_unlinked) == (1, 1)
    (event,) = _usage_events(f, spool)
    assert event.data["bb_archive_id"] == 248


async def test_the_archive_read_reaches_back_to_the_oldest_row_booked(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    spool = _linked(f)
    settings = StoredSettings(bambuddy_url=BB, bambuddy_api_key="k")
    rows = [usage().model_dump(mode="json")]
    ended = _naive(T1 + timedelta(seconds=0.9))
    filler = [{"id": 1000 + i, "printer_id": 1, "completed_at": _naive(NOW)} for i in range(100)]

    def answer(request: httpx.Request) -> httpx.Response:
        # Without a date window Bambuddy answers its newest rows, which are all later.
        params = request.url.params
        if "date_from" not in params:
            return httpx.Response(200, json=filler[:50])
        assert date.fromisoformat(params["date_from"]) <= T1.date()
        if int(params.get("offset", 0)) == 0:
            return httpx.Response(200, json=filler)
        return httpx.Response(200, json=[{"id": 248, "printer_id": 1, "completed_at": ended}])

    with respx.mock() as router:
        router.get(f"{BB}/api/v1/inventory/spools").respond(json=[bb().model_dump(mode="json")])
        router.get(f"{BB}/api/v1/inventory/usage").respond(json=rows)
        router.get(f"{BB}/api/v1/inventory/catalog").respond(json=[])
        router.get(f"{BB}/api/v1/archives/").mock(side_effect=answer)
        await read_and_pull(f, settings)
    (event,) = _usage_events(f, spool)
    assert event.data["bb_archive_id"] == 248


async def test_rows_that_would_not_book_read_no_archives(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    _linked(f)
    settings = StoredSettings(bambuddy_url=BB, bambuddy_api_key="k")
    rows = [
        usage(id=1, created_at=_naive(T0)).model_dump(mode="json"),
        usage(id=2, spool_id=99).model_dump(mode="json"),
        usage(id=3, weight_used=0).model_dump(mode="json"),
    ]
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{BB}/api/v1/inventory/spools").respond(json=[bb().model_dump(mode="json")])
        router.get(f"{BB}/api/v1/inventory/usage").respond(json=rows)
        router.get(f"{BB}/api/v1/inventory/catalog").respond(json=[])
        archives = router.get(f"{BB}/api/v1/archives/").respond(json=[])
        report = await read_and_pull(f, settings)
    assert not archives.called
    assert (report.usage_booked, report.usage_unlinked) == (0, 1)


async def test_a_failed_unbooked_query_leaves_the_pull_standing(
    pg_pool: PgPool, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_: Any) -> set[int]:
        raise RuntimeError("database gone")

    monkeypatch.setattr(bambuddy_usage, "unbooked", broken)
    f = _filament(pg_pool)
    settings = StoredSettings(bambuddy_url=BB, bambuddy_api_key="k")
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{BB}/api/v1/inventory/spools").respond(json=[bb().model_dump(mode="json")])
        router.get(f"{BB}/api/v1/inventory/usage").respond(json=[usage().model_dump(mode="json")])
        router.get(f"{BB}/api/v1/inventory/catalog").respond(json=[])
        report = await read_and_pull(f, settings)
    assert report.created == 1
    assert "could not find Bambuddy's unbooked usage rows" in caplog.text


def test_a_failing_row_does_not_stall_the_rest(
    pg_pool: PgPool, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = _filament(pg_pool)
    spool = _linked(f)
    record = ledger.record

    def flaky(conn: Any, spool_id: UUID, kind: Any, **kw: Any) -> Any:
        if kw.get("ref_id") == "13":
            conn.execute("SELECT 1/0")
        return record(conn, spool_id, kind, **kw)

    monkeypatch.setattr(ledger, "record", flaky)
    report = _book(f, [usage(id=13), usage(id=14)])
    assert report.booked == 1
    assert [e.ref_id for e in _usage_events(f, spool)] == ["14"]
    assert "could not book a Bambuddy usage row" in caplog.text


def test_a_usage_row_of_an_archived_sb_spool_is_skipped(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    spool = _linked(f)
    f.spools.archive(spool, True, actor="ann")
    report = _book(f, [usage()])
    assert (report.booked, report.unlinked) == (0, 0)
    assert _usage_events(f, spool) == []
    with f.spools.transaction() as conn:
        assert bambuddy_usage.unbooked(conn, [usage()]) == set()


async def test_a_failed_usage_read_leaves_the_pull_standing(
    pg_pool: PgPool, caplog: pytest.LogCaptureFixture
) -> None:
    f = _filament(pg_pool)
    settings = StoredSettings(bambuddy_url=BB, bambuddy_api_key="k")
    with respx.mock() as router:
        router.get(f"{BB}/api/v1/inventory/spools").respond(json=[bb().model_dump(mode="json")])
        router.get(f"{BB}/api/v1/inventory/usage").respond(503)
        router.get(f"{BB}/api/v1/inventory/catalog").respond(json=[])
        report = await read_and_pull(f, settings)
    assert (report.created, report.usage_booked) == (1, 0)
    assert "could not read Bambuddy's usage rows" in caplog.text


async def test_a_failed_archive_read_still_books_the_rows(
    pg_pool: PgPool, caplog: pytest.LogCaptureFixture
) -> None:
    f = _filament(pg_pool)
    spool = _linked(f)
    settings = StoredSettings(bambuddy_url=BB, bambuddy_api_key="k")
    with respx.mock() as router:
        router.get(f"{BB}/api/v1/inventory/spools").respond(json=[bb().model_dump(mode="json")])
        router.get(f"{BB}/api/v1/inventory/usage").respond(json=[usage().model_dump(mode="json")])
        router.get(f"{BB}/api/v1/inventory/catalog").respond(json=[])
        router.get(f"{BB}/api/v1/archives/").respond(503)
        report = await read_and_pull(f, settings)
    assert report.usage_booked == 1
    assert "could not read Bambuddy's archives" in caplog.text
    (event,) = _usage_events(f, spool)
    assert event.data["bb_archive_id"] is None


def test_the_spools_are_locked_in_id_order_before_any_booking(
    pg_pool: PgPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = _filament(pg_pool)
    made = pull(f, [bb(id=7), bb(id=8)], now=NOW).created_ids
    by_bb = {7: made[0], 8: made[1]}
    high_first = max(by_bb, key=lambda k: str(by_bb[k]))
    low = min(by_bb, key=lambda k: str(by_bb[k]))
    rows = [usage(id=1, spool_id=high_first), usage(id=2, spool_id=low), usage(id=3, spool_id=7)]
    locked: list[list[UUID]] = []
    real = capture.lock

    def spy(conn: Any, spool_ids: Any) -> None:
        locked.append(list(spool_ids))
        real(conn, spool_ids)

    monkeypatch.setattr(capture, "lock", spy)
    assert _book(f, rows).booked == 3
    assert locked == [sorted(by_bb.values())]
