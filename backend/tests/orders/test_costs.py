"""An order's real cost (#4251, spec 2026-10-18 §6.4, plan 2026-11-03 Rulings 1 and 2):
the accepted quotes beside the attempts, priced at the quote's own rate, Bambuddy's cost
shown and never summed."""

from __future__ import annotations

from decimal import Decimal

import pytest
from psycopg.types.json import Jsonb

from scadbuddy.orders.costs import CostRates, Margin, _margin, order_costs
from scadbuddy.orders.integrations import NOT_LINKED, NotLinkedError
from scadbuddy.orders.lifecycle import QUOTE_HOLDER, FulfilmentInput, OrderEdit, ShipmentInput
from scadbuddy.orders.models import (
    ChargeInput,
    ColourSlot,
    CostEntryInput,
    FilamentInput,
    Fulfilment,
    Order,
    OrderItem,
    PricingPolicy,
    ShipmentItem,
    TimeEntryInput,
)
from scadbuddy.orders.pricing import Plate, PlateFilament, price
from scadbuddy.orders.store import OrderStore
from scadbuddy.store.refs import BlobRefs
from tests.conftest import PgPool
from tests.orders.test_cost_entries import usd_rate
from tests.orders.test_lifecycle import BY, Shop, _slice_quote

pytestmark = pytest.mark.requires_postgres

#: The shop's settings: USD, every rate 0.
USD = CostRates(shop_currency="USD", machine_rate_per_hour="0", labour_per_item="0")


class Costs:
    """A Bambuddy whose archives cost ``costs``; one missing from it fails to read."""

    linked = True

    def __init__(self, costs: dict[int, float | None]) -> None:
        self.costs = costs

    async def archive_cost(self, archive_id: int) -> float | None:
        if archive_id not in self.costs:
            raise RuntimeError("Bambuddy is down")
        return self.costs[archive_id]


@pytest.fixture(name="shop")
def _shop(rows: OrderStore, pool: PgPool) -> Shop:
    return Shop(rows, BlobRefs(pool))


def sliced_order(shop: Shop, quantity: int = 2) -> tuple[Order, OrderItem]:
    """An accepted order whose one item's accepted quote is a slice: per unit 600 s and
    10 g of a 20-per-kg filament, so 20 minor of filament, 2 minor a gram."""
    order = shop.order()
    item = shop.item(order, quantity=quantity)
    quote = _slice_quote(shop, order, item)
    shop.lifecycle.accept_quote(order.id, item.id, quote.id, BY)
    return shop.lifecycle.accept_order(order.id, BY), shop.get_item(item)


def attempt(
    shop: Shop, order: Order, item: OrderItem, outcome: str, made: int, grams: float, archive: int
) -> Fulfilment:
    recorded = shop.lifecycle.record_fulfilment(
        order.id,
        item.id,
        FulfilmentInput.model_validate({"quantity": made, "outcome": outcome}),
        BY,
    )
    return shop.lifecycle.record_archive_facts(
        recorded,
        [archive],
        started_at=None,
        finished_at=None,
        actual_seconds=500,
        actual_grams=grams,
    )


async def test_failed_attempts_count_in_the_actual_cost(shop: Shop) -> None:
    order, item = sliced_order(shop)
    attempt(shop, order, item, "failed", 0, 7.5, 81)
    attempt(shop, order, item, "succeeded", 2, 21.0, 82)

    costs = await order_costs(shop.rows, NOT_LINKED, order.id, USD)

    assert costs is not None
    (line,) = costs.items
    assert (line.quote_source, line.quoted_seconds, line.quoted_grams) == ("slice", 1200, 20.0)
    assert line.quoted_filament_minor == 40
    assert (line.actual_seconds, line.actual_grams) == (1000, 28.5)
    # 28.5 g at the quote's 2 minor a gram, the failure's 7.5 g among them.
    assert line.actual_filament_minor == 57
    assert (line.failed_attempts, line.failed_grams, line.failed_filament_minor) == (1, 7.5, 15)
    assert [a.filament_minor for a in line.attempts] == [15, 42]
    assert (costs.quoted_filament_minor, costs.actual_filament_minor) == (40, 57)
    assert costs.currency == "USD" and costs.minor_exponent == 2
    assert not costs.bambuddy_unavailable


