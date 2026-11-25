"""The price and the cart line of a storefront listing of a library file (#6710, spec
2026-11-21 §7.5, §7.6, §10.1).

A file listing has no parameters: the visitor picks a listing colour for each offered slot
of the file (`FileSnapshot.offered`), and the shop's lock fills each support slot
(`FileSnapshot.support`). A print is of one material, as every listing's: the first
offered slot's filament fixes it (`file_choice`). The price is the slice of the listing's
held copy (`slice_price.py`, `held_file.py` `priced_file`): `listing_price` reads it, or
claims the slice, which costs one render of the visitor's limits; a read that finds a price
or joins a running slice costs nothing. The cart line (`drafts.py`) is a ``library_file``
item with those colours, the pinned sha256 and the copy, which the item holds; its firm
price is read by the same key (`line_prices.py` `price_of`).
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from scadbuddy.orders.estimate import StoreEstimate
from scadbuddy.orders.models import ColourSlot, Filament, LibraryFileRef
from scadbuddy.orders.storefront.held_file import priced_file
from scadbuddy.orders.storefront.inputs import (
    ColoursClashError,
    InputOutsideListingError,
    customer_filaments,
    effective_materials,
    material_key,
)
from scadbuddy.orders.storefront.limits import RenderLimited, RenderLimits
from scadbuddy.orders.storefront.listings import page_colours, page_outdated, published_listing
from scadbuddy.orders.storefront.renders import (
    limited,
    listing_refusals,
    price_book,
)
from scadbuddy.orders.storefront.slice_price import (
    NO_PRICE,
    Planned,
    last_answer,
    pending,
    planned_estimate,
    planned_variant,
    setup_key,
)
from scadbuddy.orders.storefront.visitors import VisitorStore
from scadbuddy.orders.storefront.wire import FileSlot, FileSnapshot, Listing

if TYPE_CHECKING:
    from datetime import datetime

    from scadbuddy.api.deps import AppState
    from scadbuddy.orders.component import OrdersComponent
    from scadbuddy.render.schema import ParamValue


@dataclass(frozen=True)
class FileChoice:
    """A visitor's colours on a file listing: each used slot of the file, in slot order,
    with its filament (the visitor's for an offered slot, the shop's lock for a support
    slot; None for a lock no longer in the price book)."""

    snapshot: FileSnapshot
    slots: tuple[tuple[FileSlot, Filament | None], ...]
    #: The filaments a print of the chosen material may take (#7523): the price book's of
    #: that material, and the shop's locks of the support slots.
    alike: frozenset[str] = frozenset()

    @property
    def filaments(self) -> list[Filament] | None:
        """The filament of each used slot, in order (`PricedFile.used`), or None when a
        slot has none: the line then has no slice price."""
        found = [filament for _, filament in self.slots]
        if any(filament is None for filament in found):
            return None
        return [filament for filament in found if filament is not None]

    @property
    def colours(self) -> tuple[ColourSlot, ...]:
        """The order item's slots: each used slot in its filament's colour, or in the
        file's own colour when it has none."""
        return tuple(
            ColourSlot(
                slot=slot.slot,
                colour=(slot.colour if filament is None else filament.colour)[:7].upper(),
                filament_id=None if filament is None else filament.id,
            )
            for slot, filament in self.slots
        )


def file_choice(
    listing: Listing,
    colours: Mapping[int, str],
    filaments: Mapping[str, Filament],
    materials: Sequence[str],
) -> FileChoice:
    """The used slots of the published file ``listing`` with the visitor's ``colours``
    (slot to filament id) on its offered slots. Each offered slot takes one, a customer
    filament of the listing (`customer_filaments`) of the allowed ``materials``; any other
    slot, a filament not on offer, or a missing slot is `InputOutsideListingError`
    (``colours.<slot>``, ``colour``). The first offered slot's material is the print's,
    and a slot of another is `ColoursClashError` naming those slots."""
    snapshot = listing.file_snapshot
    assert snapshot is not None
    offered = {slot.slot for slot in snapshot.offered}
    on_offer = {f.id: f for f in customer_filaments(listing, filaments, materials)}
    for slot in sorted(colours):
        if slot not in offered or colours[slot] not in on_offer:
            raise InputOutsideListingError(f"colours.{slot}", "colour")
    for slot in sorted(offered):
        if slot not in colours:
            raise InputOutsideListingError(f"colours.{slot}", "colour")
    picked = [(slot, on_offer[colours[slot]]) for slot in sorted(offered)]
    material = material_key(picked[0][1].material)
    clashing = [f"colours.{slot}" for slot, f in picked if material_key(f.material) != material]
    if clashing:
        raise ColoursClashError(clashing)
    chosen = dict(picked)
    slots: list[tuple[FileSlot, Filament | None]] = []
    for each in snapshot.slots:
        if not each.used:
            continue
        if each.slot in chosen:
            slots.append((each, chosen[each.slot]))
        else:
            slots.append((each, filaments.get(snapshot.support.get(each.slot, ""))))
    alike = {f.id for f in filaments.values() if material_key(f.material) == material}
    return FileChoice(
        snapshot=snapshot,
        slots=tuple(slots),
        alike=frozenset(alike | set(snapshot.support.values())),
    )


