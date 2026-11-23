"""Tray changes write loaded, unloaded and AMS readings at once (#6263 Task 6):
`filament/capture.py` reconciles a printer's report with the spools in its tray places;
`PrinterReportStore.advance_in` runs it in a savepoint."""

from __future__ import annotations

import copy
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from psycopg.types.json import Jsonb

from scadbuddy.filament import capture, prints
from scadbuddy.filament.catalog import CatalogStore
from scadbuddy.filament.inventory import FilamentInventory, _Keys
from scadbuddy.filament.locations import LocationStore
from scadbuddy.filament.models import LocationCreate, LocationKind, Material, SpoolCreate
from scadbuddy.filament.spools import FilamentSpoolStore
from scadbuddy.printers.models import LanLink, PrinterCreate
from scadbuddy.printers.reports import PrinterReportStore
from scadbuddy.printers.status import STALE_AFTER
from scadbuddy.printers.store import PrinterStore
from scadbuddy.spools.models import SeenTray
from tests.conftest import PgPool
from tests.printers.conftest import KEK

pytestmark = pytest.mark.requires_postgres

U1 = "A1B2C3D4E5F60718293A4B5C6D7E8F90"
U2 = "B1B2C3D4E5F60718293A4B5C6D7E8F90"
U3 = "C1B2C3D4E5F60718293A4B5C6D7E8F90"
T0 = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


def _spool(pool: PgPool, tray_uuid: str | None = None) -> UUID:
    material = next(
        r.id
        for r in CatalogStore(pool).list("materials")
        if isinstance(r, Material) and r.name == "PLA"
    )
    store = FilamentSpoolStore(pool)
    spool = store.create(SpoolCreate(material_id=material, grams_left=100.0), actor="t").id
    if tray_uuid is not None:
        store.bind_tag(spool, kind="bambu_mifare", actor="t", tray_uuid=tray_uuid)
    return spool


def _printer(pool: PgPool) -> tuple[str, PrinterReportStore]:
    printers = PrinterStore(pool, KEK)
    row = printers.create(PrinterCreate(serial="01P00A000000001", name="H2C"))
    printers.link_lan(
        row.id, LanLink(host="10.0.0.9", access_code="12345678"), cert_sha256="ab" * 32
    )
    return row.id, PrinterReportStore(pool)


def _tray(tray_id: int, uuid: str | None = None, **more: Any) -> dict[str, Any]:
    tray: dict[str, Any] = {
        "id": str(tray_id),
        "tray_type": "PLA",
        "tray_color": "FF0000FF",
        "state": 11,
    }
    if uuid is not None:
        tray["tray_uuid"] = uuid
    return tray | more


def _report(*trays: dict[str, Any], unit: str = "0", **top: Any) -> dict[str, Any]:
    return {"ams": {"ams": [{"id": unit, "humidity": "4", "tray": list(trays)}]}} | top


def _reconcile(
    pool: PgPool, pid: str, report: dict[str, Any], at: datetime = T0
) -> capture.CaptureResult:
    with pool.connection() as conn, conn.transaction():
        return capture.reconcile(conn, pid, report, at, actor=capture.WATCH_ACTOR)


def _events(pool: PgPool, spool: UUID, *kinds: str) -> list[Any]:
    found = FilamentSpoolStore(pool).events(spool)
    return [e for e in found if not kinds or e.kind in kinds]


def _row(pool: PgPool, spool: UUID) -> dict[str, Any]:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT location_id, tray_fingerprint, grams_left FROM filament_spools WHERE id = %s",
            (spool,),
        ).fetchone()
    assert row is not None
    return dict(row)


def _tray_place(pool: PgPool, pid: str, ams_id: int = 0, tray_id: int = 1) -> UUID | None:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT id FROM filament_locations WHERE printer_id = %s AND ams_id = %s"
            " AND tray_id = %s AND archived_at IS NULL",
            (pid, ams_id, tray_id),
        ).fetchone()
    return None if row is None else row["id"]


