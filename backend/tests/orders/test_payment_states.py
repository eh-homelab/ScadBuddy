"""A provider's slow money (#4958): a pending request while a bank debit settles, a failed
one when it does not, and a fee that arrives after the payment it belongs to."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest

from scadbuddy.orders.models import Order
from scadbuddy.orders.store import OrderStore
from scadbuddy.payments.credentials import PaymentCredentials, ProviderBusyError
from scadbuddy.payments.models import PaymentRequestView, ProviderEvent
from scadbuddy.payments.provider import EventTooEarlyError
from scadbuddy.store.refs import BlobRefs
from tests.conftest import PgPool
from tests.orders.test_lifecycle import BY
from tests.orders.test_payment_requests import EVENT, PayShop, _slug, connect

pytestmark = pytest.mark.requires_postgres


@pytest.fixture
def shop(rows: OrderStore, pool: PgPool) -> PayShop:
    """The fake provider offered and connected, as ``test_payment_requests.py``'s."""
    made = PayShop(rows, BlobRefs(pool))
    made.lifecycle.offered_providers = frozenset({"fake"})
    connect(rows)
    return made


PENDING = ProviderEvent(
    event_id="p1", kind="pending", provider_ref="fake_r", amount_minor=700, currency="USD"
)
SETTLED = EVENT.model_copy(update={"event_id": "p2", "fee_minor": None})
FAILED = ProviderEvent(event_id="p3", kind="failed", provider_ref="fake_r")
FEE = ProviderEvent(
    event_id="p4", kind="fee", provider_ref="fake_r", fee_minor=31,
    created_at=datetime.now(UTC),
)  # fmt: skip


def _outcomes(shop: PayShop) -> list[str]:
    with shop.rows.transaction() as conn:
        rows = conn.execute("SELECT outcome FROM payment_events ORDER BY id").fetchall()
    return [r["outcome"] for r in rows]


def _provider_payments(shop: PayShop, order: Order) -> list[int]:
    return [p.amount_minor for p in shop.rows.payments(order.id) if p.provider]


def _pending(shop: PayShop) -> tuple[Order, PaymentRequestView]:
    order, req = shop.linked()
    assert shop.lifecycle.record_provider_event("fake", PENDING) == "recorded"
    return order, req


# -- pending and failed --------------------------------------------------------------------


def test_a_pending_event_makes_the_request_pending_and_records_no_money(shop: PayShop) -> None:
    order, req = _pending(shop)
    assert shop.rows_request(req.id).status == "pending"
    assert _provider_payments(shop, order) == []
    assert "payment_request.pending" in shop.kinds(order)


def test_a_pending_request_settles_on_the_paid_event(shop: PayShop) -> None:
    order, req = _pending(shop)
    assert shop.lifecycle.record_provider_event("fake", SETTLED) == "recorded"
    [pay] = [p for p in shop.rows.payments(order.id) if p.provider]
    found = shop.rows_request(req.id)
    assert (found.status, found.payment_id, pay.amount_minor) == ("paid", pay.id, 700)


def test_a_pending_request_fails_on_the_failed_event(shop: PayShop) -> None:
    order, req = _pending(shop)
    assert shop.lifecycle.record_provider_event("fake", FAILED) == "recorded"
    assert shop.rows_request(req.id).status == "failed"
    assert _provider_payments(shop, order) == []
    assert "payment_request.failed" in shop.kinds(order)


def test_an_open_request_fails_when_the_failed_event_comes_first(shop: PayShop) -> None:
    """Stripe does not keep event order: the failure can arrive before the pending."""
    _, req = shop.linked()
    assert shop.lifecycle.record_provider_event("fake", FAILED) == "recorded"
    assert shop.lifecycle.record_provider_event("fake", PENDING) == "ignored"
    assert shop.rows_request(req.id).status == "failed"


def test_a_late_pending_or_failed_event_leaves_a_paid_request_paid(shop: PayShop) -> None:
    order, req = shop.linked()
    shop.lifecycle.record_provider_event("fake", SETTLED)
    assert shop.lifecycle.record_provider_event("fake", PENDING) == "ignored"
    assert shop.lifecycle.record_provider_event("fake", FAILED) == "ignored"
    assert shop.rows_request(req.id).status == "paid"
    assert _provider_payments(shop, order) == [700]


def test_an_expired_event_leaves_a_pending_request_pending(shop: PayShop) -> None:
    _, req = _pending(shop)
    expired = ProviderEvent(event_id="p5", kind="expired", provider_ref="fake_r")
    shop.lifecycle.record_provider_event("fake", expired)
    assert shop.rows_request(req.id).status == "pending"


def test_a_pending_request_cannot_be_cancelled(shop: PayShop) -> None:
    order, req = _pending(shop)
    error = _slug(lambda: shop.lifecycle.cancel_payment_request(order.id, req.id, BY))
    assert error.slug == "payment_request_not_open"
    assert shop.rows_request(req.id).status == "pending"


def test_a_manual_payment_never_closes_a_pending_request(shop: PayShop) -> None:
    """Money is on its way: a payment by hand that clears the balance leaves it pending,
    and a payment by hand may not name it."""
    order, req = _pending(shop)
    shop.lifecycle.add_payment(order.id, BY, amount_minor=700)
    assert shop.rows_request(req.id).status == "pending"
    error = _slug(
        lambda: shop.lifecycle.add_payment(
            order.id, BY, amount_minor=700, payment_request_id=req.id
        )
    )
    assert error.slug == "payment_request_not_open"


