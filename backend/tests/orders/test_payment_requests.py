"""The lifecycle's money (#4957): payment requests, manual payments that close them, a
provider's events and their fees (job cost spec §4.4), and a cancel that cancels them."""

from __future__ import annotations

import threading
from typing import Any

import pytest

from scadbuddy.core.events import OrderChanged
from scadbuddy.orders.lifecycle import LifecycleError
from scadbuddy.orders.models import Actor, Order
from scadbuddy.orders.store import Conn, OrderStore
from scadbuddy.payments import store as payments_store
from scadbuddy.payments.credentials import PaymentCredentials, ProviderBusyError
from scadbuddy.payments.models import HostedRequest, PaymentRequestView, ProviderEvent
from scadbuddy.payments.store import request
from scadbuddy.store.refs import BlobRefs
from tests.conftest import PgPool
from tests.orders.test_lifecycle import BY, Shop, connect

pytestmark = pytest.mark.requires_postgres

LINK = "https://payments.fake.invalid/pay/fake_r"
EVENT = ProviderEvent(
    event_id="e1",
    kind="paid",
    provider_ref="fake_r",
    amount_minor=700,
    currency="USD",
    fee_minor=31,
)


class PayShop(Shop):
    def rows_request(self, request_id: str) -> PaymentRequestView:
        with self.rows.transaction() as conn:
            found = request(conn, request_id)
        assert found is not None
        return found

    def linked(self) -> tuple[Order, PaymentRequestView]:
        """An accepted order (total 1000), 300 paid, and a fake balance request for 700."""
        order, _ = self.accepted()
        self.lifecycle.add_payment(order.id, BY, amount_minor=300)
        req = self.lifecycle.open_payment_request(order.id, BY, provider="fake")
        self.lifecycle.set_request_link(
            req.id,
            hosted=HostedRequest.model_validate({"provider_ref": "fake_r", "hosted_url": LINK}),
            error=None,
        )
        return order, req


@pytest.fixture
def shop(rows: OrderStore, pool: PgPool) -> PayShop:
    """The fake provider offered and connected, not the default."""
    made = PayShop(rows, BlobRefs(pool))
    made.lifecycle.offered_providers = frozenset({"fake"})
    connect(rows)
    return made


def _slug(call: object) -> LifecycleError:
    assert callable(call)
    with pytest.raises(LifecycleError) as e:
        call()
    return e.value


# -- requests ------------------------------------------------------------------------------


def test_a_request_for_the_balance_takes_total_minus_paid(shop: PayShop) -> None:
    order, _ = shop.accepted()
    shop.lifecycle.add_payment(order.id, BY, amount_minor=300)
    req = shop.lifecycle.open_payment_request(order.id, BY)
    assert (req.kind, req.amount_minor, req.currency, req.provider, req.status) == (
        "balance",
        700,
        "USD",
        None,
        "open",
    )
    assert "payment_request.opened" in shop.kinds(order)
    [opened] = [e for e in shop.rows.events(order.id) if e.kind == "payment_request.opened"]
    assert opened.detail == {"request_id": req.id, "kind": "balance", "amount_minor": 700}


def test_a_request_uses_the_default_provider_unless_one_is_named(shop: PayShop) -> None:
    with shop.rows.transaction() as conn:
        conn.execute("UPDATE payment_credentials SET is_default = true WHERE provider = 'fake'")
    order, _ = shop.accepted()
    assert shop.lifecycle.open_payment_request(order.id, BY).provider == "fake"
    assert shop.lifecycle.open_payment_request(order.id, BY, provider=None).provider is None


def test_a_request_takes_a_named_amount_and_kind(shop: PayShop) -> None:
    order, _ = shop.accepted()
    req = shop.lifecycle.open_payment_request(order.id, BY, kind="deposit", amount_minor=250)
    assert (req.kind, req.amount_minor) == ("deposit", 250)


def test_nothing_due_refuses_a_balance_request(shop: PayShop) -> None:
    order, _ = shop.accepted()
    shop.lifecycle.add_payment(order.id, BY, amount_minor=1000)
    assert _slug(lambda: shop.lifecycle.open_payment_request(order.id, BY)).slug == "nothing_due"
    error = _slug(lambda: shop.lifecycle.open_payment_request(order.id, BY, amount_minor=0))
    assert error.slug == "nothing_due"


