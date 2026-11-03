"""An order's real cost (#4251, spec 2026-10-18 §6.4, plan 2026-11-03 Rulings 1 and 2).

Per item, its accepted quote beside its attempts: what the quote said one unit takes
(a slice quote's plates: seconds and grams) and costs in filament (its ``filament``
lines), times the item's quantity, against what every attempt used, failures included,
since a failed print used filament too. An attempt's grams are priced at the quote's own
price per gram (its filament lines over its grams), never at the price book in force
today, which is not what the shop quoted. A ``manual`` quote prices no grams, so its
item has actual figures and no filament cost.

Bambuddy's own cost of an attempt's archives is shown beside the sums and never added to
them, corrected for suspect readings as the print page corrects it
(``bambuddy/costs.py``). Nothing here is stored.
"""

from __future__ import annotations

import asyncio
import logging
from decimal import ROUND_HALF_UP, Decimal
from typing import Protocol

from pydantic import BaseModel, computed_field

from scadbuddy.orders.currency import minor_exponent
from scadbuddy.orders.integrations import NotLinkedError
from scadbuddy.orders.models import (
    ItemStatus,
    Money,
    Outcome,
    Quote,
    QuoteSource,
)
from scadbuddy.orders.pricing import Plate
from scadbuddy.orders.store import OrderStore

logger = logging.getLogger(__name__)

#: Grams are shown to the hundredth.
GRAMS = Decimal("0.01")


class ArchiveCosts(Protocol):
    """What the costs read of the seam (``OrderIntegrations``)."""

    @property
    def linked(self) -> bool: ...

    async def archive_cost(self, archive_id: int) -> float | None: ...


class AttemptCost(BaseModel):
    fulfilment_id: str
    attempt: int
    outcome: Outcome
    #: How many it made.
    quantity: int
    accepted: bool
    actual_seconds: int | None
    actual_grams: float | None
    #: ``actual_grams`` at the quote's price per gram; None without either.
    filament_minor: Money | None
    archive_ids: list[int]
    #: Bambuddy's cost of the attempt's archives, summed, in Bambuddy's own currency;
    #: shown, never in any sum. None without Bambuddy or archives, or when unread.
    bambuddy_cost: float | None = None


class ItemCost(BaseModel):
    item_id: str
    title: str
    status: ItemStatus
    quantity: int
    #: The item's accepted quote; None until one is accepted.
    quote_id: str | None
    quote_source: QuoteSource | None
    #: The quote's figures for the item's whole quantity; the grams and seconds only
    #: from a slice quote.
    quoted_seconds: int | None
    quoted_grams: float | None
    quoted_filament_minor: Money | None
    quoted_cost_minor: Money | None
    quoted_price_minor: Money | None
    #: Every attempt's, failures included; None when no attempt says.
    actual_seconds: int | None
    actual_grams: float | None
    actual_filament_minor: Money | None
    failed_attempts: int
    failed_grams: float | None
    failed_filament_minor: Money | None
    attempts: list[AttemptCost]


class OrderCosts(BaseModel):
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


def _per_unit(quote: Quote) -> tuple[int, Decimal, Decimal] | None:
    """A slice quote's seconds, grams and filament minor for one unit."""
    if quote.source != "slice" or not quote.plates:
        return None
    plates = [Plate.model_validate(plate) for plate in quote.plates]
    seconds = sum(plate.print_seconds for plate in plates)
    grams = sum((f.used_g for plate in plates for f in plate.filaments), Decimal(0))
    filament = sum(line.amount_minor for line in quote.lines if line.kind == "filament")
    return seconds, grams, Decimal(filament)


def _item_costs(store: OrderStore, order_id: str) -> tuple[str, list[ItemCost]] | None:
    order = store.order(order_id)
    if order is None:
        return None
    lines: list[ItemCost] = []
    for item in store.items(order.id):
        quote = next((q for q in store.quotes(item.id) if q.accepted), None)
        unit = _per_unit(quote) if quote is not None else None
        rate = unit[2] / unit[1] if unit is not None and unit[1] > 0 else None
        attempts: list[AttemptCost] = []
        priced: list[Decimal | None] = []
        for attempt in store.fulfilments(item.id):
            grams = None if attempt.actual_grams is None else Decimal(str(attempt.actual_grams))
            filament = None if grams is None or rate is None else grams * rate
            priced.append(filament)
            attempts.append(
                AttemptCost(
                    fulfilment_id=attempt.id,
                    attempt=attempt.attempt,
                    outcome=attempt.outcome,
                    quantity=attempt.quantity,
                    accepted=attempt.accepted_at is not None,
                    actual_seconds=attempt.actual_seconds,
                    actual_grams=attempt.actual_grams,
                    filament_minor=_minor(filament),
                    archive_ids=attempt.archive_ids,
                )
            )
        failed = [i for i, a in enumerate(attempts) if a.outcome == "failed"]
        failed_grams = _grams_sum([attempts[i].actual_grams for i in failed])
        lines.append(
            ItemCost(
                item_id=item.id,
                title=item.title,
                status=item.status,
                quantity=item.quantity,
                quote_id=None if quote is None else quote.id,
                quote_source=None if quote is None else quote.source,
                quoted_seconds=None if unit is None else unit[0] * item.quantity,
                quoted_grams=None if unit is None else _grams(unit[1] * item.quantity),
                quoted_filament_minor=None if unit is None else _minor(unit[2] * item.quantity),
                quoted_cost_minor=None if quote is None else quote.unit_cost_minor * item.quantity,
                quoted_price_minor=None if quote is None else quote.total_minor,
                actual_seconds=_sum([a.actual_seconds for a in attempts]),
                actual_grams=_grams_sum([a.actual_grams for a in attempts]),
                actual_filament_minor=_minor(_sum(priced)),
                failed_attempts=len(failed),
                failed_grams=failed_grams,
                failed_filament_minor=_minor(_sum([priced[i] for i in failed])),
                attempts=attempts,
            )
        )
    return order.currency, lines


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
    store: OrderStore, integrations: ArchiveCosts, order_id: str
) -> OrderCosts | None:
    """The order's real cost; None for an unknown order."""
    read = await asyncio.to_thread(_item_costs, store, order_id)
    if read is None:
        return None
    currency, items = read
    unavailable = await _bambuddy(integrations, items)
    return OrderCosts(
        order_id=order_id,
        currency=currency,
        items=items,
        quoted_seconds=_sum([i.quoted_seconds for i in items]),
        quoted_grams=_grams_sum([i.quoted_grams for i in items]),
        quoted_filament_minor=_sum([i.quoted_filament_minor for i in items]),
        actual_seconds=_sum([i.actual_seconds for i in items]),
        actual_grams=_grams_sum([i.actual_grams for i in items]),
        actual_filament_minor=_sum([i.actual_filament_minor for i in items]),
        bambuddy_unavailable=unavailable,
    )
