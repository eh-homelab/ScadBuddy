"""Filament records imported from spools (#4253, spec 2026-10-18 §8.3, Ruling 3).

One record per filament the inventory holds (``integrations.SpoolGroup``), matched to
the book by its link to the group's spools, else by material, subtype, brand and the
spools' RGB (#6209, :func:`_matches`; the colour alone would make a second record of one a
person recoloured): a new record when the book has none, else its ``cost_per_kg`` set to
the most recent priced spool's (the highest id). A record takes the spools' colour,
alpha, stops and effect unless a person set them (``colour_source`` ``manual``).
It runs on click only: a quote reads the book, never Bambuddy, so a quote is the same
with Bambuddy down. A group known only by its colour's name, or with no priced spool, is
reported and left alone: a record needs a ``#RRGGBB`` and a price.

A record with no density gets the one Bambuddy's link to the slicer preset gives
(#6136, `orders/density.py` `resolve`). A density a record has is never changed.
:func:`fill_densities` does the same for every record of the book with none
(``POST …/price-book/fill-densities``), and a filament added by hand with no density
takes :func:`density_for`'s; a record no spool of the inventory matches has no source.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from typing import Literal

from scadbuddy.orders.density import DensityTable, ProfileDensity, resolve
from scadbuddy.orders.integrations import SpoolGroup
from scadbuddy.orders.models import (
    DensityFill,
    DensityFillRow,
    FilamentInput,
    FilamentPatch,
    SpoolImport,
    SpoolImportRow,
)
from scadbuddy.orders.store import ImportRecord, OrderStore

SkipReason = Literal["no_colour", "no_price", "too_long"]

#: ``filaments.cost_per_kg``'s ``numeric(12,4)``.
PLACES = Decimal("0.0001")
MAX_COST = Decimal(10**8)
#: ``FilamentInput``'s ``max_length`` for each name.
MAX_NAME = 100


def _price(value: float | None) -> Decimal | None:
    if value is None:
        return None
    try:
        price = Decimal(str(value)).quantize(PLACES)
    except InvalidOperation:
        return None
    return price if Decimal(0) <= price < MAX_COST else None


def _text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _identity(
    material: str, subtype: str | None, brand: str | None, colour: str | None
) -> tuple[str, str, str, str]:
    return (
        material.strip().upper(),
        (subtype or "").strip().upper(),
        (brand or "").strip().upper(),
        (colour or "").upper(),
    )


def _names(material: str, subtype: str | None, brand: str | None) -> tuple[str, str, str]:
    return _identity(material, subtype, brand, None)[:3]


def _same_names(
    group: SpoolGroup, records: list[ImportRecord], claimed: set[str]
) -> list[ImportRecord]:
    """The records not yet taken that have ``group``'s material, subtype and brand. A spool
    whose material was corrected in Bambuddy is another filament's, never its old record's."""
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
    spool_colour: str | None = None,
) -> ImportRecord | None:
    """The record of ``group``'s names linked to most of its spools (#6209); with
    ``spool_colour``, only among the records whose spools had that RGB."""
    ids = {spool_id for spool_id, _ in group.spools}
    linked = [
        r
        for r in _same_names(group, records, claimed)
        if ids & set(r.spool_ids) and spool_colour in (None, r.spool_colour)
    ]
    return max(linked, key=lambda r: len(ids & set(r.spool_ids))) if linked else None


def _by_colour(
    group: SpoolGroup, colour: str, records: list[ImportRecord], claimed: set[str]
) -> ImportRecord | None:
    """The record of ``group``'s names whose spools had its RGB at the last import, else
    one with no other spools' RGB whose colour is its RGB (#6209)."""
    same = _same_names(group, records, claimed)
    return next((r for r in same if r.spool_colour == colour), None) or next(
        (r for r in same if r.colour.upper() == colour and r.spool_colour in (None, colour)),
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
    RGB its spools had (``spool_colour``), as when one spool of a record was recoloured;
    then any record linked to a group's spools, as when all of them were; only then a
    record by colour (:func:`_by_colour`). A spool link always wins over a colour."""
    matched: dict[int, ImportRecord] = {}
    claimed: set[str] = set()
    eligible = [(i, g) for i, g in enumerate(groups) if _skip_reason(g) is None]

    def take(pick: Callable[[SpoolGroup, str], ImportRecord | None]) -> None:
        for index, group in eligible:
            assert group.colour is not None
            if index in matched:
                continue
            record = pick(group, group.colour)
            if record is not None:
                matched[index] = record
                claimed.add(record.id)

    take(lambda group, colour: _linked(group, records, claimed, colour))
    take(lambda group, _colour: _linked(group, records, claimed))
    take(lambda group, colour: _by_colour(group, colour, records, claimed))
    return matched


def _movers(
    groups: list[SpoolGroup], matched: dict[int, ImportRecord], records: list[ImportRecord]
) -> set[str]:
    """The matched records whose colour the import changes in this run (#6211 review): each
    ``import`` record whose group's RGB differs from its colour, unless a record that stays
    (one not moving, ``manual`` or not matched) holds that RGB among its names. A record
    held back stays too, so its colour may hold back another, until none changes. The
    movers' new colours are distinct and none is a staying record's, so they can all move
    at once, a chain or a swap of colours included (:func:`import_spools` parks them
    first)."""
    target = {r.id: groups[i].colour for i, r in matched.items()}
    movers = {
        r.id
        for r in matched.values()
        if r.colour_source == "import" and r.colour.upper() != target[r.id]
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
                and other.colour.upper() == target[record.id]
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
            price = max(priced)[1]
            record = matched.get(index)
            # The colour the group would take is a colour another record of its names
            # holds now (set by a person or by the import): a second record, or the
            # change, would break the identity index. A matched record has the group's
            # names (`_same_names`).
            group_names = _names(group.material, group.subtype, group.brand)
            taken = any(
                r.id != (None if record is None else record.id)
                and _names(r.material, r.subtype, r.brand) == group_names
                and r.colour.upper() == colour
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
                    spool_colour=colour,
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
            keep_look = record.id not in movers and current.colour.upper() != colour
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
                spool_colour=colour,
                spool_ids=spool_ids,
                unpark=record.id in movers,
            )
            # Later groups of this run check `taken` against the colour it has now.
            records[records.index(record)] = replace(
                record,
                colour=str(look.get("colour", record.colour)),
                spool_colour=colour,
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
) -> ProfileDensity | None:
    """The density the inventory's group of this identity (material, subtype, brand and
    colour, upper-cased) gives by :func:`~scadbuddy.orders.density.resolve`; ``None``
    with no such group, or no source."""
    key = _identity(material, subtype, brand, colour)
    group = next(
        (
            g
            for g in groups
            if g.colour is not None and _identity(g.material, g.subtype, g.brand, g.colour) == key
        ),
        None,
    )
    return None if group is None else resolve(group, densities)


def fill_densities(
    store: OrderStore, densities: DensityTable | None, groups: list[SpoolGroup]
) -> DensityFill:
    """Every record of the book with no density, filled by
    :func:`~scadbuddy.orders.density.resolve` from the inventory's group of its identity
    (material, subtype, brand and colour, upper-cased). One row per such record; a row
    with no density is one no source covers. Never changes a density a record has.
    Blocking: call it in a thread."""
    rows: list[DensityFillRow] = []
    with store.transaction() as conn:
        # Only the empty rows are locked: an edit of another record never waits on it.
        for record in store.filaments(conn=conn, lock=True, no_density=True):
            found = density_for(
                groups, densities, record.material, record.subtype, record.brand, record.colour
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