def test_an_ended_order_takes_no_request(shop: PayShop) -> None:
    order, _ = shop.accepted()
    shop.lifecycle.cancel_order(order.id, BY)
    error = _slug(lambda: shop.lifecycle.open_payment_request(order.id, BY))
    assert error.slug == "order_cancelled"


def test_a_request_is_cancelled_once(shop: PayShop) -> None:
    order, _ = shop.accepted()
    req = shop.lifecycle.open_payment_request(order.id, BY)
    cancelled = shop.lifecycle.cancel_payment_request(order.id, req.id, BY)
    assert cancelled.status == "cancelled"
    assert "payment_request.cancelled" in shop.kinds(order)
    again = _slug(lambda: shop.lifecycle.cancel_payment_request(order.id, req.id, BY))
    assert again.slug == "payment_request_not_open"


def test_another_orders_request_is_not_found(shop: PayShop) -> None:
    order, _ = shop.accepted()
    other, _ = shop.accepted()
    req = shop.lifecycle.open_payment_request(other.id, BY)
    error = _slug(lambda: shop.lifecycle.cancel_payment_request(order.id, req.id, BY))
    assert (error.slug, error.not_found) == ("payment_request_not_found", True)


def test_a_link_is_written_under_the_order_and_tells_the_bus(shop: PayShop) -> None:
    order, req = shop.linked()
    view = shop.rows_request(req.id)
    assert (view.provider_ref, view.hosted_url, view.link_error) == ("fake_r", LINK, None)
    kinds = shop.kinds(order)
    shop.events.clear()
    failed = shop.lifecycle.set_request_link(req.id, hosted=None, error="provider is down")
    assert (failed.provider_ref, failed.hosted_url, failed.link_error) == (
        "fake_r",
        LINK,
        "provider is down",
    )
    assert shop.kinds(order) == kinds
    changed = [e for e in shop.events if isinstance(e, OrderChanged)]
    assert [(e.order_id, e.status) for e in changed] == [(order.id, "accepted")]
    assert len(shop.events) == 1


# -- manual payments -----------------------------------------------------------------------


def test_a_manual_payment_naming_the_request_closes_it(shop: PayShop) -> None:
    order, _ = shop.accepted()
    shop.lifecycle.add_payment(order.id, BY, amount_minor=300)
    req = shop.lifecycle.open_payment_request(order.id, BY)
    pay = shop.lifecycle.add_payment(
        order.id, BY, amount_minor=700, method="cash", payment_request_id=req.id
    )
    assert pay.payment_request_id == req.id and pay.provider is None
    closed = shop.rows_request(req.id)
    assert closed.status == "paid" and closed.payment_id == pay.id
    added = [e for e in shop.rows.events(order.id) if e.kind == "payment.added"]
    assert added[-1].detail == {"payment_id": pay.id, "payment_request_id": req.id}


def test_a_named_request_closes_on_a_part_payment_too(shop: PayShop) -> None:
    order, _ = shop.accepted()
    req = shop.lifecycle.open_payment_request(order.id, BY, kind="deposit", amount_minor=300)
    pay = shop.lifecycle.add_payment(order.id, BY, amount_minor=100, payment_request_id=req.id)
    assert shop.rows_request(req.id).payment_id == pay.id


def test_a_manual_payment_that_clears_the_balance_closes_the_open_balance_request(
    shop: PayShop,
) -> None:
    order, _ = shop.accepted()
    shop.lifecycle.add_payment(order.id, BY, amount_minor=300)
    req = shop.lifecycle.open_payment_request(order.id, BY)
    pay = shop.lifecycle.add_payment(order.id, BY, amount_minor=700)
    closed = shop.rows_request(req.id)
    assert (closed.status, closed.payment_id) == ("paid", pay.id)


def test_a_part_payment_leaves_the_balance_request_open(shop: PayShop) -> None:
    order, _ = shop.accepted()
    req = shop.lifecycle.open_payment_request(order.id, BY)
    shop.lifecycle.add_payment(order.id, BY, amount_minor=600)
    assert shop.rows_request(req.id).status == "open"


