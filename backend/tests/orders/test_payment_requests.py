"""The lifecycle's money (#4957): payment requests, manual payments that close them, a
provider's events and their fees (job cost spec §4.4), and a cancel that cancels them."""

from __future__ import annotations

import pytest

from scadbuddy.core.events import OrderChanged
from scadbuddy.orders.lifecycle import LifecycleError
from scadbuddy.orders.models import Actor, Order
from scadbuddy.orders.store import OrderStore
from scadbuddy.payments.models import HostedRequest, PaymentRequestView, ProviderEvent
from scadbuddy.payments.store import request
from scadbuddy.store.refs import BlobRefs
from tests.conftest import PgPool
from tests.orders.test_lifecycle import BY, Shop

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
    return PayShop(rows, BlobRefs(pool))


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
        conn.execute(
            "INSERT INTO payment_credentials (provider, account_label, secret_sealed, dek_sealed,"
            " kek_id, is_default) VALUES ('fake', 'Fake', '\\x00', '\\x00', 'k', true)"
        )
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


def test_a_second_paid_event_keeps_the_first_payment_on_the_request(shop: PayShop) -> None:
    order, req = shop.linked()
    shop.lifecycle.record_provider_event("fake", EVENT)
    first = shop.rows_request(req.id)
    again = EVENT.model_copy(update={"event_id": "e5"})
    assert shop.lifecycle.record_provider_event("fake", again) == "recorded"
    after = shop.rows_request(req.id)
    assert (after.payment_id, after.paid_at) == (first.payment_id, first.paid_at)
    assert len([p for p in shop.rows.payments(order.id) if p.provider]) == 2


# -- cancel (Review Focus 1 and 2) -----------------------------------------------------------


def test_cancelling_the_order_cancels_its_open_requests(shop: PayShop) -> None:
    order, req = shop.linked()
    shop.lifecycle.cancel_order(order.id, BY)
    assert shop.rows_request(req.id).status == "cancelled"
    assert "payment_request.cancelled" in shop.kinds(order)


def test_a_paid_event_after_the_cancel_is_still_recorded(shop: PayShop) -> None:
    order, req = shop.linked()
    shop.lifecycle.cancel_order(order.id, BY)
    assert shop.lifecycle.record_provider_event("fake", EVENT) == "recorded"
    assert shop.rows_request(req.id).status == "paid"
    assert len(shop.rows.cost_entries(order.id)) == 1
