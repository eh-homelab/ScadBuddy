"""An item print's spool per slot (#4251, spec 2026-10-18 §5.4 "Spools", plan
2026-11-03 Ruling 4), from ScadBuddy's own spools (#6262 Task 9).

A quote names a price-book filament per colour slot (§3.3), never a spool, so the print
fills ``filament_plan.slots`` when it starts, on the printer it runs on, with SB spool
numbers (``ids`` ``"sb"``). A spool is of a filament as the spool import matches them
(#6209, ``orders/spool_import.py`` ``_matches``): the same material, subtype and brand,
upper-cased, and the record's link to it (its ``spool_ids``, whatever the spools' alpha:
a link by id is not a match by colour, spec §5.1), else the spools' own ``#RRGGBBAA`` at
the last import (``spool_colour``, :func:`link_rgba`), else, for a record the import
never linked, its own colour and alpha (:func:`record_rgba`; a spool of another opacity
is never its, `same_opacity`, #6316). So a colour a person set never hides the record's
spools, and a clear stand-in record of alpha 255 finds a new clear spool. For each slot
whose filament the item names:

- a spool the request names must be of that filament, else ``spool_not_filament``;
- otherwise the spool of that filament loaded on the run's registry printer with the
  most grams left (a spool is in the tray a registry printer's report holds it in, else,
  while Bambuddy is linked, the one Bambuddy assigns its linked BB spool to: plan
  Decision 3, until #6263; a failed assignment read places by the reports alone); with
  none loaded there, the unarchived shelf spool (loaded nowhere) of it with the most
  grams left, as the dialog's spool-first order ranks them
  (``bambuddy/filaments.py`` ``_display_order``). A spool loaded in another printer is
  never taken: using it means unloading it there, which is a person's call.
- no such spool at all is ``needs_spool_choice``, naming every such slot.

A slot whose filament the item does not name is left as the request gave it. A request
whose plan names Bambuddy spool ids (``ids`` ``"bb"``, an older client's) has them mapped
to their linked SB spools first, as ``bambuddy/filaments.py`` ``sb_plan`` maps them.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping, Sequence
from typing import Protocol

from fastapi import status

from scadbuddy.bambuddy.filaments import FilamentPlan
from scadbuddy.bambuddy.models import SlotChoice, SpoolAssignment
from scadbuddy.core.problems import DATABASE_ERRORS, ApiError
from scadbuddy.filament.colour import same_opacity
from scadbuddy.filament.models import FilamentInventorySpool
from scadbuddy.orders.integrations import OrderIntegrations, ShelfSpool
from scadbuddy.orders.models import Filament
from scadbuddy.orders.problems import PROBLEMS
from scadbuddy.orders.store import OrderStore, SpoolLink

__all__ = [
    "ShelfSpool",
    "SpoolChoiceError",
    "SpoolRows",
    "fill_plan",
    "item_plan",
    "record_rgba",
    "shelf_spools",
]


class SpoolRows(Protocol):
    """What the fill reads of ScadBuddy's spools (`filament/inventory.py`
    `FilamentInventory`): blocking, so it is called in a thread."""

    def rows(
        self,
        *,
        include_archived: bool = False,
        bb_assignments: Sequence[SpoolAssignment] = (),
    ) -> list[FilamentInventorySpool]: ...

    def bb_to_sb(self, ids: Iterable[int]) -> dict[int, int]: ...


def shelf_spools(rows: Iterable[FilamentInventorySpool]) -> list[ShelfSpool]:
    """``rows`` (`FilamentInventory.rows`) in orders' shape: each by its number, with its
    effective names and ``rgba``, its ledger's grams and its registry printer."""
    return [
        ShelfSpool(
            id=row.spool.number,
            material=row.spool.names.material or "",
            subtype=row.spool.names.subtype,
            brand=row.spool.names.brand,
            rgba=row.spool.effective.rgba,
            remaining_g=row.spool.grams_left or 0.0,
            archived=row.spool.archived_at is not None,
            loaded_printer=None if row.loaded is None else row.loaded.printer_id,
            loaded=row.loaded is not None,
        )
        for row in rows
    ]


def link_rgba(spool_colour: str, alpha: int) -> str:
    """A record's ``spool_colour`` (its spools' ``#RRGGBBAA`` at the last import, #6262
    Task 9 fix round 2) as ``#RRGGBBAA``. An old ``#RRGGBB`` (written by an API still on
    the old code during a rollout) takes the record's ``alpha``, as
    ``20261116T0300Z_filament_spool_colour_rgba.sql`` converts it."""
    colour = spool_colour.upper()
    return colour if len(colour) == 9 else colour + f"{alpha:02X}"


def record_rgba(record: Filament) -> str:
    """The record's colour with its alpha, ``#RRGGBBAA``, as an SB spool's ``rgba``."""
    return record.colour.upper() + f"{record.alpha:02X}"


