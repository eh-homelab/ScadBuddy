"""A model's real cost per unit from its past jobs, for the quote warning (job cost spec
2026-11-05 §6.3, #5328).

The history is the items of one ``slug`` that are ``fulfilled`` on an order that is not
cancelled, in one currency: the newest ``HISTORY_JOBS`` whose last accepted attempt is no
older than ``HISTORY_DAYS``. Each item's real cost is its ``ItemCost.cost_minor`` from
``costs.py`` ``_order_costs`` over ``OrderStore.cost_rows``, so it equals the order page's
read. The order's own entries and shipping are not on any item and stay out.

The **unit cost** is the items' summed cost over their summed quantity. The **main reason**
is the cost part that most exceeded what the items' own accepted quotes priced for it:
filament and machine time of the production attempts that did not fail (against the
quote's ``filament`` and ``machine_time`` lines), labour (against its ``labour`` and
``setup`` lines), and in full the parts a quote never prices: failed attempts, test
prints and cost entries. A job whose accepted quote has no per-part lines (a ``manual``
quote, a quote with a ``manual`` line, or no accepted quote) priced filament, machine time
and labour together, if at all, so those three parts are left out of its comparison: only
its failed prints, test prints and other costs can be its reason. Its price is never split
across parts. The **suggested unit price** is the unit cost marked up by
``pricing_markup_percent``; with no markup, the lowest price whose margin is not thin.

Advice only: nothing is stored and nothing is refused. With no history every figure is
None and nothing warns.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from typing import Literal

from pydantic import BaseModel, computed_field

from scadbuddy.orders.costs import (
    PERCENT,
    CostRates,
    ItemCost,
    _order_costs,
    _Rows,
)
from scadbuddy.orders.currency import minor_exponent
from scadbuddy.orders.models import Money, Order, Quote, QuoteLineKind
from scadbuddy.orders.store import OrderStore

#: §6.3: the last 10 jobs within 365 days.
HISTORY_JOBS = 10
HISTORY_DAYS = 365

#: A part of a job's cost, as the main reason names it.
CostReason = Literal["filament", "machine", "labour", "failed_prints", "test_prints", "other"]
#: The order ties are broken in: the first part listed wins.
REASONS: tuple[CostReason, ...] = (
    "failed_prints",
    "test_prints",
    "filament",
    "machine",
    "labour",
    "other",
)
#: The quote lines that price each part; a part with none is never priced.
_PRICED_BY: dict[CostReason, tuple[QuoteLineKind, ...]] = {
    "filament": ("filament",),
    "machine": ("machine_time",),
    "labour": ("labour", "setup"),
}


class UnitCostHistory(BaseModel):
    """``GET /orders/history/unit-cost``."""

    slug: str
    currency: str
    #: How many jobs (items) the figures are from; 0 with no history.
    jobs: int = 0
    units: int = 0
    #: The jobs' real cost over their units; None with no history.
    unit_cost_minor: Money | None = None
    #: A job's cost has an estimated or unknown part (an unknown part is left out).
    estimated: bool = False
    #: The part that most exceeded what the jobs' quotes priced for it; None when none did.
    main_reason: CostReason | None = None
    #: How much it exceeded them by, per unit.
    main_reason_minor: Money | None = None
    #: The settings the figures use.
    markup_percent: str
    thin_margin_percent: str
    suggested_unit_price_minor: Money | None = None
    #: The price asked about, its margin per unit against ``unit_cost_minor`` as a percent
    #: of the price, and whether that margin is below the thin threshold. A price above
    #: the cost can still warn, with a thin margin: ``below_cost`` tells the two apart.
    unit_price_minor: Money | None = None
    margin_percent: float | None = None
    warning: bool = False
    #: The price is below ``unit_cost_minor`` itself: a loss, not only a thin margin.
    below_cost: bool = False

    @computed_field  # type: ignore[prop-decorator]
    @property
    def minor_exponent(self) -> int:
        return minor_exponent(self.currency)


def _per_part(quote: Quote | None) -> bool:
    """The quote prices filament, machine time and labour each on lines of their own."""
    if quote is None or quote.source == "manual":
        return False
    return all(line.kind != "manual" for line in quote.lines)


def _quoted(quote: Quote | None, part: CostReason, quantity: int) -> int:
    kinds = _PRICED_BY.get(part, ())
    if quote is None or not kinds:
        return 0
    return sum(line.amount_minor for line in quote.lines if line.kind in kinds) * quantity


def _parts(item: ItemCost) -> dict[CostReason, int]:
    """The item's real cost by part; the parts add up to ``cost_minor``."""
    parts: dict[CostReason, int] = dict.fromkeys(REASONS, 0)
    for attempt in item.attempts:
        if attempt.purpose == "test":
            parts["test_prints"] += attempt.cost_minor
        elif attempt.outcome == "failed":
            parts["failed_prints"] += attempt.cost_minor
        else:
            parts["filament"] += attempt.filament_minor or 0
            parts["machine"] += attempt.machine_minor or 0
    parts["labour"] = item.labour_minor
    parts["other"] = item.other_minor
    return parts