async def test_bambuddy_cost_is_shown_never_summed(shop: Shop) -> None:
    order, item = sliced_order(shop)
    attempt(shop, order, item, "failed", 0, 7.5, 81)
    attempt(shop, order, item, "succeeded", 2, 21.0, 82)

    costs = await order_costs(shop.rows, Costs({81: 0.4, 82: 1.1}), order.id, USD)

    assert costs is not None
    (line,) = costs.items
    assert [a.bambuddy_cost for a in line.attempts] == [0.4, 1.1]
    assert line.actual_filament_minor == 57
    assert costs.actual_filament_minor == 57


async def test_an_unreadable_archive_is_reported_not_raised(shop: Shop) -> None:
    order, item = sliced_order(shop)
    attempt(shop, order, item, "succeeded", 2, 21.0, 82)

    costs = await order_costs(shop.rows, Costs({}), order.id, USD)

    assert costs is not None
    assert costs.bambuddy_unavailable
    assert costs.items[0].attempts[0].bambuddy_cost is None


async def test_without_bambuddy_nothing_is_asked(shop: Shop) -> None:
    order, item = sliced_order(shop)
    attempt(shop, order, item, "succeeded", 2, 21.0, 82)

    class Unlinked(Costs):
        linked = False

        async def archive_cost(self, archive_id: int) -> float | None:
            raise NotLinkedError

    costs = await order_costs(shop.rows, Unlinked({}), order.id, USD)

    assert costs is not None and not costs.bambuddy_unavailable


async def test_a_manual_quote_prices_no_grams(shop: Shop) -> None:
    order, item = shop.accepted(quantity=1)
    attempt(shop, order, item, "succeeded", 1, 12.0, 83)

    costs = await order_costs(shop.rows, NOT_LINKED, order.id, USD)

    assert costs is not None
    (line,) = costs.items
    assert line.quote_source == "manual"
    assert (line.quoted_grams, line.quoted_filament_minor) == (None, None)
    assert (line.actual_grams, line.actual_filament_minor) == (12.0, None)
    assert line.quoted_price_minor == 1000


async def test_an_unknown_order_is_none(shop: Shop) -> None:
    assert await order_costs(shop.rows, NOT_LINKED, "nope", USD) is None


# -- job cost (spec 2026-11-05 §4, §5, #5326) ---------------------------------------------


def _set_attempt(pool: PgPool, attempt_id: str, copies: int, sliced_sha256: str) -> None:
    """The columns the print path writes, set as it would."""
    with pool.connection() as conn:
        conn.execute(
            "UPDATE order_item_fulfilments SET copies = %s, sliced_sha256 = %s WHERE id = %s",
            (copies, sliced_sha256, attempt_id),
        )


def priced_order(
    shop: Shop, policy: PricingPolicy, quantity: int = 1, sha: str = "b" * 64
) -> tuple[Order, OrderItem]:
    """An accepted order whose item's accepted slice quote was priced under ``policy``:
    per unit 600 s and 10 g of a 20-per-kg filament in colour slot 0."""
    order = shop.order()
    item = shop.item(order, quantity=quantity)
    filament = shop.rows.insert_filament(
        FilamentInput(material="PLA", colour=f"#{len(shop.rows.filaments()):06d}", cost_per_kg="20")
    )
    plates = [
        Plate(
            index=1,
            print_seconds=600,
            filaments=[PlateFilament(slot=0, used_g=Decimal("10"))],
            blob_key="quote-b",
            sha256=sha,
        )
    ]
    priced = price(plates, {0: filament}, policy, currency="USD", quantity=quantity)
    quote_id = f"qp-{item.id}"
    assert shop.refs is not None
    shop.refs.add("quote-b", QUOTE_HOLDER, quote_id)
    quote = shop.lifecycle.record_slice_quote(
        order.id,
        item.id,
        priced,
        plates,
        sliced_sha256=sha,
        sliced_blob_key="quote-b",
        by=BY,
        revision_id=shop.get_item(item).current_revision_id or "",
        quote_id=quote_id,
    )
    shop.lifecycle.accept_quote(order.id, item.id, quote.id, BY)
    return shop.lifecycle.accept_order(order.id, BY), shop.get_item(item)


