"""The quote warning's history (job cost spec 2026-11-05 §6.3, #5328): a model's real cost
per unit from its last 10 fulfilled jobs within 365 days, the main reason, and the
suggested price. Every job's time is pinned: the WSL clock jumps."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from psycopg.types.json import Jsonb

from scadbuddy.orders.costs import CostRates, order_costs
from scadbuddy.orders.history import suggested_price, unit_cost_history, warns
from scadbuddy.orders.integrations import NOT_LINKED
from scadbuddy.orders.lifecycle import FulfilmentInput
from scadbuddy.orders.models import (
    ColourSlot,
    CostEntryInput,
    FilamentInput,
    Order,
    OrderItem,
    PricingPolicy,
)
from scadbuddy.orders.store import OrderStore
from scadbuddy.store.refs import BlobRefs
from tests.conftest import PgPool
from tests.orders.test_costs import attempt, priced_order
from tests.orders.test_lifecycle import BY, Shop, resolved

pytestmark = pytest.mark.requires_postgres

NOW = datetime(2026, 11, 6, 12, tzinfo=UTC)
#: USD, every rate 0, thin below 15 %.
USD = CostRates(shop_currency="USD", machine_rate_per_hour="0", labour_per_item="0")


@pytest.fixture(name="shop")
def _shop(rows: OrderStore, pool: PgPool) -> Shop:
    return Shop(rows, BlobRefs(pool))


def done_at(pool: PgPool, item: OrderItem, when: datetime) -> None:
    """The item's accepted attempts accepted at ``when``."""
    with pool.connection() as conn:
        conn.execute(
            "UPDATE order_item_fulfilments SET accepted_at = %s"
            " WHERE order_item_id = %s AND accepted_at IS NOT NULL",
            (when, item.id),
        )


def job(
    shop: Shop,
    pool: PgPool,
    days_ago: float,
    *,
    cost: int = 0,
    quantity: int = 1,
    slug: str = "demo",
    price: int = 1000,
) -> tuple[Order, OrderItem]:
    """A fulfilled item of ``slug`` priced by hand, its one attempt made by hand (no grams,
    so it costs nothing), ``cost`` as a material cost entry, done ``days_ago`` before NOW."""
    order = shop.order()
    item = shop.lifecycle.add_item(order.id, BY, resolved(slug), output_id=None, quantity=quantity)
    shop.quoted(order, item, price)
    order = shop.lifecycle.accept_order(order.id, BY)
    if cost:
        shop.lifecycle.add_cost_entry(
            order.id, CostEntryInput(order_item_id=item.id, kind="material", amount_minor=cost), BY
        )
    shop.fulfilled(order, shop.get_item(item))
    done_at(pool, item, NOW - timedelta(days=days_ago))
    return order, shop.get_item(item)


def test_the_window_is_the_last_ten_fulfilled_within_a_year(shop: Shop, pool: PgPool) -> None:
    # Twelve fulfilled jobs, one a day: only the newest ten count.
    made = [job(shop, pool, days_ago=day)[1] for day in range(1, 13)]
    # Older than 365 days, another model, and a cancelled order: none counts.
    job(shop, pool, days_ago=366)
    job(shop, pool, days_ago=0.5, slug="other")
    cancelled, _ = job(shop, pool, days_ago=0.25)
    shop.lifecycle.cancel_order(cancelled.id, BY, None)
    # Made but not yet fulfilled: its attempt is not accepted.
    order = shop.order()
    open_item = shop.item(order)
    shop.quoted(order, open_item)
    order = shop.lifecycle.accept_order(order.id, BY)
    shop.lifecycle.record_fulfilment(
        order.id, open_item.id, FulfilmentInput(quantity=1, outcome="succeeded"), BY
    )

    found = shop.rows.fulfilled_items("demo", "USD", NOW - timedelta(days=365), 10)

    assert [item_id for _, item_id in found] == [item.id for item in made[:10]]
    assert shop.rows.fulfilled_items("demo", "EUR", NOW - timedelta(days=365), 10) == []


def test_a_job_just_inside_the_year_counts(shop: Shop, pool: PgPool) -> None:
    _, inside = job(shop, pool, days_ago=364.9)
    job(shop, pool, days_ago=365.1)

    found = shop.rows.fulfilled_items("demo", "USD", NOW - timedelta(days=365), 10)

    assert [item_id for _, item_id in found] == [inside.id]


async def test_the_unit_cost_is_the_jobs_cost_over_their_units(shop: Shop, pool: PgPool) -> None:
    first, _ = job(shop, pool, days_ago=2, cost=900, quantity=1)
    second, _ = job(shop, pool, days_ago=1, cost=300, quantity=3)

    answer = unit_cost_history(shop.rows, "demo", USD, markup_percent="0", now=NOW)

    # (900 + 300) over 4 units.
    assert (answer.jobs, answer.units, answer.unit_cost_minor) == (2, 4, 300)
    # Each job's cost is the order page's read of it.
    for order, own in ((first, 900), (second, 300)):
        costs = await order_costs(shop.rows, NOT_LINKED, order.id, USD)
        assert costs is not None
        assert costs.items[0].cost_minor == own
    # The jobs' attempts by hand have no grams: their prints are unknown.
    assert answer.estimated
    # A cost entry is never priced by a quote: it counts in full.
    assert (answer.main_reason, answer.main_reason_minor) == ("other", 300)