def _per_unit(total: int, units: int) -> Money:
    return int((Decimal(total) / units).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def suggested_price(unit_cost: Money, markup_percent: str, thin_percent: str) -> Money:
    """The unit cost marked up by ``markup_percent``, rounded half up as ``pricing.py``
    rounds the markup line; with no markup, the lowest whole price whose margin is at
    least ``thin_percent`` of it, so the suggestion is never thin itself."""
    markup = Decimal(markup_percent)
    if markup > 0:
        price = Decimal(unit_cost) * (100 + markup) / 100
        return int(price.quantize(Decimal(1), rounding=ROUND_HALF_UP))
    thin = Decimal(thin_percent)
    if thin <= 0 or thin >= 100:
        return unit_cost
    price = Decimal(unit_cost) * 100 / (100 - thin)
    return int(price.quantize(Decimal(1), rounding=ROUND_CEILING))


def warns(unit_cost: Money, unit_price: Money, thin_percent: str) -> tuple[float | None, bool]:
    """The price's margin percent against the unit cost, and whether it is below the thin
    threshold (§5): the unrounded percent is judged, as ``costs.py`` ``_margin`` does."""
    if unit_price <= 0:
        return None, unit_cost > 0
    percent = Decimal(unit_price - unit_cost) * 100 / unit_price
    rounded = float(percent.quantize(PERCENT, rounding=ROUND_HALF_UP))
    return rounded, percent < Decimal(thin_percent)


def _history(
    rows: _Rows, jobs: Sequence[tuple[Order, str]], rates: CostRates
) -> list[tuple[ItemCost, Quote | None]]:
    costs = {order.id: _order_costs(rows, order, rates) for order, _ in jobs}
    found: list[tuple[ItemCost, Quote | None]] = []
    for order, item_id in jobs:
        item = next(i for i in costs[order.id].items if i.item_id == item_id)
        quotes = rows.of.quotes.get(item_id, [])
        found.append((item, next((q for q in quotes if q.accepted), None)))
    return found


def unit_cost_history(
    store: OrderStore,
    slug: str,
    rates: CostRates,
    *,
    markup_percent: str,
    now: datetime,
    currency: str | None = None,
    unit_price_minor: Money | None = None,
) -> UnitCostHistory:
    """The model's history in ``currency`` (the shop's by default), and the warning for
    ``unit_price_minor`` when one is given. Blocking: run it off the event loop. Postgres
    only (§8): no Bambuddy call."""
    currency = currency or rates.shop_currency
    answer = UnitCostHistory(
        slug=slug,
        currency=currency,
        markup_percent=markup_percent,
        thin_margin_percent=rates.thin_margin_percent,
        unit_price_minor=unit_price_minor,
    )
    jobs = store.fulfilled_items(slug, currency, now - timedelta(days=HISTORY_DAYS), HISTORY_JOBS)
    if not jobs:
        return answer
    history = _history(_Rows(store, [order for order, _ in jobs]), jobs, rates)
    units = sum(item.quantity for item, _ in history)
    cost = sum(item.cost_minor for item, _ in history)
    over: dict[CostReason, int] = dict.fromkeys(REASONS, 0)
    for item, quote in history:
        priced = _per_part(quote)
        for part, amount in _parts(item).items():
            if part in _PRICED_BY and not priced:
                continue
            over[part] += amount - _quoted(quote, part, item.quantity)
    reason = max(REASONS, key=lambda part: over[part])

    answer.jobs = len(history)
    answer.units = units
    answer.unit_cost_minor = _per_unit(cost, units)
    answer.estimated = any(item.estimated or item.unknown for item, _ in history)
    if over[reason] > 0:
        answer.main_reason = reason
        answer.main_reason_minor = _per_unit(over[reason], units)
    answer.suggested_unit_price_minor = suggested_price(
        answer.unit_cost_minor, markup_percent, rates.thin_margin_percent
    )
    if unit_price_minor is not None:
        answer.margin_percent, answer.warning = warns(
            answer.unit_cost_minor, unit_price_minor, rates.thin_margin_percent
        )
        answer.below_cost = unit_price_minor < answer.unit_cost_minor
    return answer
