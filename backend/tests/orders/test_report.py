"""The profit report by model (job cost spec 2026-11-05 §6.4, #5329): rows per currency and
product, each the sum of its items' figures from the per-order read. Every order's
``accepted_at`` is pinned: the WSL clock jumps."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from scadbuddy.orders.costs import CostRates, order_costs
from scadbuddy.orders.integrations import NOT_LINKED
from scadbuddy.orders.lifecycle import OrderEdit, ShipmentInput
from scadbuddy.orders.models import (
    ChargeInput,
    CostEntryInput,
    Order,
    OrderFilter,
    OrderItem,
    PricingPolicy,
    Product,
    ShipmentItem,
)
from scadbuddy.orders.report import ProfitReport, ProfitRow, default_period, profit_report
from scadbuddy.orders.store import OrderStore
from scadbuddy.store.refs import BlobRefs
from tests.conftest import PgPool
from tests.orders.test_costs import attempt, priced_order
from tests.orders.test_lifecycle import BY, Shop, resolved

pytestmark = pytest.mark.requires_postgres

NOW = datetime(2026, 11, 6, 12, tzinfo=UTC)
#: USD, every rate 0, thin below 15 %.
USD = CostRates(shop_currency="USD", machine_rate_per_hour="0", labour_per_item="0")
SINCE, UNTIL = default_period(NOW)


@pytest.fixture(name="shop")
def _shop(rows: OrderStore, pool: PgPool) -> Shop:
    return Shop(rows, BlobRefs(pool))


def accepted_at(pool: PgPool, order: Order, when: datetime) -> None:
    with pool.connection() as conn:
        conn.execute("UPDATE orders SET accepted_at = %s WHERE id = %s", (when, order.id))


def model_order(
    shop: Shop,
    pool: PgPool,
    days_ago: float,
    *,
    slug: str = "demo",
    price: int = 1000,
    cost: int = 0,
    quantity: int = 1,
    currency: str = "USD",
) -> tuple[Order, OrderItem]:
    """An accepted order of one ``slug`` item charged ``price`` a unit, ``cost`` as a
    material cost entry, accepted ``days_ago`` before NOW."""
    order = shop.lifecycle.create_order(shop.customer.id, BY, currency=currency)
    item = shop.lifecycle.add_item(order.id, BY, resolved(slug), output_id=None, quantity=quantity)
    shop.quoted(order, item, price)
    order = shop.lifecycle.accept_order(order.id, BY)
    if cost:
        shop.lifecycle.add_cost_entry(
            order.id, CostEntryInput(order_item_id=item.id, kind="material", amount_minor=cost), BY
        )
    accepted_at(pool, order, NOW - timedelta(days=days_ago))
    return order, shop.get_item(item)


def custom_order(
    shop: Shop, pool: PgPool, days_ago: float, *, sku: str | None, price: int = 2500
) -> Order:
    order = shop.order()
    shop.lifecycle.add_custom_item(
        order.id, BY, title="Design work", quantity=2, unit_price_minor=price, sku=sku
    )
    order = shop.lifecycle.accept_order(order.id, BY)
    accepted_at(pool, order, NOW - timedelta(days=days_ago))
    return order


def report(shop: Shop, **kwargs: object) -> ProfitReport:
    return profit_report(shop.rows, USD, since=SINCE, until=UNTIL, **kwargs)  # type: ignore[arg-type]


def row(
    answer: ProfitReport, group: str, key: str | None = None, currency: str = "USD"
) -> ProfitRow:
    (found,) = [r for r in answer.rows if (r.group, r.key, r.currency) == (group, key, currency)]
    return found


def test_rows_are_per_slug_per_sku_and_one_for_custom_with_no_sku(shop: Shop, pool: PgPool) -> None:
    model_order(shop, pool, 1, price=1000, cost=300)
    model_order(shop, pool, 2, price=500, quantity=3)
    model_order(shop, pool, 3, slug="other", price=700)
    custom_order(shop, pool, 4, sku="DES-1")
    custom_order(shop, pool, 5, sku=None)
    custom_order(shop, pool, 6, sku=None, price=100)

    answer = report(shop)

    assert [(r.group, r.key, r.product) for r in answer.rows] == [
        ("custom", None, "custom"),  # 2 x 2500 + 2 x 100 = 5200
        ("sku", "DES-1", "sku:DES-1"),  # 2 x 2500 = 5000
        ("model", "demo", "model:demo"),  # 1000 + 3 x 500 = 2500
        ("model", "other", "model:other"),  # 700
        ("total", None, None),
    ]
    demo = row(answer, "model", "demo")
    assert (demo.orders, demo.units, demo.revenue_minor, demo.cost_minor) == (2, 4, 2500, 300)
    assert (demo.margin_minor, demo.margin_percent) == (2200, 88.0)
    custom = row(answer, "custom")
    assert (custom.orders, custom.units, custom.revenue_minor) == (2, 4, 5200)
    total = row(answer, "total")
    assert (total.orders, total.units, total.revenue_minor) == (6, 11, 13400)
    assert (answer.orders_read, answer.complete, answer.read_from) == (6, True, None)
    assert answer.rows[0].minor_exponent == 2


def test_currencies_are_never_summed(shop: Shop, pool: PgPool) -> None:
    model_order(shop, pool, 1, price=1000)
    model_order(shop, pool, 2, price=900, currency="EUR")
    model_order(shop, pool, 3, price=50, currency="JPY")

    answer = report(shop)

    assert [(r.currency, r.group) for r in answer.rows] == [
        ("EUR", "model"),
        ("EUR", "total"),
        ("JPY", "model"),
        ("JPY", "total"),
        ("USD", "model"),
        ("USD", "total"),
    ]
    assert row(answer, "total", currency="EUR").revenue_minor == 900
    assert row(answer, "total", currency="USD").revenue_minor == 1000
    jpy = row(answer, "model", "demo", currency="JPY")
    assert (jpy.revenue_minor, jpy.minor_exponent) == (50, 0)


def test_the_period_is_by_accepted_time_from_inclusive_to_exclusive(
    shop: Shop, pool: PgPool
) -> None:
    model_order(shop, pool, 90, price=100)  # at `since`: in
    model_order(shop, pool, 45, price=200)
    model_order(shop, pool, 90.01, price=400)  # before the period
    model_order(shop, pool, 0, price=800)  # at `until`: out
    # A draft is never accepted, and an order cancelled before accept has no accepted
    # time: neither is in any period.
    draft = shop.order()
    shop.quoted(draft, shop.item(draft), 1600)
    gone = shop.order()
    shop.quoted(gone, shop.item(gone), 3200)
    shop.lifecycle.cancel_order(gone.id, BY, None)

    answer = report(shop)

    assert default_period(NOW) == (NOW - timedelta(days=90), NOW)
    assert (answer.period_from, answer.period_to) == (SINCE, UNTIL)
    assert row(answer, "total").revenue_minor == 300
    assert answer.orders_read == 2
    later = profit_report(shop.rows, USD, since=NOW - timedelta(days=1), until=NOW + timedelta(1))
    assert row(later, "total").revenue_minor == 800


async def test_a_cancelled_order_is_charged_nothing_and_its_cost_counts(
    shop: Shop, pool: PgPool
) -> None:
    order, _ = model_order(shop, pool, 1, price=1000, cost=300)
    shop.lifecycle.cancel_order(order.id, BY, None)
    accepted_at(pool, order, NOW - timedelta(days=1))

    answer = report(shop)

    demo = row(answer, "model", "demo")
    # Cancelled: charged nothing, its 300 a loss; its item made no units.
    assert (demo.orders, demo.units, demo.revenue_minor, demo.cost_minor) == (1, 0, 0, 300)
    assert (demo.margin_minor, demo.margin_state) == (-300, "loss")
    costs = await order_costs(shop.rows, NOT_LINKED, order.id, USD)
    assert costs is not None
    assert (costs.revenue_minor, costs.cost_minor) == (demo.revenue_minor, demo.cost_minor)


def test_waste_is_the_share_of_cost_on_test_and_failed_attempts(shop: Shop, pool: PgPool) -> None:
    """The slice quote prices 10 g at 2 a gram. A failed attempt's 30 g (60) and a test
    print's 5 g (10) are waste; the made unit's 10 g (20) is not."""
    order, item = priced_order(shop, PricingPolicy())
    attempt(shop, order, item, "failed", 0, 30.0, 91)
    trial = attempt(shop, order, item, "succeeded", 0, 5.0, 92)
    with pool.connection() as conn:
        found = conn.execute(
            "SELECT id FROM order_item_revisions WHERE item_id = %s LIMIT 1", (item.id,)
        ).fetchone()
        assert found is not None
        conn.execute(
            "UPDATE order_item_fulfilments SET purpose = 'test', revision_id = %s WHERE id = %s",
            (found["id"], trial.id),
        )
    attempt(shop, order, item, "succeeded", 1, 10.0, 93)
    accepted_at(pool, order, NOW - timedelta(days=1))

    answer = report(shop)

    demo = row(answer, "model", "demo")
    assert (demo.cost_minor, demo.waste_minor, demo.waste_percent) == (90, 70, 77.8)
    total = row(answer, "total")
    assert (total.waste_minor, total.waste_percent) == (70, 77.8)


async def test_each_row_sums_its_items_per_order_reads(shop: Shop, pool: PgPool) -> None:
    """Rows are the per-order reads' item figures summed; the ``order`` row is what the
    orders' reads have beyond their items; the total is the reads' own sums."""
    first, _ = model_order(shop, pool, 1, price=1000, cost=300)
    second, _ = model_order(shop, pool, 2, price=600, quantity=2, cost=50)
    # Two items on one order, one of each row; the order has its own charges, entry and
    # shipment cost.
    mixed = shop.order()
    shop.quoted(mixed, shop.item(mixed), 400)
    shop.lifecycle.add_custom_item(
        mixed.id, BY, title="Box", quantity=1, unit_price_minor=150, sku="BOX"
    )
    shop.lifecycle.edit_order(
        mixed.id,
        BY,
        OrderEdit(
            charges=[
                ChargeInput(kind="shipping", label="Post", amount_minor=500),
                ChargeInput(kind="tax", label="VAT", amount_minor=200),
            ]
        ),
    )
    mixed = shop.lifecycle.accept_order(mixed.id, BY)
    shop.lifecycle.add_cost_entry(mixed.id, CostEntryInput(kind="payment_fee", amount_minor=40), BY)
    items = shop.rows.items(mixed.id)
    for line in items:
        if line.kind == "model":
            shop.fulfilled(mixed, line)
    shop.lifecycle.add_shipment(
        mixed.id,
        ShipmentInput(
            carrier="Post",
            cost_minor=450,
            items=[
                ShipmentItem(order_item_id=i.id, quantity=1) for i in items if i.kind == "model"
            ],
        ),
        BY,
    )
    accepted_at(pool, mixed, NOW - timedelta(days=3))

    answer = report(shop)

    reads = {}
    for order in (first, second, mixed):
        costs = await order_costs(shop.rows, NOT_LINKED, order.id, USD)
        assert costs is not None
        reads[order.id] = costs
    lines = [line for costs in reads.values() for line in costs.items]
    by_title = {
        "demo": [li for li in lines if li.title != "Box"],
        "BOX": [li for li in lines if li.title == "Box"],
    }
    for key, group in (("demo", "model"), ("BOX", "sku")):
        found = row(answer, group, key)
        assert found.revenue_minor == sum(li.revenue_minor or 0 for li in by_title[key])
        assert found.cost_minor == sum(li.cost_minor for li in by_title[key])
    own = row(answer, "order")
    # 500 shipping charged (tax left out); 40 fee and 450 shipping cost; only the mixed
    # order has figures of its own.
    assert (own.revenue_minor, own.cost_minor, own.orders) == (500, 490, 1)
    total = row(answer, "total")
    assert total.revenue_minor == sum(c.revenue_minor or 0 for c in reads.values())
    assert total.cost_minor == sum(c.cost_minor for c in reads.values())
    products = [r for r in answer.rows if r.group != "total"]
    assert sum(r.revenue_minor for r in products) == total.revenue_minor
    assert sum(r.cost_minor for r in products) == total.cost_minor
    assert total.margin_minor == total.revenue_minor - total.cost_minor


def test_the_order_row_counts_only_orders_with_figures_of_their_own(
    shop: Shop, pool: PgPool
) -> None:
    """#5546: the ``order`` row's gate. An order whose own revenue is ``None`` (an item not
    charged yet, while another is) has no figures of its own; one whose own revenue is
    negative (a discount, no cost of its own) has."""
    # An item charged 1000 beside one with no quote: the order's revenue is None. Its
    # ``accepted_at`` is set by hand, as accepting needs every item quoted.
    open_ = shop.order()
    shop.quoted(open_, shop.item(open_), 1000)
    shop.item(open_)
    accepted_at(pool, open_, NOW - timedelta(days=1))
    discounted = shop.order()
    shop.quoted(discounted, shop.item(discounted), 1000)
    shop.lifecycle.edit_order(
        discounted.id,
        BY,
        OrderEdit(charges=[ChargeInput(kind="discount", label="Loyal", amount_minor=-100)]),
    )
    discounted = shop.lifecycle.accept_order(discounted.id, BY)
    accepted_at(pool, discounted, NOW - timedelta(days=2))

    answer = report(shop)

    own = row(answer, "order")
    assert (own.orders, own.revenue_minor, own.cost_minor) == (1, -100, 0)
    demo = row(answer, "model", "demo")
    assert (demo.orders, demo.revenue_minor, demo.uncharged_items) == (2, 2000, 1)
    # The open order's revenue is None, so it is in no sum of the total's.
    assert row(answer, "total").revenue_minor == 900


def test_the_report_reads_in_batches_and_says_where_it_stopped(shop: Shop, pool: PgPool) -> None:
    for day in range(1, 6):
        model_order(shop, pool, day, price=100)

    cut = report(shop, max_orders=3, batch=2)
    whole = report(shop, max_orders=5, batch=2)

    # Newest accepted first: the third newest is the oldest read.
    assert (cut.orders_read, cut.complete, cut.max_orders) == (3, False, 3)
    assert cut.read_from == NOW - timedelta(days=3)
    assert row(cut, "total").revenue_minor == 300
    # Exactly at the cap with nothing left is whole.
    assert (whole.orders_read, whole.complete, whole.read_from) == (5, True, None)


def test_the_list_filters_to_a_rows_orders(shop: Shop, pool: PgPool) -> None:
    demo, _ = model_order(shop, pool, 1)
    model_order(shop, pool, 2, slug="other")
    model_order(shop, pool, 3, currency="EUR")
    model_order(shop, pool, 120)  # outside the period
    sku = custom_order(shop, pool, 4, sku="DES-1")
    plain = custom_order(shop, pool, 5, sku=None)
    empty = custom_order(shop, pool, 6, sku="")
    answer = report(shop)

    def listed(product: str) -> list[str]:
        filters = OrderFilter(
            accepted_since=answer.period_from,
            accepted_until=answer.period_to,
            currency="USD",
            product=Product.parse(product),
        )
        return [o.id for o in shop.rows.orders(filters, None, 50)[0]]

    assert listed("model:demo") == [demo.id]
    assert listed("sku:DES-1") == [sku.id]
    # An empty SKU is no SKU: in the custom row, and in its list.
    assert row(answer, "custom").orders == 2
    # Newest made first.
    assert listed("custom") == [empty.id, plain.id]
    for found in answer.rows:
        if found.product is not None and found.currency == "USD":
            assert len(listed(found.product)) == found.orders


@pytest.mark.parametrize("value", ["", "model:", "sku", "other:x", "model"])
def test_a_product_is_one_of_three_forms(value: str) -> None:
    with pytest.raises(ValueError):
        Product.parse(value)
    assert str(Product.parse("model:a:b")) == "model:a:b"