def test_a_payment_may_not_name_another_orders_or_a_closed_request(shop: PayShop) -> None:
    order, _ = shop.accepted()
    other, _ = shop.accepted()
    theirs = shop.lifecycle.open_payment_request(other.id, BY)
    error = _slug(
        lambda: shop.lifecycle.add_payment(
            order.id, BY, amount_minor=100, payment_request_id=theirs.id
        )
    )
    assert (error.slug, error.not_found) == ("payment_request_not_found", True)
    mine = shop.lifecycle.open_payment_request(order.id, BY)
    shop.lifecycle.cancel_payment_request(order.id, mine.id, BY)
    error = _slug(
        lambda: shop.lifecycle.add_payment(
            order.id, BY, amount_minor=100, payment_request_id=mine.id
        )
    )
    assert error.slug == "payment_request_not_open"
    assert shop.rows.payments(order.id) == []


def test_a_refund_may_not_close_a_request(shop: PayShop) -> None:
    order, _ = shop.accepted()
    req = shop.lifecycle.open_payment_request(order.id, BY)
    error = _slug(
        lambda: shop.lifecycle.add_payment(order.id, BY, amount_minor=-5, payment_request_id=req.id)
    )
    assert (error.slug, error.unprocessable) == ("amount_minor", True)


def test_a_manual_payment_writes_no_fee(shop: PayShop) -> None:
    order, _ = shop.accepted()
    req = shop.lifecycle.open_payment_request(order.id, BY)
    shop.lifecycle.add_payment(order.id, BY, amount_minor=1000, payment_request_id=req.id)
    assert shop.rows.cost_entries(order.id) == []


# -- provider events -----------------------------------------------------------------------


def test_a_paid_event_records_one_payment_and_its_fee(shop: PayShop) -> None:
    order, req = shop.linked()
    out = shop.lifecycle.record_provider_event("fake", EVENT)
    assert out == "recorded"
    [pay] = [p for p in shop.rows.payments(order.id) if p.provider == "fake"]
    assert (pay.amount_minor, pay.method, pay.reference, pay.payment_request_id) == (
        700,
        "fake",
        "fake_r",
        req.id,
    )
    [fee] = shop.rows.cost_entries(order.id)
    assert (fee.kind, fee.amount_minor, fee.source) == ("payment_fee", 31, f"payment:{pay.id}")
    by = Actor(kind="provider", principal="payment:fake")
    assert shop.rows.events(order.id)[-1].by == by
    assert fee.created_by == by
    paid = shop.rows_request(req.id)
    assert (paid.status, paid.payment_id) == ("paid", pay.id)
    added = [e for e in shop.rows.events(order.id) if e.kind == "payment.added"][-1]
    assert added.detail == {"payment_id": pay.id, "payment_request_id": req.id, "provider": "fake"}
    assert "cost.added" in shop.kinds(order)
    with shop.rows.transaction() as conn:
        row = conn.execute(
            "SELECT outcome, order_id, payment_request_id, payment_id FROM payment_events"
        ).fetchone()
    assert row == {
        "outcome": "recorded",
        "order_id": order.id,
        "payment_request_id": req.id,
        "payment_id": pay.id,
    }


def test_a_paid_event_with_no_fee_writes_no_cost_entry(shop: PayShop) -> None:
    order, _ = shop.linked()
    event = EVENT.model_copy(update={"fee_minor": 0})
    assert shop.lifecycle.record_provider_event("fake", event) == "recorded"
    assert shop.rows.cost_entries(order.id) == []


def test_a_replayed_event_records_nothing_more(shop: PayShop) -> None:
    order, _ = shop.linked()
    shop.lifecycle.record_provider_event("fake", EVENT)
    assert shop.lifecycle.record_provider_event("fake", EVENT) == "replayed"
    provider_paid = [p for p in shop.rows.payments(order.id) if p.provider == "fake"]
    assert len(provider_paid) == 1 and len(shop.rows.cost_entries(order.id)) == 1


