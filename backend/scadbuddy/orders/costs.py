"""An order's real cost and margin (#4251, spec 2026-10-18 §6.4; job cost spec 2026-11-05
§4, §5, #5326).

Per item, its accepted quote beside its attempts, its time and its other costs; per order,
the items' sums with the order's own entries and its shipments' cost. Nothing here is
stored: every figure is computed on read.

**Prints** (§4.1). Every attempt counts, test prints and failures included. Its grams are its
``actual_grams``, else the grams of the quote it printed from (the item's quote whose
``sliced_sha256`` is the attempt's, else its accepted slice or archive quote, #4777) times
the units it was sent (``copies``, else ``quantity``, else 1), marked ``estimated``. Those
grams are priced at that quote's own price per gram (its filament lines over its grams),
else at the price book's ``cost_per_kg`` of the item's colour slots' filaments, weighted by
the quote's grams per slot when known and equally otherwise, marked ``estimated``. Its seconds
(``actual_seconds``, else the quote's seconds times the units, ``estimated``) are priced at
the quote's ``machine_time`` line over its seconds, else at today's machine rate of the
printer it printed on (#4625; a printer merged away since by the serial it kept), else
the default printer's, else today's ``pricing_machine_rate_per_hour``,
``estimated``. A figure with no source is left out and
the attempt is ``unknown``.

**Labour** (§4.3). Each time entry's minutes at the rate it copied. An item with no entries
gets a default, ``estimated``: its accepted quote's ``labour`` line, else today's
``pricing_labour_per_item``, times its quantity. A ``custom`` item gets no default (§5: its
cost is only its entries), nor does a cancelled one, which was never made.

**Other** (§4.4). The cost entries; and on the order, its shipments' ``cost_minor``.

**Charged and margin** (§5). An item is charged its accepted quote's total, nothing once
it or its order is cancelled, and has no charged figure without an accepted quote. The
order is charged its items' plus every charge but ``tax``, once every item not cancelled
has a charged figure; a cancelled order, or one whose every item is cancelled, is charged
nothing, its charges included.
Margin is charged less cost; a ``loss`` below zero, else ``unknown`` when a part is unknown,
else ``thin`` below ``orders_thin_margin_percent``, else ``ok``.

Today's settings are in the shop's ``orders_currency``: for an order in another currency,
a part that needs one is ``unknown``. Bambuddy's own cost of an attempt's archives is shown
beside the sums and never added to them (``bambuddy/costs.py``).

The orders list's margins (§6.2, #5327) are ``order_margins``: the same ``_order_costs`` as
the order's own read, over rows read once for every order of the page, and no Bambuddy call.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal, Protocol

from pydantic import BaseModel, computed_field

from scadbuddy.orders.currency import minor_exponent
from scadbuddy.orders.integrations import NotLinkedError
from scadbuddy.orders.models import (
    MEASURED_SOURCES,
    CostEntry,
    Filament,
    Fulfilment,
    FulfilmentPurpose,
    ItemStatus,
    Money,
    Order,
    OrderItem,
    Outcome,
    Quote,
    QuoteSource,
    TimeEntry,
)
from scadbuddy.orders.pricing import GRAMS_PER_KG, SECONDS_PER_HOUR, Plate
from scadbuddy.orders.store import OrderStore

logger = logging.getLogger(__name__)

#: Grams are shown to the hundredth.
GRAMS = Decimal("0.01")
#: A margin percent is shown to a tenth.
PERCENT = Decimal("0.1")

#: An attempt's figure taken from a quote or today's settings rather than measured.
EstimatedFigure = Literal["grams", "seconds", "filament", "machine"]
#: An item's or an order's part of the cost.
CostPart = Literal["prints", "labour"]
MarginState = Literal["ok", "thin", "loss", "unknown"]


class ArchiveCosts(Protocol):
    """What the costs read of the seam (``OrderIntegrations``)."""

    @property
    def linked(self) -> bool: ...

    async def archive_cost(self, archive_id: int) -> float | None: ...


class CostRates(BaseModel):
    """Today's settings the read may fall back on, in ``shop_currency``."""

    shop_currency: str
    machine_rate_per_hour: str = "0"
    #: Each registry printer's machine rate where it has a profile (#4625, from its
    #: stored intervals): an attempt printed on one is priced at it instead of
    #: ``machine_rate_per_hour``.
    printer_rates: dict[str, str] = {}
    #: The same rates by each printer's serial: an attempt whose printer was merged away
    #: since (its row deleted) is priced at the rate of the row now holding its serial.
    serial_rates: dict[str, str] = {}
    labour_per_item: str = "0"
    thin_margin_percent: str = "15"


