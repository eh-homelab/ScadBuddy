"""The profit report by model (job cost spec 2026-11-05 §6.4, §9; #5329).

The report covers the orders accepted in a period (``accepted_at`` at or after ``since``
and before ``until``), cancelled ones too: what a cancelled job cost is a real loss (§5).
Each order is costed by ``costs.py`` ``_order_costs`` over ``OrderStore.cost_rows``, the
same computation as its own ``GET /orders/{id}/costs``; nothing here is a second formula.

Rows are per currency, never summed across currencies, and per product: an item with a
model ``slug`` is in that model's row, else an item with a ``sku`` in that SKU's row, else
in the ``custom`` row ("Custom, no SKU"). A row's figures are its items' figures, summed:
charged (``revenue_minor``), real cost, and waste, the cost of its test and failed
attempts. An order's own entries, its shipments' cost and its charges are on no item, so
they are the ``order`` row: the order's figures less its items', over the orders that have
any. No list filter matches those orders, so the page gives that row no link. A
currency's ``total`` is the sum of its orders' own figures, so with every order charged
its rows add up to it.

Margins are not stored (§9). The report reads the period's orders in batches of
``REPORT_BATCH``, newest accepted first, and computes each batch's costs; it stops after
``MAX_REPORT_ORDERS`` orders. Then ``complete`` is false and ``read_from`` is the oldest
``accepted_at`` read: the report covers the orders accepted from then.

Postgres only (§8): no Bambuddy call.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from pydantic import BaseModel, computed_field

from scadbuddy.orders.costs import (
    PERCENT,
    CostPart,
    CostRates,
    ItemCost,
    Margin,
    MarginState,
    OrderCosts,
    _margin,
    _order_costs,
    _Rows,
)
from scadbuddy.orders.currency import minor_exponent
from scadbuddy.orders.models import Money, OrderItem, Product
from scadbuddy.orders.store import OrderStore

#: The period's default length (§6.4).
DEFAULT_DAYS = 90
#: Orders read and costed at a time.
REPORT_BATCH = 100
#: The report stops after this many orders and says so (``complete``).
MAX_REPORT_ORDERS = 2000

#: A row's kind: a product (``model``, ``sku``, ``custom``), the orders' own figures
#: (``order``), or a currency's sum (``total``).
RowGroup = Literal["model", "sku", "custom", "order", "total"]
ReportGroup = Literal["model"]


class ProfitRow(BaseModel):
    group: RowGroup
    #: The slug or SKU; None for the other groups.
    key: str | None = None
    #: The orders list's ``product`` filter for the row; None for ``order`` and ``total``.
    product: str | None = None
    currency: str
    #: The orders behind the row.
    orders: int = 0
    #: The items' quantities, cancelled items left out.
    units: int = 0
    #: What was charged: the charged figures known.
    revenue_minor: Money = 0
    #: Items not cancelled with no charged figure (no accepted quote), in no sum.
    uncharged_items: int = 0
    cost_minor: Money = 0
    margin_minor: Money | None = None
    margin_percent: float | None = None
    margin_state: MarginState | None = None
    #: The cost of test and failed attempts, and its share of ``cost_minor``.
    waste_minor: Money = 0
    waste_percent: float | None = None
    #: A part of the cost is estimated (a quote's grams, a default labour).
    estimated: bool = False
    #: Parts with no source, left out of ``cost_minor``.
    unknown: list[CostPart] = []

    @computed_field  # type: ignore[prop-decorator]
    @property
    def minor_exponent(self) -> int:
        return minor_exponent(self.currency)


class ProfitReport(BaseModel):
    """``GET /orders/reports/profit``."""

    group: ReportGroup = "model"
    #: The period: orders accepted at or after ``period_from`` and before ``period_to``.
    period_from: datetime
    period_to: datetime
    thin_margin_percent: str
    #: Per currency (alphabetical), the products by charged figure, then the ``order``
    #: row, then the ``total``.
    rows: list[ProfitRow]
    orders_read: int
    #: False when the report stopped at ``MAX_REPORT_ORDERS``.
    complete: bool = True
    #: With ``complete`` false, the oldest ``accepted_at`` read: the report covers the
    #: orders accepted from then to ``period_to``.
    read_from: datetime | None = None
    max_orders: int = MAX_REPORT_ORDERS


def product_of(item: OrderItem) -> Product:
    """The row an item is in (§6.4)."""
    if item.reference is not None:
        return Product(kind="model", key=item.reference.slug)
    if item.sku:
        return Product(kind="sku", key=item.sku)
    return Product(kind="custom")


def _waste(item: ItemCost) -> Money:
    return sum(a.cost_minor for a in item.attempts if a.purpose == "test" or a.outcome == "failed")


class _Sum:
    """A row's running sums."""

    def __init__(self) -> None:
        self.orders: set[str] = set()
        self.units = 0
        self.revenue = 0
        self.charged = False
        self.uncharged = 0
        self.cost = 0
        self.waste = 0
        self.estimated = False
        self.unknown: list[CostPart] = []

    def add(
        self,
        order_id: str,
        *,
        cost: Money,
        revenue: Money | None,
        units: int = 0,
        uncharged: int = 0,
        waste: Money = 0,
        estimated: bool = False,
        unknown: list[CostPart] | None = None,
    ) -> None:
        self.orders.add(order_id)
        self.units += units
        self.cost += cost
        self.waste += waste
        self.uncharged += uncharged
        if revenue is not None:
            self.revenue += revenue
            self.charged = True
        self.estimated = self.estimated or estimated
        for part in unknown or []:
            if part not in self.unknown:
                self.unknown.append(part)

    def row(
        self, group: RowGroup, currency: str, thin: Decimal, product: Product | None
    ) -> ProfitRow:
        view = Margin(
            cost_minor=self.cost,
            revenue_minor=self.revenue if self.charged else None,
            unknown=self.unknown,
        )
        _margin(view, thin)
        waste = None
        if self.cost > 0:
            share = Decimal(self.waste) * 100 / self.cost
            waste = float(share.quantize(PERCENT, rounding=ROUND_HALF_UP))
        return ProfitRow(
            group=group,
            key=None if product is None else product.key,
            product=None if product is None else str(product),
            currency=currency,
            orders=len(self.orders),
            units=self.units,
            revenue_minor=self.revenue,
            uncharged_items=self.uncharged,
            cost_minor=self.cost,
            margin_minor=view.margin_minor,
            margin_percent=view.margin_percent,
            margin_state=view.margin_state,
            waste_minor=self.waste,
            waste_percent=waste,
            estimated=self.estimated,
            unknown=list(self.unknown),
        )


