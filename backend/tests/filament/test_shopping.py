"""The shopping list (#6266)."""

from __future__ import annotations

from uuid import UUID

import pytest
from pydantic import BaseModel

from scadbuddy.filament.catalog import CatalogStore
from scadbuddy.filament.models import (
    Brand,
    BrandCreate,
    Material,
    SpoolTypeCreate,
    Subtype,
    SubtypeCreate,
)
from scadbuddy.filament.purchasing_models import ShoppingItemCreate, ShoppingItemPatch
from scadbuddy.filament.shopping import ShoppingRefError, ShoppingStore
from scadbuddy.filament.types import SpoolTypeStore
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_postgres


def _named(catalog: CatalogStore, kind: str, name: str, body: BaseModel) -> BaseModel:
    found = next((r for r in catalog.list(kind) if getattr(r, "name", None) == name), None)
    return found if found is not None else catalog.create(kind, body)


def _named_type(pool: PgPool) -> UUID:
    catalog = CatalogStore(pool)
    material = next(
        r.id for r in catalog.list("materials") if isinstance(r, Material) and r.name == "PLA"
    )
    brand = _named(catalog, "brands", "Bambu Lab", BrandCreate(name="Bambu Lab"))
    subtype = _named(catalog, "subtypes", "Basic", SubtypeCreate(name="Basic"))
    assert isinstance(brand, Brand)
    assert isinstance(subtype, Subtype)
    return (
        SpoolTypeStore(pool)
        .create(
            SpoolTypeCreate.model_validate(
                {
                    "code": "PLA-MB",
                    "material_id": material,
                    "brand_id": brand.id,
                    "subtype_id": subtype.id,
                    "colour_name": "Misty Blue",
                    "rgba": "#6396C8",
                    "label_weight_g": 1000,
                }
            )
        )
        .id
    )


def _add(store: ShoppingStore, material: str = "PETG", **over: object) -> UUID:
    return store.add(ShoppingItemCreate.model_validate({"material": material, **over})).id


def test_an_item_from_a_type_takes_its_names(pg_pool: PgPool) -> None:
    store = ShoppingStore(pg_pool)
    type_id = _named_type(pg_pool)
    item = store.add(ShoppingItemCreate(type_id=type_id))
    assert (item.material, item.brand, item.subtype, item.colour_name) == (
        "PLA",
        "Bambu Lab",
        "Basic",
        "Misty Blue",
    )
    assert item.type_id == type_id
    assert item.status == "pending"
    given = store.add(ShoppingItemCreate(type_id=type_id, colour_name="Other"))
    assert given.colour_name == "Other"
    assert given.brand == "Bambu Lab"


def test_a_missing_type_is_a_ref_error(pg_pool: PgPool) -> None:
    store = ShoppingStore(pg_pool)
    with pytest.raises(ShoppingRefError):
        store.add(ShoppingItemCreate(type_id=UUID("00000000-0000-4000-8000-000000000001")))


def test_an_item_survives_its_types_archive(pg_pool: PgPool) -> None:
    store = ShoppingStore(pg_pool)
    type_id = _named_type(pg_pool)
    store.add(ShoppingItemCreate(type_id=type_id))
    with pg_pool.connection() as conn:
        conn.execute(
            "UPDATE filament_spool_types SET archived_at = now() WHERE id = %s", (type_id,)
        )
    [item] = store.list()
    assert (item.material, item.brand, item.colour_name) == ("PLA", "Bambu Lab", "Misty Blue")
    assert item.type_id == type_id


def test_marking_purchased_stamps_the_time_and_pending_clears_it(pg_pool: PgPool) -> None:
    store = ShoppingStore(pg_pool)
    item_id = _add(store)
    bought = store.update(item_id, ShoppingItemPatch(status="purchased"))
    assert bought is not None
    assert bought.status == "purchased"
    assert bought.purchased_at is not None
    stamp = bought.purchased_at
    again = store.update(item_id, ShoppingItemPatch(status="purchased", note="x"))
    assert again is not None
    assert again.purchased_at == stamp
    assert again.note == "x"
    back = store.update(item_id, ShoppingItemPatch(status="pending"))
    assert back is not None
    assert (back.status, back.purchased_at) == ("pending", None)
    assert back.updated_at > back.created_at
    assert store.update(UUID(int=1), ShoppingItemPatch(note="n")) is None


def test_a_patch_changes_only_what_it_sends(pg_pool: PgPool) -> None:
    store = ShoppingStore(pg_pool)
    item_id = _add(store, quantity_spools=2, note="keep")
    changed = store.update(item_id, ShoppingItemPatch(quantity_spools=5))
    assert changed is not None
    assert (changed.quantity_spools, changed.note) == (5, "keep")
    cleared = store.update(item_id, ShoppingItemPatch.model_validate({"note": None}))
    assert cleared is not None
    assert cleared.note is None


def test_pending_lists_first_then_newest(pg_pool: PgPool) -> None:
    store = ShoppingStore(pg_pool)
    first = _add(store, "A")
    second = _add(store, "B")
    third = _add(store, "C")
    store.update(third, ShoppingItemPatch(status="purchased"))
    assert [i.id for i in store.list()] == [second, first, third]
    assert [i.id for i in store.list(status="purchased")] == [third]


def test_remove_says_whether_it_removed(pg_pool: PgPool) -> None:
    store = ShoppingStore(pg_pool)
    item_id = _add(store)
    assert store.remove(item_id) is True
    assert store.remove(item_id) is False


def test_clear_purchased_only_keeps_pending(pg_pool: PgPool) -> None:
    store = ShoppingStore(pg_pool)
    bought = _add(store, "A")
    _add(store, "B")
    _add(store, "C")
    store.update(bought, ShoppingItemPatch(status="purchased"))
    assert store.clear(purchased_only=True) == 1
    assert len(store.list()) == 2
    assert store.clear(purchased_only=False) == 2
    assert store.list() == []


def test_an_archived_type_can_still_be_added(pg_pool: PgPool) -> None:
    store = ShoppingStore(pg_pool)
    type_id = _named_type(pg_pool)
    with pg_pool.connection() as conn:
        conn.execute(
            "UPDATE filament_spool_types SET archived_at = now() WHERE id = %s", (type_id,)
        )
    item = store.add(ShoppingItemCreate(type_id=type_id))
    assert (item.material, item.type_id) == ("PLA", type_id)