class AttemptCost(BaseModel):
    fulfilment_id: str
    attempt: int
    #: A ``test`` print of a revision (#5280) counts toward the cost as a production one.
    purpose: FulfilmentPurpose = "production"
    outcome: Outcome
    #: How many it made.
    quantity: int
    accepted: bool
    actual_seconds: int | None
    actual_grams: float | None
    #: The grams and seconds it is costed at: the actual ones, else its quote's.
    grams: float | None = None
    seconds: int | None = None
    #: ``grams`` at its price per gram; None with no grams or no price.
    filament_minor: Money | None
    #: ``seconds`` at the machine rate; None with no seconds or no rate.
    machine_minor: Money | None = None
    #: The known parts' sum.
    cost_minor: Money = 0
    estimated: list[EstimatedFigure] = []
    #: A part had no source and is left out of ``cost_minor``.
    unknown: bool = False
    archive_ids: list[int]
    #: Bambuddy's cost of the attempt's archives, summed, in Bambuddy's own currency;
    #: shown, never in any sum. None without Bambuddy or archives, or when unread.
    bambuddy_cost: float | None = None


class Margin(BaseModel):
    """The cost parts, charged and margin of an item or the order."""

    #: The attempts' cost, filament and machine.
    prints_minor: Money = 0
    machine_minor: Money = 0
    labour_minor: Money = 0
    #: The cost entries (and on the order, its shipments' cost too).
    other_minor: Money = 0
    cost_minor: Money = 0
    #: What was charged; None without a charged figure.
    revenue_minor: Money | None = None
    margin_minor: Money | None = None
    margin_percent: float | None = None
    margin_state: MarginState | None = None
    estimated: list[CostPart] = []
    unknown: list[CostPart] = []


class ItemCost(Margin):
    item_id: str
    title: str
    status: ItemStatus
    quantity: int
    #: The item's accepted quote; None until one is accepted.
    quote_id: str | None
    quote_source: QuoteSource | None
    #: The quote's figures for the item's whole quantity; the grams and seconds only
    #: from a slice or archive quote (#4777).
    quoted_seconds: int | None
    quoted_grams: float | None
    quoted_filament_minor: Money | None
    quoted_cost_minor: Money | None
    quoted_price_minor: Money | None
    #: Every attempt's recorded figures, failures included; None when no attempt says.
    #: Only recorded grams: an attempt whose grams were estimated is left out here.
    actual_seconds: int | None
    actual_grams: float | None
    #: Every attempt's ``filament_minor``, estimated grams included, so it can cover more
    #: attempts than ``actual_grams`` (each attempt's ``grams`` is what was priced).
    actual_filament_minor: Money | None
    failed_attempts: int
    #: As ``actual_grams`` and ``actual_filament_minor``, for the failed attempts.
    failed_grams: float | None
    failed_filament_minor: Money | None
    #: The labour is the default (no time entry yet), not entries.
    labour_default: bool = False
    attempts: list[AttemptCost]
    time_entries: list[TimeEntry] = []
    cost_entries: list[CostEntry] = []


class OrderCosts(Margin):
    """``GET /orders/{id}/costs``."""

    order_id: str
    currency: str
    items: list[ItemCost]
    #: The items' sums, of the figures known.
    quoted_seconds: int | None
    quoted_grams: float | None
    quoted_filament_minor: Money | None
    actual_seconds: int | None
    actual_grams: float | None
    actual_filament_minor: Money | None
    #: The shipments' cost, in ``other_minor`` too.
    shipping_minor: Money = 0
    #: Every charge but tax, signed (a discount is negative), in ``revenue_minor``.
    charges_minor: Money = 0
    #: The order's own entries, on no item.
    time_entries: list[TimeEntry] = []
    cost_entries: list[CostEntry] = []
    #: ``orders_thin_margin_percent`` as read.
    thin_margin_percent: str = "15"
    #: Bambuddy is linked and some archive's cost could not be read.
    bambuddy_unavailable: bool = False

    @computed_field  # type: ignore[prop-decorator]
    @property
    def minor_exponent(self) -> int:
        return minor_exponent(self.currency)


def _sum[T: (int, float, Decimal)](values: list[T | None]) -> T | None:
    known = [v for v in values if v is not None]
    return sum(known[1:], known[0]) if known else None


