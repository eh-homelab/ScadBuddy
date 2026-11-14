"""Filament records imported from spools (#4253, spec 2026-10-18 §8.3, Ruling 3).

One record per filament the inventory holds (``integrations.SpoolGroup``), matched to
the book by the record's identity (material, subtype, brand and colour, upper-cased, as
``filaments_identity`` and ``_filament_key`` both are): a new record when the book has
none, else its ``cost_per_kg`` set to the most recent priced spool's (the highest id).
A record's density comes from the group's newest spool's slicer preset
(``SpoolGroup.density_g_cm3``, #6168): set on a record made, and on a matched record only
when it has none, so a density someone entered is never overwritten. A preset that gives
none leaves the density empty and is reported on the row; it fails nothing.
It runs on click only: a quote reads the book, never Bambuddy, so a quote is the same
with Bambuddy down. A group known only by its colour's name, or with no priced spool, is
reported and left alone: a record needs a ``#RRGGBB`` and a price.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from scadbuddy.orders.integrations import SpoolGroup
from scadbuddy.orders.models import (
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


def import_spools(store: OrderStore, groups: list[SpoolGroup]) -> SpoolImport:
    """Make or update one record per group, in one transaction. Blocking: call it in a
    thread."""
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
                made = store.insert_filament(
                    FilamentInput(
                        material=group.material,
                        subtype=group.subtype,
                        brand=group.brand,
                        colour_name=group.colour_name,
                        colour=group.colour,
                        cost_per_kg=_text(price),
                        density_g_cm3=group.density_g_cm3,
                    ),
                    conn=conn,
                )
                rows.append(
                    row.model_copy(
                        update={
                            "action": "created",
                            "filament_id": made.id,
                            "cost_per_kg": made.cost_per_kg,
                            **_density(group, made.density_g_cm3),
                        }
                    )
                )
                continue
            current = store.filament(existing_id, conn=conn)
            assert current is not None
            previous = Decimal(current.cost_per_kg)
            patch: dict[str, str] = {}
            if previous != price:
                patch["cost_per_kg"] = _text(price)
            density = current.density_g_cm3
            if density is None and group.density_g_cm3 is not None:
                density = patch["density_g_cm3"] = group.density_g_cm3
            update: dict[str, object] = {
                "filament_id": existing_id,
                "cost_per_kg": _text(price),
                "previous_cost_per_kg": current.cost_per_kg,
                **_density(group, density),
            }
            if not patch:
                rows.append(row.model_copy(update={**update, "action": "unchanged"}))
                continue
            store.update_filament(existing_id, FilamentPatch.model_validate(patch), conn=conn)
            rows.append(row.model_copy(update={**update, "action": "updated"}))
    return SpoolImport(rows=rows)


def _density(group: SpoolGroup, density: str | None) -> dict[str, object]:
    """A made or matched record's density on its row, and why it has none."""
    if density is not None:
        return {"density_g_cm3": density}
    return {"no_density": "no_preset" if group.preset is None else "not_in_preset"}