class SpoolChoiceError(Exception):
    """A plan the print cannot start with: ``slug`` is ``needs_spool_choice`` or
    ``spool_not_filament``; ``slots`` the colour slots (from 0) in the way."""

    def __init__(self, slug: str, message: str, slots: Sequence[int]) -> None:
        super().__init__(message)
        self.slug = slug
        self.slots = list(slots)


def _key(*parts: str | None) -> tuple[str, ...]:
    return tuple((part or "").strip().upper() for part in parts)


def _of(spool: ShelfSpool, record: Filament, link: SpoolLink | None) -> bool:
    """Whether ``spool`` is of ``record`` (``link``: the import's link to its spools)."""
    if spool.rgba is None:
        return False
    if _key(spool.material, spool.subtype, spool.brand) != _key(
        record.material, record.subtype, record.brand
    ):
        return False
    if link is not None and spool.id in link.spool_ids:
        return True
    if link is not None and link.spool_colour is not None:
        # The spools' own colour and alpha at the last import, whatever the record's alpha
        # (a clear stand-in record of alpha 255 keeps finding its clear spools).
        return spool.rgba.upper() == link_rgba(link.spool_colour, record.alpha)
    if not same_opacity(spool.rgba, record_rgba(record)):
        return False
    return spool.rgba.upper() == record_rgba(record)


def fill_plan(
    filaments: Mapping[int, Filament],
    spools: Sequence[ShelfSpool],
    printer: str | None,
    supplied: Mapping[int, int],
    links: Mapping[str, SpoolLink] | None = None,
) -> dict[int, int]:
    """Every slot's spool (``{slot_id: SB number}``, ``slot_id`` from 1 as the dialog's)
    for ``filaments`` (``{colour slot from 0: record}``) on registry printer ``printer``,
    the request's ``supplied`` choices kept when they are of their slot's filament;
    ``links`` the import's link of each record to its spools, by record id (#6209).
    Raises :class:`SpoolChoiceError`."""
    by_id = {spool.id: spool for spool in spools}
    plan = dict(supplied)
    wrong: list[int] = []
    missing: list[int] = []
    for slot, record in sorted(filaments.items()):
        slot_id = slot + 1
        link = (links or {}).get(record.id)
        named = supplied.get(slot_id)
        if named is not None:
            spool = by_id.get(named)
            if spool is None or not _of(spool, record, link):
                wrong.append(slot)
            continue
        matching = [s for s in spools if not s.archived and _of(s, record, link)]
        here = [s for s in matching if printer is not None and s.loaded_printer == printer]
        shelf = [s for s in matching if not s.loaded and s.loaded_printer is None]
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


def _item_filaments(
    store: OrderStore, item_id: str
) -> tuple[dict[int, Filament], dict[str, SpoolLink]]:
    item = store.item(item_id)
    if item is None:
        return {}, {}
    named: dict[int, Filament] = {}
    for slot in item.colours:
        if slot.filament_id is not None:
            record = store.filament(slot.filament_id)
            if record is not None:
                named[slot.slot] = record
    return named, store.spool_links([record.id for record in named.values()])


async def item_plan(
    store: OrderStore,
    inventory: SpoolRows,
    integrations: OrderIntegrations,
    item_id: str,
    plan: FilamentPlan,
    printer: str | None,
) -> FilamentPlan | None:
    """The request's ``plan`` filled from the item's filaments with ScadBuddy's spools on
    registry printer ``printer`` (None: no printer's loaded spools count), naming SB
    spool numbers; None when the item names no filament. 409 ``needs_spool_choice`` or
    ``spool_not_filament`` naming the colour slots; 503 when ScadBuddy's spools cannot
    be read. While ``integrations`` is linked, Bambuddy's tray assignments place the
    spools no registry printer reports; nothing else is read from Bambuddy."""
    filaments, links = await asyncio.to_thread(_item_filaments, store, item_id)
    if not filaments:
        return None
    supplied = {slot.slot_id: slot.spool_id for slot in plan.slots}
    try:
        assignments = await integrations.spool_assignments() if integrations.linked else []
        rows = await asyncio.to_thread(
            inventory.rows, include_archived=True, bb_assignments=assignments
        )
        if plan.ids == "bb":
            named = [spool_id for spool_id in supplied.values() if spool_id > 0]
            mapped = await asyncio.to_thread(inventory.bb_to_sb, named) if named else {}
            supplied = {
                slot_id: spool_id if spool_id <= 0 else mapped[spool_id]
                for slot_id, spool_id in supplied.items()
                if spool_id <= 0 or spool_id in mapped
            }
    except DATABASE_ERRORS as error:
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "ScadBuddy's spools could not be read to choose the item's; try again",
        ) from error
    try:
        filled = fill_plan(filaments, shelf_spools(rows), printer, supplied, links)
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
        ids="sb",
    )