def _grams(value: Decimal | None) -> float | None:
    return None if value is None else float(value.quantize(GRAMS, rounding=ROUND_HALF_UP))


def _grams_sum(values: list[float | None]) -> float | None:
    """Float grams summed in decimal, so 7.5 + 21.0 is 28.5 and nothing else."""
    return _grams(_sum([None if v is None else Decimal(str(v)) for v in values]))


def _minor(value: Decimal | None) -> Money | None:
    return None if value is None else int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


class _Unit:
    """A slice or archive quote's figures for one unit."""

    def __init__(self, quote: Quote) -> None:
        plates = [Plate.model_validate(plate) for plate in quote.plates]
        self.seconds = sum(plate.print_seconds for plate in plates)
        self.grams = sum((f.used_g for plate in plates for f in plate.filaments), Decimal(0))
        #: Grams per colour slot: a stored plate's ``slot`` is the item's colour slot
        #: already (``quote.py`` ``read_plate`` takes the sliced file's 1-based slot down).
        self.slot_grams: dict[int, Decimal] = {}
        for plate in plates:
            for used in plate.filaments:
                grams = self.slot_grams.get(used.slot, Decimal(0))
                self.slot_grams[used.slot] = grams + used.used_g
        self.filament = Decimal(sum(ln.amount_minor for ln in quote.lines if ln.kind == "filament"))
        machine = [ln.amount_minor for ln in quote.lines if ln.kind == "machine_time"]
        #: Minor units a gram and a second, as the quote priced them; None unpriced.
        self.per_gram = self.filament / self.grams if self.grams > 0 else None
        self.per_second = (
            Decimal(sum(machine)) / self.seconds if machine and self.seconds > 0 else None
        )


def _unit(quote: Quote | None) -> _Unit | None:
    if quote is None or quote.source not in MEASURED_SOURCES or not quote.plates:
        return None
    return _Unit(quote)


class _Rows:
    """The orders' rows (``OrderStore.cost_rows``), each table read once for every order,
    and the price book, read only when an item needs it and then once for them all."""

    def __init__(self, store: OrderStore, orders: Sequence[Order]) -> None:
        self.store = store
        self.of = store.cost_rows(orders)
        self._filaments: dict[str, Filament] | None = None

    def filament(self, filament_id: str) -> Filament | None:
        if self._filaments is None:
            book = self.store.filaments(include_archived=True)
            self._filaments = {f.id: f for f in book}
        return self._filaments.get(filament_id)


