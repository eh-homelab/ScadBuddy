"""The catalog's cost calculator (#7152, plan `2026-11-23-catalog-cost-stats.md`): what a
spool's price, tax, shipping and net weight come to per kg, in the shop's currency.

Pure, on `Decimal`, with the invoice's rounding (`invoice_money`, four places, half up):
`cost_per_kg` is what an invoice line gives its spools (price, tax and shipping in,
R-7152-2), `price_per_kg` what a price point is (tax in, shipping out, spec §5.8).
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel

from scadbuddy.filament.invoice_money import spool_cost_per_kg
from scadbuddy.filament.models import LabelWeight, _In
from scadbuddy.filament.purchasing_models import Money, Quantity

#: `numeric(12,4)`: a cost per kg must be under this (`models.Cost`).
COST_LIMIT = Decimal(10) ** 8


class CostTooHighError(ValueError):
    """The cost per kg is past what a type's ``cost_per_kg`` holds."""


class CostQuery(_In):
    """What was paid for ``quantity`` spools of ``net_weight_g`` grams of filament each."""

    price: Money
    tax: Money = Decimal(0)
    shipping: Money = Decimal(0)
    quantity: Quantity = 1
    net_weight_g: LabelWeight


class CostCalculation(BaseModel):
    """The sums, in ``currency`` (the shop's ``orders_currency``, R-7152-3)."""

    currency: str
    price: Decimal
    tax: Decimal
    shipping: Decimal
    quantity: int
    net_weight_g: int
    #: Price, tax and shipping.
    total: Decimal
    #: ``total`` per kg of filament: what "Use as default cost" puts in a type's cost.
    cost_per_kg: Decimal
    #: Price and tax per kg, shipping out: a price point's basis.
    price_per_kg: Decimal


def calculate(query: CostQuery, currency: str) -> CostCalculation:
    """The cost per kg of ``query``; `CostTooHighError` past `COST_LIMIT`."""
    total = query.price + query.tax + query.shipping
    cost = spool_cost_per_kg(total, query.quantity, query.net_weight_g)
    if cost >= COST_LIMIT:
        raise CostTooHighError("the cost per kg is more than a cost per kg can hold (10^8)")
    return CostCalculation(
        currency=currency,
        price=query.price,
        tax=query.tax,
        shipping=query.shipping,
        quantity=query.quantity,
        net_weight_g=query.net_weight_g,
        total=total,
        cost_per_kg=cost,
        price_per_kg=spool_cost_per_kg(query.price + query.tax, query.quantity, query.net_weight_g),
    )
