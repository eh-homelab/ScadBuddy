"""An item print's spool per slot (#4251, spec 2026-10-18 §5.4 "Spools", plan
2026-11-03 Ruling 4).

A quote names a price-book filament per colour slot (§3.3), never a spool, so the print
fills ``filament_plan.slots`` when it starts, on the printer it runs on. A spool is of a
filament when its identity is the record's: material, subtype, brand and ``#RRGGBB``,
upper-cased, as the spool import matches them (§8.3, ``orders/bambuddy.py``
``spool_groups``). For each slot whose filament the item names:

- a spool the request names must be of that filament, else ``spool_not_filament``;
- otherwise the spool of that filament loaded on the run's printer with the most grams
  left; with none loaded there, the unarchived shelf spool (loaded nowhere) of it with
  the most grams left, as the dialog's spool-first order ranks them
  (``bambuddy/filaments.py`` ``_display_order``). A spool loaded in another printer is
  never taken: using it means unloading it there, which is a person's call.
- no such spool at all is ``needs_spool_choice``, naming every such slot.

A slot whose filament the item does not name is left as the request gave it. Pure: the
seam reads the inventory (``OrderIntegrations.spool_inventory``).
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Protocol

from fastapi import status

from scadbuddy.bambuddy.filaments import FilamentPlan
from scadbuddy.bambuddy.models import SlotChoice
from scadbuddy.core.problems import ApiError
from scadbuddy.orders.integrations import NotLinkedError, ShelfSpool
from scadbuddy.orders.models import Filament
from scadbuddy.orders.problems import PROBLEMS
from scadbuddy.orders.store import OrderStore

__all__ = ["ShelfSpool", "SpoolChoiceError", "fill_plan", "item_plan"]


class SpoolInventory(Protocol):
    """What the fill reads of the seam (``OrderIntegrations``)."""

    @property
    def linked(self) -> bool: ...

    async def spool_inventory(self) -> list[ShelfSpool]: ...


class SpoolChoiceError(Exception):
    """A plan the print cannot start with: ``slug`` is ``needs_spool_choice`` or
    ``spool_not_filament``; ``slots`` the colour slots (from 0) in the way."""

    def __init__(self, slug: str, message: str, slots: Sequence[int]) -> None:
        super().__init__(message)
        self.slug = slug
        self.slots = list(slots)


def _key(*parts: str | None) -> tuple[str, ...]:
    return tuple((part or "").strip().upper() for part in parts)


def record_key(record: Filament) -> tuple[str, ...]:
    return _key(record.material, record.subtype, record.brand, record.colour)


def spool_key(spool: ShelfSpool) -> tuple[str, ...] | None:
    if spool.colour is None:
        return None
    return _key(spool.material, spool.subtype, spool.brand, spool.colour)


def fill_plan(
    filaments: Mapping[int, Filament],
    spools: Sequence[ShelfSpool],
    printer_id: int | None,
    supplied: Mapping[int, int],
) -> dict[int, int]:
    """Every slot's spool (``{slot_id: spool_id}``, ``slot_id`` from 1 as the dialog's)
    for ``filaments`` (``{colour slot from 0: record}``), the request's ``supplied``
    choices kept when they are of their slot's filament. Raises
    :class:`SpoolChoiceError`."""
    by_id = {spool.id: spool for spool in spools}
    plan = dict(supplied)
    wrong: list[int] = []
    missing: list[int] = []
    for slot, record in sorted(filaments.items()):
        slot_id = slot + 1
        want = record_key(record)
        named = supplied.get(slot_id)
        if named is not None:
            spool = by_id.get(named)
            if spool is None or spool_key(spool) != want:
                wrong.append(slot)
            continue
        matching = [s for s in spools if not s.archived and spool_key(s) == want]
        here = [s for s in matching if printer_id is not None and s.loaded_printer_id == printer_id]
        shelf = [s for s in matching if s.loaded_printer_id is None]
        pick = max(here or shelf, key=lambda s: s.remaining_g, default=None)
        if pick is None:
            missing.append(slot)
        else:
            plan[slot_id] = pick.id
    if wrong:
        raise SpoolChoiceError(
            "spool_not_filament",
            "the spool chosen for colour slot "
            + ", ".join(map(str, wrong))
            + " is not of the filament the item was quoted with: a different filament is a"
            " change to the quote, never to the print",
            wrong,
        )
    if missing:
        raise SpoolChoiceError(
            "needs_spool_choice",
            "no spool of the quoted filament is loaded on this printer or on the shelf for"
            " colour slot " + ", ".join(map(str, missing)) + ": choose one",
            missing,
        )
    return plan


def _item_filaments(store: OrderStore, item_id: str) -> dict[int, Filament]:
    item = store.item(item_id)
    if item is None:
        return {}
    named: dict[int, Filament] = {}
    for slot in item.colours:
        if slot.filament_id is not None:
            record = store.filament(slot.filament_id)
            if record is not None:
                named[slot.slot] = record
    return named


async def item_plan(
    store: OrderStore,
    integrations: SpoolInventory,
    item_id: str,
    plan: FilamentPlan,
    printer_id: int | None,
) -> FilamentPlan | None:
    """The request's ``plan`` filled from the item's filaments on Bambuddy printer
    ``printer_id`` (None: no printer's loaded spools count); None when there is nothing
    to fill (no Bambuddy, or an item that names no filament). 409 ``needs_spool_choice``
    or ``spool_not_filament`` naming the colour slots; 503 when Bambuddy cannot be read."""
    if not integrations.linked:
        return None
    filaments = await asyncio.to_thread(_item_filaments, store, item_id)
    if not filaments:
        return None
    try:
        spools = await integrations.spool_inventory()
    except NotLinkedError:
        return None
    except Exception as error:
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Bambuddy's spools could not be read to choose the item's; try again",
        ) from error
    supplied = {slot.slot_id: slot.spool_id for slot in plan.slots}
    try:
        filled = fill_plan(filaments, spools, printer_id, supplied)
    except SpoolChoiceError as error:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            str(error),
            type_=PROBLEMS + error.slug,
            slots=error.slots,
        ) from None
    return FilamentPlan(
        slots=[SlotChoice(slot_id=s, spool_id=filled[s]) for s in sorted(filled)],
        force_colour_match=plan.force_colour_match,
    )