async def checked_file_choice(
    orders: OrdersComponent,
    listing_id: str,
    listing_updated_at: datetime,
    colours: Mapping[int, str],
    shop_materials: Sequence[str],
    *,
    values: Collection[str] | Mapping[str, ParamValue] = (),
) -> tuple[Listing, FileChoice]:
    """The published file listing ``listing_id`` and the visitor's colours on it. A file
    has no parameters, so any of ``values`` is outside it. Refusals: 404
    ``listing_not_found``, 409 ``listing_changed`` (moved, or switched to a template, since
    the page read it), 422 ``input_outside_listing`` (with ``name`` and ``reason``), 422
    ``listing_colours_clash`` (with ``parts``)."""
    listing = await asyncio.to_thread(
        published_listing, orders.listings, listing_id, source="library_file"
    )
    if listing.updated_at != listing_updated_at:
        raise page_outdated()
    filaments = await asyncio.to_thread(price_book, orders)
    with listing_refusals():
        for name in values:
            raise InputOutsideListingError(name, "unknown")
        choice = file_choice(
            listing, colours, filaments, effective_materials(listing, shop_materials)
        )
    return listing, choice


def library_file(choice: FileChoice) -> LibraryFileRef:
    """The order item's file: the listing's file as it was pinned, and its held copy."""
    snapshot = choice.snapshot
    return LibraryFileRef(
        id=snapshot.file_id,
        filename=snapshot.filename,
        library=snapshot.library,
        sha256=snapshot.sha256,
        copy_key=snapshot.copy_key,
    )


async def listing_price(
    state: AppState,
    visitor_id: str,
    client: str,
    listing_id: str,
    listing_updated_at: datetime,
    colours: Mapping[int, str],
) -> StoreEstimate:
    """The price of the visitor's colours on a file listing (#6710 §7.5): the slice price
    of its held copy (`planned_estimate`), or none. While it is worked out, the listing's
    last firm price of its file (`SlicePrices.listing_last`) as ``last`` with the pending
    ``retry_after``, so the page always shows a price and reads again; ``pending`` when
    there is none. A read that claims a new slice counts as one render of the visitor's and
    the client's limits; one that finds the price, or a slice that stands, costs nothing.
    Over the limits no slice starts: the last price, with the limit's ``retry_after``, else
    429 ``render_limited`` (#7523). Refusals as `checked_file_choice`."""
    from scadbuddy.orders.component import ORDERS

    orders: OrdersComponent = state.components.get(ORDERS)
    runtime = await asyncio.to_thread(lambda: state.settings_store.snapshot().runtime)
    listing, choice = await checked_file_choice(
        orders, listing_id, listing_updated_at, colours, runtime.storefront_materials
    )
    planned = await plan_file(state, listing, choice)
    if planned is None:
        return NO_PRICE
    visitor = await asyncio.to_thread(VisitorStore(state.projection.pool).ensure, visitor_id)
    limits = RenderLimits(state.projection.pool)

    async def charge(price_key: str) -> Callable[[], Awaitable[None]]:
        row = await asyncio.to_thread(
            lambda: limits.admit_slice(
                visitor.id,
                client,
                listing_id=listing.id,
                price_key=price_key,
                settings=runtime,
            )
        )

        async def refund() -> None:
            await asyncio.to_thread(limits.refund_slice, row)

        return refund

    over: RenderLimited | None = None
    try:
        answer = await planned_estimate(state, planned, charge=charge)
    except RenderLimited as error:
        over, answer = error, pending(planned.key)
    if answer.source != "pending":
        return answer
    last = await asyncio.to_thread(
        functools.partial(
            orders.slice_prices.listing_last,
            listing.id,
            choice.snapshot.sha256,
            filaments=choice.alike,
            setup_key=setup_key(planned.variant),
        )
    )
    if last is not None:
        wait = answer.retry_after if over is None else over.retry_after
        return last_answer(last).model_copy(update={"retry_after": wait})
    if over is not None:
        raise limited(over)
    return answer


async def plan_file(state: AppState, listing: Listing, choice: FileChoice) -> Planned | None:
    """The slice of ``choice`` on the file ``listing``, as the shop's settings stand now
    (`planned_variant`); None when it has no slice price."""
    return await planned_variant(
        state,
        filaments=choice.filaments,
        library_file=priced_file(choice.snapshot),
        listing_id=listing.id,
    )


def default_choice(
    listing: Listing, filaments: Mapping[str, Filament], shop_materials: Sequence[str]
) -> FileChoice | None:
    """The colours a visitor's page of the published file ``listing`` opens on (#7523,
    `page_colours`), or None when it cannot be ordered."""
    colours = page_colours(listing, filaments, shop_materials)
    if colours is None:
        return None
    return file_choice(listing, colours, filaments, effective_materials(listing, shop_materials))
