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

from scadbuddy.bambuddy.linking import ARCHIVE_PAGE, MAX_ARCHIVE_PAGES
from scadbuddy.bambuddy.models import Archive, SpoolUsage
from scadbuddy.bambuddy.models import Spool as BbSpool
from scadbuddy.filament import bambuddy_usage, capture, ledger
from scadbuddy.filament.bambuddy_pull import pull
from scadbuddy.filament.bambuddy_usage import book_usage
from scadbuddy.filament.catalog import CatalogStore
from scadbuddy.filament.component import Filament
from scadbuddy.filament.inventory import FilamentInventory
from scadbuddy.filament.invoices import InvoiceStore
from scadbuddy.filament.locations import LocationStore
from scadbuddy.filament.metrics_store import MetricsSettings, MetricsStore
from scadbuddy.filament.presets import PoolSpoolPresets
from scadbuddy.filament.rfid_mode import RfidModeStore
from scadbuddy.filament.sensors import SensorStore
from scadbuddy.filament.shopping import ShoppingStore
from scadbuddy.filament.spools import FilamentSpoolStore
from scadbuddy.filament.suppliers import SuppliersStore
from scadbuddy.filament.types import SpoolTypeStore
from scadbuddy.library.settings_store import StoredSettings
from tests.conftest import PgPool
from tests.filament.pull_support import read_and_pull

pytestmark = pytest.mark.requires_postgres

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
T0 = datetime(2026, 11, 14, 10, 0, tzinfo=UTC)
T1 = datetime(2026, 11, 14, 11, 0, tzinfo=UTC)
BB = "https://bambuddy.test"
JOB = "scadbuddy-ac13ac3c-p1"


