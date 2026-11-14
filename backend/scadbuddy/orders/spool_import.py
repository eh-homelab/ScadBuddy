"""Filament records imported from spools (#4253, spec 2026-10-18 §8.3, Ruling 3).

One record per filament the inventory holds (``integrations.SpoolGroup``), matched to
the book by the record's identity (material, subtype, brand and colour, upper-cased, as
``filaments_identity`` and ``_filament_key`` both are): a new record when the book has
none, else its ``cost_per_kg`` set to the most recent priced spool's (the highest id).
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

from decimal import Decimal, InvalidOperation

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
from scadbuddy.orders.store import OrderStore

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


def import_spools(
    store: OrderStore, groups: list[SpoolGroup], densities: DensityTable | None = None
) -> SpoolImport:
    """Make or update one record per group, in one transaction, filling an empty
    density by :func:`~scadbuddy.orders.density.resolve` (``densities`` is the image's
    vendor presets, ``None`` without them). Blocking: call it in a thread."""
    rows: list[SpoolImportRow] = []
    with store.transaction() as conn:
        for group in groups:
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
                spool_ids=spool_ids,
                min_cost_per_kg=_text(min(prices)) if len(prices) > 1 else None,
                max_cost_per_kg=_text(max(prices)) if len(prices) > 1 else None,
            )
            if group.colour is None:
                rows.append(row.model_copy(update={"reason": "no_colour"}))
                continue
            if not priced:
                rows.append(row.model_copy(update={"reason": "no_price"}))
                continue
            names = (group.material, group.subtype, group.brand, group.colour_name)
            if any(name is not None and len(name) > MAX_NAME for name in names):
                rows.append(row.model_copy(update={"reason": "too_long"}))
                continue
            price = max(priced)[1]
            existing_id = store.existing_filament(
                conn, group.material, group.subtype, group.brand, group.colour
            )
            if existing_id is None:
                found = resolve(group, densities)
                made = store.insert_filament(
                    FilamentInput(
                        material=group.material,
                        subtype=group.subtype,
                        brand=group.brand,
                        colour_name=group.colour_name,
                        colour=group.colour,
                        cost_per_kg=_text(price),
                        density_g_cm3=None if found is None else found.density_g_cm3,
                    ),
                    conn=conn,
                    density_profile=None if found is None else found.profile,
                )
                rows.append(
                    row.model_copy(
                        update={
                            "action": "created",
                            "filament_id": made.id,
                            "cost_per_kg": made.cost_per_kg,
                            "density_g_cm3": made.density_g_cm3,
                            "density_profile": made.density_profile,
                        }
                    )
                )
                continue
            current = store.filament(existing_id, conn=conn, lock=True)
            assert current is not None
            previous = Decimal(current.cost_per_kg)
            found = None if current.density_g_cm3 is not None else resolve(group, densities)
            update: dict[str, object] = {
                "filament_id": existing_id,
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
            if not patch:
                rows.append(row.model_copy(update={**update, "action": "unchanged"}))
                continue
            store.update_filament(
                existing_id,
                FilamentPatch(**patch),
                conn=conn,
                density_profile=None if found is None else found.profile,
            )
            rows.append(row.model_copy(update={**update, "action": "updated"}))
    return SpoolImport(rows=rows)


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
        for record in store.filaments(conn=conn, lock=True):
            if record.density_g_cm3 is not None:
                continue
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