def test_a_refund_is_a_negative_payment_with_no_fee(shop: PayShop) -> None:
    order, req = shop.linked()
    shop.lifecycle.record_provider_event("fake", EVENT)
    refund = ProviderEvent(
        event_id="e2",
        kind="refunded",
        provider_ref="fake_r",
        amount_minor=200,
        currency="USD",
        fee_minor=None,
    )
    assert shop.lifecycle.record_provider_event("fake", refund) == "recorded"
    provider_paid = [p for p in shop.rows.payments(order.id) if p.provider == "fake"]
    assert [p.amount_minor for p in provider_paid] == [700, -200]
    assert provider_paid[1].payment_request_id == req.id
    assert shop.rows_request(req.id).status == "paid"
    assert len(shop.rows.cost_entries(order.id)) == 1


def test_an_unknown_ref_is_recorded_as_unmatched(shop: PayShop) -> None:
    order, _ = shop.linked()
    stray = EVENT.model_copy(update={"provider_ref": "fake_nobody"})
    assert shop.lifecycle.record_provider_event("fake", stray) == "unmatched"
    no_ref = EVENT.model_copy(update={"event_id": "e9", "provider_ref": None})
    assert shop.lifecycle.record_provider_event("fake", no_ref) == "unmatched"
    assert [p for p in shop.rows.payments(order.id) if p.provider] == []
    with shop.rows.transaction() as conn:
        outcomes = conn.execute("SELECT outcome FROM payment_events ORDER BY id").fetchall()
    assert [r["outcome"] for r in outcomes] == ["unmatched", "unmatched"]


def test_another_currency_records_no_payment(shop: PayShop) -> None:
    order, req = shop.linked()
    euros = EVENT.model_copy(update={"currency": "EUR"})
    assert shop.lifecycle.record_provider_event("fake", euros) == "currency_mismatch"
    assert [p for p in shop.rows.payments(order.id) if p.provider] == []
    assert shop.rows.cost_entries(order.id) == []
    assert shop.rows_request(req.id).status == "open"


def test_an_expired_event_expires_an_open_request(shop: PayShop) -> None:
    _, req = shop.linked()
    expired = ProviderEvent(event_id="e3", kind="expired", provider_ref="fake_r")
    assert shop.lifecycle.record_provider_event("fake", expired) == "recorded"
    assert shop.rows_request(req.id).status == "expired"


def test_an_expired_event_leaves_a_paid_request_paid(shop: PayShop) -> None:
    _, req = shop.linked()
    shop.lifecycle.record_provider_event("fake", EVENT)
    expired = ProviderEvent(event_id="e3", kind="expired", provider_ref="fake_r")
    shop.lifecycle.record_provider_event("fake", expired)
    assert shop.rows_request(req.id).status == "paid"


def test_another_kind_is_ignored(shop: PayShop) -> None:
    _, req = shop.linked()
    other = ProviderEvent(event_id="e4", kind="other", provider_ref="fake_r")
    assert shop.lifecycle.record_provider_event("fake", other) == "ignored"
    assert shop.rows_request(req.id).status == "open"


def test_a_second_paid_event_for_a_request_the_provider_paid_is_ignored(shop: PayShop) -> None:
    order, req = shop.linked()
    shop.lifecycle.record_provider_event("fake", EVENT)
    first = shop.rows_request(req.id)
    again = EVENT.model_copy(update={"event_id": "e5"})
    assert shop.lifecycle.record_provider_event("fake", again) == "ignored"
    after = shop.rows_request(req.id)
    assert (after.payment_id, after.paid_at) == (first.payment_id, first.paid_at)
    assert len([p for p in shop.rows.payments(order.id) if p.provider]) == 1
    assert len(shop.rows.cost_entries(order.id)) == 1
    with shop.rows.transaction() as conn:
        outcomes = conn.execute("SELECT outcome FROM payment_events ORDER BY id").fetchall()
    assert [r["outcome"] for r in outcomes] == ["recorded", "ignored"]