def by_hand(shop: Shop, order: Order, item: OrderItem, outcome: str, made: int) -> Fulfilment:
    return shop.lifecycle.record_fulfilment(
        order.id,
        item.id,
        FulfilmentInput.model_validate({"quantity": made, "outcome": outcome}),
        BY,
    )


async def test_an_attempt_with_no_grams_is_costed_from_its_quote(shop: Shop) -> None:
    order, item = sliced_order(shop, quantity=2)
    # A failed attempt by hand: no archive, no copies, nothing made. One unit's grams.
    by_hand(shop, order, item, "failed", 0)
    rates = USD.model_copy(update={"machine_rate_per_hour": "36"})

    costs = await order_costs(shop.rows, NOT_LINKED, order.id, rates)

    assert costs is not None
    (at,) = costs.items[0].attempts
    assert (at.grams, at.seconds, at.actual_grams) == (10.0, 600, None)
    # 10 g at the quote's 2 a gram; 600 s at today's 36 an hour (no machine line).
    assert (at.filament_minor, at.machine_minor, at.cost_minor) == (20, 600, 620)
    assert at.estimated == ["grams", "seconds", "filament", "machine"]
    assert not at.unknown
    assert costs.items[0].estimated == ["prints", "labour"]


async def test_an_attempt_is_costed_from_the_quote_it_printed_and_its_copies(
    shop: Shop, pool: PgPool
) -> None:
    order, item = priced_order(shop, PricingPolicy(machine_rate_per_hour="6"), quantity=3)
    attempt_ = by_hand(shop, order, item, "succeeded", 3)
    _set_attempt(pool, attempt_.id, copies=3, sliced_sha256="b" * 64)

    costs = await order_costs(shop.rows, NOT_LINKED, order.id, USD)

    assert costs is not None
    (at,) = costs.items[0].attempts
    # Three units of 10 g and 600 s: the quote's 2 a gram and its machine line of 100
    # for 600 s.
    assert (at.grams, at.seconds) == (30.0, 1800)
    assert (at.filament_minor, at.machine_minor) == (60, 300)
    assert at.estimated == ["grams", "seconds", "filament", "machine"]


async def test_measured_figures_at_the_quotes_rates_are_not_estimated(shop: Shop) -> None:
    order, item = priced_order(shop, PricingPolicy(machine_rate_per_hour="6"))
    attempt(shop, order, item, "succeeded", 1, 12.0, 90)

    costs = await order_costs(shop.rows, NOT_LINKED, order.id, USD)

    assert costs is not None
    (at,) = costs.items[0].attempts
    # 12 g at 2 a gram; 500 s at 100 per 600 s.
    assert (at.filament_minor, at.machine_minor, at.estimated) == (24, 83, [])


async def test_a_manual_quote_prices_grams_at_the_price_book(shop: Shop, pool: PgPool) -> None:
    order, item = shop.accepted(quantity=1)
    filament = shop.rows.insert_filament(
        FilamentInput(material="PETG", colour="#00ff00", cost_per_kg="30")
    )
    slots = [ColourSlot(slot=0, colour="#ff0000", filament_id=filament.id)]
    with pool.connection() as conn:
        conn.execute(
            "UPDATE order_items SET colours = %s WHERE id = %s",
            (Jsonb([s.model_dump() for s in slots]), item.id),
        )
    attempt(shop, order, item, "succeeded", 1, 12.0, 83)

    costs = await order_costs(shop.rows, NOT_LINKED, order.id, USD)

    assert costs is not None
    (at,) = costs.items[0].attempts
    # 12 g at 30 a kilogram: 36 cents; 500 s at today's rate, 0. Both estimated.
    assert (at.filament_minor, at.machine_minor) == (36, 0)
    assert at.estimated == ["filament", "machine"]
    assert costs.items[0].unknown == []


