"""A storefront cart line's firm price (#6153, spec 2026-11-13 §5.4).

The slice price is firm (owner, 2026-11-14): a line whose variant has a ``done`` slice
price is charged that price, with no check by the shop. The server never takes a price
from the browser. When the visitor adds a line (`variant_at_add`), and again at checkout
(`reprice`), the CRUD side makes the variant's ``price_key`` the way the estimate does
(``slice_price.py`` `firm_price`: the render's 3MF content, the price-book filament of
each slot, the shop slice profile, the price book and the live machine rate) and reads
the price by that key. A key with no ``done`` row (a slice still running or failed, a
book or rate that changed, or a design that moved) gives the line no firm price: the page
shows no number, and the shop prices that line by hand.

A line keeps, in ``storefront_item_prices`` (:class:`LinePrices`), what its key was made
from when it was added: the listing, the 3MF's sha256 and the filament of each slot. The
render job is pruned after a day, and a cart lives for weeks. It keeps the price the
visitor saw too, so the checkout tells the customer of a price that changed, came or went
(`Repriced.told`).
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final, Literal

from pydantic import BaseModel

from scadbuddy.orders.lifecycle import FirmLine
from scadbuddy.orders.models import Filament, OrderItem, Reference
from scadbuddy.orders.pricing import Plate, PlateFilament
from scadbuddy.orders.storefront.limits import RenderLimits
from scadbuddy.orders.storefront.renders import fetch_files, variant_of
from scadbuddy.orders.storefront.slice_price import PriceRow, firm_price
from scadbuddy.orders.storefront.wire import Listing
from scadbuddy.render.job_models import JobNotFoundError

if TYPE_CHECKING:
    from scadbuddy.api.deps import AppState
    from scadbuddy.orders.component import OrdersComponent

_COLUMNS: Final = (
    "order_item_id, listing_id, model_sha256, slot_filaments, price_key, unit_price_minor, currency"
)


@dataclass(frozen=True)
class Variant:
    """What a line's price key is made from, besides the shop's settings: the listing it
    was added from, the sha256 of its render's 3MF, and the price-book filament id of
    each slot of the render (from 0)."""

    listing_id: str
    model_sha256: str
    slot_filaments: tuple[str, ...]


class LinePrice(BaseModel):
    """One ``storefront_item_prices`` row: a line's variant, and the firm price the
    visitor last saw (``price_key`` and ``unit_price_minor`` null for none)."""

    order_item_id: str
    listing_id: str
    model_sha256: str
    slot_filaments: list[str]
    price_key: str | None = None
    unit_price_minor: int | None = None
    currency: str | None = None

    @property
    def variant(self) -> Variant:
        return Variant(self.listing_id, self.model_sha256, tuple(self.slot_filaments))


class LinePrices:
    """The ``storefront_item_prices`` rows (#6153). The CRUD process only (#5295)."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    @staticmethod
    def record(conn: Any, item_id: str, variant: Variant, price: PriceRow | None) -> None:
        """The added line's variant and price, in the add's transaction (``conn``)."""
        conn.execute(
            "INSERT INTO storefront_item_prices (order_item_id, listing_id, model_sha256,"
            " slot_filaments, price_key, unit_price_minor, currency)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                item_id,
                variant.listing_id,
                variant.model_sha256,
                list(variant.slot_filaments),
                *_shown(price),
            ),
        )

    def of_order(self, order_id: str) -> dict[str, LinePrice]:
        """The order's lines' rows, by item id."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM storefront_item_prices WHERE order_item_id IN"
                " (SELECT id FROM order_items WHERE order_id = %s)",
                (order_id,),
            ).fetchall()
        return {row["order_item_id"]: LinePrice.model_validate(row) for row in rows}

    def set_price(self, item_id: str, price: PriceRow | None) -> None:
        """The price the checkout found for the line (#6153), which the page shows next."""
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE storefront_item_prices SET price_key = %s, unit_price_minor = %s,"
                " currency = %s, priced_at = clock_timestamp() WHERE order_item_id = %s",
                (*_shown(price), item_id),
            )


def _shown(price: PriceRow | None) -> tuple[str | None, int | None, str | None]:
    if price is None:
        return None, None, None
    return price.price_key, price.unit_price_minor, price.currency


def _orders(state: AppState) -> OrdersComponent:
    from scadbuddy.orders.component import ORDERS

    orders: OrdersComponent = state.components.get(ORDERS)
    return orders


