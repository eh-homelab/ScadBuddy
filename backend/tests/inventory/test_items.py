"""Items (#7067, spec 6.4): the catalog entry of a thing, of one kind."""

from __future__ import annotations

from decimal import Decimal

import pytest

from scadbuddy.entities.properties import EntityPropertyStore, PropertyPatch
from scadbuddy.entities.refs import EntityRef
from scadbuddy.inventory.items import (
    FilamentItemRefused,
    ItemExists,
    ItemNotFound,
    ItemStore,
    KindArchived,
)
from scadbuddy.inventory.kinds import KindNotFound, KindStore
from scadbuddy.inventory.models import ItemInput, ItemPatch, KindInput
from scadbuddy.orders.models import Actor
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_postgres

BY = Actor(kind="browser")


def test_an_uncounted_item_has_no_stock_and_is_never_low(items: ItemStore) -> None:
    made = items.create(
        ItemInput(kind_id="service", name="Laser cutting", low_stock=Decimal(5)), BY
    )

    assert made.tracking == "none"
    assert made.on_hand == 0
    assert made.stock_value_minor == 0
    assert made.low is False


def test_an_item_knows_its_kind_path_and_saleable(items: ItemStore) -> None:
    made = items.create(ItemInput(kind_id="hardware", name="M3 nut"), BY)

    got = items.get(made.id)
    assert got is not None
    assert got.kind_path == ["part", "hardware"]
    assert got.tracking == "lot"
    assert got.uom == "each"
    assert got.saleable is None
    assert got.effective_saleable is False
    assert got.edited_fields == []

    sold = items.create(ItemInput(kind_id="hardware", name="M3 bolt", saleable=True), BY)
    assert sold.effective_saleable is True
    printed = items.create(ItemInput(kind_id="printed_part", name="Hook"), BY)
    assert printed.effective_saleable is True


def test_list_by_kind_takes_descendants_unless_told_not_to(items: ItemStore) -> None:
    made = items.create(ItemInput(kind_id="hardware", name="M3 nut"), BY)

    assert [i.id for i in items.list(kind="part")] == [made.id]
    assert items.list(kind="part", descendants=False) == []
    assert [i.id for i in items.list(kind="hardware", descendants=False)] == [made.id]
    assert items.list(kind="consumable") == []


def test_an_items_name_is_unique_in_its_kind_in_any_case(items: ItemStore) -> None:
    items.create(ItemInput(kind_id="hardware", name="M3 nut"), BY)

    with pytest.raises(ItemExists):
        items.create(ItemInput(kind_id="hardware", name="m3 NUT"), BY)
    items.create(ItemInput(kind_id="ams_part", name="M3 nut"), BY)


def test_a_filament_item_is_refused(items: ItemStore) -> None:
    with pytest.raises(FilamentItemRefused):
        items.create(ItemInput(kind_id="filament", name="PLA Basic Red"), BY)


def test_an_item_of_an_unknown_or_archived_kind_is_refused(
    items: ItemStore, kinds: KindStore
) -> None:
    with pytest.raises(KindNotFound):
        items.create(ItemInput(kind_id="nope", name="Thing"), BY)
    kinds.create(KindInput(id="widget", name="Widget", tracking="lot"), BY)
    kinds.archive("widget")
    with pytest.raises(KindArchived):
        items.create(ItemInput(kind_id="widget", name="Thing"), BY)


def test_an_imported_items_edits_are_remembered(items: ItemStore) -> None:
    made = items.create(
        ItemInput(kind_id="nozzle", name="0.4 hardened", source="bambu_store", source_ref="SKU-1"),
        BY,
    )

    updated = items.update(made.id, ItemPatch(name="0.4 mm hardened", note=""), BY)
    assert updated.name == "0.4 mm hardened"
    assert updated.edited_fields == ["name"]
    updated = items.update(made.id, ItemPatch(name="0.4 mm hardened", low_stock=Decimal(2)), BY)
    assert updated.edited_fields == ["name", "low_stock"]

    manual = items.create(ItemInput(kind_id="hardware", name="M3 nut"), BY)
    assert items.update(manual.id, ItemPatch(name="M3 nut DIN 934"), BY).edited_fields == []


