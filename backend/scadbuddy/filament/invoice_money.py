"""Invoice arithmetic (#6266, epic #6260): tax, shipping and cost per kg.

Pure functions on `Decimal`, never float. Every result has four decimal places, rounded
half up, to match the ``numeric(12,4)`` money columns.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

RECONCILE_TOLERANCE = Decimal("0.05")

_UNIT = Decimal("0.0001")


class InvoiceMathError(ValueError):
    """The numbers of an invoice cannot be worked out."""


class MixedLineTaxError(ValueError):
    """Some lines of an invoice give a tax and others do not."""


def _q(value: Decimal) -> Decimal:
    return value.quantize(_UNIT, rounding=ROUND_HALF_UP)


def spread(total: Decimal, weights: Sequence[Decimal]) -> list[Decimal]:
    """Share ``total`` by ``weights`` (largest remainder); the shares sum to ``total``.

    All-zero weights share equally. Ties go to the lowest index.
    """
    if total < 0:
        raise InvoiceMathError("cannot share a negative total")
    if not weights:
        return []
    total = _q(total)
    weight_sum = sum(weights, Decimal(0))
    if weight_sum == 0:
        weights = [Decimal(1)] * len(weights)
        weight_sum = Decimal(len(weights))
    exact = [total * w / weight_sum for w in weights]
    floors = [e.quantize(_UNIT, rounding=ROUND_DOWN) for e in exact]
    leftover = int((total - sum(floors, Decimal(0))) / _UNIT)
    order = sorted(range(len(exact)), key=lambda i: (-(exact[i] - floors[i]), i))
    for i in order[:leftover]:
        floors[i] += _UNIT
    return floors


def tax_per_line(
    subtotals: Sequence[Decimal],
    given: Sequence[Decimal | None],
    invoice_tax: Decimal,
) -> tuple[list[Decimal], str]:
    """The tax of each line and the basis text that says where it came from."""
    set_values = [g for g in given if g is not None]
    if set_values and len(set_values) == len(given):
        return [_q(g) for g in set_values], "line"
    if set_values:
        raise MixedLineTaxError("some lines give a tax and others do not")
    if invoice_tax > 0:
        subtotal_sum = sum(subtotals, Decimal(0))
        if subtotal_sum == 0:
            raise InvoiceMathError("tax on a receipt whose lines cost nothing")
        rate = (invoice_tax / subtotal_sum * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
        return spread(invoice_tax, subtotals), f"receipt-wide {rate}%"
    return [_q(Decimal(0)) for _ in subtotals], "none"


def allocate(line_totals: Sequence[Decimal], shipping: Decimal) -> list[Decimal]:
    """Each line total plus its share of ``shipping``, shared by value."""
    shares = spread(shipping, line_totals)
    return [_q(t + s) for t, s in zip(line_totals, shares, strict=True)]


def spool_cost_per_kg(allocated_cost: Decimal, quantity: int, label_weight_g: int) -> Decimal:
    return _q(allocated_cost / quantity / (Decimal(label_weight_g) / 1000))


def unit_cost(allocated_cost: Decimal, quantity: int) -> Decimal:
    """What one spool of a line cost: its ``allocated_cost`` over ``quantity``."""
    return _q(allocated_cost / quantity)


def price_per_pack(line_total: Decimal, quantity: int, pack_size: int) -> Decimal:
    """The price of one pack: the line total over ``quantity`` units, times ``pack_size``."""
    return _q(line_total / quantity * pack_size)


def price_per_kg(price: Decimal, pack_size: int, label_weight_g: int) -> Decimal:
    return _q(price / pack_size / (Decimal(label_weight_g) / 1000))


def difference(total: Decimal, line_totals: Sequence[Decimal], shipping: Decimal) -> Decimal:
    """What the invoice total is above the lines plus shipping."""
    return _q(total - (sum(line_totals, Decimal(0)) + shipping))
