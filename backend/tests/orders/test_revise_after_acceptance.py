"""An accepted item revised after acceptance (#5350, spec 2026-10-18 §3.5 "As built"): the
new design waits for its own quote and that quote's acceptance, and the old accepted price
stands until then. Refused before acceptance, once shipped, ended or on hold, on a settled
item, on an item with no accepted quote, while a print runs, and once an attempt is
accepted (#5592). The lock stays per item:
the quantity stays pinned, the old quote is never accepted again."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from scadbuddy.orders.lifecycle import (
    FulfilmentInput,
    LifecycleError,
    ManualQuoteInput,
    requote_open,
    stale_attempt,
)
from scadbuddy.orders.models import Fulfilment, Order, OrderItem, Quote
from scadbuddy.orders.store import OrderStore
from scadbuddy.store.refs import BlobRefs
from tests.conftest import PgPool
from tests.orders.test_add_after_acceptance import START
from tests.orders.test_lifecycle import BY, Shop, resolved
from tests.orders.test_printing import run_row, set_run, settle, started

pytestmark = pytest.mark.requires_postgres


@pytest.fixture
def shop(rows: OrderStore, pool: PgPool) -> Shop:
    return Shop(rows, BlobRefs(pool))


def revise(shop: Shop, order: Order, item: OrderItem, slug: str = "other") -> None:
    shop.lifecycle.add_revision(order.id, item.id, BY, resolved(slug), after_acceptance=True)


def accepted_quote(shop: Shop, item: OrderItem) -> Quote | None:
    return next((q for q in shop.rows.quotes(item.id) if q.accepted), None)


def requote(shop: Shop, order: Order, item: OrderItem, price: int) -> Quote:
    return shop.lifecycle.record_manual_quote(
        order.id, item.id, ManualQuoteInput(unit_price_minor=price, label="new design"), BY
    )


def test_the_old_price_stands_until_the_new_quote_is_accepted(shop: Shop) -> None:
    order, item = shop.accepted()
    old = accepted_quote(shop, item)
    assert old is not None

    revise(shop, order, item)

    item = shop.get_item(item)
    assert (item.status, item.revised_after_acceptance) == ("draft", True)
    assert item.reference is not None and item.reference.slug == "other"
    # The agreed price is still the accepted one.
    assert accepted_quote(shop, item) == old
    assert shop.get(order).status == "accepted"
    rows = [e for e in shop.rows.events(order.id) if e.kind == "revision.added_after_acceptance"]
    assert len(rows) == 1
    assert rows[0].detail["order_status"] == "accepted"
    assert rows[0].detail["accepted_quote_id"] == old.id
    assert rows[0].detail["revision_id"] == item.current_revision_id

    new = requote(shop, order, item, 1200)
    item = shop.get_item(item)
    assert item.status == "quoted"
    # A quote recorded is not yet the price: the old one stands.
    assert accepted_quote(shop, item) == old
    # The old quote prices the earlier design: it is never accepted again.
    with pytest.raises(LifecycleError) as stale:
        shop.lifecycle.accept_quote(order.id, item.id, old.id, BY)
    assert stale.value.slug == "quote_stale"

    item = shop.lifecycle.accept_quote(order.id, item.id, new.id, BY)
    assert (item.status, item.revised_after_acceptance) == ("approved", False)
    assert accepted_quote(shop, item) == new.model_copy(update={"accepted": True})
    assert [q.accepted for q in shop.rows.quotes(item.id)] == [False, True]


@pytest.mark.parametrize("start", ["accepted", "in_production"])
def test_accepted_and_in_production_take_it(shop: Shop, start: str) -> None:
    order, item = START[start](shop)
    revise(shop, order, item)
    item = shop.get_item(item)
    # A failed attempt's item is `draft` too: it is not printed at the old quote.
    assert (item.status, item.revised_after_acceptance) == ("draft", True)
    assert shop.get(order).status == start


def test_a_ready_order_refuses_it_for_its_fulfilled_item(shop: Shop) -> None:
    order, item = START["ready"](shop)
    with pytest.raises(LifecycleError) as refused:
        revise(shop, order, item)
    assert refused.value.slug == "item_not_open"


@pytest.mark.parametrize("status", ["draft", "quoted"])
def test_before_acceptance_is_refused(shop: Shop, status: str) -> None:
    order = shop.order()
    item = shop.item(order)
    if status == "quoted":
        item = shop.quoted(order, item)
    assert shop.get(order).status == status
    with pytest.raises(LifecycleError) as refused:
        revise(shop, order, item)
    assert refused.value.slug == "order_not_accepted"
    assert f"/items/{item.id}/revisions" in str(refused.value)


def test_a_shipped_order_refuses_it(shop: Shop) -> None:
    order, item = shop.accepted()
    other = shop.lifecycle.add_item(
        order.id, BY, resolved(), output_id=None, quantity=1, after_acceptance=True
    )
    shop.quoted(order, other)
    shop.fulfilled(order, shop.get_item(item))
    shop.shipped(order, shop.get_item(item))
    # The other item is not done, but the order shipped what it fulfilled.
    shop.lifecycle.cancel_item(order.id, other.id, BY)
    assert shop.get(order).status == "shipped"
    with pytest.raises(LifecycleError) as refused:
        revise(shop, order, shop.get_item(item))
    # Every item of a shipped order is settled, so the item's own check answers first.
    assert refused.value.slug == "item_not_open"


@pytest.mark.parametrize("end", ["cancel", "close"])
def test_an_ended_order_refuses_it(shop: Shop, end: str) -> None:
    order, item = shop.accepted()
    if end == "cancel":
        shop.lifecycle.cancel_order(order.id, BY)
    else:
        shop.fulfilled(order, item)
        shop.shipped(order, shop.get_item(item))
        shop.lifecycle.close_order(order.id, BY)
    with pytest.raises(LifecycleError) as refused:
        revise(shop, order, item)
    assert refused.value.slug == f"order_{'cancelled' if end == 'cancel' else 'closed'}"


def test_on_hold_is_refused(shop: Shop) -> None:
    order, item = shop.accepted()
    shop.lifecycle.hold(order.id, BY)
    with pytest.raises(LifecycleError) as refused:
        revise(shop, order, item)
    assert refused.value.slug == "order_on_hold"
    assert shop.get_item(item).revised_after_acceptance is False
    shop.lifecycle.release(order.id, BY)
    revise(shop, order, item)
    # On hold again, the new quote is recorded but not accepted.
    shop.lifecycle.hold(order.id, BY)
    new = requote(shop, order, item, 1200)
    with pytest.raises(LifecycleError) as held:
        shop.lifecycle.accept_quote(order.id, item.id, new.id, BY)
    assert held.value.slug == "order_on_hold"


def test_a_cancelled_item_is_refused(shop: Shop) -> None:
    order, item = shop.accepted()
    other = shop.lifecycle.add_item(
        order.id, BY, resolved(), output_id=None, quantity=1, after_acceptance=True
    )
    shop.quoted(order, other)
    shop.lifecycle.cancel_item(order.id, item.id, BY)
    with pytest.raises(LifecycleError) as refused:
        revise(shop, order, item)
    assert refused.value.slug == "item_not_open"


def test_an_item_with_no_accepted_quote_uses_the_plain_route(shop: Shop) -> None:
    order, _ = shop.accepted()
    added = shop.lifecycle.add_item(
        order.id, BY, resolved(), output_id=None, quantity=1, after_acceptance=True
    )
    with pytest.raises(LifecycleError) as refused:
        revise(shop, order, added)
    assert refused.value.slug == "item_not_priced"
    # The plain route takes it (#5349).
    shop.lifecycle.add_revision(order.id, added.id, BY, resolved("other"))


def test_a_custom_item_is_refused(shop: Shop) -> None:
    order, _ = shop.accepted()
    custom = shop.lifecycle.add_custom_item(
        order.id,
        BY,
        title="Tag",
        quantity=1,
        unit_price_minor=300,
        accept=False,
        after_acceptance=True,
    )
    with pytest.raises(LifecycleError) as refused:
        revise(shop, order, custom)
    assert refused.value.slug == "item_custom"


def test_the_plain_route_refuses_an_accepted_item_and_names_this_one(shop: Shop) -> None:
    order, item = shop.accepted()
    with pytest.raises(LifecycleError) as refused:
        shop.lifecycle.add_revision(order.id, item.id, BY, resolved("other"))
    assert refused.value.slug == "item_locked"
    assert f"/items/{item.id}/revisions/after-acceptance" in str(refused.value)


def test_the_lock_stays_per_item(shop: Shop) -> None:
    order, item = shop.accepted()
    assert not requote_open(order, item, shop.rows.quotes(item.id))
    revise(shop, order, item)
    item = shop.get_item(item)
    assert requote_open(shop.get(order), item, shop.rows.quotes(item.id))
    # The quantity stays pinned: it would reprice the old quote.
    with pytest.raises(LifecycleError) as pinned:
        shop.lifecycle.update_item(order.id, item.id, BY, quantity=3)
    assert pinned.value.slug == "item_locked"
    # A further design takes the same path, and the old price still stands.
    old = accepted_quote(shop, item)
    shop.lifecycle.add_revision(order.id, item.id, BY, resolved("third"))
    assert accepted_quote(shop, shop.get_item(item)) == old
    # Nothing is printed or fulfilled at the old quote.
    with pytest.raises(LifecycleError) as unapproved:
        shop.lifecycle.record_fulfilment(
            order.id, item.id, FulfilmentInput(quantity=1, outcome="succeeded"), BY
        )
    assert unapproved.value.slug == "item_not_approved"


def test_a_cancelled_and_reopened_item_still_waits_for_its_quote(shop: Shop) -> None:
    order, item = shop.accepted()
    other = shop.lifecycle.add_item(
        order.id, BY, resolved(), output_id=None, quantity=1, after_acceptance=True
    )
    shop.quoted(order, other)
    revise(shop, order, item)
    shop.lifecycle.cancel_item(order.id, item.id, BY)
    item = shop.lifecycle.reopen_item(order.id, item.id, BY)
    assert (item.status, item.revised_after_acceptance) == ("draft", True)
    requote(shop, order, item, 1200)
    shop.lifecycle.cancel_item(order.id, item.id, BY)
    item = shop.lifecycle.reopen_item(order.id, item.id, BY)
    assert item.status == "quoted"


def test_a_print_in_flight_is_refused(shop: Shop) -> None:
    order, item = shop.accepted()
    shop.lifecycle.claim_print(
        order.id, item.id, BY, request_id="req-1", sliced_sha256="a" * 64, sliced_blob_key="q-a"
    )
    assert shop.get_item(item).status == "queued"
    with pytest.raises(LifecycleError) as refused:
        revise(shop, order, item)
    assert refused.value.slug == "print_in_flight"


def _postdate(pool: PgPool, attempt: Fulfilment, by: timedelta) -> None:
    """Moves the attempt's time past the revision that follows it, as a clock that steps
    back between the two writes would (#5704): the revision's number orders them still."""
    with pool.connection() as conn:
        conn.execute(
            "UPDATE order_item_fulfilments SET created_at = created_at + %s WHERE id = %s",
            (by, attempt.id),
        )


def _printed(shop: Shop, pool: PgPool, order: Order, item: OrderItem) -> Fulfilment:
    """The item printed, its attempt not accepted yet: the item is `printed`."""
    run_id = run_row(pool)
    started(shop, order, item, run_id)
    set_run(pool, run_id, "succeeded")
    shop.lifecycle.follow_print(run_id)
    shop.lifecycle.follow_print(run_id, progressed=True)
    settle(pool, run_id, "finished")
    attempt = shop.lifecycle.follow_print(run_id)
    assert attempt is not None
    return attempt


@pytest.mark.parametrize("how", ["print", "hand"])
def test_the_old_designs_attempt_is_never_accepted(shop: Shop, pool: PgPool, how: str) -> None:
    order, item = shop.accepted()
    if how == "print":
        old = _printed(shop, pool, order, item)
        assert shop.get_item(item).status == "printed"
    else:
        old = shop.lifecycle.record_fulfilment(
            order.id, item.id, FulfilmentInput(quantity=1, outcome="succeeded"), BY
        )
    _postdate(pool, old, timedelta(hours=1))
    revise(shop, order, item)
    assert shop.get_item(item).status == "draft"
    new = requote(shop, order, item, 1200)
    item = shop.lifecycle.accept_quote(order.id, item.id, new.id, BY)
    assert item.status == "approved"

    # The old attempt printed the earlier design: it never counts as the new one made.
    with pytest.raises(LifecycleError) as refused:
        shop.lifecycle.accept_fulfilment(order.id, item.id, old.id, BY)
    assert refused.value.slug == "fulfilment_stale"
    assert shop.get_item(item).status == "approved"
    stale = next(f for f in shop.rows.fulfilments(item.id) if f.id == old.id)
    assert stale.accepted_at is None

    # An attempt of the new design is accepted.
    attempt = shop.lifecycle.record_fulfilment(
        order.id, item.id, FulfilmentInput(quantity=1, outcome="succeeded"), BY
    )
    shop.lifecycle.accept_fulfilment(order.id, item.id, attempt.id, BY)
    assert shop.get_item(item).status == "fulfilled"


@pytest.mark.parametrize("how", ["print", "hand"])
def test_a_clock_step_back_never_makes_the_priced_designs_attempt_stale(
    shop: Shop, pool: PgPool, how: str
) -> None:
    """#5704: the wall clock steps back (WSL steps it by ±14 s) between the revision's
    write and the attempt's. The attempt still printed the revision the accepted quote
    prices, so it is accepted."""
    order, item = shop.accepted()
    revision = shop.rows.revisions(item.id)[0]
    with pool.connection() as conn:
        conn.execute(
            "UPDATE order_item_revisions SET created_at = now() + interval '1 minute'"
            " WHERE id = %s",
            (revision.id,),
        )
    if how == "print":
        attempt = _printed(shop, pool, order, item)
    else:
        attempt = shop.lifecycle.record_fulfilment(
            order.id, item.id, FulfilmentInput(quantity=1, outcome="succeeded"), BY
        )
    shop.lifecycle.accept_fulfilment(order.id, item.id, attempt.id, BY)
    assert shop.get_item(item).status == "fulfilled"


@pytest.mark.parametrize("how", ["print", "hand"])
def test_a_production_attempt_names_the_items_current_revision(
    shop: Shop, pool: PgPool, how: str
) -> None:
    """#5704: `OrderStore.insert_fulfilment` stores the item's current revision on a
    production attempt, by hand and by a print's claim: the design it printed."""
    order, item = shop.accepted()

    def attempt() -> Fulfilment:
        if how == "print":
            return _printed(shop, pool, order, item)
        return shop.lifecycle.record_fulfilment(
            order.id, item.id, FulfilmentInput(quantity=1, outcome="failed"), BY
        )

    first = attempt()
    assert first.revision_id == shop.rows.revisions(item.id)[0].id
    if how == "print":
        # A print that made the item's quantity refuses another: this one made nothing.
        shop.lifecycle.update_fulfilment(
            order.id, item.id, first.id, FulfilmentInput(quantity=0, outcome="failed"), BY
        )
    revise(shop, order, item)
    new = requote(shop, order, item, 1200)
    shop.lifecycle.accept_quote(order.id, item.id, new.id, BY)
    second = attempt()
    assert second.revision_id == shop.rows.revisions(item.id)[1].id
    stored = {f.id: f.revision_id for f in shop.rows.fulfilments(item.id)}
    assert stored == {first.id: first.revision_id, second.id: second.revision_id}


def test_an_item_with_an_accepted_attempt_refuses_it(shop: Shop) -> None:
    """#5592: units accepted of the agreed design are real goods. A new design would count
    them as made of it, so the revision is refused, and nothing changes."""
    order, item = shop.accepted(quantity=3)
    attempt = shop.lifecycle.record_fulfilment(
        order.id, item.id, FulfilmentInput(quantity=1, outcome="succeeded"), BY
    )
    shop.lifecycle.accept_fulfilment(order.id, item.id, attempt.id, BY)
    before = shop.get_item(item)
    assert before.status == "approved"

    with pytest.raises(LifecycleError) as refused:
        revise(shop, order, item)
    assert refused.value.slug == "item_partly_fulfilled"
    assert refused.value.items == [item.id]
    assert "1 of 3" in str(refused.value)
    assert shop.get_item(item) == before
    assert len(shop.rows.revisions(item.id)) == 1


def test_an_accepted_attempt_answers_before_a_print_in_flight(shop: Shop) -> None:
    """#5598: `item_partly_fulfilled` is permanent and `print_in_flight` is transient, so
    the permanent refusal answers first. A caller that waits out the print is not then
    refused anyway."""
    order, item = shop.accepted(quantity=3)
    attempt = shop.lifecycle.record_fulfilment(
        order.id, item.id, FulfilmentInput(quantity=1, outcome="succeeded"), BY
    )
    shop.lifecycle.accept_fulfilment(order.id, item.id, attempt.id, BY)
    shop.lifecycle.claim_print(
        order.id, item.id, BY, request_id="req-1", sliced_sha256="a" * 64, sliced_blob_key="q-a"
    )
    assert shop.get_item(item).status == "queued"

    with pytest.raises(LifecycleError) as refused:
        revise(shop, order, item)
    assert refused.value.slug == "item_partly_fulfilled"


def test_attempts_not_accepted_let_it_through(shop: Shop) -> None:
    """Only an accepted attempt refuses the revision: one not accepted stays, stale."""
    order, item = shop.accepted(quantity=3)
    shop.lifecycle.record_fulfilment(
        order.id, item.id, FulfilmentInput(quantity=1, outcome="succeeded"), BY
    )
    shop.lifecycle.record_fulfilment(
        order.id, item.id, FulfilmentInput(quantity=0, outcome="failed"), BY
    )
    revise(shop, order, item)
    assert shop.get_item(item).status == "draft"


def _pin(pool: PgPool, attempt: Fulfilment, at: datetime) -> None:
    with pool.connection() as conn:
        conn.execute(
            "UPDATE order_item_fulfilments SET created_at = %s WHERE id = %s", (at, attempt.id)
        )


def test_stale_attempt_follows_the_accepted_quotes_revision(shop: Shop, pool: PgPool) -> None:
    """`stale_attempt` is the one rule for the refusal and the order detail (#5592)."""
    order, item = shop.accepted()
    first = shop.rows.revisions(item.id)[0]
    old = shop.lifecycle.record_fulfilment(
        order.id, item.id, FulfilmentInput(quantity=1, outcome="succeeded"), BY
    )
    # Its time is after any revision made later: the revision's number orders them (#5704).
    _pin(pool, old, first.created_at + timedelta(hours=1))

    def stale(attempt_id: str) -> bool:
        attempt = next(f for f in shop.rows.fulfilments(item.id) if f.id == attempt_id)
        return stale_attempt(attempt, shop.rows.quotes(item.id), shop.rows.revisions(item.id))

    assert stale(old.id) is False
    revise(shop, order, item)
    # The old quote still stands, and it prices the first revision.
    assert stale(old.id) is False
    new = requote(shop, order, item, 1200)
    shop.lifecycle.accept_quote(order.id, item.id, new.id, BY)
    assert stale(old.id) is True
    fresh = shop.lifecycle.record_fulfilment(
        order.id, item.id, FulfilmentInput(quantity=1, outcome="succeeded"), BY
    )
    assert stale(fresh.id) is False
    # A test print's attempt is never stale: it is never accepted.
    attempt = next(f for f in shop.rows.fulfilments(item.id) if f.id == old.id)
    test = attempt.model_copy(update={"purpose": "test"})
    assert stale_attempt(test, shop.rows.quotes(item.id), shop.rows.revisions(item.id)) is False