async def test_a_test_print_counts_toward_the_cost(shop: Shop, pool: PgPool) -> None:
    order, item = sliced_order(shop, quantity=1)
    made = attempt(shop, order, item, "succeeded", 0, 4.0, 91)
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT id FROM order_item_revisions WHERE item_id = %s LIMIT 1", (item.id,)
        ).fetchone()
        assert row is not None
        conn.execute(
            "UPDATE order_item_fulfilments SET purpose = 'test', revision_id = %s WHERE id = %s",
            (row["id"], made.id),
        )

    costs = await order_costs(shop.rows, NOT_LINKED, order.id, USD)

    assert costs is not None
    (at,) = costs.items[0].attempts
    # 4 g at the quote's 2 a gram, though the attempt made nothing for the order.
    assert (at.purpose, at.filament_minor) == ("test", 8)
    assert costs.items[0].prints_minor == 8


async def test_the_price_book_weighs_slots_equally_with_no_slot_grams(
    shop: Shop, pool: PgPool
) -> None:
    order, item = shop.accepted(quantity=1)  # a manual quote: no slot grams anywhere
    cheap = shop.rows.insert_filament(
        FilamentInput(material="PLA", colour="#111111", cost_per_kg="20")
    )
    dear = shop.rows.insert_filament(
        FilamentInput(material="PLA", colour="#222222", cost_per_kg="40")
    )
    slots = [
        ColourSlot(slot=0, colour="#ff0000", filament_id=cheap.id),
        ColourSlot(slot=1, colour="#00ff00", filament_id=dear.id),
    ]
    with pool.connection() as conn:
        conn.execute(
            "UPDATE order_items SET colours = %s WHERE id = %s",
            (Jsonb([s.model_dump() for s in slots]), item.id),
        )
    attempt(shop, order, item, "succeeded", 1, 10.0, 94)

    costs = await order_costs(shop.rows, NOT_LINKED, order.id, USD)

    assert costs is not None
    (at,) = costs.items[0].attempts
    # 10 g at the two filaments' mean, 30 a kilogram: 30 cents.
    assert at.filament_minor == 30
    assert "filament" in at.estimated


async def test_the_price_book_is_weighted_by_each_colour_slots_grams(
    shop: Shop, pool: PgPool
) -> None:
    order = shop.order()
    item = shop.item(order)
    cheap = shop.rows.insert_filament(
        FilamentInput(material="PLA", colour="#111111", cost_per_kg="20")
    )
    dear = shop.rows.insert_filament(
        FilamentInput(material="PLA", colour="#222222", cost_per_kg="40")
    )
    slots = [
        ColourSlot(slot=0, colour="#ff0000", filament_id=cheap.id),
        ColourSlot(slot=1, colour="#00ff00", filament_id=dear.id),
    ]
    # A slice quote (never accepted) whose colour slot 0 took 30 g and slot 1 10 g.
    plates = [
        Plate(
            index=1,
            print_seconds=600,
            filaments=[
                PlateFilament(slot=0, used_g=Decimal("30")),
                PlateFilament(slot=1, used_g=Decimal("10")),
            ],
        )
    ]
    priced = price(plates, {0: cheap, 1: dear}, PricingPolicy(), currency="USD", quantity=1)
    shop.lifecycle.record_slice_quote(
        order.id,
        item.id,
        priced,
        plates,
        sliced_sha256="c" * 64,
        sliced_blob_key="quote-c",
        by=BY,
        revision_id=shop.get_item(item).current_revision_id or "",
        quote_id=f"qw-{item.id}",
    )
    shop.quoted(order, item)  # the accepted quote is a manual one, pricing no grams
    order = shop.lifecycle.accept_order(order.id, BY)
    # The slots' filaments, as the quote's run pins them on the item.
    with pool.connection() as conn:
        conn.execute(
            "UPDATE order_items SET colours = %s WHERE id = %s",
            (Jsonb([s.model_dump() for s in slots]), item.id),
        )
    attempt(shop, order, item, "succeeded", 1, 12.0, 92)

    costs = await order_costs(shop.rows, NOT_LINKED, order.id, USD)

    assert costs is not None
    (at,) = costs.items[0].attempts
    # (20 x 30 + 40 x 10) / 40 = 25 a kilogram; 12 g is 30 cents.
    assert (at.filament_minor, at.estimated) == (30, ["filament", "machine"])