def test_a_price_below_the_thin_threshold_warns(shop: Shop, pool: PgPool) -> None:
    job(shop, pool, days_ago=1, cost=300)

    thin = unit_cost_history(
        shop.rows, "demo", USD, markup_percent="0", now=NOW, unit_price_minor=340
    )
    ok = unit_cost_history(
        shop.rows, "demo", USD, markup_percent="0", now=NOW, unit_price_minor=400
    )

    below = unit_cost_history(
        shop.rows, "demo", USD, markup_percent="0", now=NOW, unit_price_minor=250
    )

    # 40 over 340 is 11.8 %, below 15, though 340 is above the cost of 300: thin, not a
    # loss. 100 over 400 is 25 %. 250 is below the cost itself.
    assert (thin.margin_percent, thin.warning, thin.below_cost) == (11.8, True, False)
    assert (ok.margin_percent, ok.warning, ok.below_cost) == (25.0, False, False)
    assert (below.margin_percent, below.warning, below.below_cost) == (-20.0, True, True)


def test_the_suggested_price_marks_up_or_reaches_the_thin_threshold(
    shop: Shop, pool: PgPool
) -> None:
    job(shop, pool, days_ago=1, cost=300)

    marked = unit_cost_history(shop.rows, "demo", USD, markup_percent="20", now=NOW)
    plain = unit_cost_history(shop.rows, "demo", USD, markup_percent="0", now=NOW)

    # 300 marked up 20 %; with no markup, 300 / 0.85 = 352.94, up to 353.
    assert marked.suggested_unit_price_minor == 360
    assert plain.suggested_unit_price_minor == 353
    assert (marked.markup_percent, plain.thin_margin_percent) == ("20", "15")
    # The no-markup suggestion is not thin itself; one minor unit less is.
    assert warns(300, 353, "15") == (15.0, False)
    assert warns(300, 352, "15")[1]


def test_suggested_price_edges() -> None:
    assert suggested_price(1000, "12.5", "15") == 1125
    assert suggested_price(1000, "0", "0") == 1000
    assert suggested_price(1000, "0", "20") == 1250
    assert warns(100, 0, "15") == (None, True)


def test_no_history_answers_nothing_and_never_warns(shop: Shop, pool: PgPool) -> None:
    job(shop, pool, days_ago=1, cost=300)

    answer = unit_cost_history(
        shop.rows, "nothing-here", USD, markup_percent="20", now=NOW, unit_price_minor=1
    )

    assert (answer.jobs, answer.unit_cost_minor, answer.main_reason) == (0, None, None)
    assert (answer.suggested_unit_price_minor, answer.warning) == (None, False)
    assert (answer.unit_price_minor, answer.margin_percent) == (1, None)


def test_failed_prints_count_in_full_and_can_be_the_main_reason(shop: Shop, pool: PgPool) -> None:
    """A slice quote prices 10 g a unit at 2 a gram (20). The made unit took its 10 g, so
    filament is as quoted; a failed attempt's 30 g (60) were never priced."""
    order, item = priced_order(shop, PricingPolicy())
    attempt(shop, order, item, "failed", 0, 30.0, 71)
    made = attempt(shop, order, item, "succeeded", 1, 10.0, 72)
    shop.lifecycle.accept_fulfilment(order.id, item.id, made.id, BY)
    shop.lifecycle.add_cost_entry(
        order.id, CostEntryInput(order_item_id=item.id, kind="material", amount_minor=40), BY
    )
    done_at(pool, item, NOW - timedelta(days=3))

    answer = unit_cost_history(shop.rows, "demo", USD, markup_percent="0", now=NOW)

    # 60 failed + 20 made + 40 other; the failed 60 beats the other 40.
    assert answer.unit_cost_minor == 120
    assert (answer.main_reason, answer.main_reason_minor) == ("failed_prints", 60)


def test_filament_over_the_quote_is_the_main_reason(shop: Shop, pool: PgPool) -> None:
    order, item = priced_order(shop, PricingPolicy())
    made = attempt(shop, order, item, "succeeded", 1, 25.0, 73)
    shop.lifecycle.accept_fulfilment(order.id, item.id, made.id, BY)
    done_at(pool, item, NOW - timedelta(days=3))

    answer = unit_cost_history(shop.rows, "demo", USD, markup_percent="0", now=NOW)

    # 25 g at 2 a gram is 50, against the quote's 20 for 10 g.
    assert answer.unit_cost_minor == 50
    assert (answer.main_reason, answer.main_reason_minor) == ("filament", 30)