def test_a_paid_event_told_again_with_its_fee_writes_the_fee_once(shop: PayShop) -> None:
    """#5666: Square tells a payment's fee in a later ``payment.updated`` of the same
    payment, a new event that is ``paid`` again. The money is not recorded twice; the fee
    is written on the first payment, once."""
    order, req = shop.linked()
    shop.lifecycle.record_provider_event("fake", EVENT.model_copy(update={"fee_minor": None}))
    assert shop.rows.cost_entries(order.id) == []
    with_fee = EVENT.model_copy(update={"event_id": "e6"})
    assert shop.lifecycle.record_provider_event("fake", with_fee) == "recorded"
    [pay] = [p for p in shop.rows.payments(order.id) if p.provider]
    [fee] = shop.rows.cost_entries(order.id)
    assert (fee.amount_minor, fee.source) == (31, f"payment:{pay.id}")
    assert shop.rows_request(req.id).payment_id == pay.id
    again = EVENT.model_copy(update={"event_id": "e7"})
    assert shop.lifecycle.record_provider_event("fake", again) == "ignored"
    assert len(shop.rows.cost_entries(order.id)) == 1


def test_a_provider_payment_told_again_on_a_request_paid_by_hand_is_recorded_once(
    shop: PayShop,
) -> None:
    """#5725 review: the request's ``payment_id`` is the cash that paid it, so the repeat
    is told by the provider's own payment on the request, not by that pointer."""
    order, req = shop.linked()
    cash = shop.lifecycle.add_payment(order.id, BY, amount_minor=700, payment_request_id=req.id)
    no_fee = EVENT.model_copy(update={"fee_minor": None})
    assert shop.lifecycle.record_provider_event("fake", no_fee) == "recorded"
    with_fee = EVENT.model_copy(update={"event_id": "e8"})
    assert shop.lifecycle.record_provider_event("fake", with_fee) == "recorded"
    again = EVENT.model_copy(update={"event_id": "e9"})
    assert shop.lifecycle.record_provider_event("fake", again) == "ignored"
    provider_paid = [p for p in shop.rows.payments(order.id) if p.provider == "fake"]
    assert [p.amount_minor for p in provider_paid] == [700]
    [fee] = shop.rows.cost_entries(order.id)
    assert (fee.amount_minor, fee.source) == (31, f"payment:{provider_paid[0].id}")
    assert shop.rows_request(req.id).payment_id == cash.id


def test_a_paid_event_for_a_request_paid_by_hand_is_recorded(shop: PayShop) -> None:
    order, req = shop.linked()
    cash = shop.lifecycle.add_payment(order.id, BY, amount_minor=700, payment_request_id=req.id)
    assert shop.lifecycle.record_provider_event("fake", EVENT) == "recorded"
    assert [p.amount_minor for p in shop.rows.payments(order.id)] == [300, 700, 700]
    assert shop.rows_request(req.id).payment_id == cash.id
    assert len(shop.rows.cost_entries(order.id)) == 1


def test_a_refund_with_no_amount_is_ignored(shop: PayShop) -> None:
    order, _ = shop.linked()
    shop.lifecycle.record_provider_event("fake", EVENT)
    refund = ProviderEvent(event_id="e6", kind="refunded", provider_ref="fake_r", currency="USD")
    assert shop.lifecycle.record_provider_event("fake", refund) == "ignored"
    assert [p.amount_minor for p in shop.rows.payments(order.id) if p.provider] == [700]


def test_a_paid_event_with_no_currency_is_a_mismatch(shop: PayShop) -> None:
    order, req = shop.linked()
    bare = EVENT.model_copy(update={"currency": None})
    assert shop.lifecycle.record_provider_event("fake", bare) == "currency_mismatch"
    assert [p for p in shop.rows.payments(order.id) if p.provider] == []
    assert shop.rows_request(req.id).status == "open"


def test_a_provider_payment_that_clears_the_balance_closes_the_balance_request(
    shop: PayShop,
) -> None:
    order, _ = shop.accepted()
    deposit = shop.lifecycle.open_payment_request(
        order.id, BY, kind="deposit", amount_minor=400, provider="fake"
    )
    shop.lifecycle.set_request_link(
        deposit.id,
        hosted=HostedRequest.model_validate({"provider_ref": "fake_d", "hosted_url": LINK}),
        error=None,
    )
    balance = shop.lifecycle.open_payment_request(order.id, BY, provider=None)
    shop.lifecycle.add_payment(order.id, BY, amount_minor=600)
    assert shop.rows_request(balance.id).status == "open"
    paid = ProviderEvent(
        event_id="e7", kind="paid", provider_ref="fake_d", amount_minor=400, currency="USD"
    )
    assert shop.lifecycle.record_provider_event("fake", paid) == "recorded"
    [pay] = [p for p in shop.rows.payments(order.id) if p.provider == "fake"]
    closed = shop.rows_request(balance.id)
    assert (closed.status, closed.payment_id) == ("paid", pay.id)