class _Item:
    """One item's read, with what pricing it falls back on."""

    def __init__(self, rows: _Rows, order: Order, item: OrderItem, rates: CostRates) -> None:
        self.item = item
        self.order = order
        self.rates = rates
        self.exponent = minor_exponent(order.currency)
        #: Today's settings apply only in the shop's own currency (§5).
        self.today = order.currency == rates.shop_currency
        self.quotes = rows.of.quotes.get(item.id, [])
        self.accepted = next((q for q in self.quotes if q.accepted), None)
        self.attempts = rows.of.fulfilments.get(item.id, [])
        self.rows = rows
        #: The item's slots' price-book prices per kilogram, read once.
        self._prices: dict[int, Decimal] | None = None

    def today_minor(self, major: str) -> Decimal | None:
        """A setting in major units as minor units; None outside the shop's currency."""
        return Decimal(major).scaleb(self.exponent) if self.today else None

    def quote_of(self, attempt: Fulfilment) -> Quote | None:
        """The quote it printed from: by its sliced file, else the accepted one."""
        if attempt.sliced_sha256 is not None:
            for quote in self.quotes:
                if quote.sliced_sha256 == attempt.sliced_sha256:
                    return quote
        return self.accepted

    def weights_of(self, unit: _Unit | None) -> dict[int, Decimal]:
        """Grams per colour slot to weigh the price book by: the attempt's quote's, else
        the item's newest slice or archive quote's (a manual quote names none); empty unknown."""
        if unit is None or not unit.slot_grams:
            slices = [q for q in self.quotes if q.source in MEASURED_SOURCES and q.plates]
            newest = max(slices, key=lambda q: (q.created_at, q.id), default=None)
            unit = _unit(newest)
        return {} if unit is None else unit.slot_grams

    def book_per_gram(self, unit: _Unit | None) -> Decimal | None:
        """The price book's minor units a gram for the item's slots' filaments, weighted
        by the slots' share of the sliced grams when known and equally otherwise."""
        if not self.today:
            return None
        if self._prices is None:
            self._prices = {}
            for slot in self.item.colours:
                record = None if slot.filament_id is None else self.rows.filament(slot.filament_id)
                if record is not None:
                    self._prices[slot.slot] = Decimal(record.cost_per_kg)
        prices = self._prices
        if not prices:
            return None
        known = self.weights_of(unit)
        weights = {s: known.get(s, Decimal(0)) for s in prices}
        total = sum(weights.values(), Decimal(0))
        if total <= 0:
            weights = dict.fromkeys(prices, Decimal(1))
            total = Decimal(len(prices))
        per_kg = sum((prices[s] * weights[s] for s in prices), Decimal(0)) / total
        return per_kg.scaleb(self.exponent) / GRAMS_PER_KG

    def attempt(self, attempt: Fulfilment) -> tuple[AttemptCost, Decimal | None]:
        """The attempt's cost, and its filament cost unrounded (for the item's sums)."""
        quote = self.quote_of(attempt)
        unit = _unit(quote)
        units = attempt.copies or attempt.quantity or 1
        estimated: list[EstimatedFigure] = []

        grams: Decimal | None = None
        if attempt.actual_grams is not None:
            grams = Decimal(str(attempt.actual_grams))
        elif unit is not None:
            grams = unit.grams * units
            estimated.append("grams")
        seconds: int | None = attempt.actual_seconds
        if seconds is None and unit is not None:
            seconds = unit.seconds * units
            estimated.append("seconds")

        filament: Decimal | None = None
        if grams is not None:
            per_gram = unit.per_gram if unit is not None else None
            if per_gram is None:
                per_gram = self.book_per_gram(unit)
                if per_gram is not None:
                    estimated.append("filament")
            elif "grams" in estimated:
                estimated.append("filament")
            filament = None if per_gram is None else grams * per_gram

        machine: Decimal | None = None
        if seconds is not None:
            per_second = unit.per_second if unit is not None else None
            if per_second is None:
                rate = self.rates.printer_rates.get(attempt.printer or "") or (
                    self.rates.serial_rates.get(
                        attempt.printer_serial or "", self.rates.machine_rate_per_hour
                    )
                )
                hourly = self.today_minor(rate)
                if hourly is not None:
                    per_second = hourly / SECONDS_PER_HOUR
                    estimated.append("machine")
            elif "seconds" in estimated:
                estimated.append("machine")
            machine = None if per_second is None else seconds * per_second

        filament_minor, machine_minor = _minor(filament), _minor(machine)
        return (
            AttemptCost(
                fulfilment_id=attempt.id,
                purpose=attempt.purpose,
                attempt=attempt.attempt,
                outcome=attempt.outcome,
                quantity=attempt.quantity,
                accepted=attempt.accepted_at is not None,
                actual_seconds=attempt.actual_seconds,
                actual_grams=attempt.actual_grams,
                grams=_grams(grams),
                seconds=seconds,
                filament_minor=filament_minor,
                machine_minor=machine_minor,
                cost_minor=(filament_minor or 0) + (machine_minor or 0),
                estimated=estimated,
                unknown=filament_minor is None or machine_minor is None,
                archive_ids=attempt.archive_ids,
            ),
            filament,
        )

    def default_labour(self) -> Money | None:
        """§4.3's default for an item with no time entry."""
        if self.accepted is not None:
            labour = [ln.amount_minor for ln in self.accepted.lines if ln.kind == "labour"]
            if labour:
                return sum(labour) * self.item.quantity
        per_item = self.today_minor(self.rates.labour_per_item)
        return None if per_item is None else _minor(per_item * self.item.quantity)


def _entries_labour(entries: Sequence[TimeEntry]) -> tuple[Money, bool]:
    """The entries' labour, each rounded once, and whether one had no rate."""
    total, unknown = 0, False
    for entry in entries:
        if entry.rate_minor_per_hour is None:
            unknown = True
            continue
        total += _minor(Decimal(entry.minutes * entry.rate_minor_per_hour) / 60) or 0
    return total, unknown


def _margin(view: Margin, thin: Decimal) -> None:
    """``margin_*`` from ``revenue_minor`` and ``cost_minor``, in place."""
    if view.revenue_minor is None:
        return
    view.margin_minor = view.revenue_minor - view.cost_minor
    percent: Decimal | None = None
    if view.revenue_minor > 0:
        percent = Decimal(view.margin_minor) * 100 / view.revenue_minor
        view.margin_percent = float(percent.quantize(PERCENT, rounding=ROUND_HALF_UP))
    if view.margin_minor < 0:
        view.margin_state = "loss"
    elif view.unknown:
        view.margin_state = "unknown"
    elif percent is None:
        view.margin_state = None
    else:
        # The unrounded percent: 14.96 is thin under 15, though it reads 15.0.
        view.margin_state = "thin" if percent < thin else "ok"