def _shelf(pool: PgPool) -> UUID:
    kind = next(
        r.id for r in CatalogStore(pool).list("location-kinds") if isinstance(r, LocationKind)
    )
    return LocationStore(pool).create(LocationCreate(name="Shelf S", kind_id=kind)).id


def test_a_tagged_spool_loads_and_reads(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool, tray_uuid=U1)
    pid, _ = _printer(pg_pool)
    result = _reconcile(pg_pool, pid, _report(_tray(1, U1, remain=80)))
    assert result == capture.CaptureResult(loaded=1, unloaded=0, readings=1)
    kinds = [e.kind for e in _events(pg_pool, spool) if e.kind not in ("created", "tag_bound")]
    assert kinds == ["loaded", "moved", "ams_reading"]
    place = _tray_place(pg_pool, pid)
    moved = _events(pg_pool, spool, "moved")[0]
    assert moved.at == T0 and moved.data["to"] == str(place)
    (reading,) = _events(pg_pool, spool, "ams_reading")
    assert reading.data["remain_pct"] == 80 and reading.source == "ams_remain"
    row = _row(pg_pool, spool)
    assert row["location_id"] == place and row["tray_fingerprint"] == "PLA|#FF0000FF"


def test_a_swap_unloads_the_old_spool_and_loads_the_new(pg_pool: PgPool) -> None:
    a, b = _spool(pg_pool, tray_uuid=U1), _spool(pg_pool, tray_uuid=U2)
    pid, _ = _printer(pg_pool)
    shelf = _shelf(pg_pool)
    LocationStore(pg_pool).move(a, shelf, actor="t")
    _reconcile(pg_pool, pid, _report(_tray(1, U1)))
    result = _reconcile(pg_pool, pid, _report(_tray(1, U2)), T0 + timedelta(minutes=1))
    assert (result.loaded, result.unloaded) == (1, 1)
    (gone,) = _events(pg_pool, a, "unloaded")
    assert gone.data == {"by": "tag"} and gone.ams_id == 0 and gone.tray_id == 1
    assert _row(pg_pool, a)["location_id"] == shelf
    assert _row(pg_pool, a)["tray_fingerprint"] is None
    assert len(_events(pg_pool, b, "loaded")) == 1
    assert _row(pg_pool, b)["location_id"] == _tray_place(pg_pool, pid)


def test_a_tagged_spool_moved_to_a_lower_tray_stays_loaded(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool, tray_uuid=U1)
    pid, _ = _printer(pg_pool)
    _reconcile(pg_pool, pid, _report(_tray(2, U1)))
    result = _reconcile(pg_pool, pid, _report(_tray(1, U1), {"id": "2"}), T0 + timedelta(minutes=1))
    assert (result.loaded, result.unloaded) == (1, 0)
    assert len(_events(pg_pool, spool, "loaded")) == 2
    assert _events(pg_pool, spool, "unloaded") == []
    row = _row(pg_pool, spool)
    assert row["location_id"] == _tray_place(pg_pool, pid, tray_id=1)
    assert row["tray_fingerprint"] == "PLA|#FF0000FF"


def test_an_emptied_tray_unloads(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool, tray_uuid=U1)
    pid, _ = _printer(pg_pool)
    _reconcile(pg_pool, pid, _report(_tray(1, U1, remain=50)))
    result = _reconcile(pg_pool, pid, _report({"id": "1"}), T0 + timedelta(minutes=1))
    assert (result.loaded, result.unloaded, result.readings) == (0, 1, 0)
    assert len(_events(pg_pool, spool, "unloaded")) == 1
    assert len(_events(pg_pool, spool, "ams_reading")) == 1
    assert _row(pg_pool, spool)["location_id"] is None


def test_a_partial_report_unloads_nothing(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool, tray_uuid=U1)
    pid, _ = _printer(pg_pool)
    _reconcile(pg_pool, pid, _report(_tray(1, U1)))
    before = len(_events(pg_pool, spool))
    assert _reconcile(pg_pool, pid, {"gcode_state": "IDLE"}) == capture.CaptureResult()
    assert len(_events(pg_pool, spool)) == before


