"""Filament records imported from ScadBuddy's spools (#4253, spec 2026-10-18 §8.3, Ruling
3; #6262 Task 9).

One record per filament the SB inventory holds (:func:`spool_groups`: every unarchived SB
spool of the same effective material, subtype, brand and ``#RRGGBBAA``), matched to the
book by its link to the group's spools, else by material, subtype, brand and the spools'
``#RRGGBBAA`` (#6209, :func:`_matches`; the colour alone would make a second record of
one a person recoloured): a new record when the book has none, else its ``cost_per_kg``
set to the most recent priced spool's (the highest number). Every colour compare holds
the alpha byte, so an opaque and a translucent spool of one hue are two records, and a
translucent record never takes an opaque group (#6316). A record takes the spools'
colour, alpha, stops and effect unless a person set them (``colour_source``
``manual``). It runs on click only: a quote reads the book, never the spools. A group
known only by its colour's name, or with no priced spool, is reported and left alone: a
record needs a ``#RRGGBB`` and a price.

A record with no density gets the one its spools give (#6136, `orders/density.py`
`resolve`). A density a record has is never changed. :func:`fill_densities` does the
same for every record of the book with none (``POST …/price-book/fill-densities``), and
a filament added by hand with no density takes :func:`density_for`'s; a record no group
of the inventory matches has no source.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from typing import Literal, Protocol, cast, get_args
from uuid import UUID

from scadbuddy.filament.models import EffectivePreset, FilamentInventorySpool
from scadbuddy.orders.density import DensityTable, ProfileDensity, density_text, resolve
from scadbuddy.orders.integrations import (
    LocalDensity,
    NotLinkedError,
    OrderIntegrations,
    SpoolDensity,
    SpoolGroup,
)
from scadbuddy.orders.models import (
    DensityFill,
    DensityFillRow,
    FilamentEffect,
    FilamentInput,
    FilamentPatch,
    SpoolImport,
    SpoolImportRow,
)
from scadbuddy.orders.spools import link_rgba
from scadbuddy.orders.store import ImportRecord, OrderStore

SkipReason = Literal["no_colour", "no_price", "too_long"]

#: ``filaments.cost_per_kg``'s ``numeric(12,4)``.
PLACES = Decimal("0.0001")
MAX_COST = Decimal(10**8)
#: ``FilamentInput``'s ``max_length`` for each name.
MAX_NAME = 100


def _price(value: Decimal | float | None) -> Decimal | None:
    if value is None:
        return None
    try:
        price = Decimal(str(value)).quantize(PLACES)
    except InvalidOperation:
        return None
    return price if Decimal(0) <= price < MAX_COST else None


def _text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _rgba(colour: str, alpha: int) -> str:
    """``colour`` (``#RRGGBB``) with ``alpha``, as ``#RRGGBBAA``."""
    return colour.upper() + f"{alpha:02X}"


def _record_rgba(record: ImportRecord) -> str:
    return _rgba(record.colour, record.alpha)


def _link_rgba(record: ImportRecord) -> str | None:
    """The spools' own ``#RRGGBBAA`` at the last import (`spools.link_rgba`: an old
    ``#RRGGBB`` takes the record's alpha); ``None`` before one."""
    return None if record.spool_colour is None else link_rgba(record.spool_colour, record.alpha)


def _names(material: str, subtype: str | None, brand: str | None) -> tuple[str, str, str]:
    return (
        material.strip().upper(),
        (subtype or "").strip().upper(),
        (brand or "").strip().upper(),
    )


# -- the groups (§8.3) -----------------------------------------------------------------


class SpoolRows(Protocol):
    """What the import reads of ScadBuddy's spools (`filament/inventory.py`
    `FilamentInventory.rows`): blocking, so it is called in a thread."""

    def rows(self, *, include_archived: bool = False) -> list[FilamentInventorySpool]: ...

    def tray_presets(self) -> dict[UUID, str]: ...


def _effect(name: str | None) -> FilamentEffect | None:
    """An effect catalog name (``Dual Color``) as a record spells it (``dual-color``);
    ``None`` for one a record has no value for."""
    text = (name or "").strip().lower().replace("_", "-").replace(" ", "-")
    return cast(FilamentEffect, text) if text in get_args(FilamentEffect) else None


def _preset(presets: list[EffectivePreset]) -> str | None:
    """The preset id a spool's density is read from: its H2C 0.4 preset, else its first
    (`resolver.PRINTER_MODEL` is the one model the registry prints on)."""
    named = [p for p in presets if p.slicer_filament.strip()]
    h2c = next((p for p in named if (p.printer_model, p.nozzle_diameter) == ("H2C", "0.4")), None)
    chosen = h2c or (named[0] if named else None)
    return None if chosen is None else chosen.slicer_filament.strip()


def spool_groups(
    rows: Iterable[FilamentInventorySpool],
    local: Mapping[str, LocalDensity] | None = None,
    trays: Mapping[UUID, str] | None = None,
) -> list[SpoolGroup]:
    """Every unarchived SB spool of ``rows`` with a material, by the record identity it
    would price: its effective material, subtype and brand (upper-cased) and its
    ``#RRGGBBAA``, in that order. An opaque and a translucent spool of one RGB are two
    groups (#6316). A spool with no ``rgba`` keeps its colour's name as its key, never
    matched to an rgba one (#2918). A group's names, stops and effect are its newest
    spool's (the highest number). ``local`` is Bambuddy's local presets with a density,
    by id (#6136); ``trays`` the preset id of the tray each held spool is in, by spool
    (`FilamentInventory.tray_presets`)."""
    groups: dict[tuple[str, str, str, str], list[FilamentInventorySpool]] = defaultdict(list)
    for row in rows:
        spool = row.spool
        material = (spool.names.material or "").strip()
        if spool.archived_at is not None or not material:
            continue
        rgba = spool.effective.rgba
        colour = rgba.upper() if rgba else "name:" + (spool.effective.colour_name or "").upper()
        key = (*_names(material, spool.names.subtype, spool.names.brand), colour)
        groups[key].append(row)
    out: list[SpoolGroup] = []
    for key in sorted(groups):
        members = sorted(groups[key], key=lambda r: r.spool.number)
        latest = members[-1].spool
        newest_first = list(reversed(members))
        slicer_filament = next((p for r in newest_first if (p := _preset(r.presets))), None)
        density = next(
            (
                SpoolDensity(number=r.spool.number, density_g_cm3=text)
                for r in newest_first
                if (text := density_text(r.spool.effective.density_g_cm3)) is not None
            ),
            None,
        )
        tray_filament = next(
            (p for r in newest_first if (p := (trays or {}).get(r.spool.id))), None
        )
        rgba = latest.effective.rgba
        out.append(
            SpoolGroup(
                material=(latest.names.material or "").strip(),
                subtype=(latest.names.subtype or "").strip() or None,
                brand=(latest.names.brand or "").strip() or None,
                colour_name=(latest.effective.colour_name or "").strip() or None,
                colour=None if rgba is None else rgba[:7].upper(),
                alpha=255 if rgba is None else int(rgba[7:9], 16),
                extra_colours=list(latest.effective.extra_colours or []),
                effect=_effect(latest.names.effect),
                spools=[(r.spool.number, r.spool.effective.cost_per_kg) for r in members],
                slicer_filament=slicer_filament,
                local_preset=(local or {}).get(slicer_filament or ""),
                spool_density=density,
                tray_filament=tray_filament,
                tray_local_preset=(local or {}).get(tray_filament or ""),
            )
        )
    return out


async def inventory_groups(
    inventory: SpoolRows, integrations: OrderIntegrations
) -> list[SpoolGroup]:
    """:func:`spool_groups` of ScadBuddy's spools and the trays holding them, with
    Bambuddy's local presets read only when a group's preset id (its spools' or its
    tray's) is a numeric local id and a Bambuddy is linked."""
    rows = await asyncio.to_thread(inventory.rows)
    trays = await asyncio.to_thread(inventory.tray_presets)
    groups = spool_groups(rows, trays=trays)
    if not any(
        (g.slicer_filament or "").isdigit() or (g.tray_filament or "").isdigit() for g in groups
    ):
        return groups
    try:
        local = await integrations.local_densities()
    except NotLinkedError:
        return groups
    return spool_groups(rows, local, trays)


# -- the match -------------------------------------------------------------------------


def _same_names(
    group: SpoolGroup, records: list[ImportRecord], claimed: set[str]
) -> list[ImportRecord]:
    """The records not yet taken that have ``group``'s material, subtype and brand. A spool
    whose material was corrected (in ScadBuddy, or by the Bambuddy pull) is another
    filament's, never its old record's."""
    names = _names(group.material, group.subtype, group.brand)
    return [
        r
        for r in records
        if r.id not in claimed and _names(r.material, r.subtype, r.brand) == names
    ]


def _linked(
    group: SpoolGroup,
    records: list[ImportRecord],
    claimed: set[str],
    rgba: str | None = None,
) -> ImportRecord | None:
    """The record of ``group``'s names linked to most of its spools (#6209); with
    ``rgba``, only among the records whose spools had that ``#RRGGBBAA``."""
    ids = {spool_id for spool_id, _ in group.spools}
    linked = [
        r
        for r in _same_names(group, records, claimed)
        if ids & set(r.spool_ids) and (rgba is None or _link_rgba(r) == rgba)
    ]
    return max(linked, key=lambda r: len(ids & set(r.spool_ids))) if linked else None


def _by_colour(
    group: SpoolGroup, rgba: str, records: list[ImportRecord], claimed: set[str]
) -> ImportRecord | None:
    """The record of ``group``'s names whose spools had its ``#RRGGBBAA`` at the last
    import (``spool_colour``), else one with no other spools' colour whose
    own ``#RRGGBBAA`` is its (#6209, #6316)."""
    same = _same_names(group, records, claimed)
    return next((r for r in same if _link_rgba(r) == rgba), None) or next(
        (r for r in same if _record_rgba(r) == rgba and _link_rgba(r) in (None, rgba)),
        None,
    )


def _skip_reason(group: SpoolGroup) -> SkipReason | None:
    """Why the import leaves ``group`` alone, before any record is matched."""
    if group.colour is None:
        return "no_colour"
    if all(_price(raw) is None for _, raw in group.spools):
        return "no_price"
    names = (group.material, group.subtype, group.brand, group.colour_name)
    if any(name is not None and len(name) > MAX_NAME for name in names):
        return "too_long"
    return None


def _matches(groups: list[SpoolGroup], records: list[ImportRecord]) -> dict[int, ImportRecord]:
    """Each group's record, by the group's place in ``groups`` (#6209, #6211 review), so
    the order of the groups never decides a match. First a record linked to a group whose
    colour its spools had (``spool_colour``), as when one spool of a record
    was recoloured;
    then any record linked to a group's spools, as when all of them were; only then a
    record by colour (:func:`_by_colour`). A spool link always wins over a colour."""
    matched: dict[int, ImportRecord] = {}
    claimed: set[str] = set()
    eligible = [(i, g) for i, g in enumerate(groups) if _skip_reason(g) is None]

    def take(pick: Callable[[SpoolGroup, str], ImportRecord | None]) -> None:
        for index, group in eligible:
            assert group.rgba is not None
            if index in matched:
                continue
            record = pick(group, group.rgba)
            if record is not None:
                matched[index] = record
                claimed.add(record.id)

    take(lambda group, rgba: _linked(group, records, claimed, rgba))
    take(lambda group, _rgba: _linked(group, records, claimed))
    take(lambda group, rgba: _by_colour(group, rgba, records, claimed))
    return matched


def _movers(
    groups: list[SpoolGroup], matched: dict[int, ImportRecord], records: list[ImportRecord]
) -> set[str]:
    """The matched records whose colour the import changes in this run (#6211 review): each
    ``import`` record whose group's ``#RRGGBBAA`` differs from its own, unless a record
    that stays (one not moving, ``manual`` or not matched) holds it among its names. A record
    held back stays too, so its colour may hold back another, until none changes. The
    movers' new colours are distinct and none is a staying record's, so they can all move
    at once, a chain or a swap of colours included (:func:`import_spools` parks them
    first)."""
    target = {r.id: groups[i].rgba for i, r in matched.items()}
    movers = {
        r.id
        for r in matched.values()
        if r.colour_source == "import" and _record_rgba(r) != target[r.id]
    }
    held = True
    while held:
        held = False
        for record in matched.values():
            if record.id in movers and any(
                other.id != record.id
                and other.id not in movers
                and _names(other.material, other.subtype, other.brand)
                == _names(record.material, record.subtype, record.brand)
                and _record_rgba(other) == target[record.id]
                for other in records
            ):
                movers.discard(record.id)
                held = True
    return movers


def _look(group: SpoolGroup, colour: str) -> dict[str, object]:
    return {
        "colour": colour,
        "alpha": group.alpha,
        "extra_colours": group.extra_colours,
        "effect": group.effect,
    }


def import_spools(
    store: OrderStore, groups: list[SpoolGroup], densities: DensityTable | None = None
) -> SpoolImport:
    """Make or update one record per group, in one transaction, filling an empty
    density by :func:`~scadbuddy.orders.density.resolve` (``densities`` is the image's
    vendor presets, ``None`` without them). A record is matched by :func:`_matches`, so a
    colour a person changed makes no second record; it takes the group's colour, alpha,
    stops and effect unless a person set them (``colour_source`` ``manual``, #6209), and
    keeps the link to the group's spools. Blocking: call it in a thread."""
    rows: dict[int, SpoolImportRow] = {}
    with store.transaction() as conn:
        records = store.import_records(conn)
        matched = _matches(groups, records)
        movers = _movers(groups, matched, records)
        # Out of the identity index until each takes its new colour, so a swap or a chain
        # of colours never collides on the way (`_movers`).
        store.park_filaments(conn, sorted(movers))
        # The matched groups first, so a new group checks `taken` against the colours the
        # run gave; the rows keep the groups' order.
        for index in sorted(range(len(groups)), key=lambda i: i not in matched):
            group = groups[index]
            spool_ids = [spool_id for spool_id, _ in group.spools]
            priced = [(i, p) for i, raw in group.spools if (p := _price(raw)) is not None]
            prices = {p for _, p in priced}
            row = SpoolImportRow(
                action="skipped",
                material=group.material,
                subtype=group.subtype,
                brand=group.brand,
                colour_name=group.colour_name,
                colour=group.colour,
                alpha=group.alpha,
                extra_colours=group.extra_colours,
                effect=group.effect,
                spool_ids=spool_ids,
                min_cost_per_kg=_text(min(prices)) if len(prices) > 1 else None,
                max_cost_per_kg=_text(max(prices)) if len(prices) > 1 else None,
            )
            skip = _skip_reason(group)
            if skip is not None or group.colour is None:
                rows[index] = row.model_copy(update={"reason": skip})
                continue
            colour = group.colour
            rgba = _rgba(colour, group.alpha)
            price = max(priced)[1]
            record = matched.get(index)
            # The colour and alpha the group would take are another record's of its names
            # now (set by a person or by the import): a second record, or the change,
            # would break the identity index, which holds the alpha (#6261). A record of
            # the RGB at another alpha does not hold it (#6316). A matched record has the
            # group's names (`_same_names`).
            group_names = _names(group.material, group.subtype, group.brand)
            taken = any(
                r.id != (None if record is None else record.id)
                and _names(r.material, r.subtype, r.brand) == group_names
                and _record_rgba(r) == rgba
                for r in records
            )
            if record is None:
                if taken:
                    rows[index] = row.model_copy(update={"reason": "colour_taken"})
                    continue
                found = resolve(group, densities)
                made = store.insert_filament(
                    FilamentInput(
                        material=group.material,
                        subtype=group.subtype,
                        brand=group.brand,
                        colour_name=group.colour_name,
                        colour=colour,
                        cost_per_kg=_text(price),
                        density_g_cm3=None if found is None else found.density_g_cm3,
                        alpha=group.alpha,
                        extra_colours=group.extra_colours,
                        effect=group.effect,
                    ),
                    conn=conn,
                    density_profile=None if found is None else found.profile,
                    colour_source="import",
                    spool_colour=rgba,
                    spool_ids=spool_ids,
                )
                rows[index] = row.model_copy(
                    update={
                        "action": "created",
                        "filament_id": made.id,
                        "colour_source": "import",
                        "cost_per_kg": made.cost_per_kg,
                        "density_g_cm3": made.density_g_cm3,
                        "density_profile": made.density_profile,
                    }
                )
                continue
            current = store.filament(record.id, conn=conn)
            assert current is not None
            previous = Decimal(current.cost_per_kg)
            found = None if current.density_g_cm3 is not None else resolve(group, densities)
            # A record whose colour cannot become the group's (a staying record holds it,
            # `_movers`) keeps its whole look, so it never takes one filament's RGB with
            # another's alpha or effect.
            keep_look = record.id not in movers and _rgba(current.colour, current.alpha) != rgba
            wanted = _look(group, colour)
            look = (
                {
                    name: value
                    for name, value in wanted.items()
                    if (
                        str(value) != current.colour.upper()
                        if name == "colour"
                        else value != getattr(current, name)
                    )
                }
                if current.colour_source == "import" and not keep_look
                else {}
            )
            store.set_import_look(
                conn,
                record.id,
                look=look,
                spool_colour=rgba,
                spool_ids=spool_ids,
                unpark=record.id in movers,
            )
            # Later groups of this run check `taken` against the colour it has now.
            records[records.index(record)] = replace(
                record,
                colour=str(look.get("colour", record.colour)),
                alpha=group.alpha if "alpha" in look else record.alpha,
                spool_colour=rgba,
                spool_ids=spool_ids,
            )
            shown = current.model_copy(update=look)
            update: dict[str, object] = {
                "filament_id": record.id,
                "colour": shown.colour,
                "alpha": shown.alpha,
                "extra_colours": shown.extra_colours,
                "effect": shown.effect,
                "colour_source": shown.colour_source,
                "cost_per_kg": _text(price),
                "previous_cost_per_kg": current.cost_per_kg,
                "density_g_cm3": current.density_g_cm3,
                "density_profile": current.density_profile,
            }
            patch: dict[str, str] = {}
            if previous != price:
                patch["cost_per_kg"] = _text(price)
            if found is not None:
                patch["density_g_cm3"] = found.density_g_cm3
                update["density_g_cm3"] = found.density_g_cm3
                update["density_profile"] = found.profile
            if keep_look and current.colour_source == "import":
                update["reason"] = "colour_taken"
            if not patch and not look:
                rows[index] = row.model_copy(update={**update, "action": "unchanged"})
                continue
            if patch:
                store.update_filament(
                    record.id,
                    FilamentPatch.model_validate(patch),
                    conn=conn,
                    density_profile=None if found is None else found.profile,
                )
            rows[index] = row.model_copy(update={**update, "action": "updated"})
    return SpoolImport(rows=[rows[i] for i in sorted(rows)])


def density_for(
    groups: list[SpoolGroup],
    densities: DensityTable | None,
    material: str,
    subtype: str | None,
    brand: str | None,
    colour: str,
    alpha: int = 255,
) -> ProfileDensity | None:
    """The density the inventory's group of this identity (material, subtype and brand,
    upper-cased, and ``colour`` at ``alpha``) gives by
    :func:`~scadbuddy.orders.density.resolve`; ``None`` with no such group, or no
    source."""
    names, rgba = _names(material, subtype, brand), _rgba(colour, alpha)
    group = next(
        (g for g in groups if g.rgba == rgba and _names(g.material, g.subtype, g.brand) == names),
        None,
    )
    return None if group is None else resolve(group, densities)


def fill_densities(
    store: OrderStore, densities: DensityTable | None, groups: list[SpoolGroup]
) -> DensityFill:
    """Every record of the book with no density, filled by
    :func:`~scadbuddy.orders.density.resolve` from the inventory's group of its identity
    (material, subtype and brand, upper-cased, and its colour at its alpha). One row per
    such record; a row with no density is one no source covers. Never changes a density
    a record has. Blocking: call it in a thread."""
    rows: list[DensityFillRow] = []
    with store.transaction() as conn:
        # Only the empty rows are locked: an edit of another record never waits on it.
        for record in store.filaments(conn=conn, lock=True, no_density=True):
            found = density_for(
                groups,
                densities,
                record.material,
                record.subtype,
                record.brand,
                record.colour,
                record.alpha,
            )
            if found is not None:
                store.update_filament(
                    record.id,
                    FilamentPatch(density_g_cm3=found.density_g_cm3),
                    conn=conn,
                    density_profile=found.profile,
                )
            rows.append(
                DensityFillRow(
                    filament_id=record.id,
                    material=record.material,
                    subtype=record.subtype,
                    brand=record.brand,
                    colour_name=record.colour_name,
                    colour=record.colour,
                    density_g_cm3=None if found is None else found.density_g_cm3,
                    density_profile=None if found is None else found.profile,
                )
            )
    return DensityFill(rows=rows)