def _item_cost(
    reader: _Item, time: Sequence[TimeEntry], other: Sequence[CostEntry], thin: Decimal
) -> ItemCost:
    """One item's cost and margin. An item of a cancelled order is charged nothing, as a
    cancelled item is, even one fulfilled before the cancel (§5)."""
    item, quote = reader.item, reader.accepted
    unit = _unit(quote)
    attempts: list[AttemptCost] = []
    priced: list[Decimal | None] = []
    for fulfilment in reader.attempts:
        cost, filament = reader.attempt(fulfilment)
        attempts.append(cost)
        priced.append(filament)
    failed = [i for i, a in enumerate(attempts) if a.outcome == "failed"]

    estimated: list[CostPart] = []
    unknown: list[CostPart] = []
    if any(a.estimated for a in attempts):
        estimated.append("prints")
    if any(a.unknown for a in attempts):
        unknown.append("prints")

    labour, labour_unknown = _entries_labour(time)
    labour_default = False
    if not time and item.kind != "custom" and item.status != "cancelled":
        default = reader.default_labour()
        labour_default = True
        if default is None:
            labour_unknown = True
        else:
            labour = default
            estimated.append("labour")
    if labour_unknown:
        unknown.append("labour")

    prints = sum(a.cost_minor for a in attempts)
    others = sum(entry.amount_minor for entry in other)
    if item.status == "cancelled" or reader.order.status == "cancelled":
        revenue: Money | None = 0
    else:
        revenue = None if quote is None else quote.total_minor
    line = ItemCost(
        item_id=item.id,
        title=item.title,
        status=item.status,
        quantity=item.quantity,
        quote_id=None if quote is None else quote.id,
        quote_source=None if quote is None else quote.source,
        quoted_seconds=None if unit is None else unit.seconds * item.quantity,
        quoted_grams=None if unit is None else _grams(unit.grams * item.quantity),
        quoted_filament_minor=None if unit is None else _minor(unit.filament * item.quantity),
        quoted_cost_minor=None if quote is None else quote.unit_cost_minor * item.quantity,
        quoted_price_minor=None if quote is None else quote.total_minor,
        actual_seconds=_sum([a.actual_seconds for a in attempts]),
        actual_grams=_grams_sum([a.actual_grams for a in attempts]),
        actual_filament_minor=_minor(_sum(priced)),
        failed_attempts=len(failed),
        failed_grams=_grams_sum([attempts[i].actual_grams for i in failed]),
        failed_filament_minor=_minor(_sum([priced[i] for i in failed])),
        attempts=attempts,
        time_entries=list(time),
        cost_entries=list(other),
        labour_default=labour_default,
        prints_minor=prints,
        machine_minor=sum(a.machine_minor or 0 for a in attempts),
        labour_minor=labour,
        other_minor=others,
        cost_minor=prints + labour + others,
        revenue_minor=revenue,
        estimated=estimated,
        unknown=unknown,
    )
    _margin(line, thin)
    return line


