"""Spool types and effective values (#6261)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any
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
from scadbuddy.orders.models import FilamentPatch
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
    second = store.create(_body(pg_pool, code="OTHER", rgba="#ffffff", cost_per_kg="12"))
    assert second.filament_id == first.filament_id


@pg
def test_a_translucent_type_makes_a_translucent_book_row(pg_pool: PgPool) -> None:
    """The book row keeps the type's alpha, stops and effect (#6209 landed in #6211)."""
    rows = [r.model_dump() for r in CatalogStore(pg_pool).list("effects")]
    effect = next(r["id"] for r in rows if r["name"] == "Dual Color")
    made = SpoolTypeStore(pg_pool).create(
        _body(
            pg_pool,
            rgba="#FFFFFF80",
            extra_colours=["#FF0000", "#00FF00"],
            effect_id=effect,
            cost_per_kg="10",
        )
    )
    book = OrderStore(pg_pool).filament(made.filament_id or "")
    assert book is not None
    assert (book.colour, book.alpha, book.extra_colours, book.effect) == (
        "#FFFFFF",
        0x80,
        ["#FF0000FF", "#00FF00FF"],
        "dual-color",
    )


@pg
def test_an_opaque_and_a_translucent_type_of_one_hue_get_two_rows(pg_pool: PgPool) -> None:
    store = SpoolTypeStore(pg_pool)
    orders = OrderStore(pg_pool)
    solid = store.create(_body(pg_pool, cost_per_kg="10"))
    clear = store.create(_body(pg_pool, code="CLEAR", rgba="#FFFFFF80", cost_per_kg="12"))
    assert solid.filament_id is not None
    assert clear.filament_id is not None
    assert clear.filament_id != solid.filament_id
    books = [orders.filament(solid.filament_id), orders.filament(clear.filament_id)]
    assert [b.alpha if b is not None else None for b in books] == [0xFF, 0x80]
    again = store.create(_body(pg_pool, code="CLEAR2", rgba="#FFFFFF80", cost_per_kg="12"))
    assert again.filament_id == clear.filament_id


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


@pg
def test_changing_a_type_colour_never_rewrites_a_shared_row(pg_pool: PgPool) -> None:
    store = SpoolTypeStore(pg_pool)
    orders = OrderStore(pg_pool)
    a = store.create(_body(pg_pool, code="A", rgba="#FFFFFFFF", cost_per_kg="10"))
    b = store.create(_body(pg_pool, code="B", rgba="#FFFFFF", cost_per_kg="10"))
    assert a.filament_id == b.filament_id
    shared = a.filament_id
    assert shared is not None
    changed = store.update(a.id, SpoolTypePatch(rgba="#FF0000"))
    assert changed is not None
    assert changed.filament_id != shared
    kept = orders.filament(shared)
    assert kept is not None
    assert kept.colour == "#FFFFFF"
    moved = store.get(b.id)
    assert moved is not None
    assert moved.filament_id == shared
    fresh = orders.filament(changed.filament_id or "")
    assert fresh is not None
    assert fresh.colour == "#FF0000"


@pg
def test_a_patch_without_price_or_identity_leaves_the_row_alone(pg_pool: PgPool) -> None:
    store = SpoolTypeStore(pg_pool)
    orders = OrderStore(pg_pool)
    made = store.create(_body(pg_pool, colour_name="Snow", cost_per_kg="10"))
    assert made.filament_id is not None
    book = orders.filament(made.filament_id)
    assert book is not None
    orders.update_filament(made.filament_id, FilamentPatch(colour_name="Mine"))
    store.update(made.id, SpoolTypePatch(note="x"))
    store.archive(made.id, True)
    again = orders.filament(made.filament_id)
    assert again is not None
    assert again.colour_name == "Mine"
    assert again.archived_at is None


@pg
def test_a_failure_after_the_price_book_write_rolls_back_the_type(
    pg_pool: PgPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SpoolTypeStore(pg_pool)
    real = OrderStore.insert_filament

    def inserted_then_failed(self: OrderStore, *args: Any, **kwargs: Any) -> Any:
        real(self, *args, **kwargs)
        raise RuntimeError("boom")

    monkeypatch.setattr(OrderStore, "insert_filament", inserted_then_failed)
    with pytest.raises(RuntimeError):
        store.create(_body(pg_pool, cost_per_kg="10"))
    assert store.list(include_archived=True) == []
    assert OrderStore(pg_pool).filaments() == []
