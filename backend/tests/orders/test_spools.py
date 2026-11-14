"""An item print's spool per slot (#4251, spec 2026-10-18 §5.4 "Spools", plan
2026-11-03 Ruling 4)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from scadbuddy.bambuddy.filaments import FilamentPlan
from scadbuddy.core.problems import ApiError
from scadbuddy.orders.integrations import NOT_LINKED
from scadbuddy.orders.models import Filament, FilamentInput
from scadbuddy.orders.spools import ShelfSpool, SpoolChoiceError, fill_plan, item_plan
from scadbuddy.orders.store import OrderStore, SpoolLink
from tests.orders.test_lifecycle import BY, Shop

NOW = datetime(2026, 11, 3, tzinfo=UTC)
PRINTER = 7


def filament(colour: str = "#FF0000", brand: str | None = "Bambu") -> Filament:
    return Filament(
        id="f" + colour,
        material="PLA",
        subtype="Basic",
        brand=brand,
        colour_name=None,
        colour=colour,
        cost_per_kg="20",
        density_g_cm3=None,
        archived_at=None,
        created_at=NOW,
        updated_at=NOW,
    )


def spool(spool_id: int, colour: str | None = "#ff0000", **fields: object) -> ShelfSpool:
    return ShelfSpool.model_validate(
        {
            "id": spool_id,
            "material": "pla",
            "subtype": "basic",
            "brand": "bambu",
            "colour": colour,
            "remaining_g": 500.0,
            **fields,
        }
    )


def test_the_spool_loaded_on_the_printer_is_taken_first() -> None:
    spools = [
        spool(1, remaining_g=900),
        spool(2, loaded_printer_id=PRINTER, remaining_g=100),
    ]
    assert fill_plan({0: filament()}, spools, PRINTER, {}) == {1: 2}


def test_else_the_shelf_spool_with_most_left() -> None:
    spools = [
        spool(1, remaining_g=200),
        spool(2, remaining_g=700),
        spool(3, archived=True, remaining_g=999),
    ]
    assert fill_plan({0: filament()}, spools, PRINTER, {}) == {1: 2}


def test_a_spool_loaded_elsewhere_is_never_taken() -> None:
    spools = [spool(1, loaded_printer_id=99, remaining_g=900)]
    with pytest.raises(SpoolChoiceError) as raised:
        fill_plan({0: filament()}, spools, PRINTER, {})
    assert (raised.value.slug, raised.value.slots) == ("needs_spool_choice", [0])


def test_another_filament_or_a_name_only_spool_is_not_the_slots() -> None:
    spools = [spool(1, colour="#00FF00"), spool(2, colour=None), spool(3, brand="Elegoo")]
    with pytest.raises(SpoolChoiceError) as raised:
        fill_plan({0: filament(), 1: filament("#0000FF")}, spools, PRINTER, {})
    assert raised.value.slots == [0, 1]


def test_a_supplied_spool_must_be_the_slots_filament() -> None:
    spools = [spool(1), spool(2, colour="#00FF00")]
    assert fill_plan({0: filament()}, spools, PRINTER, {1: 1}) == {1: 1}
    with pytest.raises(SpoolChoiceError) as raised:
        fill_plan({0: filament()}, spools, PRINTER, {1: 2})
    assert (raised.value.slug, raised.value.slots) == ("spool_not_filament", [0])
    # One loaded elsewhere may be named by a person.
    assert fill_plan({0: filament()}, [spool(5, loaded_printer_id=99)], PRINTER, {1: 5}) == {1: 5}


def test_a_colour_set_by_hand_still_finds_its_spools() -> None:
    """#6211 review: spools are matched as the import matches them, by the record's link
    (its spool ids, else the spools' RGB), so a hand-set colour never hides them."""
    clear = filament("#EEF3F4")
    links = {clear.id: SpoolLink("#000000", frozenset({4}))}
    by_rgb = [spool(1, colour="#000000", remaining_g=300)]
    assert fill_plan({0: clear}, by_rgb, PRINTER, {}, links) == {1: 1}
    by_id = [spool(4, colour="#111111")]
    assert fill_plan({0: clear}, by_id, PRINTER, {}, links) == {1: 4}
    assert fill_plan({0: clear}, by_rgb, PRINTER, {1: 1}, links) == {1: 1}
    # The spool's colour, not the record's hand-set one, and never another brand's.
    with pytest.raises(SpoolChoiceError):
        fill_plan({0: clear}, [spool(2, colour="#EEF3F4")], PRINTER, {}, links)
    with pytest.raises(SpoolChoiceError):
        fill_plan({0: clear}, [spool(4, colour="#000000", brand="Elegoo")], PRINTER, {}, links)
    # With no link, the record's own colour, as before.
    assert fill_plan({0: clear}, [spool(2, colour="#EEF3F4")], PRINTER, {}) == {1: 2}


def test_a_slot_with_no_filament_is_left_as_given() -> None:
    assert fill_plan({}, [spool(1)], PRINTER, {2: 9}) == {2: 9}


class Inventory:
    linked = True

    def __init__(self, *spools: ShelfSpool) -> None:
        self.spools = list(spools)

    async def spool_inventory(self) -> list[ShelfSpool]:
        return self.spools


@pytest.fixture(name="shop")
def _shop(rows: OrderStore) -> Shop:
    return Shop(rows)


def priced_item(shop: Shop) -> str:
    """An item whose colour slot 0 is red Bambu PLA Basic."""
    record = shop.rows.insert_filament(
        FilamentInput(
            material="PLA", subtype="Basic", brand="Bambu", colour="#FF0000", cost_per_kg="20"
        )
    )
    order = shop.order()
    item = shop.item(order)
    shop.lifecycle.update_item(order.id, item.id, BY, filaments={0: record.id})
    return item.id


@pytest.mark.requires_postgres
async def test_the_item_print_fills_its_plan_from_its_filaments(shop: Shop) -> None:
    item_id = priced_item(shop)
    seam = Inventory(spool(4, loaded_printer_id=PRINTER), spool(5, remaining_g=900))

    plan = await item_plan(shop.rows, seam, item_id, FilamentPlan(force_colour_match=True), PRINTER)

    assert plan is not None
    assert [(s.slot_id, s.spool_id) for s in plan.slots] == [(1, 4)]
    assert plan.force_colour_match


@pytest.mark.requires_postgres
async def test_the_item_print_refuses_with_no_spool(shop: Shop) -> None:
    item_id = priced_item(shop)
    with pytest.raises(ApiError) as raised:
        await item_plan(shop.rows, Inventory(), item_id, FilamentPlan(), PRINTER)
    assert raised.value.status == 409
    assert raised.value.type.endswith("/needs_spool_choice")


@pytest.mark.requires_postgres
async def test_without_bambuddy_the_plan_is_the_requests(shop: Shop) -> None:
    item_id = priced_item(shop)
    assert await item_plan(shop.rows, NOT_LINKED, item_id, FilamentPlan(), PRINTER) is None