def test_a_rescan_with_no_keys_keeps_the_spool(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool, tray_uuid=U1)
    pid, _ = _printer(pg_pool)
    _reconcile(pg_pool, pid, _report(_tray(1, U1)))
    before = len(_events(pg_pool, spool))
    zeros = _tray(1, "0" * 32, tag_uid="0" * 16)
    assert _reconcile(pg_pool, pid, _report(zeros), T0 + timedelta(minutes=1)) == (
        capture.CaptureResult()
    )
    assert len(_events(pg_pool, spool)) == before


def _link(pool: PgPool, pid: str, spool: UUID, rgba: str = "#FF0000FF") -> None:
    FilamentInventory(pool).link(
        UUID(pid),
        0,
        1,
        spool,
        SeenTray(ams_id=0, tray_id=1, material="PLA", colour=rgba[:7], rgba=rgba),
        actor="t",
    )


@pytest.mark.parametrize("loading_state", [1, 5], ids=["legacy", "bitfield"])
def test_a_loading_tray_keeps_an_untagged_spool(pg_pool: PgPool, loading_state: int) -> None:
    """SPOOL set and STEADY clear is a tray loading or scanning (`bambuddy/trays.py`
    `spool_loaded`; an older firmware's state 1, below ``TRAY_STATE_LEGACY_MAX``, reads
    the same): no news, not an empty tray."""
    spool = _spool(pg_pool)
    pid, _ = _printer(pg_pool)
    _link(pg_pool, pid, spool)
    place = _tray_place(pg_pool, pid)
    fingerprint = _row(pg_pool, spool)["tray_fingerprint"]
    assert fingerprint is not None
    before = len(_events(pg_pool, spool))
    loading = _report(_tray(1, state=loading_state))
    assert _reconcile(pg_pool, pid, loading, T0 + timedelta(minutes=1)) == capture.CaptureResult()
    steady = _report(_tray(1, remain=50))
    assert _reconcile(pg_pool, pid, steady, T0 + timedelta(minutes=2)) == capture.CaptureResult()
    assert len(_events(pg_pool, spool)) == before
    assert _events(pg_pool, spool, "unloaded", "ams_reading") == []
    row = _row(pg_pool, spool)
    assert row["location_id"] == place
    assert row["tray_fingerprint"] == fingerprint


@pytest.mark.parametrize("loading_state", [1, 5], ids=["legacy", "bitfield"])
def test_a_loading_tray_keeps_a_tagged_spool_without_churn(
    pg_pool: PgPool, loading_state: int
) -> None:
    spool = _spool(pg_pool, tray_uuid=U1)
    pid, _ = _printer(pg_pool)
    _reconcile(pg_pool, pid, _report(_tray(1, U1, remain=50)))
    before = len(_events(pg_pool, spool))
    loading = _report(_tray(1, U1, remain=50, state=loading_state))
    assert _reconcile(pg_pool, pid, loading, T0 + timedelta(minutes=1)) == capture.CaptureResult()
    steady = _report(_tray(1, U1, remain=50))
    assert _reconcile(pg_pool, pid, steady, T0 + timedelta(minutes=2)) == capture.CaptureResult()
    assert len(_events(pg_pool, spool)) == before
    assert _row(pg_pool, spool)["location_id"] == _tray_place(pg_pool, pid)


def test_unbound_keys_keep_the_placed_spool(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool)
    pid, _ = _printer(pg_pool)
    _link(pg_pool, pid, spool)
    before = len(_events(pg_pool, spool))
    assert _reconcile(pg_pool, pid, _report(_tray(1, U3))) == capture.CaptureResult()
    assert len(_events(pg_pool, spool)) == before
    assert _row(pg_pool, spool)["location_id"] == _tray_place(pg_pool, pid)


