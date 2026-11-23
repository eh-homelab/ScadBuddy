"""A location page's read (#6955)."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from scadbuddy.filament.catalog import CatalogStore
from scadbuddy.filament.locations import LocationStore
from scadbuddy.filament.models import LocationCreate, LocationKind, Material, SpoolCreate
from scadbuddy.filament.spools import FilamentSpoolStore
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_postgres


def _kind(pool: PgPool, name: str) -> UUID:
    rows = CatalogStore(pool).list("location-kinds")
    return next(r.id for r in rows if isinstance(r, LocationKind) and r.name == name)


def _printer(pool: PgPool, name: str = "H2C") -> UUID:
    printer = uuid4()
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO printers (id, serial, name) VALUES (%s, %s, %s)",
            (printer, f"SN{printer.hex[:8]}", name),
        )
    return printer


def test_detail_of_a_slot(pg_pool: PgPool) -> None:
    store = LocationStore(pg_pool)
    printer = _printer(pg_pool)
    slot = store.ensure_ams(printer, 0, 1, label="AMS-A slot 2")
    root = store.ensure_printer(printer)
    material = next(
        r.id for r in CatalogStore(pg_pool).list("materials") if isinstance(r, Material)
    )
    spools = FilamentSpoolStore(pg_pool)
    spool = spools.create(
        SpoolCreate(material_id=material, rgba="#FF0000FF", grams_left=250), actor="ann"
    )
    spools.create(SpoolCreate(material_id=material), actor="ann")  # elsewhere
    LocationStore(pg_pool).move(spool.id, slot.id, actor="ann")
    got = store.detail(slot.id)
    assert got is not None
    assert got.code == f"L-{slot.number}"
    assert got.kind_name == _kind_name(pg_pool, slot.kind_id)
    assert [p.name for p in got.path] == ["H2C", "H2C AMS-A"]
    assert got.path[0].id == root
    assert [(s.id, s.code, s.colour_rgba, s.grams_left) for s in got.spools] == [
        (spool.id, f"S-{spool.number}", "#FF0000FF", 250)
    ]
    assert got.spools[0].material is not None


def _kind_name(pool: PgPool, kind_id: UUID) -> str:
    rows = CatalogStore(pool).list("location-kinds")
    return next(r.name for r in rows if isinstance(r, LocationKind) and r.id == kind_id)


def test_detail_of_a_printer_lists_its_units_as_children(pg_pool: PgPool) -> None:
    store = LocationStore(pg_pool)
    printer = _printer(pg_pool)
    store.ensure_ams(printer, 0, 0, label="x")
    store.ensure_ams(printer, 1, 0, label="y")
    beside = store.create(
        LocationCreate(name="Beside", kind_id=_kind(pg_pool, "Shelf"), printer_id=printer)
    )
    root = store.ensure_printer(printer)
    got = store.detail(root)
    assert got is not None
    assert got.path == []
    assert [c.name for c in got.children] == ["H2C AMS-A", "H2C AMS-B"]
    assert beside.id not in [c.id for c in got.children]


def test_detail_of_an_unknown_place_is_none(pg_pool: PgPool) -> None:
    assert LocationStore(pg_pool).detail(uuid4()) is None