def _filament(pool: PgPool) -> Filament:
    types = SpoolTypeStore(pool)
    spools = FilamentSpoolStore(pool)
    return Filament(
        catalog=CatalogStore(pool),
        types=types,
        spools=spools,
        locations=LocationStore(pool),
        sensors=SensorStore(pool),
        inventory=FilamentInventory(pool),
        presets=PoolSpoolPresets(pool),
        rfid=RfidModeStore(pool),
        suppliers=SuppliersStore(pool, lambda: "USD"),
        invoices=InvoiceStore(pool, lambda: "USD", spools, types),
        shopping=ShoppingStore(pool),
        metrics=MetricsStore(pool, spools, MetricsSettings),
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


async def test_an_archived_bb_spool_comes_in_with_its_usage(pg_pool: PgPool) -> None:
    """#6707: a BB spool Bambuddy archived before any pull is made archived, and its usage
    rows are booked (with their archive) under the reading it was made with, which still
    sets what is left. A second pull makes and books nothing."""
    f = _filament(pg_pool)
    settings = StoredSettings(bambuddy_url=BB, bambuddy_api_key="k")
    done = T1 + timedelta(minutes=5)
    spent = bb(
        id=125, weight_used=1020.93, archived_at=_naive(done), updated_at=_naive(done)
    ).model_dump(mode="json")
    rows = [
        usage(id=1, spool_id=125, weight_used=600.0, created_at=_naive(T0)).model_dump(mode="json"),
        usage(id=2, spool_id=125, weight_used=420.93).model_dump(mode="json"),
    ]
    ended = _naive(T1 + timedelta(seconds=0.9))
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{BB}/api/v1/inventory/spools").respond(json=[spent])
        router.get(f"{BB}/api/v1/inventory/usage").respond(json=rows)
        router.get(f"{BB}/api/v1/inventory/catalog").respond(json=[])
        router.get(f"{BB}/api/v1/archives/").respond(
            json=[{"id": 248, "printer_id": 1, "completed_at": ended}]
        )
        report = await read_and_pull(f, settings)
        again = await read_and_pull(f, settings)
    (spool,) = report.created_ids
    assert (report.archived_created, report.usage_booked, report.usage_unlinked) == (1, 2, 0)
    events = _usage_events(f, spool)
    assert sorted((e.ref_id, e.delta_g) for e in events) == [("1", -600.0), ("2", -420.93)]
    assert {e.ref_id: e.data["bb_archive_id"] for e in events} == {"1": None, "2": 248}
    got = f.spools.get(spool)
    assert got is not None and got.archived_at == done
    assert _left(f, spool) == 0
    assert (again.created, again.archived_created, again.usage_booked) == (0, 0, 0)
    assert len(_usage_events(f, spool)) == 2


async def test_an_archived_bb_spool_with_a_row_after_its_updated_at_keeps_bbs_value(
    pg_pool: PgPool,
) -> None:
    """R-6707-14: BB may not move ``updated_at`` for every change, so a usage row can be
    later than it. BB's ``weight_used`` holds that row already, so grams left stays
    label - used (1000 - 600), and that row is not taken off again."""
    f = _filament(pg_pool)
    settings = StoredSettings(bambuddy_url=BB, bambuddy_api_key="k")
    spent = bb(id=125, weight_used=600.0, archived_at=_naive(T1), updated_at=_naive(T1)).model_dump(
        mode="json"
    )
    late = T1 + timedelta(hours=1)
    rows = [
        usage(id=1, spool_id=125, weight_used=400.0, created_at=_naive(T0)).model_dump(mode="json"),
        usage(id=2, spool_id=125, weight_used=200.0, created_at=_naive(late)).model_dump(
            mode="json"
        ),
    ]
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{BB}/api/v1/inventory/spools").respond(json=[spent])
        router.get(f"{BB}/api/v1/inventory/usage").respond(json=rows)
        router.get(f"{BB}/api/v1/inventory/catalog").respond(json=[])
        router.get(f"{BB}/api/v1/archives/").respond(json=[])
        report = await read_and_pull(f, settings)
    (spool,) = report.created_ids
    assert report.usage_booked == 2
    assert _left(f, spool) == 400
    # The row keeps its own time; the reading and the archive are not earlier than it.
    events = f.spools.events(spool)
    assert {e.ref_id: e.at for e in events if e.ref_kind == ledger.BB_USAGE} == {
        "1": T0,
        "2": late,
    }
    assert [e.at for e in events if e.kind in ("created", "archived")] == [late, late]
    got = f.spools.get(spool)
    assert got is not None and got.archived_at == T1


def test_an_archived_bb_spools_link_keeps_bbs_own_updated_at(pg_pool: PgPool) -> None:
    """R-6707-17: a later usage row times the reading only. The link keeps BB's own
    ``updated_at``, so once a person unarchives the spool in SB, a BB edit made after that
    time (but before the row) still reaches SB."""
    f = _filament(pg_pool)
    late = T1 + timedelta(hours=1)
    spent = {"id": 125, "weight_used": 600.0, "archived_at": _naive(T1)}
    rows = [usage(id=2, spool_id=125, weight_used=200.0, created_at=_naive(late))]
    (spool,) = pull(f, [bb(**spent, updated_at=_naive(T1))], now=NOW, usage=rows).created_ids
    f.spools.archive(spool, False, actor="ann")
    edited = bb(**spent, updated_at=_naive(T1 + timedelta(minutes=30)), color_name="Crimson")
    report = pull(f, [edited], now=NOW)
    assert report.updated_ids == [spool]
    got = f.spools.get(spool)
    assert got is not None and got.colour_name == "Crimson"


def test_a_spool_archived_in_sb_still_books_no_earlier_usage(pg_pool: PgPool) -> None:
    """Only a spool the pull made archived books the rows before its reading: one a person
    archived keeps the rule of any linked spool."""
    f = _filament(pg_pool)
    spool = _linked(f)
    f.spools.archive(spool, True, actor="ann")
    assert _book(f, [usage(created_at=_naive(T0 - timedelta(hours=1)))]).booked == 0


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


async def test_a_failed_usage_read_defers_a_new_archived_spool(pg_pool: PgPool) -> None:
    """R-6707-16: with no usage rows, an archived BB spool is not made: its reading could
    not be timed after its rows, so a row would be taken off twice. The live spool is
    made as always, and the next pull that reads the rows makes the archived one."""
    f = _filament(pg_pool)
    settings = StoredSettings(bambuddy_url=BB, bambuddy_api_key="k")
    spent = bb(id=125, weight_used=600.0, archived_at=_naive(T1), updated_at=_naive(T1)).model_dump(
        mode="json"
    )
    late = T1 + timedelta(hours=1)
    rows = [
        usage(id=1, spool_id=125, weight_used=400.0, created_at=_naive(T0)).model_dump(mode="json"),
        usage(id=2, spool_id=125, weight_used=200.0, created_at=_naive(late)).model_dump(
            mode="json"
        ),
    ]
    live = bb(id=7).model_dump(mode="json")
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{BB}/api/v1/inventory/spools").respond(json=[spent, live])
        usage_route = router.get(f"{BB}/api/v1/inventory/usage")
        usage_route.respond(503)
        router.get(f"{BB}/api/v1/inventory/catalog").respond(json=[])
        router.get(f"{BB}/api/v1/archives/").respond(json=[])
        first = await read_and_pull(f, settings)
        usage_route.respond(json=rows)
        second = await read_and_pull(f, settings)
    (made_live,) = first.created_ids
    assert (first.archived_created, first.archived_deferred) == (0, 1)
    assert first.archived_deferred_bb_ids == [125]
    assert [s.id for s in f.spools.list()] == [made_live]
    (spool,) = second.created_ids
    assert (second.archived_created, second.archived_deferred) == (1, 0)
    assert second.usage_booked == 2
    assert _left(f, spool) == 400


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


def _printer_1(pool: PgPool) -> None:
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO printers (id, serial, name, bambuddy_printer_id)"
            " VALUES (gen_random_uuid(), 'TEST00000000001', 'H2C', 1)"
        )


