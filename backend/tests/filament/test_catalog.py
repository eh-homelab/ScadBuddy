"""The catalog store (#6261): one generic store over the nine catalog tables."""

from __future__ import annotations

import pytest

from scadbuddy.filament.catalog import CATALOGS, CatalogExistsError, CatalogStore
from scadbuddy.filament.models import Material, MaterialCreate, MaterialPatch
from tests.conftest import PgPool

pg = pytest.mark.requires_postgres

SEEDED = {
    "materials": "PLA",
    "subtypes": "Basic",
    "effects": "Sparkle",
    "insert-materials": "Cardboard",
    "location-kinds": "AMS",
}
EMPTY = ("brands", "categories", "insert-types", "colours")


def test_every_kind_is_declared() -> None:
    assert set(CATALOGS) == {
        "materials",
        "subtypes",
        "brands",
        "categories",
        "effects",
        "insert-materials",
        "insert-types",
        "colours",
        "location-kinds",
    }


@pg
@pytest.mark.parametrize("kind", sorted(SEEDED))
def test_each_catalog_lists_its_seeds(pg_pool: PgPool, kind: str) -> None:
    rows = CatalogStore(pg_pool).list(kind)
    assert SEEDED[kind] in [row.name for row in rows]  # type: ignore[attr-defined]


@pg
@pytest.mark.parametrize("kind", EMPTY)
def test_an_unseeded_catalog_lists_nothing(pg_pool: PgPool, kind: str) -> None:
    assert CatalogStore(pg_pool).list(kind) == []


@pg
def test_an_archived_material_stays_readable_but_leaves_the_list(
    pg_pool: PgPool,
) -> None:
    store = CatalogStore(pg_pool)
    made = store.create("materials", MaterialCreate(name="Wax"))
    assert isinstance(made, Material)
    with pg_pool.connection() as conn:
        conn.execute(
            "INSERT INTO filament_subtypes (name, material_id) VALUES ('Soft', %s)", (made.id,)
        )
    gone = store.archive("materials", made.id, True)
    assert gone is not None and gone.archived_at is not None  # type: ignore[attr-defined]
    assert made.id not in [row.id for row in store.list("materials")]  # type: ignore[attr-defined]
    assert made.id in [row.id for row in store.list("materials", include_archived=True)]  # type: ignore[attr-defined]
    with pg_pool.connection() as conn:
        row = conn.execute(
            "SELECT m.name FROM filament_subtypes s JOIN filament_materials m"
            " ON m.id = s.material_id WHERE s.name = 'Soft'"
        ).fetchone()
    assert row is not None and row["name"] == "Wax"
    back = store.archive("materials", made.id, False)
    assert back is not None and back.archived_at is None  # type: ignore[attr-defined]


@pg
def test_a_second_material_with_the_same_name_is_refused(pg_pool: PgPool) -> None:
    store = CatalogStore(pg_pool)
    with pytest.raises(CatalogExistsError):
        store.create("materials", MaterialCreate(name="pla"))
    made = store.create("materials", MaterialCreate(name="Resin"))
    with pytest.raises(CatalogExistsError):
        store.update("materials", made.id, MaterialPatch(name="PETG"))  # type: ignore[attr-defined]


@pg
def test_a_patch_changes_only_the_fields_sent(pg_pool: PgPool) -> None:
    store = CatalogStore(pg_pool)
    made = store.create("materials", MaterialCreate(name="Resin", family="x", abrasive=True))
    changed = store.update("materials", made.id, MaterialPatch(family=None))  # type: ignore[attr-defined]
    assert isinstance(changed, Material)
    assert (changed.family, changed.abrasive) == (None, True)
    assert store.update("materials", made.id, MaterialPatch()) is not None  # type: ignore[attr-defined]