def test_a_refund_never_closes_a_request(shop: PayShop) -> None:
    order, _ = shop.accepted()
    shop.lifecycle.add_payment(order.id, BY, amount_minor=1200)
    req = shop.lifecycle.open_payment_request(order.id, BY, amount_minor=100)
    shop.lifecycle.add_payment(order.id, BY, amount_minor=-50)
    assert shop.rows_request(req.id).status == "open"


def test_a_link_for_a_request_no_longer_open_is_not_written(shop: PayShop) -> None:
    order, _ = shop.accepted()
    req = shop.lifecycle.open_payment_request(order.id, BY, provider="fake")
    shop.lifecycle.cancel_order(order.id, BY)
    late = shop.lifecycle.set_request_link(
        req.id,
        hosted=HostedRequest.model_validate({"provider_ref": "fake_late", "hosted_url": LINK}),
        error=None,
    )
    # #5700: the page is kept as a page to close, never as a link to pay.
    assert (late.status, late.provider_ref, late.hosted_url) == ("cancelled", "fake_late", None)
    assert late.close_pending_at is not None
    assert late.link_error == "the payment request is cancelled"


def test_a_page_made_after_a_payment_by_hand_is_kept_to_close(shop: PayShop) -> None:
    """#5700: paid by hand while the provider answered."""
    order, _ = shop.accepted()
    req = shop.lifecycle.open_payment_request(order.id, BY, provider="fake")
    shop.lifecycle.add_payment(order.id, BY, amount_minor=1000, payment_request_id=req.id)
    late = shop.lifecycle.set_request_link(
        req.id,
        hosted=HostedRequest.model_validate({"provider_ref": "fake_late", "hosted_url": LINK}),
        error=None,
    )
    assert (late.status, late.provider_ref, late.hosted_url) == ("paid", "fake_late", None)
    assert late.close_pending_at is not None and late.link_error is None


# -- cancel (Review Focus 1 and 2) -----------------------------------------------------------


def test_cancelling_the_order_cancels_its_open_requests(shop: PayShop) -> None:
    order, req = shop.linked()
    shop.lifecycle.cancel_order(order.id, BY)
    assert shop.rows_request(req.id).status == "cancelled"
    assert "payment_request.cancelled" in shop.kinds(order)


def test_a_paid_event_after_the_cancel_is_still_recorded(shop: PayShop) -> None:
    order, req = shop.linked()
    shop.lifecycle.cancel_order(order.id, BY)
    assert shop.rows_request(req.id).close_pending_at is not None
    assert shop.lifecycle.record_provider_event("fake", EVENT) == "recorded"
    assert shop.rows_request(req.id).status == "paid"
    assert len(shop.rows.cost_entries(order.id)) == 1
    # #5700: the page was paid, so there is nothing left to close.
    assert shop.rows_request(req.id).close_pending_at is None


def test_a_manual_payment_marks_the_page_it_closes(shop: PayShop) -> None:
    """#5700: the provider's page must be closed, and the sweep tries until it is."""
    order, req = shop.linked()
    shop.lifecycle.add_payment(order.id, BY, amount_minor=700)
    assert shop.rows_request(req.id).close_pending_at is not None


def test_the_providers_own_payment_marks_nothing(shop: PayShop) -> None:
    _, req = shop.linked()
    shop.lifecycle.record_provider_event("fake", EVENT)
    assert shop.rows_request(req.id).status == "paid"
    assert shop.rows_request(req.id).close_pending_at is None