def test_a_translucent_spool_is_not_an_opaque_trays_spool(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool)
    pid, _ = _printer(pg_pool)
    _link(pg_pool, pid, spool, "#FFFFFF80")
    result = _reconcile(pg_pool, pid, _report(_tray(1, tray_color="FFFFFFFF")))
    assert result.unloaded == 1
    assert _row(pg_pool, spool)["location_id"] is None


def test_the_same_remain_books_one_reading(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool, tray_uuid=U1)
    pid, _ = _printer(pg_pool)
    counts = []
    for minute, remain in ((0, 80), (1, 80), (2, 79)):
        at = T0 + timedelta(minutes=minute)
        _reconcile(pg_pool, pid, _report(_tray(1, U1, remain=remain)), at)
        counts.append(len(_events(pg_pool, spool, "ams_reading")))
    assert counts == [1, 1, 2]


def test_no_reading_without_a_remain(pg_pool: PgPool) -> None:
    ext, tray = _spool(pg_pool, tray_uuid=U1), _spool(pg_pool, tray_uuid=U2)
    pid, _ = _printer(pg_pool)
    report = _report(_tray(0, U2), vt_tray=_tray(254, U1, remain=0))
    result = _reconcile(pg_pool, pid, report)
    assert (result.loaded, result.readings) == (2, 0)
    assert _events(pg_pool, ext, "ams_reading") == []
    assert _events(pg_pool, tray, "ams_reading") == []


def test_a_reading_while_printing_is_marked(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool, tray_uuid=U1)
    pid, _ = _printer(pg_pool)
    report = _report(_tray(1, U1, remain=60), gcode_state="RUNNING", subtask_name="job-1")
    _reconcile(pg_pool, pid, report)
    (reading,) = _events(pg_pool, spool, "ams_reading")
    assert reading.data == {"remain_pct": 60, "printing": True, "job": "job-1"}
    assert _row(pg_pool, spool)["grams_left"] == 100.0


def test_humidity_alone_runs_no_reconcile() -> None:
    first = _report(_tray(1, U1, remain=80))
    second = copy.deepcopy(first)
    second["ams"]["ams"][0]["humidity"] = "5"
    second["ams"]["ams"][0]["temp"] = "30"
    assert capture.trays_changed(first, second) is False
    second["ams"]["ams"][0]["tray"][0]["remain"] = 79
    assert capture.trays_changed(first, second) is True
    assert capture.trays_changed(None, first) is True


def test_a_resent_report_books_nothing(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool, tray_uuid=U1)
    pid, reports = _printer(pg_pool)
    report = _report(_tray(1, U1, remain=80))
    sizes = []
    for _ in range(2):
        with pg_pool.connection() as conn, conn.transaction():
            reports.advance_in(conn, pid, report, T0)
        sizes.append(len(_events(pg_pool, spool)))
    assert sizes[0] == 5 and sizes[1] == sizes[0]