async def test_an_attempt_with_no_source_is_unknown(shop: Shop) -> None:
    order, item = shop.accepted(quantity=1)
    by_hand(shop, order, item, "succeeded", 1)

    costs = await order_costs(shop.rows, NOT_LINKED, order.id, USD)

    assert costs is not None
    line = costs.items[0]
    (at,) = line.attempts
    assert (at.grams, at.seconds, at.cost_minor, at.unknown) == (None, None, 0, True)
    assert line.unknown == ["prints"]
    assert line.margin_state == "unknown"


async def test_labour_is_the_entries_else_a_default(shop: Shop) -> None:
    order, item = priced_order(shop, PricingPolicy(labour_per_item="2.50"), quantity=2)
    rates = USD.model_copy(update={"labour_per_item": "9"})

    costs = await order_costs(shop.rows, NOT_LINKED, order.id, rates)
    assert costs is not None
    line = costs.items[0]
    # No entries: the accepted quote's labour line, 250 a unit, for two.
    assert (line.labour_minor, line.labour_default, line.estimated) == (500, True, ["labour"])

    shop.lifecycle.add_time_entry(
        order.id,
        TimeEntryInput(order_item_id=item.id, kind="setup", minutes=20),
        BY,
        labour_rate=usd_rate("30"),
    )
    shop.lifecycle.add_time_entry(
        order.id, TimeEntryInput(kind="packing", minutes=10), BY, labour_rate=usd_rate("30")
    )
    costs = await order_costs(shop.rows, NOT_LINKED, order.id, rates)
    assert costs is not None
    line = costs.items[0]
    # One real entry replaces the default: 20 minutes at 30.00 an hour.
    assert (line.labour_minor, line.labour_default, line.estimated) == (1000, False, [])
    assert [e.minutes for e in line.time_entries] == [20]
    # The order's own 10 minutes count on the order, not the item.
    assert [e.minutes for e in costs.time_entries] == [10]
    assert costs.labour_minor == 1500


async def test_a_manual_quote_defaults_labour_to_todays_setting(shop: Shop) -> None:
    order, _ = shop.accepted(quantity=3)
    rates = USD.model_copy(update={"labour_per_item": "1.25"})

    costs = await order_costs(shop.rows, NOT_LINKED, order.id, rates)

    assert costs is not None
    assert (costs.items[0].labour_minor, costs.items[0].labour_default) == (375, True)


async def test_charged_cost_and_margin(shop: Shop) -> None:
    order = shop.order()
    item = shop.item(order)
    shop.quoted(order, item)  # charged 1000
    shop.lifecycle.edit_order(
        order.id,
        BY,
        OrderEdit(
            charges=[
                ChargeInput(kind="shipping", label="Post", amount_minor=500),
                ChargeInput(kind="discount", label="Friend", amount_minor=-100),
                ChargeInput(kind="tax", label="VAT", amount_minor=200),
            ]
        ),
    )
    order = shop.lifecycle.accept_order(order.id, BY)
    shop.lifecycle.add_cost_entry(
        order.id, CostEntryInput(order_item_id=item.id, kind="material", amount_minor=300), BY
    )
    shop.lifecycle.add_cost_entry(order.id, CostEntryInput(kind="payment_fee", amount_minor=50), BY)
    shop.fulfilled(order, shop.get_item(item))
    shop.lifecycle.add_shipment(
        order.id,
        ShipmentInput(
            carrier="Post", cost_minor=450, items=[ShipmentItem(order_item_id=item.id, quantity=1)]
        ),
        BY,
    )
    rates = USD.model_copy(update={"labour_per_item": "1"})

    costs = await order_costs(shop.rows, NOT_LINKED, order.id, rates)

    assert costs is not None
    line = costs.items[0]
    # The attempt by hand has no source (0, unknown); labour 100 by default; 300 material.
    assert (line.cost_minor, line.revenue_minor, line.margin_minor) == (400, 1000, 600)
    assert (line.margin_percent, line.margin_state) == (60.0, "unknown")
    # 1000 + 500 - 100 charged (tax is not revenue); 400 + 50 + 450 shipping in the cost.
    assert (costs.charges_minor, costs.revenue_minor) == (400, 1400)
    assert (costs.shipping_minor, costs.other_minor, costs.cost_minor) == (450, 800, 900)
    assert (costs.margin_minor, costs.margin_percent) == (500, 35.7)


