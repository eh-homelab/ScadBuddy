"""Every location has a number that is never reused (#6955)."""

from __future__ import annotations

import pytest

from scadbuddy.filament.catalog import CatalogStore
from scadbuddy.filament.locations import LocationStore
from scadbuddy.filament.models import LocationCreate, LocationKind, LocationPatch
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_postgres


def _shelf(pool: PgPool) -> LocationKind:
    rows = CatalogStore(pool).list("location-kinds")
    return next(r for r in rows if isinstance(r, LocationKind) and r.name == "Shelf")


def test_existing_and_new_locations_get_distinct_numbers(pg_pool: PgPool) -> None:
    store = LocationStore(pg_pool)
    kind = _shelf(pg_pool).id
    a = store.create(LocationCreate(name="A", kind_id=kind))
    b = store.create(LocationCreate(name="B", kind_id=kind))
    assert isinstance(a.number, int)
    assert isinstance(b.number, int)
    assert a.number != b.number


def test_printer_kind_is_seeded(pg_pool: PgPool) -> None:
    rows = CatalogStore(pg_pool).list("location-kinds")
    printer = [r for r in rows if isinstance(r, LocationKind) and r.name == "Printer"]
    assert len(printer) == 1
    assert printer[0].ams is False
    assert printer[0].dry_storage is False


def test_numbers_are_not_reused_after_archive(pg_pool: PgPool) -> None:
    store = LocationStore(pg_pool)
    kind = _shelf(pg_pool).id
    old = store.create(LocationCreate(name="Old", kind_id=kind))
    store.update(old.id, LocationPatch(archived=True))
    new = store.create(LocationCreate(name="New", kind_id=kind))
    assert new.number > old.number