async def variant_at_add(
    state: AppState,
    *,
    visitor_id: str,
    listing: Listing,
    render_key: str,
    colours: Mapping[int, str],
    material: str | None,
) -> Variant | None:
    """The variant of the line the visitor adds: its own done render of ``render_key``
    (`RenderLimits.job_of`), the slot filaments of ``colours`` in ``material`` as the
    estimate takes them (``renders.py`` `variant_of`). None when there is no done render
    of the listing's design (slug and commit) whose files this process can read, or a
    slot has no listing filament: the line has no firm price."""

    def job() -> Any:
        job_id = RenderLimits(state.projection.pool).job_of(visitor_id, render_key)
        if job_id is None:
            return None
        try:
            return state.render.store.read(job_id)
        except JobNotFoundError:
            return None

    found = await asyncio.to_thread(job)
    if found is None or found.state != "done" or found.result is None:
        return None
    if found.slug != listing.slug or found.model_version != listing.commit:
        return None
    if not await fetch_files(state, found):
        return None
    digest, slots = await variant_of(state, listing, found, colours, material)
    if slots is None:
        return None
    return Variant(listing.id, digest, tuple(f.id for f in slots))


async def price_of(state: AppState, reference: Reference, variant: Variant) -> PriceRow | None:
    """The firm price of the variant of a line whose design is ``reference``, now
    (`firm_price`), or None. None too when the listing it was added from is gone or
    unpublished, or now shows another design (its slug or commit moved, so the line's
    design is not the listing's), or a slot's filament is gone or archived."""
    orders = _orders(state)
    listing = await asyncio.to_thread(orders.listings.get, variant.listing_id)
    if (
        listing is None
        or not listing.published
        or listing.snapshot is None
        or (listing.slug, listing.commit) != (reference.slug, reference.commit)
    ):
        return None
    records = [
        await asyncio.to_thread(orders.store.filament, filament_id)
        for filament_id in variant.slot_filaments
    ]
    filaments: list[Filament] = [r for r in records if r is not None and r.archived_at is None]
    if len(filaments) != len(records):
        return None
    return await firm_price(
        state, reference=reference, model_sha256=variant.model_sha256, filaments=filaments
    )


class ChangedLine(BaseModel):
    """A line in a 409 ``prices_changed``: its item id and its price's state now."""

    id: str
    price: Literal["firm", "by_hand"]


@dataclass
class Repriced:
    """What the checkout found (`reprice`): each line's firm price by item id, and the
    lines whose price is not the one the visitor saw (`told`), each with its state now:
    a price that changed or came is ``firm``, a price that went is ``by_hand`` (the shop
    prices the line). The customer sees the change before anything is paid."""

    firm: dict[str, FirmLine] = field(default_factory=dict)
    told: list[ChangedLine] = field(default_factory=list)


async def reprice(state: AppState, items: Sequence[OrderItem]) -> Repriced:
    """Each listing line of the cart priced again at checkout (#6153): the price its
    key has now (`price_of`). A price unlike the one the line recorded is written, so
    the page shows it next. Lines with no row (a request, or a line added with no
    variant) have no firm price."""
    if not items:
        return Repriced()
    prices = LinePrices(state.projection.pool)
    lines = await asyncio.to_thread(prices.of_order, items[0].order_id)
    found = Repriced()
    for item in items:
        line = lines.get(item.id)
        if line is None or item.status == "cancelled" or item.reference is None:
            continue
        now = await price_of(state, item.reference, line.variant)
        if _shown(now) != (line.price_key, line.unit_price_minor, line.currency):
            await asyncio.to_thread(prices.set_price, item.id, now)
        shown = _shown(now)[1:]
        if shown != (line.unit_price_minor, line.currency):
            found.told.append(ChangedLine(id=item.id, price="by_hand" if now is None else "firm"))
        if now is None:
            continue
        assert now.currency is not None and now.unit_price_minor is not None
        assert now.unit_cost_minor is not None
        found.firm[item.id] = FirmLine(
            lines=tuple(now.lines or ()),
            unit_cost_minor=now.unit_cost_minor,
            unit_price_minor=now.unit_price_minor,
            currency=now.currency,
            printer=now.printer,
            plates=_plates(now),
        )
    return found


def _plates(row: PriceRow) -> tuple[dict[str, Any], ...]:
    """The plates a storefront quote carries for job cost (#6153): the row's own, else
    one plate of its whole seconds and grams per slot (a row written before it kept
    plates), else none when either is not known: job cost then reads them as unknown."""
    if row.plates is not None:
        return tuple(plate.model_dump(mode="json") for plate in row.plates)
    if row.print_seconds is None or row.grams is None:
        return ()
    whole = Plate(
        index=1,
        print_seconds=row.print_seconds,
        filaments=[
            PlateFilament(slot=int(slot), used_g=Decimal(grams))
            for slot, grams in sorted(row.grams.items(), key=lambda kv: int(kv[0]))
        ],
    )
    return (whole.model_dump(mode="json"),)