def _second_spool(f: Filament) -> UUID:
    (made,) = pull(f, [bb(id=8)], now=NOW).created_ids
    return made


def _estimate(f: Filament, spool: UUID, slot: int = 0, run: str = "run-1") -> str:
    ref = f"{run}/{JOB}/{slot}"
    with f.spools.transaction() as conn:
        ledger.record(
            conn,
            spool,
            "print_started",
            source="print_estimate",
            delta_g=-70.0,
            ref_kind=ledger.PRINT_RUN,
            ref_id=ref,
            at=T0 + timedelta(minutes=10),
            data={"job": JOB},
        )
    return ref


def test_a_usage_row_on_another_spool_than_the_estimate_covers_the_estimate(
    pg_pool: PgPool,
) -> None:
    f = _filament(pg_pool)
    _printer_1(pg_pool)
    z = _linked(f)
    y = _second_spool(f)
    ref = _estimate(f, y)
    assert _left(f, y) == pytest.approx(900 - 70.0)
    assert _book(f, [usage()]).booked == 1
    (zevent,) = _usage_events(f, z)
    (cover,) = [e for e in f.spools.events(y) if e.kind == "print_ended"]
    assert (cover.source, cover.delta_g) == ("print_actual", 0)
    assert (cover.ref_kind, cover.ref_id) == (ledger.PRINT_RUN, ref)
    assert cover.data["covered_by"] == str(zevent.id)
    assert _left(f, z) == pytest.approx(900 - 75.1)
    assert _left(f, y) == pytest.approx(900)


def test_a_two_slot_job_with_two_open_estimates_is_left_alone_and_logged(
    pg_pool: PgPool, caplog: pytest.LogCaptureFixture
) -> None:
    f = _filament(pg_pool)
    _printer_1(pg_pool)
    z = _linked(f)
    y = _second_spool(f)
    (other,) = pull(f, [bb(id=9)], now=NOW).created_ids
    _estimate(f, y, slot=0)
    _estimate(f, other, slot=1)
    with caplog.at_level("WARNING"):
        assert _book(f, [usage()]).booked == 1
    assert not [e for e in f.spools.events(y) if e.kind == "print_ended"]
    assert not [e for e in f.spools.events(other) if e.kind == "print_ended"]
    assert _left(f, y) == pytest.approx(900 - 70.0)
    assert _left(f, z) == pytest.approx(900 - 75.1)
    assert "filament.usage.unpaired" in caplog.text


