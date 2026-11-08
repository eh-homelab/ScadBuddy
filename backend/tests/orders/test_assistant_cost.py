"""The assistant's spend on an order (job cost spec 2026-11-05 §4.4, phase 5, #5330): the
agent service reports a session's spend in USD, and the lifecycle writes one `assistant`
cost entry per order and session, in the order's currency, at `pricing_usd_rate`."""

from __future__ import annotations

from decimal import Decimal

import pytest

from scadbuddy.orders.lifecycle import LifecycleError, UsdRate
from scadbuddy.orders.models import Actor
from scadbuddy.orders.store import OrderStore
from scadbuddy.store.refs import BlobRefs
from tests.conftest import PgPool
from tests.orders.test_lifecycle import BY, Shop

pytestmark = pytest.mark.requires_postgres

SESSION = "0b0e3c55-6f1e-4a39-9d0e-8a1c2b3d4e5f"
OTHER_SESSION = "1c1f4d66-7a2f-4b4a-8e1f-9b2d3c4e5f60"
AGENT = Actor(kind="agent", principal="person:owner", session=SESSION)


@pytest.fixture(name="shop")
def _shop(rows: OrderStore, pool: PgPool) -> Shop:
    return Shop(rows, BlobRefs(pool))


def test_the_spend_is_one_entry_in_the_orders_currency(shop: Shop) -> None:
    order, item = shop.accepted()
    entry, written = shop.lifecycle.add_assistant_cost(
        order.id,
        session_id=SESSION,
        order_item_id=item.id,
        cost_usd=Decimal("1.2345"),
        usd_rate=UsdRate(Decimal("1"), "USD"),
        by=AGENT,
    )
    assert written and entry is not None
    assert (entry.kind, entry.amount_minor, entry.order_item_id) == ("assistant", 123, item.id)
    assert entry.source == f"ai_session:{SESSION}"
    assert entry.note == f"assistant session {SESSION}: 1.2345 USD"
    assert entry.created_by == AGENT
    assert shop.kinds(order)[-1] == "cost.added"


def test_a_second_report_of_the_session_writes_nothing(shop: Shop) -> None:
    order, _ = shop.accepted()
    rate = UsdRate(Decimal("1"), "USD")
    first, _ = shop.lifecycle.add_assistant_cost(
        order.id,
        session_id=SESSION,
        order_item_id=None,
        cost_usd=Decimal("2"),
        usd_rate=rate,
        by=AGENT,
    )
    again, written = shop.lifecycle.add_assistant_cost(
        order.id,
        session_id=SESSION,
        order_item_id=None,
        cost_usd=Decimal("5"),
        usd_rate=rate,
        by=AGENT,
    )
    assert not written and again == first
    assert [e.amount_minor for e in shop.rows.cost_entries(order.id)] == [200]


def test_the_shop_currency_converts_at_the_usd_rate(shop: Shop) -> None:
    order, _ = shop.accepted()
    # The orders here are in USD; a shop in USD with a rate of 2 still charges USD at 1.
    entry, _ = shop.lifecycle.add_assistant_cost(
        order.id,
        session_id=SESSION,
        order_item_id=None,
        cost_usd=Decimal("1"),
        usd_rate=UsdRate(Decimal("2"), "USD"),
        by=AGENT,
    )
    assert entry is not None and entry.amount_minor == 100
    assert UsdRate(Decimal("0.9"), "EUR").factor("EUR") == Decimal("0.9")
    assert UsdRate(Decimal("0.9"), "EUR").factor("USD") == Decimal("1")
    assert UsdRate(Decimal("0.9"), "EUR").factor("GBP") is None


def test_an_order_in_a_currency_no_rate_reaches_is_refused(shop: Shop) -> None:
    order, _ = shop.accepted()
    with shop.rows.transaction() as conn:
        conn.execute("UPDATE orders SET currency = 'GBP' WHERE id = %s", (order.id,))
    with pytest.raises(LifecycleError) as refused:
        shop.lifecycle.add_assistant_cost(
            order.id,
            session_id=SESSION,
            order_item_id=None,
            cost_usd=Decimal("1"),
            usd_rate=UsdRate(Decimal("0.9"), "EUR"),
            by=AGENT,
        )
    assert refused.value.slug == "currency_unconvertible"
    assert shop.rows.cost_entries(order.id) == []


def test_a_spend_under_one_minor_unit_writes_nothing(shop: Shop) -> None:
    order, _ = shop.accepted()
    entry, written = shop.lifecycle.add_assistant_cost(
        order.id,
        session_id=SESSION,
        order_item_id=None,
        cost_usd=Decimal("0.004"),
        usd_rate=UsdRate(Decimal("1"), "USD"),
        by=AGENT,
    )
    assert (entry, written) == (None, False)
    assert shop.rows.cost_entries(order.id) == []


def test_a_repeat_report_under_one_minor_unit_returns_the_first_entry(shop: Shop) -> None:
    order, _ = shop.accepted()
    rate = UsdRate(Decimal("1"), "USD")
    first, _ = shop.lifecycle.add_assistant_cost(
        order.id,
        session_id=SESSION,
        order_item_id=None,
        cost_usd=Decimal("1"),
        usd_rate=rate,
        by=AGENT,
    )
    again, written = shop.lifecycle.add_assistant_cost(
        order.id,
        session_id=SESSION,
        order_item_id=None,
        cost_usd=Decimal("0.001"),
        usd_rate=rate,
        by=AGENT,
    )
    assert (again, written) == (first, False)


def test_an_item_gone_or_of_another_order_puts_the_entry_on_the_order(shop: Shop) -> None:
    """The item the session revised was deleted before the report landed (a draft item),
    or is not this order's: the spend still counts, on the order."""
    order = shop.order()
    item = shop.item(order)
    shop.lifecycle.delete_item(order.id, item.id, BY)
    _, other = shop.accepted()
    rate = UsdRate(Decimal("1"), "USD")
    gone, _ = shop.lifecycle.add_assistant_cost(
        order.id,
        session_id=SESSION,
        order_item_id=item.id,
        cost_usd=Decimal("1"),
        usd_rate=rate,
        by=AGENT,
    )
    assert gone is not None and (gone.order_item_id, gone.amount_minor) == (None, 100)
    elsewhere, _ = shop.lifecycle.add_assistant_cost(
        order.id,
        session_id=OTHER_SESSION,
        order_item_id=other.id,
        cost_usd=Decimal("1"),
        usd_rate=rate,
        by=AGENT,
    )
    assert elsewhere is not None and elsewhere.order_item_id is None
    assert shop.rows.cost_entries(other.order_id) == []