def test_test_prints_count_in_full(shop: Shop, pool: PgPool) -> None:
    order, item = priced_order(shop, PricingPolicy())
    trial = attempt(shop, order, item, "succeeded", 0, 5.0, 74)
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT id FROM order_item_revisions WHERE item_id = %s LIMIT 1", (item.id,)
        ).fetchone()
        assert row is not None
        conn.execute(
            "UPDATE order_item_fulfilments SET purpose = 'test', revision_id = %s WHERE id = %s",
            (row["id"], trial.id),
        )
    made = attempt(shop, order, item, "succeeded", 1, 10.0, 75)
    shop.lifecycle.accept_fulfilment(order.id, item.id, made.id, BY)
    done_at(pool, item, NOW - timedelta(days=3))

    answer = unit_cost_history(shop.rows, "demo", USD, markup_percent="0", now=NOW)

    # The test print's 5 g (10) were never quoted; the made unit is as quoted.
    assert answer.unit_cost_minor == 30
    assert (answer.main_reason, answer.main_reason_minor) == ("test_prints", 10)


def test_no_part_over_its_quote_names_no_reason(shop: Shop, pool: PgPool) -> None:
    order, item = priced_order(shop, PricingPolicy())
    made = attempt(shop, order, item, "succeeded", 1, 8.0, 76)
    shop.lifecycle.accept_fulfilment(order.id, item.id, made.id, BY)
    done_at(pool, item, NOW - timedelta(days=3))

    answer = unit_cost_history(shop.rows, "demo", USD, markup_percent="0", now=NOW)

    assert answer.unit_cost_minor == 16
    assert (answer.main_reason, answer.main_reason_minor) == (None, None)


def manual_job(shop: Shop, pool: PgPool, *, other: int) -> None:
    """A job priced by hand at 10.00, its 12 g at the price book's 30 a kilogram (36), and
    ``other`` as a cost entry, done three days before NOW. Its quote has one ``manual``
    line and no filament line."""
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
    made = attempt(shop, order, item, "succeeded", 1, 12.0, 77)
    shop.lifecycle.accept_fulfilment(order.id, item.id, made.id, BY)
    shop.lifecycle.add_cost_entry(
        order.id, CostEntryInput(order_item_id=item.id, kind="material", amount_minor=other), BY
    )
    done_at(pool, item, NOW - timedelta(days=3))


def test_a_manual_quote_is_never_over_on_filament(shop: Shop, pool: PgPool) -> None:
    """The quote's 1000 covered the 36 of filament: one ``manual`` line prices every part
    together, so filament is not compared. Only the cost entry can be the reason."""
    manual_job(shop, pool, other=5)

    answer = unit_cost_history(shop.rows, "demo", USD, markup_percent="0", now=NOW)

    assert answer.unit_cost_minor == 41
    assert (answer.main_reason, answer.main_reason_minor) == ("other", 5)


def test_a_mixed_history_names_filament_from_the_slice_jobs_alone(shop: Shop, pool: PgPool) -> None:
    """The slice job's 25 g (50) is 30 over its quote's 20. The manual job's 36 of filament
    is left out, so filament is 30 over across the 2 units, 15 a unit (not 33)."""
    order, item = priced_order(shop, PricingPolicy())
    made = attempt(shop, order, item, "succeeded", 1, 25.0, 78)
    shop.lifecycle.accept_fulfilment(order.id, item.id, made.id, BY)
    done_at(pool, item, NOW - timedelta(days=2))
    manual_job(shop, pool, other=20)

    answer = unit_cost_history(shop.rows, "demo", USD, markup_percent="0", now=NOW)

    # (50 + 36 + 20) over 2 units.
    assert (answer.jobs, answer.unit_cost_minor) == (2, 53)
    assert (answer.main_reason, answer.main_reason_minor) == ("filament", 15)


def test_the_items_parts_replace_the_jobs_own_once(shop: Shop, pool: PgPool) -> None:
    """§11 (#5543): a job's parts entries (``part:``) leave its cost and this item's parts
    at today's unit cost take their place, so they are counted once."""
    order, item = job(shop, pool, 3, cost=100, quantity=2)
    shop.lifecycle.add_cost_entry(
        order.id,
        CostEntryInput(
            order_item_id=item.id,
            kind="material",
            amount_minor=40,
            source=f"part:{item.id}:hook",
        ),
        BY,
    )

    plain = unit_cost_history(shop.rows, "demo", USD, markup_percent="0", now=NOW)
    parts = unit_cost_history(
        shop.rows, "demo", USD, markup_percent="0", now=NOW, parts_unit_minor=Decimal("30.5")
    )

    assert plain.unit_cost_minor == 70
    assert plain.parts_unit_minor is None
    # (100 + 2 x 30.5) over 2 units: 80.5, rounded once.
    assert (parts.unit_cost_minor, parts.parts_unit_minor) == (81, 31)
    assert (parts.main_reason, parts.main_reason_minor) == ("other", 81)