def test_a_link_for_a_paid_request_leaves_the_request_as_it_is(shop: PayShop) -> None:
    _, req = shop.linked()
    shop.lifecycle.record_provider_event("fake", EVENT)
    paid = shop.rows_request(req.id)
    late = shop.lifecycle.set_request_link(
        req.id,
        hosted=HostedRequest.model_validate({"provider_ref": "fake_late", "hosted_url": LINK}),
        error=None,
    )
    assert late == paid
    assert late.link_error is None
    failed = shop.lifecycle.set_request_link(req.id, hosted=None, error="down")
    assert failed == paid


def test_a_paid_event_on_a_cancelled_request_records_the_payment(shop: PayShop) -> None:
    order, req = shop.linked()
    shop.lifecycle.cancel_payment_request(order.id, req.id, BY)
    assert shop.lifecycle.record_provider_event("fake", EVENT) == "recorded"
    [pay] = [p for p in shop.rows.payments(order.id) if p.provider == "fake"]
    found = shop.rows_request(req.id)
    assert (found.status, found.payment_id, pay.amount_minor) == ("paid", pay.id, 700)
    assert len(shop.rows.cost_entries(order.id)) == 1


def test_a_paid_event_on_an_expired_request_records_the_payment(shop: PayShop) -> None:
    order, req = shop.linked()
    expired = ProviderEvent(event_id="e8", kind="expired", provider_ref="fake_r")
    shop.lifecycle.record_provider_event("fake", expired)
    assert shop.rows_request(req.id).status == "expired"
    assert shop.lifecycle.record_provider_event("fake", EVENT) == "recorded"
    [pay] = [p for p in shop.rows.payments(order.id) if p.provider == "fake"]
    found = shop.rows_request(req.id)
    assert (found.status, found.payment_id, pay.amount_minor) == ("paid", pay.id, 700)
    assert len(shop.rows.cost_entries(order.id)) == 1


def test_a_default_provider_no_longer_offered_is_no_provider(shop: PayShop) -> None:
    """The fake flag went off while fake was the default (plan gap 8)."""
    with shop.rows.transaction() as conn:
        conn.execute("UPDATE payment_credentials SET is_default = true WHERE provider = 'fake'")
    shop.lifecycle.offered_providers = frozenset()
    order, _ = shop.accepted()
    assert shop.lifecycle.open_payment_request(order.id, BY).provider is None


def test_a_named_provider_not_connected_is_refused_and_writes_nothing(shop: PayShop) -> None:
    """A disconnect that committed before the open (plan gap 11)."""
    with shop.rows.transaction() as conn:
        conn.execute("DELETE FROM payment_credentials")
    order, _ = shop.accepted()
    refused = _slug(lambda: shop.lifecycle.open_payment_request(order.id, BY, provider="fake"))
    assert (refused.slug, refused.unprocessable) == ("provider", True)
    with shop.rows.transaction() as conn:
        assert payments_store.requests_for_order(conn, order.id) == []
    assert "payment_request.opened" not in shop.kinds(order)


def test_a_disconnect_waits_for_an_open_and_then_sees_its_request(
    shop: PayShop, rows: OrderStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The open holds the credential row FOR SHARE until it commits, so the disconnect
    (EXCLUSIVE on the table) waits, then finds the open request and refuses."""
    order, _ = shop.accepted()
    credentials = PaymentCredentials(rows, None)
    real_insert = payments_store.insert_request
    outcome: list[object] = []

    threads: list[threading.Thread] = []

    def disconnect_meanwhile(conn: Conn, **kwargs: Any) -> PaymentRequestView:
        def run() -> None:
            try:
                outcome.append(credentials.disconnect("fake"))
            except ProviderBusyError as busy:
                outcome.append(busy)

        worker = threading.Thread(target=run)
        worker.start()
        # The disconnect cannot finish while this transaction holds the row.
        worker.join(timeout=1)
        assert worker.is_alive()
        threads.append(worker)
        return real_insert(conn, **kwargs)

    monkeypatch.setattr(payments_store, "insert_request", disconnect_meanwhile)
    opened = shop.lifecycle.open_payment_request(order.id, BY, provider="fake")
    threads[0].join(timeout=10)
    assert isinstance(outcome[0], ProviderBusyError)
    assert shop.rows_request(opened.id).provider == "fake"