def test_a_tray_capture_that_raises_still_writes_the_report(
    pg_pool: PgPool, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    pid, reports = _printer(pg_pool)

    def boom(*_: Any, **__: Any) -> None:
        raise RuntimeError("secret detail")

    monkeypatch.setattr(capture, "reconcile", boom)
    with caplog.at_level(logging.WARNING), pg_pool.connection() as conn, conn.transaction():
        reports.advance_in(conn, pid, _report(_tray(1, U1)), T0)
    assert reports.get(pid) is not None
    (record,) = [r for r in caplog.records if r.message == prints.FILAMENT_CAPTURE_FAILED]
    assert record.__dict__["what"] == "trays" and record.__dict__["error"] == "RuntimeError"
    assert "secret detail" not in caplog.text


def test_a_pulled_tray_that_only_loses_its_presence_bit_unloads(pg_pool: PgPool) -> None:
    """An AMS-HT echoes a stale ``tray_type`` after its spool is pulled; only
    ``tray_exist_bits`` says so (`bambuddy/trays.py` `_filled`)."""
    spool = _spool(pg_pool, tray_uuid=U1)
    pid, _ = _printer(pg_pool)
    tray = _tray(1, U1)
    present = {"ams": {"tray_exist_bits": "2", "ams": [{"id": "0", "tray": [tray]}]}}
    pulled = {"ams": {"tray_exist_bits": "0", "ams": [{"id": "0", "tray": [tray]}]}}
    assert capture.trays_changed(present, pulled) is True
    assert _reconcile(pg_pool, pid, present).loaded == 1
    assert _reconcile(pg_pool, pid, pulled, T0 + timedelta(minutes=1)).unloaded == 1
    assert _row(pg_pool, spool)["location_id"] is None


def test_a_tray_state_change_is_a_tray_change() -> None:
    first = _report(_tray(1, U1))
    second = copy.deepcopy(first)
    second["ams"]["ams"][0]["tray"][0]["state"] = 9
    assert capture.trays_changed(first, second) is True


@pytest.mark.parametrize(
    ("keys", "rgba", "found"),
    [
        ({"tray_uuid": U1}, "#FF0000FF", "tag"),
        ({"tray_uuid": U3}, "#FF0000FF", "link"),
        ({}, "#FF0000FF", "link"),
        ({}, "#FF000080", None),
        ({"tray_uuid": U3}, "#FF000080", None),
    ],
)
def test_holder_on_and_the_batch_reader_agree(
    pg_pool: PgPool, keys: dict[str, str], rgba: str, found: str | None
) -> None:
    tagged, placed = _spool(pg_pool, tray_uuid=U1), _spool(pg_pool)
    pid, _ = _printer(pg_pool)
    _link(pg_pool, pid, placed)
    seen = SeenTray(
        ams_id=0,
        tray_id=1,
        material="PLA",
        colour=rgba[:7],
        rgba=rgba,
        **keys,  # type: ignore[arg-type]
    )
    with pg_pool.connection() as conn:
        own = FilamentInventory.holder_on(conn, UUID(pid), seen)
        other = _Keys(conn).hold(UUID(pid), seen)
    expected = {"tag": tagged, "link": placed}.get(found or "")
    assert own == other == (expected, found)


def test_capture_and_link_name_a_place_alike(pg_pool: PgPool) -> None:
    pid, _ = _printer(pg_pool)
    spool = _spool(pg_pool, tray_uuid=U1)
    _reconcile(pg_pool, pid, _report(_tray(1, U1), vt_tray=_tray(254, U2)))
    ext = _spool(pg_pool, tray_uuid=U2)
    _reconcile(pg_pool, pid, _report(_tray(1, U1), vt_tray=_tray(254, U2)), T0 + timedelta(1))
    with pg_pool.connection() as conn:
        names = {
            r["tray_id"]: r["name"]
            for r in conn.execute(
                "SELECT ams_id, tray_id, name FROM filament_locations WHERE printer_id = %s"
                " AND ams_id = 0",
                (pid,),
            ).fetchall()
        }
        external = conn.execute(
            "SELECT name FROM filament_locations WHERE printer_id = %s AND ams_id = 255", (pid,)
        ).fetchone()
    assert names[1] == "H2C AMS-A slot 2"
    assert external == {"name": "H2C External slot 1"}
    assert spool != ext


def _boom(*_: Any, **__: Any) -> None:
    raise RuntimeError("secret detail")


def test_a_lock_failure_skips_the_rest_and_still_writes_the_report(
    pg_pool: PgPool, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    spool = _spool(pg_pool, tray_uuid=U1)
    pid, reports = _printer(pg_pool)
    monkeypatch.setattr(capture, "lock", _boom)
    with caplog.at_level(logging.WARNING), pg_pool.connection() as conn, conn.transaction():
        reports.advance_in(conn, pid, _report(_tray(1, U1)), T0)
    assert reports.get(pid) is not None
    (record,) = [r for r in caplog.records if r.message == prints.FILAMENT_CAPTURE_FAILED]
    assert record.__dict__["what"] == "lock" and record.__dict__["error"] == "RuntimeError"
    assert _events(pg_pool, spool, "loaded") == []
    assert "secret detail" not in caplog.text


def test_a_failed_lock_skips_the_plate_end_and_the_trays(
    pg_pool: PgPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    spool = _spool(pg_pool, tray_uuid=U1)
    pid, reports = _printer(pg_pool)
    job = {"subtask_name": "job-1"}
    with pg_pool.connection() as conn, conn.transaction():
        reports.advance_in(conn, pid, {"gcode_state": "RUNNING"} | job, T0)
    ended: list[object] = []
    monkeypatch.setattr(capture, "lock", _boom)
    monkeypatch.setattr(prints, "plate_ended", lambda *args: ended.append(args))
    done = _report(_tray(1, U1)) | {"gcode_state": "FINISH"} | job
    with pg_pool.connection() as conn, conn.transaction():
        reports.advance_in(conn, pid, done, T0 + timedelta(minutes=5))
    assert ended == []
    assert _events(pg_pool, spool, "loaded") == []


def test_a_plate_end_failure_still_writes_the_report_and_the_trays(
    pg_pool: PgPool, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    spool = _spool(pg_pool, tray_uuid=U1)
    pid, reports = _printer(pg_pool)
    job = {"subtask_name": "job-1"}
    with pg_pool.connection() as conn, conn.transaction():
        reports.advance_in(conn, pid, {"gcode_state": "RUNNING"} | job, T0)
    monkeypatch.setattr(prints, "plate_ended", _boom)
    done = _report(_tray(1, U1)) | {"gcode_state": "FINISH"} | job
    with caplog.at_level(logging.WARNING), pg_pool.connection() as conn, conn.transaction():
        reports.advance_in(conn, pid, done, T0 + timedelta(minutes=5))
    stored = reports.get(pid)
    assert stored is not None and stored[0]["gcode_state"] == "FINISH"
    (record,) = [r for r in caplog.records if r.message == prints.FILAMENT_CAPTURE_FAILED]
    assert record.__dict__["what"] == "plate_end"
    assert len(_events(pg_pool, spool, "loaded")) == 1


def _write_report(pool: PgPool, pid: str, report: dict[str, Any], seen_at: datetime) -> None:
    """A report row with no capture, as a watch that missed the change leaves it."""
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO printer_reports (printer_id, report, seen_at) VALUES (%s, %s, %s)"
            " ON CONFLICT (printer_id) DO UPDATE SET report = EXCLUDED.report,"
            " seen_at = EXCLUDED.seen_at",
            (pid, Jsonb(report), seen_at),
        )


def test_the_backstop_books_a_change_the_watch_missed(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool, tray_uuid=U1)
    pid, _ = _printer(pg_pool)
    _write_report(pg_pool, pid, _report(_tray(1, U1, remain=80)), T0)
    first = capture.reconcile_all(pg_pool, [pid], now=T0 + timedelta(seconds=5))
    assert first.loaded == 1
    assert [e.kind for e in _events(pg_pool, spool, "loaded")] == ["loaded"]
    assert capture.reconcile_all(pg_pool, [pid], now=T0 + timedelta(seconds=10)) == (
        capture.CaptureResult()
    )


def test_a_stale_report_is_left_to_the_watch(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool, tray_uuid=U1)
    pid, _ = _printer(pg_pool)
    _write_report(pg_pool, pid, _report(_tray(1, U1, remain=80)), T0)
    late = T0 + STALE_AFTER + timedelta(seconds=1)
    assert capture.reconcile_all(pg_pool, [pid], now=late) == capture.CaptureResult()
    assert _events(pg_pool, spool, "loaded") == []


def test_a_printer_that_raises_is_skipped_and_the_next_still_runs(
    pg_pool: PgPool, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    spool = _spool(pg_pool, tray_uuid=U1)
    pid, _ = _printer(pg_pool)
    _write_report(pg_pool, pid, _report(_tray(1, U1)), T0)
    real = capture.reconcile
    calls: list[str] = []

    def flaky(conn: Any, printer_id: str, *args: Any, **kwargs: Any) -> Any:
        calls.append(printer_id)
        if len(calls) == 1:
            raise RuntimeError("secret detail")
        return real(conn, printer_id, *args, **kwargs)

    monkeypatch.setattr(capture, "reconcile", flaky)
    with caplog.at_level(logging.WARNING):
        result = capture.reconcile_all(pg_pool, [pid, pid], now=T0 + timedelta(seconds=5))
    assert result.loaded == 1
    assert "secret detail" not in caplog.text
    assert [e.kind for e in _events(pg_pool, spool, "loaded")] == ["loaded"]


def test_capture_leaves_an_ams_unit_place_alone(pg_pool: PgPool) -> None:
    pid, _ = _printer(pg_pool)
    with pg_pool.connection() as conn:
        unit = LocationStore.ensure_ams_unit_on(conn, UUID(pid), 0)
    spool = _spool(pg_pool, tray_uuid=U1)
    _reconcile(pg_pool, pid, _report(_tray(1, U1)))
    assert _tray_place(pg_pool, pid) is not None
    assert _row(pg_pool, spool)["location_id"] != unit
    with pg_pool.connection() as conn:
        row = conn.execute("SELECT archived_at FROM filament_locations WHERE id = %s", (unit,))
        assert row.fetchone() == {"archived_at": None}


def _flatten(pool: PgPool, pid: str) -> UUID:
    """Make the printer's places as an install from before printer places has them: no
    root, the unit place with no parent. Answers the unit place's id."""
    with pool.connection() as conn:
        row = conn.execute(
            "UPDATE filament_locations SET parent_id = NULL WHERE printer_id = %s"
            " AND ams_id IS NOT NULL AND tray_id IS NULL RETURNING id",
            (pid,),
        ).fetchone()
        conn.execute(
            "DELETE FROM filament_locations WHERE printer_id = %s AND printer_root", (pid,)
        )
    assert row is not None
    return UUID(str(row["id"]))


def _root(pool: PgPool, pid: str) -> UUID | None:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT id FROM filament_locations WHERE printer_id = %s AND printer_root"
            " AND archived_at IS NULL",
            (pid,),
        ).fetchone()
    return None if row is None else UUID(str(row["id"]))


def test_a_flat_install_gets_its_printer_place_on_the_next_capture(pg_pool: PgPool) -> None:
    """#6955: a unit place with no parent and a tray place under it (made before printer
    places) gets the printer's place on the next capture, even one that loads nothing."""
    pid, _ = _printer(pg_pool)
    spool = _spool(pg_pool, U1)
    _reconcile(pg_pool, pid, _report(_tray(1, U1)))
    unit = _flatten(pg_pool, pid)
    assert _root(pg_pool, pid) is None
    result = _reconcile(pg_pool, pid, _report(_tray(1, U1)), T0 + timedelta(minutes=1))
    assert result.loaded == 0
    root = _root(pg_pool, pid)
    assert root is not None
    placed = LocationStore(pg_pool).get(unit)
    assert placed is not None and placed.parent_id == root
    assert _row(pg_pool, spool)["location_id"] == _tray_place(pg_pool, pid)


def test_a_flat_install_with_no_printer_kind_still_captures(pg_pool: PgPool) -> None:
    pid, _ = _printer(pg_pool)
    spool = _spool(pg_pool, U1)
    _reconcile(pg_pool, pid, _report(_tray(1, U1)))
    unit = _flatten(pg_pool, pid)
    with pg_pool.connection() as conn:
        conn.execute("UPDATE filament_location_kinds SET printer = false")
    _reconcile(pg_pool, pid, _report({"id": "1"}), T0 + timedelta(minutes=1))
    assert _root(pg_pool, pid) is None
    placed = LocationStore(pg_pool).get(unit)
    assert placed is not None and placed.parent_id is None
    assert _row(pg_pool, spool)["location_id"] is None