def test_a_blank_source_ref_is_none(items: ItemStore) -> None:
    made = items.create(ItemInput(kind_id="hardware", name="M3 nut", source_ref="  "), BY)
    assert made.source_ref is None


def test_q_matches_name_manufacturer_and_property_values(
    items: ItemStore, props: EntityPropertyStore
) -> None:
    bearing = items.create(ItemInput(kind_id="hardware", name="608 bearing"), BY)
    items.create(ItemInput(kind_id="hardware", name="M3 nut", manufacturer="Würth"), BY)
    props.apply(EntityRef("item", str(bearing.id)), PropertyPatch(set={"mpn": "X-0451"}), BY)

    assert [i.id for i in items.list(q="0451")] == [bearing.id]
    assert [i.name for i in items.list(q="würth")] == ["M3 nut"]
    assert [i.name for i in items.list(q="608")] == ["608 bearing"]
    assert items.list(q="%") == []


def test_list_filters_low_compatible_and_archived(items: ItemStore, pg_pool: PgPool) -> None:
    low = items.create(ItemInput(kind_id="hardware", name="M3 nut", low_stock=Decimal(5)), BY)
    nozzle = items.create(
        ItemInput(kind_id="nozzle", name="0.4 nozzle", compatible_with=["H2C", "H2D"]), BY
    )
    gone = items.create(ItemInput(kind_id="hardware", name="M2 nut"), BY)
    items.archive(gone.id)

    assert [i.id for i in items.list(low=True)] == [low.id]
    assert [i.id for i in items.list(compatible="H2C")] == [nozzle.id]
    assert gone.id not in [i.id for i in items.list()]
    assert gone.id in [i.id for i in items.list(include_archived=True)]
    items.restore(gone.id)
    assert gone.id in [i.id for i in items.list()]


def test_stock_figures_read_lots_and_units(items: ItemStore, pg_pool: PgPool) -> None:
    nut = items.create(ItemInput(kind_id="hardware", name="M3 nut", low_stock=Decimal(10)), BY)
    nozzle = items.create(ItemInput(kind_id="nozzle", name="0.4 nozzle"), BY)
    actor = '{"kind": "browser"}'
    with pg_pool.connection() as conn:
        conn.execute(
            "INSERT INTO stock_lots (item_id, received_qty, on_hand, unit_cost_minor,"
            " cost_unknown, origin, created_by) VALUES"
            " (%s, 4, 4, 2.5, false, 'manual', %s), (%s, 6, 3, 1, true, 'manual', %s),"
            " (%s, 2, 0, 9, true, 'manual', %s)",
            (nut.id, actor, nut.id, actor, nut.id, actor),
        )
        conn.execute(
            "INSERT INTO stock_units (item_id, cost_minor, status, origin, created_by) VALUES"
            " (%s, 1500, 'in_stock', 'manual', %s), (%s, 900, 'retired', 'manual', %s)",
            (nozzle.id, actor, nozzle.id, actor),
        )

    got = items.get(nut.id)
    assert got is not None
    assert got.on_hand == Decimal(7)
    assert got.stock_value_minor == Decimal(13)
    assert got.cost_unknown is True
    assert got.low is True
    unit = items.get(nozzle.id)
    assert unit is not None
    assert (unit.on_hand, unit.stock_value_minor, unit.cost_unknown) == (
        Decimal(1),
        Decimal(1500),
        False,
    )


def test_update_and_archive_name_a_missing_item(items: ItemStore) -> None:
    from uuid import uuid4

    assert items.get(uuid4()) is None
    with pytest.raises(ItemNotFound):
        items.update(uuid4(), ItemPatch(name="x"), BY)
    with pytest.raises(ItemNotFound):
        items.archive(uuid4())
