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
from tests.orders.test_lifecycle import BY, connect
from tests.orders.test_payment_requests import EVENT, PayShop, _slug

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


# -- a refund that failed (#5681) ----------------------------------------------------------

REFUND = ProviderEvent(
    event_id="refund:re_1", kind="refunded", provider_ref="fake_r", amount_minor=200,
    currency="USD",
)  # fmt: skip
REFUND_FAILED = ProviderEvent(
    event_id="refund-failed:re_1", kind="refund_failed", provider_ref="fake_r",
    currency="USD", reverses="refund:re_1", created_at=datetime.now(UTC),
)  # fmt: skip


def _refunded(shop: PayShop) -> tuple[Order, PaymentRequestView]:
    order, req = shop.linked()
    shop.lifecycle.record_provider_event("fake", SETTLED)
    assert shop.lifecycle.record_provider_event("fake", REFUND) == "recorded"
    return order, req


def test_a_failed_refund_writes_a_payment_that_cancels_it(shop: PayShop) -> None:
    order, req = _refunded(shop)
    before = shop.rows_request(req.id)
    assert shop.lifecycle.record_provider_event("fake", REFUND_FAILED) == "recorded"
    pays = [p for p in shop.rows.payments(order.id) if p.provider]
    assert [p.amount_minor for p in pays] == [700, -200, 200]
    refund, reversal = pays[1], pays[2]
    assert (reversal.provider, reversal.payment_request_id, reversal.reference) == (
        refund.provider, refund.payment_request_id, refund.reference,
    )  # fmt: skip
    after = shop.rows_request(req.id)
    assert (after.status, after.payment_id, after.paid_at) == (
        before.status, before.payment_id, before.paid_at,
    )  # fmt: skip
    # The manual 300, the provider's 700, its refund and the reversal.
    assert shop.kinds(order).count("payment.added") == 4


def test_a_failed_refund_told_again_is_replayed(shop: PayShop) -> None:
    order, _ = _refunded(shop)
    shop.lifecycle.record_provider_event("fake", REFUND_FAILED)
    assert shop.lifecycle.record_provider_event("fake", REFUND_FAILED) == "replayed"
    assert _provider_payments(shop, order) == [700, -200, 200]


def test_a_failure_before_its_refund_is_recorded_at_once_and_the_late_refund_writes_nothing(
    shop: PayShop,
) -> None:
    """Stripe does not keep event order, and a refund that never succeeded fails too. The
    failure is kept at once (``ignored``: no payment, no retry, no order change told); a
    succeeded snapshot of the same refund told after it writes nothing, so the two cancel
    out."""
    order, _ = shop.linked()
    shop.lifecycle.record_provider_event("fake", SETTLED)
    told = len(shop.events)
    assert shop.lifecycle.record_provider_event("fake", REFUND_FAILED) == "ignored"
    assert len(shop.events) == told
    assert _provider_payments(shop, order) == [700]
    assert shop.lifecycle.record_provider_event("fake", REFUND) == "ignored"
    assert _provider_payments(shop, order) == [700]
    assert _outcomes(shop) == ["recorded", "ignored", "ignored"]


def test_a_failure_for_a_refund_never_told_needs_no_time_or_open_request(
    shop: PayShop,
) -> None:
    """No window: an old event, one with no time, or one on an ended request is recorded
    at once, never refused for a retry."""
    order, req = shop.linked()
    shop.lifecycle.cancel_payment_request(order.id, req.id, BY)
    old = REFUND_FAILED.model_copy(update={"created_at": None})
    assert shop.lifecycle.record_provider_event("fake", old) == "ignored"
    assert _provider_payments(shop, order) == []


def test_a_failed_refund_of_a_refund_told_but_not_recorded_is_ignored(shop: PayShop) -> None:
    """The refund's event came and wrote no money (no amount): nothing to reverse."""
    order, _ = shop.linked()
    shop.lifecycle.record_provider_event("fake", SETTLED)
    bare = REFUND.model_copy(update={"amount_minor": None})
    assert shop.lifecycle.record_provider_event("fake", bare) == "ignored"
    assert shop.lifecycle.record_provider_event("fake", REFUND_FAILED) == "ignored"
    assert _provider_payments(shop, order) == [700]


def test_a_failed_refund_takes_its_amount_and_currency_from_the_refund(
    shop: PayShop,
) -> None:
    """The refund's own row decides: an event that names another currency, or an amount,
    still reverses exactly the refund."""
    order, _ = _refunded(shop)
    other = REFUND_FAILED.model_copy(update={"currency": "EUR", "amount_minor": 999})
    assert shop.lifecycle.record_provider_event("fake", other) == "recorded"
    assert _provider_payments(shop, order) == [700, -200, 200]


def test_a_refund_and_its_failure_at_once_cancel_out(shop: PayShop) -> None:
    """The two events serialize on the order's lock: whichever runs second sees the
    first's committed row, so the order nets no refund in either order."""
    order, _ = shop.linked()
    shop.lifecycle.record_provider_event("fake", SETTLED)
    start = threading.Barrier(2)
    outcomes: list[str] = []

    def send(event: ProviderEvent) -> None:
        start.wait()
        outcomes.append(shop.lifecycle.record_provider_event("fake", event))

    workers = [threading.Thread(target=send, args=(e,)) for e in (REFUND, REFUND_FAILED)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(timeout=10)
    assert len(outcomes) == 2
    assert sum(_provider_payments(shop, order)) == 700
    assert _provider_payments(shop, order) in ([700], [700, -200, 200])


# -- the method a provider names (#4960) ---------------------------------------------------


def test_a_paid_event_that_names_venmo_records_the_payment_as_venmo(shop: PayShop) -> None:
    order, _ = shop.linked()
    venmo = ProviderEvent.model_validate(SETTLED.model_dump() | {"method": "venmo"})
    assert shop.lifecycle.record_provider_event("fake", venmo) == "recorded"
    [paid] = [p for p in shop.rows.payments(order.id) if p.provider]
    assert (paid.method, paid.provider) == ("venmo", "fake")


def test_a_paid_event_that_names_no_method_records_the_provider(shop: PayShop) -> None:
    order, _ = shop.linked()
    assert shop.lifecycle.record_provider_event("fake", SETTLED) == "recorded"
    [paid] = [p for p in shop.rows.payments(order.id) if p.provider]
    assert (paid.method, paid.provider) == ("fake", "fake")


def test_an_event_method_is_one_scadbuddy_knows() -> None:
    with pytest.raises(ValueError):
        ProviderEvent.model_validate({"event_id": "x", "kind": "paid", "method": "zelle"})
