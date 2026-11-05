"""Time and cost entries (job cost spec 2026-11-05 §4.3-4.5, #5326): written through the
lifecycle in any order status, each on the audit line and told after commit."""

from __future__ import annotations

import pytest

from scadbuddy.core.events import OrderChanged, OrderItemChanged
from scadbuddy.orders.lifecycle import LifecycleError
from scadbuddy.orders.models import CostEntryInput, TimeEntryInput
from scadbuddy.orders.store import OrderStore
from scadbuddy.store.refs import BlobRefs
from tests.conftest import PgPool
from tests.orders.test_lifecycle import BY, Shop

pytestmark = pytest.mark.requires_postgres


@pytest.fixture(name="shop")
def _shop(rows: OrderStore, pool: PgPool) -> Shop:
    return Shop(rows, BlobRefs(pool))


def test_a_time_entry_is_recorded_with_its_rate_and_audit_row(shop: Shop) -> None:
    order, item = shop.accepted()
    shop.events.clear()

    entry, _ = shop.lifecycle.add_time_entry(
        order.id,
        TimeEntryInput(order_item_id=item.id, kind="post_processing", minutes=25, note="sand"),
        BY,
        rate_minor_per_hour=3000,
    )

    assert (entry.order_item_id, entry.minutes, entry.rate_minor_per_hour) == (item.id, 25, 3000)
    assert shop.rows.time_entries(order.id) == [entry]
    assert shop.kinds(order)[-1] == "time.added"
    assert shop.rows.events(order.id)[-1].detail == {
        "entry_id": entry.id,
        "item_id": item.id,
        "entry_kind": "post_processing",
        "minutes": 25,
    }
    assert any(isinstance(e, OrderChanged) for e in shop.events)
    assert any(isinstance(e, OrderItemChanged) and e.item_id == item.id for e in shop.events)


@pytest.mark.parametrize("state", ["closed", "cancelled", "on_hold"])
def test_entries_are_allowed_in_every_status(shop: Shop, state: str) -> None:
    order, item = shop.accepted()
    if state == "closed":
        shop.fulfilled(order, item)
        shop.shipped(order, shop.get_item(item))
        shop.lifecycle.close_order(order.id, BY)
    elif state == "cancelled":
        shop.lifecycle.cancel_order(order.id, BY, None)
    else:
        shop.lifecycle.hold(order.id, BY)

    shop.lifecycle.add_time_entry(
        order.id, TimeEntryInput(kind="packing", minutes=5), BY, rate_minor_per_hour=None
    )
    cost, _ = shop.lifecycle.add_cost_entry(
        order.id, CostEntryInput(kind="payment_fee", amount_minor=59), BY
    )
    shop.lifecycle.remove_cost_entry(order.id, cost.id, BY)

    assert [e.minutes for e in shop.rows.time_entries(order.id)] == [5]
    assert shop.rows.cost_entries(order.id) == []
    assert shop.kinds(order)[-3:] == ["time.added", "cost.added", "cost.removed"]


def test_an_item_of_another_order_is_refused(shop: Shop) -> None:
    order, _ = shop.accepted()
    _, other_item = shop.accepted()
    with pytest.raises(LifecycleError) as refused:
        shop.lifecycle.add_cost_entry(
            order.id,
            CostEntryInput(order_item_id=other_item.id, kind="material", amount_minor=100),
            BY,
        )
    assert refused.value.slug == "item_not_found"
    assert shop.rows.cost_entries(order.id) == []


def test_one_source_is_written_once(shop: Shop) -> None:
    order, _ = shop.accepted()
    body = CostEntryInput(kind="assistant", amount_minor=120, source="ai_session:s1")

    first, written = shop.lifecycle.add_cost_entry(order.id, body, BY)
    again, again_written = shop.lifecycle.add_cost_entry(order.id, body, BY)

    assert again == first
    assert (written, again_written) == (True, False)
    assert len(shop.rows.cost_entries(order.id)) == 1
    assert shop.kinds(order).count("cost.added") == 1


def test_a_timer_entry_retried_is_written_once(shop: Shop) -> None:
    """A lost reply to "Stop timer" is retried with the same source (#5338's review)."""
    order, _ = shop.accepted()
    body = TimeEntryInput(kind="setup", minutes=12, source="timer:2026-11-05T10:00:00Z")

    first, written = shop.lifecycle.add_time_entry(order.id, body, BY, rate_minor_per_hour=0)
    again, again_written = shop.lifecycle.add_time_entry(order.id, body, BY, rate_minor_per_hour=0)

    assert again == first
    assert (written, again_written) == (True, False)
    assert len(shop.rows.time_entries(order.id)) == 1
    assert shop.kinds(order).count("time.added") == 1


def test_an_item_with_entries_is_never_deleted(shop: Shop) -> None:
    """Its logged time and costs stay on the record: the item is cancelled instead."""
    order = shop.order()
    item = shop.item(order)
    shop.lifecycle.add_cost_entry(
        order.id, CostEntryInput(order_item_id=item.id, kind="material", amount_minor=80), BY
    )

    with pytest.raises(LifecycleError) as refused:
        shop.lifecycle.delete_item(order.id, item.id, BY)

    assert refused.value.slug == "item_not_draft"
    assert [e.order_item_id for e in shop.rows.cost_entries(order.id)] == [item.id]


def test_a_removal_is_audited_and_an_unknown_entry_is_not_found(shop: Shop) -> None:
    order, item = shop.accepted()
    other, _ = shop.accepted()
    entry, _ = shop.lifecycle.add_time_entry(
        order.id,
        TimeEntryInput(order_item_id=item.id, kind="design", minutes=60),
        BY,
        rate_minor_per_hour=0,
    )

    with pytest.raises(LifecycleError) as refused:
        shop.lifecycle.remove_time_entry(other.id, entry.id, BY)
    assert refused.value.slug == "time_entry_not_found"

    shop.lifecycle.remove_time_entry(order.id, entry.id, BY)
    assert shop.rows.time_entries(order.id) == []
    assert shop.kinds(order)[-1] == "time.removed"
    with pytest.raises(LifecycleError):
        shop.lifecycle.remove_time_entry(order.id, entry.id, BY)
