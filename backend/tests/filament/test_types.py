"""Spool types and effective values (#6261)."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest

from scadbuddy.filament.catalog import CatalogExistsError, CatalogStore
from scadbuddy.filament.models import (
    Material,
    SpoolTypeCreate,
    SpoolTypePatch,
    TypeKValue,
    TypePreset,
)
from scadbuddy.filament.types import SpoolTypeStore, TypeTempError, effective, overridden
from scadbuddy.orders.store import OrderStore
from tests.conftest import PgPool

pg = pytest.mark.requires_postgres


def test_effective_takes_spool_then_type_then_subtype_then_material() -> None:
    material = {"density_g_cm3": Decimal("1.24"), "nozzle_temp_min": 190}
    subtype = {"density_g_cm3": None, "nozzle_temp_min": 200, "material_id": UUID(int=1)}
    assert effective({}, {}, None, material).density_g_cm3 == Decimal("1.24")
    assert effective({}, {}, subtype, material).nozzle_temp_min == 200
    assert effective({}, {"nozzle_temp_min": 210}, subtype, material).nozzle_temp_min == 210
    spool = {"nozzle_temp_min": 220, "density_g_cm3": Decimal("1.30")}
    one = effective(spool, {"density_g_cm3": Decimal("1.27")}, subtype, material)
    assert one.density_g_cm3 == Decimal("1.30")
    assert one.nozzle_temp_min == 220
    assert "density_g_cm3" in overridden(spool)
    assert overridden({"density_g_cm3": None}) == []


def test_a_subtype_does_not_supply_the_material() -> None:
    subtype = {"material_id": UUID(int=1)}
    assert effective({}, {"material_id": UUID(int=2)}, subtype, None).material_id == UUID(int=2)
    assert effective({}, {}, subtype, None).material_id is None


def _material(pool: PgPool, name: str = "PLA") -> UUID:
    rows = CatalogStore(pool).list("materials")
    return next(r.id for r in rows if isinstance(r, Material) and r.name == name)


def _body(pool: PgPool, **over: object) -> SpoolTypeCreate:
    values: dict[str, object] = {
        "code": "PLA-WHT",
        "material_id": _material(pool),
        "rgba": "#FFFFFF",
        "label_weight_g": 1000,
    }
    return SpoolTypeCreate.model_validate(values | over)


@pg
def test_a_priced_type_writes_its_price_book_row(pg_pool: PgPool) -> None:
    store = SpoolTypeStore(pg_pool)
    orders = OrderStore(pg_pool)
    made = store.create(_body(pg_pool, cost_per_kg="19.99"))
    assert made.filament_id is not None
    book = orders.filament(made.filament_id)
    assert book is not None
    assert (book.material, book.colour, Decimal(book.cost_per_kg)) == (
        "PLA",
        "#FFFFFF",
        Decimal("19.99"),
    )
    changed = store.update(made.id, SpoolTypePatch(cost_per_kg=Decimal("21.5")))
    assert changed is not None
    assert changed.filament_id == made.filament_id
    book = orders.filament(made.filament_id)
    assert book is not None
    assert Decimal(book.cost_per_kg) == Decimal("21.5")


@pg
def test_an_unpriced_type_makes_no_price_book_row(pg_pool: PgPool) -> None:
    made = SpoolTypeStore(pg_pool).create(_body(pg_pool))
    assert made.filament_id is None


@pg
def test_a_type_joins_an_existing_price_book_row(pg_pool: PgPool) -> None:
    store = SpoolTypeStore(pg_pool)
    first = store.create(_body(pg_pool, cost_per_kg="10"))
    second = store.create(_body(pg_pool, code="OTHER", rgba="#FFFFFF80", cost_per_kg="12"))
    assert second.filament_id == first.filament_id


@pg
def test_match_compares_alpha(pg_pool: PgPool) -> None:
    store = SpoolTypeStore(pg_pool)
    store.create(_body(pg_pool, code="SOLID", rgba="#FFFFFFFF"))
    clear = store.create(_body(pg_pool, code="CLEAR", rgba="#FFFFFF80"))
    found = store.match(
        brand=None, material="PLA", subtype=None, rgba="#FFFFFF80", label_weight_g=1000
    )
    assert found is not None
    assert found.id == clear.id


@pg
def test_rrggbb_input_matches_rgba(pg_pool: PgPool) -> None:
    store = SpoolTypeStore(pg_pool)
    solid = store.create(_body(pg_pool, code="SOLID", rgba="#ffffffff"))
    found = store.match(
        brand=None, material="pla", subtype=None, rgba="ffffff", label_weight_g=1000
    )
    assert found is not None
    assert found.id == solid.id
    assert (
        store.match(brand=None, material="PLA", subtype=None, rgba="#FFFFFF", label_weight_g=250)
        is None
    )


@pg
def test_match_by_code_ignores_case_and_archived(pg_pool: PgPool) -> None:
    store = SpoolTypeStore(pg_pool)
    made = store.create(_body(pg_pool, code="Abc-1"))
    found = store.match(
        code="aBC-1", brand=None, material="x", subtype=None, rgba="#000000", label_weight_g=1
    )
    assert found is not None
    assert found.id == made.id
    store.archive(made.id, True)
    assert (
        store.match(
            code="abc-1", brand=None, material="x", subtype=None, rgba="#000000", label_weight_g=1
        )
        is None
    )


@pg
def test_a_taken_code_is_refused(pg_pool: PgPool) -> None:
    store = SpoolTypeStore(pg_pool)
    store.create(_body(pg_pool))
    with pytest.raises(CatalogExistsError):
        store.create(_body(pg_pool, code="pla-wht"))


@pg
def test_a_temperature_range_must_not_be_backwards(pg_pool: PgPool) -> None:
    store = SpoolTypeStore(pg_pool)
    with pytest.raises(TypeTempError):
        store.create(_body(pg_pool, nozzle_temp_min=250, nozzle_temp_max=200))
    made = store.create(_body(pg_pool, bed_temp_min=50, bed_temp_max=60))
    with pytest.raises(TypeTempError):
        store.update(made.id, SpoolTypePatch(bed_temp_max=40))
    unchanged = store.get(made.id)
    assert unchanged is not None
    assert unchanged.bed_temp_max == 60


@pg
def test_presets_and_k_values_are_replaced_whole(pg_pool: PgPool) -> None:
    store = SpoolTypeStore(pg_pool)
    made = store.create(_body(pg_pool))
    one = TypePreset(
        type_id=made.id,
        printer_model="H2C",
        nozzle_diameter="0.4",
        slicer_filament="GFA00",
        slicer_filament_name=None,
    )
    assert store.set_presets(made.id, [one]) == [one]
    assert store.presets(made.id) == [one]
    assert store.set_presets(made.id, []) == []
    assert store.presets(made.id) == []
    assert store.set_presets(UUID(int=9), [one]) is None
    unknown_printer = TypeKValue(
        type_id=made.id,
        printer_id=UUID(int=9),
        extruder=0,
        nozzle_diameter="0.4",
        nozzle_type=None,
        k_value=Decimal("0.02"),
        name=None,
        cali_idx=None,
        setting_id=None,
    )
    with pytest.raises(ValueError, match="points at"):
        store.set_k_values(made.id, [unknown_printer])


@pg
def test_list_hides_archived_types(pg_pool: PgPool) -> None:
    store = SpoolTypeStore(pg_pool)
    made = store.create(_body(pg_pool))
    assert [t.id for t in store.list()] == [made.id]
    store.archive(made.id, True)
    assert store.list() == []
    assert [t.id for t in store.list(include_archived=True)] == [made.id]