def test_thin_is_judged_on_the_unrounded_percent() -> None:
    """14.96% reads 15.0, and is still thin under 15 (#5338's review)."""
    view = Margin(revenue_minor=2500, cost_minor=2126)
    _margin(view, Decimal("15"))
    assert (view.margin_percent, view.margin_state) == (15.0, "thin")


async def test_thin_and_loss(shop: Shop) -> None:
    order, item = shop.accepted(quantity=1)  # charged 1000
    shop.lifecycle.add_cost_entry(
        order.id, CostEntryInput(order_item_id=item.id, kind="material", amount_minor=900), BY
    )
    costs = await order_costs(shop.rows, NOT_LINKED, order.id, USD)
    assert costs is not None
    assert (costs.items[0].margin_percent, costs.items[0].margin_state) == (10.0, "thin")
    assert costs.margin_state == "thin"

    shop.lifecycle.add_cost_entry(
        order.id, CostEntryInput(order_item_id=item.id, kind="other", amount_minor=200), BY
    )
    costs = await order_costs(shop.rows, NOT_LINKED, order.id, USD)
    assert costs is not None
    assert (costs.items[0].margin_minor, costs.items[0].margin_state) == (-100, "loss")


async def test_a_cancelled_item_is_charged_nothing_and_its_cost_counts(shop: Shop) -> None:
    order, item = sliced_order(shop, quantity=1)
    attempt(shop, order, item, "failed", 0, 7.5, 81)
    shop.lifecycle.cancel_order(order.id, BY, None)

    costs = await order_costs(shop.rows, NOT_LINKED, order.id, USD)

    assert costs is not None
    line = costs.items[0]
    # 7.5 g at 2 a gram; no default labour for what was never made.
    assert (line.revenue_minor, line.cost_minor, line.labour_minor) == (0, 15, 0)
    assert (line.margin_minor, line.margin_state) == (-15, "loss")
    assert (costs.revenue_minor, costs.margin_state) == (0, "loss")


async def test_a_cancelled_order_is_charged_nothing_its_charges_included(shop: Shop) -> None:
    """§5: a cancelled order follows the cancelled item's rule, so its shipping charge
    never shows a job that fell through as profit (#5338's review)."""
    order = shop.order()
    item = shop.item(order)
    shop.quoted(order, item)
    shop.lifecycle.edit_order(
        order.id,
        BY,
        OrderEdit(charges=[ChargeInput(kind="shipping", label="Post", amount_minor=500)]),
    )
    shop.lifecycle.accept_order(order.id, BY)
    shop.lifecycle.add_cost_entry(order.id, CostEntryInput(kind="payment_fee", amount_minor=30), BY)
    shop.lifecycle.cancel_order(order.id, BY, None)

    costs = await order_costs(shop.rows, NOT_LINKED, order.id, USD)

    assert costs is not None
    assert costs.items[0].revenue_minor == 0
    assert (costs.charges_minor, costs.revenue_minor, costs.cost_minor) == (500, 0, 30)
    assert (costs.margin_minor, costs.margin_state) == (-30, "loss")


async def test_an_order_whose_every_item_is_cancelled_is_charged_nothing(shop: Shop) -> None:
    order = shop.order()
    item = shop.item(order)
    shop.quoted(order, item)
    shop.lifecycle.edit_order(
        order.id,
        BY,
        OrderEdit(charges=[ChargeInput(kind="shipping", label="Post", amount_minor=500)]),
    )
    shop.lifecycle.accept_order(order.id, BY)
    shop.lifecycle.cancel_item(order.id, item.id, BY)
    current = shop.rows.order(order.id)
    assert current is not None and current.status != "cancelled"  # the order stays open

    costs = await order_costs(shop.rows, NOT_LINKED, order.id, USD)

    assert costs is not None
    assert (costs.revenue_minor, costs.margin_minor, costs.margin_state) == (0, 0, None)


