"""An order's real cost (#4251, spec 2026-10-18 §6.4, plan 2026-11-03 Rulings 1 and 2):
the accepted quotes beside the attempts, priced at the quote's own rate, Bambuddy's cost
shown and never summed."""

from __future__ import annotations

import pytest

from scadbuddy.orders.costs import order_costs
from scadbuddy.orders.integrations import NOT_LINKED, NotLinkedError
from scadbuddy.orders.lifecycle import FulfilmentInput
from scadbuddy.orders.models import Fulfilment, Order, OrderItem
from scadbuddy.orders.store import OrderStore
from scadbuddy.store.refs import BlobRefs
from tests.conftest import PgPool
from tests.orders.test_lifecycle import BY, Shop, _slice_quote

pytestmark = pytest.mark.requires_postgres


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
    written = shop.rows.set_archive_facts(
        recorded.id,
        [archive],
        started_at=None,
        finished_at=None,
        actual_seconds=500,
        actual_grams=grams,
    )
    assert written is not None
    return written


async def test_failed_attempts_count_in_the_actual_cost(shop: Shop) -> None:
    order, item = sliced_order(shop)
    attempt(shop, order, item, "failed", 0, 7.5, 81)
    attempt(shop, order, item, "succeeded", 2, 21.0, 82)

    costs = await order_costs(shop.rows, NOT_LINKED, order.id)

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

    costs = await order_costs(shop.rows, Costs({81: 0.4, 82: 1.1}), order.id)

    assert costs is not None
    (line,) = costs.items
    assert [a.bambuddy_cost for a in line.attempts] == [0.4, 1.1]
    assert line.actual_filament_minor == 57
    assert costs.actual_filament_minor == 57


async def test_an_unreadable_archive_is_reported_not_raised(shop: Shop) -> None:
    order, item = sliced_order(shop)
    attempt(shop, order, item, "succeeded", 2, 21.0, 82)

    costs = await order_costs(shop.rows, Costs({}), order.id)

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

    costs = await order_costs(shop.rows, Unlinked({}), order.id)

    assert costs is not None and not costs.bambuddy_unavailable


async def test_a_manual_quote_prices_no_grams(shop: Shop) -> None:
    order, item = shop.accepted(quantity=1)
    attempt(shop, order, item, "succeeded", 1, 12.0, 83)

    costs = await order_costs(shop.rows, NOT_LINKED, order.id)

    assert costs is not None
    (line,) = costs.items
    assert line.quote_source == "manual"
    assert (line.quoted_grams, line.quoted_filament_minor) == (None, None)
    assert (line.actual_grams, line.actual_filament_minor) == (12.0, None)
    assert line.quoted_price_minor == 1000


async def test_an_unknown_order_is_none(shop: Shop) -> None:
    assert await order_costs(shop.rows, NOT_LINKED, "nope") is None
