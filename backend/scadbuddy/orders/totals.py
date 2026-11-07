"""The order's totals (§3.4): what the order page shows and the lifecycle reads, so a
balance request and the page agree on what is due (#4957)."""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, Field

from scadbuddy.orders.models import Charge, Money, OrderItem, Payment, Quote
from scadbuddy.orders.store import Conn, OrderStore


class OrderTotals(BaseModel):
    """§3.4: the items' prices, the charges on top, what was paid. Every item not
    cancelled counts at its accepted quote's total or, before one is accepted (a draft
    or quoted order), at its newest quote's unit price times its quantity now, so a
    draft adds up as it is entered; ``pending_minor`` is the part of the subtotal no
    accepted quote stands behind yet, and ``unpriced_items`` the items with no quote."""

    subtotal_minor: Money
    #: The part of ``subtotal_minor`` from quotes not yet accepted.
    pending_minor: Money = 0
    #: The items not cancelled with no quote at all, in no sum.
    unpriced_items: list[str] = Field(default_factory=list)
    charges_minor: Money
    total_minor: Money
    paid_minor: Money
    balance_minor: Money


def item_price(item: OrderItem, quotes: Sequence[Quote]) -> tuple[Money, bool] | None:
    """The item's price and whether an accepted quote stands behind it: the accepted
    quote's total, else the newest quote's unit price times the item's quantity now;
    None with no quote."""
    accepted = next((quote for quote in quotes if quote.accepted), None)
    if accepted is not None:
        return accepted.total_minor, True
    newest = max(quotes, key=lambda quote: (quote.created_at, quote.id), default=None)
    if newest is None:
        return None
    return newest.unit_price_minor * item.quantity, False


def order_totals(
    items: Sequence[tuple[OrderItem, Sequence[Quote]]],
    charges: Sequence[Charge],
    payments: Sequence[Payment],
) -> OrderTotals:
    subtotal = pending = 0
    unpriced: list[str] = []
    for item, quotes in items:
        if item.status == "cancelled":
            continue
        priced = item_price(item, quotes)
        if priced is None:
            unpriced.append(item.id)
            continue
        amount, accepted = priced
        subtotal += amount
        if not accepted:
            pending += amount
    charged = sum(charge.amount_minor for charge in charges)
    paid = sum(payment.amount_minor for payment in payments)
    return OrderTotals(
        subtotal_minor=subtotal,
        pending_minor=pending,
        unpriced_items=unpriced,
        charges_minor=charged,
        total_minor=subtotal + charged,
        paid_minor=paid,
        balance_minor=subtotal + charged - paid,
    )


def totals_of(store: OrderStore, conn: Conn, order_id: str) -> OrderTotals:
    """The order's totals as its rows stand in ``conn``'s transaction."""
    items = [(item, store.quotes(item.id, conn=conn)) for item in store.items(order_id, conn=conn)]
    return order_totals(
        items, store.charges(order_id, conn=conn), store.payments(order_id, conn=conn)
    )