def _order_costs(rows: _Rows, order: Order, rates: CostRates) -> OrderCosts:
    """One order's costs from rows read for it (and maybe others): the order page's read
    and the list's margins are this one computation (§8)."""
    thin = Decimal(rates.thin_margin_percent)
    time = rows.of.time_entries.get(order.id, [])
    other = rows.of.cost_entries.get(order.id, [])
    items = [
        _item_cost(
            _Item(rows, order, item, rates),
            [e for e in time if e.order_item_id == item.id],
            [e for e in other if e.order_item_id == item.id],
            thin,
        )
        for item in rows.of.items.get(order.id, [])
    ]
    own_time = [e for e in time if e.order_item_id is None]
    own_other = [e for e in other if e.order_item_id is None]
    own_labour, own_unknown = _entries_labour(own_time)
    shipping = rows.of.shipping_minor.get(order.id, 0)
    charges = sum(c.amount_minor for c in rows.of.charges.get(order.id, []) if c.kind != "tax")
    open_items = [i for i in items if i.status != "cancelled"]
    revenue: Money | None = None
    if order.status == "cancelled" or (items and not open_items):
        # The job fell through, the order cancelled or every item: what it cost shows as a
        # loss, whatever was charged (§5).
        revenue = 0
    elif items and all(i.revenue_minor is not None for i in open_items):
        revenue = sum(i.revenue_minor or 0 for i in items) + charges

    labour = sum(i.labour_minor for i in items) + own_labour
    others = sum(i.other_minor for i in items) + sum(e.amount_minor for e in own_other)
    prints = sum(i.prints_minor for i in items)
    parts: tuple[CostPart, ...] = ("prints", "labour")
    unknown = [p for p in parts if any(p in i.unknown for i in items)]
    if own_unknown and "labour" not in unknown:
        unknown.append("labour")
    costs = OrderCosts(
        order_id=order.id,
        currency=order.currency,
        items=items,
        quoted_seconds=_sum([i.quoted_seconds for i in items]),
        quoted_grams=_grams_sum([i.quoted_grams for i in items]),
        quoted_filament_minor=_sum([i.quoted_filament_minor for i in items]),
        actual_seconds=_sum([i.actual_seconds for i in items]),
        actual_grams=_grams_sum([i.actual_grams for i in items]),
        actual_filament_minor=_sum([i.actual_filament_minor for i in items]),
        time_entries=own_time,
        cost_entries=own_other,
        thin_margin_percent=rates.thin_margin_percent,
        prints_minor=prints,
        machine_minor=sum(i.machine_minor for i in items),
        labour_minor=labour,
        other_minor=others + shipping,
        shipping_minor=shipping,
        charges_minor=charges,
        cost_minor=prints + labour + others + shipping,
        revenue_minor=revenue,
        estimated=[p for p in parts if any(p in i.estimated for i in items)],
        unknown=unknown,
    )
    _margin(costs, thin)
    return costs


def _read(store: OrderStore, order_id: str, rates: CostRates) -> OrderCosts | None:
    order = store.order(order_id)
    if order is None:
        return None
    return _order_costs(_Rows(store, [order]), order, rates)


class OrderMargin(BaseModel):
    """An order's margin as the orders list shows it (§6.2): the figures of its
    ``OrderCosts``, never computed apart from them."""

    revenue_minor: Money | None = None
    cost_minor: Money = 0
    margin_minor: Money | None = None
    margin_percent: float | None = None
    margin_state: MarginState | None = None
    estimated: list[CostPart] = []
    unknown: list[CostPart] = []


def order_margins(
    store: OrderStore, orders: Sequence[Order], rates: CostRates
) -> dict[str, OrderMargin]:
    """Each order's margin by id, from one read of every order's rows (§8: Postgres
    only, no Bambuddy call). Blocking: run it off the event loop."""
    rows = _Rows(store, orders)
    margins: dict[str, OrderMargin] = {}
    for order in orders:
        costs = _order_costs(rows, order, rates)
        margins[order.id] = OrderMargin.model_validate(costs.model_dump(include=_MARGIN_FIELDS))
    return margins


_MARGIN_FIELDS = set(OrderMargin.model_fields)


async def _bambuddy(integrations: ArchiveCosts, items: list[ItemCost]) -> bool:
    """Each attempt's Bambuddy cost filled in; whether some archive could not be read."""
    archive_ids = sorted({a for item in items for at in item.attempts for a in at.archive_ids})
    if not integrations.linked or not archive_ids:
        return False
    answers = await asyncio.gather(
        *(integrations.archive_cost(a) for a in archive_ids), return_exceptions=True
    )
    costs: dict[int, float | None] = {}
    unavailable = False
    for archive_id, answer in zip(archive_ids, answers, strict=True):
        if isinstance(answer, NotLinkedError):
            return False
        if isinstance(answer, BaseException):
            logger.warning("could not read archive %s's cost", archive_id, exc_info=answer)
            unavailable = True
        else:
            costs[archive_id] = answer
    for item in items:
        for attempt in item.attempts:
            if attempt.archive_ids and all(a in costs for a in attempt.archive_ids):
                attempt.bambuddy_cost = _sum([costs[a] for a in attempt.archive_ids])
    return unavailable


async def order_costs(
    store: OrderStore, integrations: ArchiveCosts, order_id: str, rates: CostRates
) -> OrderCosts | None:
    """The order's real cost and margin; None for an unknown order."""
    costs = await asyncio.to_thread(_read, store, order_id, rates)
    if costs is None:
        return None
    costs.bambuddy_unavailable = await _bambuddy(integrations, costs.items)
    return costs
