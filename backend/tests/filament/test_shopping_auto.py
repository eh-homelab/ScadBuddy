"""An empty spool adds its type to the shopping list (#6868).

The move into empty is checked in `ledger.record`, the one writer of every spool event, so
each path that empties a spool (a print's grams, a weighing, an AMS reading, a person's
edit, the Bambuddy pull, an archive) adds the item in its own transaction."""

from __future__ import annotations

from datetime import UTC
from uuid import UUID

import pytest
from psycopg.types.json import Jsonb

from scadbuddy.filament import ledger
from scadbuddy.filament.catalog import CatalogStore
from scadbuddy.filament.models import Material, SpoolCreate, SpoolEventAdd, SpoolTypeCreate
from scadbuddy.filament.purchasing_models import (
    ShoppingItem,
    ShoppingItemCreate,
    ShoppingItemPatch,
)
from scadbuddy.filament.shopping import AUTO_ADD_SETTING, ShoppingStore
from scadbuddy.filament.spools import FilamentSpoolStore
from scadbuddy.filament.types import SpoolTypeStore
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_postgres


def _material(pool: PgPool) -> UUID:
    rows = CatalogStore(pool).list("materials")
    return next(r.id for r in rows if isinstance(r, Material) and r.name == "PLA")


def _type(pool: PgPool, code: str = "PLA-RED", colour: str = "Red") -> UUID:
    return (
        SpoolTypeStore(pool)
        .create(
            SpoolTypeCreate(
                code=code,
                material_id=_material(pool),
                colour_name=colour,
                rgba="#FF0000",
                label_weight_g=1000,
            )
        )
        .id
    )


def _spool(pool: PgPool, grams: float | None = 100, **over: object) -> UUID:
    body = SpoolCreate.model_validate({"grams_left": grams} | over)
    return FilamentSpoolStore(pool).create(body, actor="ann").id


def _use(pool: PgPool, spool: UUID, grams: float, ref: str = "run/J/0") -> None:
    with pool.connection() as conn, conn.transaction():
        ledger.record(
            conn,
            spool,
            "print_ended",
            source="print_actual",
            delta_g=-grams,
            ref_kind=ledger.PRINT_RUN,
            ref_id=ref,
        )


def _weigh(pool: PgPool, spool: UUID, grams: float) -> None:
    FilamentSpoolStore(pool).add_event(
        spool, SpoolEventAdd(kind="weighed", grams=grams), actor="ann"
    )


def _items(pool: PgPool) -> list[ShoppingItem]:
    return ShoppingStore(pool).list()


def _number(pool: PgPool, spool: UUID) -> int:
    with pool.connection() as conn:
        row = conn.execute("SELECT number FROM filament_spools WHERE id = %s", (spool,)).fetchone()
    assert row is not None
    number: int = row["number"]
    return number


def test_a_spool_emptied_by_a_print_adds_its_type_once(pg_pool: PgPool) -> None:
    kind = _type(pg_pool)
    spool = _spool(pg_pool, type_id=kind)
    _use(pg_pool, spool, 40)
    assert _items(pg_pool) == []
    _use(pg_pool, spool, 60, ref="run/J/1")
    [item] = _items(pg_pool)
    assert (item.type_id, item.material, item.colour_name) == (kind, "PLA", "Red")
    assert (item.status, item.quantity_spools) == ("pending", 1)
    assert item.empty_spool_id == spool
    assert item.note is not None
    assert f"Spool #{_number(pg_pool, spool)}" in item.note
    assert "ran out" in item.note
    # Later writes on the empty spool add nothing more.
    _use(pg_pool, spool, 5, ref="run/J/2")
    _weigh(pg_pool, spool, 0)
    assert len(_items(pg_pool)) == 1


def test_a_weighing_to_zero_adds_the_item(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool, type_id=_type(pg_pool))
    _weigh(pg_pool, spool, 0)
    assert len(_items(pg_pool)) == 1


def test_the_note_names_when_the_spool_ran_out(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool, type_id=_type(pg_pool))
    _weigh(pg_pool, spool, 0)
    weighed = FilamentSpoolStore(pg_pool).events(spool)[-1]
    assert weighed.kind == "weighed"
    when = weighed.at.astimezone(UTC).strftime("%Y-%m-%d %H:%M")
    [item] = _items(pg_pool)
    assert item.note == f"Spool #{_number(pg_pool, spool)} ran out on {when} UTC."


def test_archiving_a_spool_adds_its_type(pg_pool: PgPool) -> None:
    kind = _type(pg_pool)
    spool = _spool(pg_pool, grams=300, type_id=kind)
    FilamentSpoolStore(pg_pool).archive(spool, True, actor="ann")
    [item] = _items(pg_pool)
    assert (item.type_id, item.empty_spool_id) == (kind, spool)
    assert item.note is not None
    assert "archived" in item.note