def test_a_disconnect_is_refused_while_a_request_is_pending(
    shop: PayShop, rows: OrderStore
) -> None:
    _pending(shop)
    with pytest.raises(ProviderBusyError):
        PaymentCredentials(rows, None).disconnect("fake")


# -- the late fee --------------------------------------------------------------------------


def test_a_fee_event_writes_the_fee_on_the_payment_that_paid_the_request(shop: PayShop) -> None:
    order, _ = shop.linked()
    shop.lifecycle.record_provider_event("fake", SETTLED)
    assert shop.rows.cost_entries(order.id) == []
    assert shop.lifecycle.record_provider_event("fake", FEE) == "recorded"
    [pay] = [p for p in shop.rows.payments(order.id) if p.provider]
    [fee] = shop.rows.cost_entries(order.id)
    assert (fee.kind, fee.amount_minor, fee.source) == ("payment_fee", 31, f"payment:{pay.id}")
    assert "cost.added" in shop.kinds(order)
    with shop.rows.transaction() as conn:
        row = conn.execute("SELECT payment_id FROM payment_events WHERE event_id = 'p4'").fetchone()
    assert row == {"payment_id": pay.id}


def test_a_fee_is_written_once(shop: PayShop) -> None:
    """The paid event already carried it; a fee event then writes nothing."""
    order, _ = shop.linked()
    shop.lifecycle.record_provider_event("fake", EVENT)
    assert shop.lifecycle.record_provider_event("fake", FEE) == "ignored"
    again = FEE.model_copy(update={"event_id": "p6", "fee_minor": 99})
    assert shop.lifecycle.record_provider_event("fake", again) == "ignored"
    assert [f.amount_minor for f in shop.rows.cost_entries(order.id)] == [31]
    assert _outcomes(shop) == ["recorded", "ignored", "ignored"]


def test_a_fee_before_its_payment_is_too_early_and_leaves_no_row(shop: PayShop) -> None:
    """Stripe does not keep event order: a fee can arrive before the paid event commits.
    While the request is open and the event young, it is refused with nothing written,
    so the provider sends it again."""
    order, _ = shop.linked()
    with pytest.raises(EventTooEarlyError):
        shop.lifecycle.record_provider_event("fake", FEE)
    assert _outcomes(shop) == []
    shop.lifecycle.record_provider_event("fake", SETTLED)
    assert shop.lifecycle.record_provider_event("fake", FEE) == "recorded"
    assert [f.amount_minor for f in shop.rows.cost_entries(order.id)] == [31]


def test_a_fee_before_a_pending_request_settles_is_too_early(shop: PayShop) -> None:
    _pending(shop)
    with pytest.raises(EventTooEarlyError):
        shop.lifecycle.record_provider_event("fake", FEE)


@pytest.mark.parametrize("end", ["cancelled", "expired", "failed", "by_hand"])
def test_a_fee_with_no_payment_on_an_ended_request_is_ignored(shop: PayShop, end: str) -> None:
    """A poison event must not answer 503 for ever: once the request has left open and
    pending, a fee with no provider payment is recorded as ignored."""
    order, req = shop.linked()
    if end == "cancelled":
        shop.lifecycle.cancel_payment_request(order.id, req.id, BY)
    elif end == "expired":
        shop.lifecycle.record_provider_event(
            "fake", ProviderEvent(event_id="x1", kind="expired", provider_ref="fake_r")
        )
    elif end == "failed":
        shop.lifecycle.record_provider_event("fake", FAILED)
    else:
        shop.lifecycle.add_payment(order.id, BY, amount_minor=700, payment_request_id=req.id)
    assert shop.lifecycle.record_provider_event("fake", FEE) == "ignored"
    assert shop.rows.cost_entries(order.id) == []


def test_a_fee_event_older_than_a_day_is_ignored(shop: PayShop) -> None:
    order, _ = shop.linked()
    old = FEE.model_copy(update={"created_at": datetime.now(UTC) - timedelta(hours=25)})
    assert shop.lifecycle.record_provider_event("fake", old) == "ignored"
    assert shop.rows.cost_entries(order.id) == []


def test_a_fee_event_with_no_time_is_ignored_rather_than_retried(shop: PayShop) -> None:
    _, _ = shop.linked()
    bare = FEE.model_copy(update={"created_at": None})
    assert shop.lifecycle.record_provider_event("fake", bare) == "ignored"


def test_a_fee_event_with_no_fee_is_ignored(shop: PayShop) -> None:
    order, _ = shop.linked()
    shop.lifecycle.record_provider_event("fake", SETTLED)
    bare = FEE.model_copy(update={"fee_minor": None})
    assert shop.lifecycle.record_provider_event("fake", bare) == "ignored"
    assert shop.rows.cost_entries(order.id) == []


def test_two_fee_events_at_once_write_one_fee(shop: PayShop) -> None:
    order, _ = shop.linked()
    shop.lifecycle.record_provider_event("fake", SETTLED)
    start = threading.Barrier(2)
    outcomes: list[str] = []

    def send(event_id: str) -> None:
        start.wait()
        event = FEE.model_copy(update={"event_id": event_id})
        outcomes.append(shop.lifecycle.record_provider_event("fake", event))

    workers = [threading.Thread(target=send, args=(e,)) for e in ("f1", "f2")]
    for w in workers:
        w.start()
    for w in workers:
        w.join(timeout=10)
    assert sorted(outcomes) == ["ignored", "recorded"]
    assert len(shop.rows.cost_entries(order.id)) == 1
