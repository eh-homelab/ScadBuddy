"""The Bambuddy path's picker rows (#6262 Task 7): every SB spool, the tray it is in (a
printer's report first, else Bambuddy's assignment of its linked spool), and its
Bambuddy spool; and the reads that turn a Bambuddy spool id into an SB number
(`filament/inventory.py`)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from scadbuddy.bambuddy.models import SpoolAssignment
from scadbuddy.filament.catalog import CatalogStore
from scadbuddy.filament.inventory import FilamentInventory
from scadbuddy.filament.models import Material, SpoolCreate, SpoolPresetCreate, TrayPlace
from scadbuddy.filament.spools import FilamentSpoolStore
from scadbuddy.printers.reports import PrinterReportStore
from scadbuddy.spools.models import SeenTray
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_postgres

UUID_A = "A1B2C3D4E5F60718293A4B5C6D7E8F90"


def _spool(pool: PgPool) -> UUID:
    material = next(
        r.id
        for r in CatalogStore(pool).list("materials")
        if isinstance(r, Material) and r.name == "PLA"
    )
    return FilamentSpoolStore(pool).create(SpoolCreate(material_id=material), actor="ann").id


def _number(pool: PgPool, spool_id: UUID) -> int:
    spool = FilamentSpoolStore(pool).get(spool_id)
    assert spool is not None
    return spool.number


def _printer(pool: PgPool, name: str, *, bambuddy_id: int | None, lan: bool) -> UUID:
    printer = uuid4()
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO printers (id, serial, name, bambuddy_printer_id, lan_host)"
            " VALUES (%s, %s, %s, %s, %s)",
            (printer, f"SN{printer.hex[:8]}", name, bambuddy_id, "10.0.0.9" if lan else None),
        )
    return printer


def _link_bb(pool: PgPool, spool_id: UUID, bb_spool_id: int) -> None:
    now = datetime.now(UTC)
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO filament_bb_links (spool_id, bb_spool_id, bb_updated_at, linked_at)"
            " VALUES (%s, %s, %s, %s)",
            (spool_id, bb_spool_id, now, now),
        )


def _report_tagged(pool: PgPool, printer: UUID, *, ams_id: int, tray_id: int) -> None:
    report = {
        "ams": {
            "ams": [
                {
                    "id": str(ams_id),
                    "tray": [
                        {
                            "id": str(tray_id),
                            "tray_type": "PLA",
                            "tray_color": "000000FF",
                            "state": 11,
                            "tray_uuid": UUID_A,
                        }
                    ],
                }
            ]
        }
    }
    PrinterReportStore(pool).put(str(printer), report, datetime.now(UTC))


def _assigned(bb_spool_id: int, *, printer_id: int, ams_id: int, tray_id: int) -> SpoolAssignment:
    return SpoolAssignment(
        id=bb_spool_id * 10,
        spool_id=bb_spool_id,
        printer_id=printer_id,
        printer_name="Bambuddy's name",
        ams_id=ams_id,
        tray_id=tray_id,
    )


def test_rows_place_a_spool_by_its_tray_then_by_bb(pg_pool: PgPool) -> None:
    """A spool a LAN report holds is in that tray, whatever Bambuddy assigns; a linked
    spool no report holds is in the tray Bambuddy assigns its Bambuddy spool to (the last
    assignment wins); a spool with neither is loaded nowhere."""
    tagged, assigned, shelved = _spool(pg_pool), _spool(pg_pool), _spool(pg_pool)
    FilamentSpoolStore(pg_pool).bind_tag(tagged, tray_uuid=UUID_A, kind="bambu_mifare", actor="ann")
    _link_bb(pg_pool, tagged, 70)
    _link_bb(pg_pool, assigned, 71)
    lan = _printer(pg_pool, "Garage", bambuddy_id=1, lan=True)
    bb_only = _printer(pg_pool, "Attic", bambuddy_id=2, lan=False)
    _report_tagged(pg_pool, lan, ams_id=0, tray_id=2)

    rows = FilamentInventory(pg_pool).rows(
        bb_assignments=[
            _assigned(70, printer_id=2, ams_id=1, tray_id=3),
            _assigned(71, printer_id=1, ams_id=0, tray_id=0),
            _assigned(71, printer_id=2, ams_id=1, tray_id=1),
        ]
    )

    by_id = {row.spool.id: row for row in rows}
    assert set(by_id) == {tagged, assigned, shelved}
    assert by_id[tagged].loaded == TrayPlace(
        printer_id=str(lan), bambuddy_printer_id=1, printer_name="Garage", ams_id=0, tray_id=2
    )
    assert by_id[tagged].bb_spool_id == 70
    assert by_id[assigned].loaded == TrayPlace(
        printer_id=str(bb_only),
        bambuddy_printer_id=2,
        printer_name="Bambuddy's name",
        ams_id=1,
        tray_id=1,
    )
    assert by_id[assigned].bb_spool_id == 71
    assert (by_id[shelved].loaded, by_id[shelved].bb_spool_id) == (None, None)


def test_an_assignment_on_a_printer_with_no_registry_row_has_no_printer_id(
    pg_pool: PgPool,
) -> None:
    spool = _spool(pg_pool)
    _link_bb(pg_pool, spool, 80)
    [row] = [
        r
        for r in FilamentInventory(pg_pool).rows(
            bb_assignments=[_assigned(80, printer_id=9, ams_id=0, tray_id=1)]
        )
        if r.spool.id == spool
    ]
    assert row.loaded is not None
    assert (row.loaded.printer_id, row.loaded.bambuddy_printer_id) == (None, 9)


def test_an_archived_spool_is_a_row_only_when_asked_for(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool)
    FilamentSpoolStore(pg_pool).archive(spool, True, actor="ann")
    inventory = FilamentInventory(pg_pool)
    assert spool not in {row.spool.id for row in inventory.rows()}
    assert spool in {row.spool.id for row in inventory.rows(include_archived=True)}


def test_bb_ids_map_to_their_linked_sb_numbers(pg_pool: PgPool) -> None:
    """F14: an unlinked Bambuddy id is left out, never read as an SB number."""
    a, b = _spool(pg_pool), _spool(pg_pool)
    _link_bb(pg_pool, a, 90)
    _link_bb(pg_pool, b, 91)
    inventory = FilamentInventory(pg_pool)
    assert inventory.bb_to_sb([90, 91, 92, 90]) == {
        90: _number(pg_pool, a),
        91: _number(pg_pool, b),
    }
    assert inventory.bb_to_sb([]) == {}


def test_a_spool_is_found_by_its_number_with_its_bb_link(pg_pool: PgPool) -> None:
    linked, unlinked = _spool(pg_pool), _spool(pg_pool)
    _link_bb(pg_pool, linked, 95)
    inventory = FilamentInventory(pg_pool)
    assert inventory.by_number(_number(pg_pool, linked)) == (linked, 95)
    assert inventory.by_number(_number(pg_pool, unlinked)) == (unlinked, None)
    FilamentSpoolStore(pg_pool).archive(unlinked, True, actor="ann")
    assert inventory.by_number(_number(pg_pool, unlinked)) is None


def test_a_spool_placed_on_a_printer_with_no_report_is_in_that_tray(pg_pool: PgPool) -> None:
    """#6262 Task 7 review I4: a registry printer with no stored report has no trays to
    read, so a spool placed in one of its trays (`_Keys.placed`) is there, as last
    linked, whatever Bambuddy assigns its linked spool to."""
    spool = _spool(pg_pool)
    _link_bb(pg_pool, spool, 90)
    garage = _printer(pg_pool, "Garage", bambuddy_id=1, lan=True)
    seen = SeenTray(ams_id=0, tray_id=1, material="PLA", colour="#FF0000FF")
    FilamentInventory(pg_pool).link(garage, 0, 1, spool, seen, actor="ann")

    [row] = [
        r
        for r in FilamentInventory(pg_pool).rows(
            bb_assignments=[_assigned(90, printer_id=1, ams_id=1, tray_id=3)]
        )
        if r.spool.id == spool
    ]
    assert row.loaded == TrayPlace(
        printer_id=str(garage), bambuddy_printer_id=1, printer_name="Garage", ams_id=0, tray_id=1
    )


def test_a_row_carries_its_spools_effective_presets(pg_pool: PgPool) -> None:
    """#6262 Task 7 review R2: the picker names each spool's preset, so a row carries
    the spool's effective presets (its own over its type's)."""
    spool = _spool(pg_pool)
    row = SpoolPresetCreate(
        printer_model="H2C",
        nozzle_diameter="0.4",
        slicer_filament="GFL99",
        slicer_filament_name="Generic PLA",
    )
    assert FilamentSpoolStore(pg_pool).set_presets(spool, [row], actor="ann") is not None

    [found] = [r for r in FilamentInventory(pg_pool).rows() if r.spool.id == spool]
    assert [(p.nozzle_diameter, p.slicer_filament_name) for p in found.presets] == [
        ("0.4", "Generic PLA")
    ]