class _Currency:
    """One currency's rows."""

    def __init__(self) -> None:
        self.products: dict[str, tuple[Product, _Sum]] = {}
        self.order = _Sum()
        self.total = _Sum()

    def add(self, costs: OrderCosts, items: dict[str, OrderItem]) -> None:
        order_id = costs.order_id
        for line in costs.items:
            product = product_of(items[line.item_id])
            _, sums = self.products.setdefault(str(product), (product, _Sum()))
            open_ = line.status != "cancelled"
            sums.add(
                order_id,
                cost=line.cost_minor,
                revenue=line.revenue_minor,
                units=line.quantity if open_ else 0,
                uncharged=1 if open_ and line.revenue_minor is None else 0,
                waste=_waste(line),
                estimated=bool(line.estimated),
                unknown=line.unknown,
            )
        # The order's own figures: what its read has beyond its items'.
        items_revenue = sum(line.revenue_minor or 0 for line in costs.items)
        own_unknown: list[CostPart] = [
            part for part in costs.unknown if not any(part in line.unknown for line in costs.items)
        ]
        own_cost = costs.cost_minor - sum(line.cost_minor for line in costs.items)
        own_revenue = None if costs.revenue_minor is None else costs.revenue_minor - items_revenue
        # Only an order with figures of its own is behind the ``order`` row.
        if own_cost != 0 or own_revenue or own_unknown:
            self.order.add(order_id, cost=own_cost, revenue=own_revenue, unknown=own_unknown)
        self.total.add(
            order_id,
            cost=costs.cost_minor,
            revenue=costs.revenue_minor,
            units=sum(line.quantity for line in costs.items if line.status != "cancelled"),
            uncharged=sum(
                1
                for line in costs.items
                if line.status != "cancelled" and line.revenue_minor is None
            ),
            waste=sum(_waste(line) for line in costs.items),
            estimated=bool(costs.estimated),
            unknown=costs.unknown,
        )

    def rows(self, currency: str, thin: Decimal) -> list[ProfitRow]:
        products = [
            sums.row(product.kind, currency, thin, product)
            for product, sums in self.products.values()
        ]
        products.sort(key=lambda row: (-row.revenue_minor, row.group, row.key or ""))
        own = self.order
        # The orders' own row only when some order has figures of its own.
        shown = bool(own.orders)
        return [
            *products,
            *([own.row("order", currency, thin, None)] if shown else []),
            self.total.row("total", currency, thin, None),
        ]


def default_period(now: datetime) -> tuple[datetime, datetime]:
    """The last ``DEFAULT_DAYS`` days to ``now``."""
    return now - timedelta(days=DEFAULT_DAYS), now


def profit_report(
    store: OrderStore,
    rates: CostRates,
    *,
    since: datetime,
    until: datetime,
    max_orders: int = MAX_REPORT_ORDERS,
    batch: int = REPORT_BATCH,
) -> ProfitReport:
    """The report for the orders accepted in ``[since, until)``. Blocking: run it off the
    event loop."""
    thin = Decimal(rates.thin_margin_percent)
    currencies: dict[str, _Currency] = {}
    read = 0
    after: tuple[datetime, str] | None = None
    oldest: datetime | None = None
    complete = True
    while read < max_orders:
        want = min(batch, max_orders - read)
        orders = store.accepted_orders(since, until, after, want)
        if orders:
            rows = _Rows(store, orders)
            for order in orders:
                costs = _order_costs(rows, order, rates)
                items = {item.id: item for item in rows.of.items.get(order.id, [])}
                currencies.setdefault(order.currency, _Currency()).add(costs, items)
            read += len(orders)
            last = orders[-1]
            assert last.accepted_at is not None
            oldest = last.accepted_at
            after = (last.accepted_at, last.id)
        if len(orders) < want:
            break
    else:
        # At the cap: one more read tells a whole period from a cut one.
        complete = not store.accepted_orders(since, until, after, 1)
    return ProfitReport(
        period_from=since,
        period_to=until,
        thin_margin_percent=rates.thin_margin_percent,
        rows=[
            row
            for currency in sorted(currencies)
            for row in currencies[currency].rows(currency, thin)
        ],
        orders_read=read,
        complete=complete,
        read_from=None if complete else oldest,
        max_orders=max_orders,
    )