def test_archiving_a_spool_already_empty_adds_nothing_more(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool, type_id=_type(pg_pool))
    _weigh(pg_pool, spool, 0)
    FilamentSpoolStore(pg_pool).archive(spool, True, actor="ann")
    assert len(_items(pg_pool)) == 1


def test_an_archived_spool_that_reaches_zero_adds_nothing(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool, type_id=_type(pg_pool))
    FilamentSpoolStore(pg_pool).archive(spool, True, actor="ann")
    ShoppingStore(pg_pool).clear(purchased_only=False)
    _use(pg_pool, spool, 100)
    assert _items(pg_pool) == []


def test_a_second_empty_spool_of_the_type_adds_nothing_while_one_is_pending(
    pg_pool: PgPool,
) -> None:
    kind = _type(pg_pool)
    first, second = _spool(pg_pool, type_id=kind), _spool(pg_pool, type_id=kind)
    _weigh(pg_pool, first, 0)
    _weigh(pg_pool, second, 0)
    [item] = _items(pg_pool)
    assert item.empty_spool_id == first
    # Once it is bought, the next empty spool of the type adds one again.
    store = ShoppingStore(pg_pool)
    store.update(item.id, ShoppingItemPatch(status="purchased"))
    third = _spool(pg_pool, type_id=kind)
    _weigh(pg_pool, third, 0)
    assert [i.empty_spool_id for i in _items(pg_pool) if i.status == "pending"] == [third]


def test_a_pending_item_added_by_hand_counts(pg_pool: PgPool) -> None:
    kind = _type(pg_pool)
    ShoppingStore(pg_pool).add(ShoppingItemCreate(type_id=kind))
    _weigh(pg_pool, _spool(pg_pool, type_id=kind), 0)
    assert len(_items(pg_pool)) == 1


def test_the_setting_off_adds_nothing(pg_pool: PgPool) -> None:
    with pg_pool.connection() as conn:
        conn.execute(
            "INSERT INTO settings (name, value) VALUES (%s, %s)", (AUTO_ADD_SETTING, Jsonb(False))
        )
    spool = _spool(pg_pool, type_id=_type(pg_pool))
    _weigh(pg_pool, spool, 0)
    FilamentSpoolStore(pg_pool).archive(
        _spool(pg_pool, type_id=_type(pg_pool, "B", "Blue")), True, actor="ann"
    )
    assert _items(pg_pool) == []


def test_a_spool_with_no_type_adds_a_text_item(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool, material_id=_material(pg_pool), colour_name="Teal")
    _weigh(pg_pool, spool, 0)
    [item] = _items(pg_pool)
    assert (item.type_id, item.material, item.colour_name) == (None, "PLA", "Teal")
    assert item.empty_spool_id == spool
    # A second typeless spool of the same names adds nothing while that one is pending.
    _weigh(pg_pool, _spool(pg_pool, material_id=_material(pg_pool), colour_name="teal"), 0)
    assert len(_items(pg_pool)) == 1
    _weigh(pg_pool, _spool(pg_pool, material_id=_material(pg_pool), colour_name="Lime"), 0)
    assert len(_items(pg_pool)) == 2


def test_a_spool_made_empty_adds_nothing(pg_pool: PgPool) -> None:
    # Unknown to empty is no move into empty: an import or a first reading of an old reel.
    spool = _spool(pg_pool, grams=None, type_id=_type(pg_pool))
    _weigh(pg_pool, spool, 0)
    _spool(pg_pool, grams=0, type_id=_type(pg_pool, "B", "Blue"))
    assert _items(pg_pool) == []


def test_the_pulls_archived_import_adds_nothing(pg_pool: PgPool) -> None:
    # `bambuddy_pull._create_archived`: a spool Bambuddy had archived before SB knew it.
    spool = _spool(pg_pool, type_id=_type(pg_pool))
    with pg_pool.connection() as conn, conn.transaction():
        conn.execute("UPDATE filament_spools SET archived_at = now() WHERE id = %s", (spool,))
        ledger.record(conn, spool, "archived", data={"bb_spool_id": 7, "bb_archived_at": "x"})
    assert _items(pg_pool) == []


def test_a_rolled_back_write_adds_nothing(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool, type_id=_type(pg_pool))

    class RolledBackError(Exception):
        pass

    with pytest.raises(RolledBackError), pg_pool.connection() as conn, conn.transaction():
        ledger.record(conn, spool, "weighed", source="person", absolute_g=0)
        assert conn.execute("SELECT count(*) AS n FROM filament_shopping_items").fetchone() == {
            "n": 1
        }
        raise RolledBackError()
    assert _items(pg_pool) == []
