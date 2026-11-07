"""The order's totals (§3.4), moved out of the route into ``orders/totals.py`` so the
lifecycle reads the same balance the order page shows (#4957)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from scadbuddy.orders.models import Charge, OrderItem, Payment, Quote
from scadbuddy.orders.totals import order_totals

AT = datetime(2026, 11, 7, tzinfo=UTC)


def _item(item_id: str, quantity: int = 1, status: str = "quoted") -> OrderItem:
    return OrderItem.model_construct(id=item_id, quantity=quantity, status=status)


def _quote(quote_id: str, *, accepted: bool, unit: int, total: int, age: int = 0) -> Quote:
    return Quote.model_construct(
        id=quote_id,
        accepted=accepted,
        unit_price_minor=unit,
        total_minor=total,
        created_at=AT - timedelta(minutes=age),
    )


def test_order_totals_match_the_old_route() -> None:
    accepted = _item("a")
    quoted = _item("q", quantity=2)
    unpriced = _item("u")
    cancelled = _item("c", status="cancelled")
    items = [
        (accepted, [_quote("a1", accepted=True, unit=1000, total=1000)]),
        (
            quoted,
            [
                _quote("q1", accepted=False, unit=900, total=900, age=5),
                _quote("q2", accepted=False, unit=300, total=300),
            ],
        ),
        (unpriced, []),
        (cancelled, [_quote("c1", accepted=True, unit=50, total=50)]),
    ]
    charges = [Charge.model_construct(amount_minor=500)]
    payments = [Payment.model_construct(amount_minor=200)]
    t = order_totals(items, charges, payments)
    assert (
        t.subtotal_minor,
        t.pending_minor,
        t.charges_minor,
        t.total_minor,
        t.paid_minor,
        t.balance_minor,
    ) == (1600, 600, 500, 2100, 200, 1900)
    assert t.unpriced_items == [unpriced.id]