def test_two_unpaired_usage_rows_of_one_job_cover_nothing(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    _printer_1(pg_pool)
    _linked(f)
    y = _second_spool(f)
    (third,) = pull(f, [bb(id=9)], now=NOW).created_ids
    _estimate(f, y)
    _book(f, [usage(), usage(id=458, spool_id=9)])
    assert not [e for e in f.spools.events(y) if e.kind == "print_ended"]
    assert _left(f, third) == pytest.approx(900 - 75.1)


def test_a_retried_booking_covers_once(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    _printer_1(pg_pool)
    _linked(f)
    y = _second_spool(f)
    _estimate(f, y)
    _book(f, [usage()])
    assert _book(f, [usage()]).booked == 0
    assert len([e for e in f.spools.events(y) if e.kind == "print_ended"]) == 1


def test_a_second_usage_row_of_a_job_already_paired_on_the_estimates_spool_covers_nothing(
    pg_pool: PgPool,
) -> None:
    f = _filament(pg_pool)
    _printer_1(pg_pool)
    z = _linked(f)
    y = _second_spool(f)
    _estimate(f, y)
    # Y's own row pairs with its estimate by `ledger.basis`: no covering print_ended on Y.
    assert _book(f, [usage(id=460, spool_id=8)]).booked == 1
    assert _book(f, [usage()]).booked == 1
    assert len(_usage_events(f, z)) == 1
    ended = [e for e in f.spools.events(y) if e.kind == "print_ended"]
    assert [e.ref_kind for e in ended] == [ledger.BB_USAGE]


def test_an_estimate_event_of_another_printer_is_not_covered(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    _printer_1(pg_pool)
    with pg_pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO printers (id, serial, name, bambuddy_printer_id)"
            " VALUES (gen_random_uuid(), 'TEST00000000002', 'P2', 2) RETURNING id"
        ).fetchone()
    assert row is not None
    _linked(f)
    y = _second_spool(f)
    with f.spools.transaction() as conn:
        ledger.record(
            conn,
            y,
            "print_started",
            source="print_estimate",
            delta_g=-70.0,
            ref_kind=ledger.PRINT_RUN,
            ref_id=f"run-1/{JOB}/0",
            at=T0 + timedelta(minutes=10),
            printer=row["id"],
            data={"job": JOB},
        )
    assert _book(f, [usage()]).booked == 1
    assert not [e for e in f.spools.events(y) if e.kind == "print_ended"]


def test_a_failed_cover_rolls_back_the_booking_and_the_next_pull_retries_both(
    pg_pool: PgPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = _filament(pg_pool)
    _printer_1(pg_pool)
    z = _linked(f)
    y = _second_spool(f)
    _estimate(f, y)
    real = bambuddy_usage._cover
    calls = {"n": 0}

    def flaky(*args: Any, **kwargs: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        real(*args, **kwargs)

    monkeypatch.setattr(bambuddy_usage, "_cover", flaky)
    assert _book(f, [usage()]).booked == 0
    assert not _usage_events(f, z)
    assert _book(f, [usage()]).booked == 1
    assert len(_usage_events(f, z)) == 1
    assert len([e for e in f.spools.events(y) if e.kind == "print_ended"]) == 1


def test_an_archived_estimate_spool_is_not_written_to_and_is_logged(
    pg_pool: PgPool, caplog: pytest.LogCaptureFixture
) -> None:
    f = _filament(pg_pool)
    _printer_1(pg_pool)
    _linked(f)
    y = _second_spool(f)
    _estimate(f, y)
    with pg_pool.connection() as conn:
        conn.execute("UPDATE filament_spools SET archived_at = now() WHERE id = %s", (y,))
    with caplog.at_level("WARNING"):
        assert _book(f, [usage()]).booked == 1
    assert not [e for e in f.spools.events(y) if e.kind == "print_ended"]
    (record,) = [r for r in caplog.records if r.message == "filament.usage.unpaired"]
    assert getattr(record, "reason", None) == "archived"


async def test_the_archive_read_stops_at_the_page_cap_and_says_what_it_missed(
    pg_pool: PgPool, caplog: pytest.LogCaptureFixture
) -> None:
    f = _filament(pg_pool)
    spool = _linked(f)
    settings = StoredSettings(bambuddy_url=BB, bambuddy_api_key="k")
    pages: list[int] = []

    def answer(request: httpx.Request) -> httpx.Response:
        pages.append(int(request.url.params.get("offset", 0)))
        base = 1000 * len(pages)
        return httpx.Response(
            200,
            json=[
                {"id": base + i, "printer_id": 1, "completed_at": _naive(NOW)}
                for i in range(ARCHIVE_PAGE)
            ],
        )

    with respx.mock() as router, caplog.at_level("WARNING"):
        router.get(f"{BB}/api/v1/inventory/spools").respond(json=[bb().model_dump(mode="json")])
        router.get(f"{BB}/api/v1/inventory/usage").respond(json=[usage().model_dump(mode="json")])
        router.get(f"{BB}/api/v1/inventory/catalog").respond(json=[])
        router.get(f"{BB}/api/v1/archives/").mock(side_effect=answer)
        report = await read_and_pull(f, settings)
    assert len(pages) == MAX_ARCHIVE_PAGES
    assert report.usage_booked == 1
    assert len(_usage_events(f, spool)) == 1
    (warning,) = [r for r in caplog.records if r.message.startswith("stopped reading")]
    assert "oldest pending rows" in warning.message


@pytest.mark.parametrize("y_first", [True, False])
def test_a_batch_row_on_the_estimates_spool_stops_a_cover_in_either_order(
    pg_pool: PgPool, y_first: bool
) -> None:
    f = _filament(pg_pool)
    _printer_1(pg_pool)
    z = _linked(f)
    y = _second_spool(f)
    _estimate(f, y)
    rows = [usage(id=460, spool_id=8), usage()]
    assert _book(f, rows if y_first else rows[::-1]).booked == 2
    assert len(_usage_events(f, z)) == 1
    ended = [e for e in f.spools.events(y) if e.kind == "print_ended"]
    assert [e.ref_kind for e in ended] == [ledger.BB_USAGE]


def test_two_rows_of_one_job_on_one_spool_cover_nothing_in_a_batch_or_across_pulls(
    pg_pool: PgPool, caplog: pytest.LogCaptureFixture
) -> None:
    f = _filament(pg_pool)
    _printer_1(pg_pool)
    z = _linked(f)
    y = _second_spool(f)
    first, second = usage(id=457), usage(id=458, created_at=_naive(T1 + timedelta(seconds=1)))
    # One batch.
    _estimate(f, y)
    with caplog.at_level("WARNING"):
        assert _book(f, [first, second]).booked == 2
    assert not [e for e in f.spools.events(y) if e.kind == "print_ended"]
    assert "filament.usage.unpaired" in caplog.text
    # Across two pulls: the first row is booked before Y's estimate exists.
    f = _filament(pg_pool)
    caplog.clear()
    with pg_pool.connection() as conn:
        conn.execute("DELETE FROM filament_spool_events WHERE spool_id = ANY(%s)", ([z, y],))
    assert _book(f, [first]).booked == 1
    _estimate(f, y, run="run-2")
    with caplog.at_level("WARNING"):
        assert _book(f, [second]).booked == 1
    assert not [e for e in f.spools.events(y) if e.kind == "print_ended"]
    assert "filament.usage.unpaired" in caplog.text


def test_a_row_that_covered_an_estimate_does_not_make_a_later_row_of_the_job_ambiguous(
    pg_pool: PgPool,
) -> None:
    f = _filament(pg_pool)
    _printer_1(pg_pool)
    _linked(f)
    y = _second_spool(f)
    _estimate(f, y)
    assert _book(f, [usage()]).booked == 1
    _estimate(f, y, run="run-2")
    later = usage(id=458, created_at=_naive(T1 + timedelta(hours=1)))
    assert _book(f, [later]).booked == 1
    covers = [e for e in f.spools.events(y) if e.kind == "print_ended"]
    assert sorted(str(e.ref_id) for e in covers) == [f"run-1/{JOB}/0", f"run-2/{JOB}/0"]