async def test_a_custom_item_costs_only_its_entries(shop: Shop) -> None:
    order = shop.order()
    item = shop.lifecycle.add_custom_item(
        order.id, BY, title="Engraving", quantity=2, unit_price_minor=500
    )
    rates = USD.model_copy(update={"labour_per_item": "9"})

    costs = await order_costs(shop.rows, NOT_LINKED, order.id, rates)

    assert costs is not None
    line = costs.items[0]
    assert line.item_id == item.id
    assert (line.revenue_minor, line.cost_minor, line.labour_default) == (1000, 0, False)
    assert line.margin_state == "ok"


async def test_a_custom_items_print_counts_with_no_default_labour(shop: Shop) -> None:
    order = shop.order()
    item = shop.lifecycle.add_custom_item(
        order.id, BY, title="Stand", quantity=1, unit_price_minor=500, requires_printing=True
    )
    order = shop.lifecycle.accept_order(order.id, BY)
    attempt(shop, order, item, "succeeded", 1, 12.0, 93)
    rates = USD.model_copy(update={"labour_per_item": "9", "machine_rate_per_hour": "36"})

    costs = await order_costs(shop.rows, NOT_LINKED, order.id, rates)

    assert costs is not None
    line = costs.items[0]
    (at,) = line.attempts
    # 500 s at today's rate, estimated; no colour slot names a filament to price 12 g.
    assert (at.machine_minor, at.filament_minor, at.unknown) == (500, None, True)
    assert (line.labour_minor, line.labour_default, line.cost_minor) == (0, False, 500)
    assert line.unknown == ["prints"]


async def test_a_cancelled_item_with_no_cost_has_no_margin_state(shop: Shop) -> None:
    order = shop.order()
    item = shop.item(order)
    shop.quoted(order, item)
    shop.lifecycle.cancel_item(order.id, item.id, BY)

    costs = await order_costs(shop.rows, NOT_LINKED, order.id, USD)

    assert costs is not None
    line = costs.items[0]
    # Charged nothing and cost nothing: no percent, and no state to tint it.
    assert (line.revenue_minor, line.margin_minor) == (0, 0)
    assert (line.margin_percent, line.margin_state) == (None, None)


async def test_an_item_with_no_accepted_quote_has_no_margin(shop: Shop) -> None:
    order = shop.order()
    item = shop.item(order)
    shop.lifecycle.add_time_entry(
        order.id,
        TimeEntryInput(order_item_id=item.id, kind="design", minutes=90),
        BY,
        labour_rate=usd_rate("20"),
    )

    costs = await order_costs(shop.rows, NOT_LINKED, order.id, USD)

    assert costs is not None
    line = costs.items[0]
    assert (line.labour_minor, line.cost_minor) == (3000, 3000)
    assert (line.revenue_minor, line.margin_minor, line.margin_state) == (None, None, None)
    assert (costs.revenue_minor, costs.margin_state) == (None, None)


async def test_another_currency_leaves_todays_rates_unknown(shop: Shop) -> None:
    order = shop.lifecycle.create_order(shop.customer.id, BY, currency="EUR")
    item = shop.item(order)
    shop.quoted(order, item)
    order = shop.lifecycle.accept_order(order.id, BY)
    shop.lifecycle.add_cost_entry(
        order.id, CostEntryInput(order_item_id=item.id, kind="material", amount_minor=100), BY
    )
    rates = USD.model_copy(update={"labour_per_item": "1", "machine_rate_per_hour": "5"})

    costs = await order_costs(shop.rows, NOT_LINKED, order.id, rates)

    assert costs is not None
    line = costs.items[0]
    # The entry still counts; today's labour (in USD) does not.
    assert (line.cost_minor, line.labour_minor, line.unknown) == (100, 0, ["labour"])
    # A default with no source is unknown, never also estimated (#5338's review).
    assert line.estimated == []
    assert line.margin_state == "unknown"
