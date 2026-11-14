"""The rough estimate (#4250 spec 2026-10-18 §5.3; #5282 spec 2026-11-02 §5.4): grams from
a render's part volumes (`orders/pricing.py` `estimate_grams`), never a quote, never
stored as one.

The back office's ``GET /orders/estimate`` prices an output's parts at one filament
(`filament_estimate`). The storefront's estimate prices a render job's parts, each at the
listing filament the visitor chose for its colour, through the quote's formula without
machine time (`rough_estimate`, plan point 4): a render knows no print seconds.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel

from scadbuddy.orders.currency import minor_exponent
from scadbuddy.orders.density import UsedDensity, used_density
from scadbuddy.orders.models import Filament, Money, PricingPolicy, QuoteLine
from scadbuddy.orders.pricing import Plate, PlateFilament, estimate_grams, price, to_minor
from scadbuddy.render.geometry import analyze_3mf


class EstimatedPart(BaseModel):
    """One part of one plate: its enclosed volume, or ``None`` when it is not checked
    and closed (``render/geometry.py`` measures only those), and so not counted."""

    plate: int
    part: int
    name: str
    colour: str
    volume_mm3: float | None


class OrderEstimate(BaseModel):
    """A rough estimate (§5.3): never a quote, never stored, never accepted."""

    #: Always true: this is an estimate.
    estimate: bool = True
    #: Decimal string, to the hundredth of a gram.
    grams: str
    #: The grams at the filament's cost per kilogram, rounded as a quote line is.
    filament_minor: Money
    currency: str
    minor_exponent: int
    #: ``rough_estimate_factor`` as it stood.
    factor: str
    parts: list[EstimatedPart]
    #: How many parts have no volume and are left out of ``grams``.
    unknown_parts: int
    #: The density each filament was weighed with, and where it came from (#6136).
    densities: list[UsedDensity] = []


class RoughEstimate(OrderEstimate):
    """A render's estimate (spec 2026-11-02 §5.4): a price with every line of a quote's
    formula but machine time. Its lines name the price book's rates, so a visitor gets
    only its `StoreEstimate`."""

    price_minor: Money
    lines: list[QuoteLine]

    def public(self) -> StoreEstimate:
        """What a visitor may read: the price, never the cost, the markup or a rate."""
        return StoreEstimate(
            price_minor=self.price_minor,
            currency=self.currency,
            minor_exponent=self.minor_exponent,
            grams=self.grams,
            parts=self.parts,
            unknown_parts=self.unknown_parts,
        )


class StoreEstimate(BaseModel):
    """The storefront's estimate of a render, as a visitor reads it. The public page
    shows it as "about $X, confirmed after we check it", never as a quote. It carries no
    line, no filament cost and no label that names a rate (the edge sends on only these
    fields, `storefront/routes.py` `ESTIMATE_FIELDS`)."""

    #: Always true: this is an estimate.
    estimate: bool = True
    price_minor: Money
    currency: str
    minor_exponent: int
    #: Decimal string, to the hundredth of a gram.
    grams: str
    parts: list[EstimatedPart]
    #: How many parts have no volume and are left out of ``grams``.
    unknown_parts: int


def part_volumes(model_3mf: Path, plates: int, warnings: Sequence[str]) -> list[EstimatedPart]:
    """Every part of plates 1 to ``plates`` of a ScadBuddy 3MF, with its enclosed volume.
    Blocking: call it in a thread. ``ValueError`` or ``zipfile.BadZipFile`` for a 3MF that
    cannot be analysed."""
    parts: list[EstimatedPart] = []
    for plate in range(1, plates + 1):
        analysis = analyze_3mf(model_3mf, warnings=warnings, plate=plate)
        parts += [
            EstimatedPart(
                plate=plate,
                part=part.part,
                name=part.name,
                colour=part.colour,
                volume_mm3=part.volume_mm3,
            )
            for part in analysis.parts
        ]
    return parts


def filament_estimate(
    parts: Sequence[EstimatedPart],
    record: Filament,
    *,
    currency: str,
    factor: str,
) -> OrderEstimate:
    """Every part at one filament (the back office's estimate), weighed with its
    density. ``ValueError`` for a filament with no density."""
    used = used_density(record)
    grams = estimate_grams(
        [part.volume_mm3 for part in parts if part.volume_mm3 is not None],
        used.density_g_cm3,
        factor,
    )
    return OrderEstimate(
        grams=format(grams, "f"),
        filament_minor=to_minor(grams * Decimal(record.cost_per_kg) / 1000, currency),
        currency=currency,
        minor_exponent=minor_exponent(currency),
        factor=factor,
        parts=list(parts),
        unknown_parts=sum(1 for part in parts if part.volume_mm3 is None),
        densities=[used],
    )


def rough_estimate(
    parts: Sequence[EstimatedPart],
    filament_for: Callable[[str], Filament],
    policy: PricingPolicy,
    *,
    currency: str,
    factor: str,
    quantity: int = 1,
) -> RoughEstimate:
    """Each part's grams at the filament ``filament_for`` gives for its colour, priced
    under ``policy`` as a quote is but with no machine time: a filament line per
    filament, a setup line per plate, labour, markup and rounding. ``ValueError`` for a
    filament with no density."""
    slots: dict[str, int] = {}
    records: dict[int, Filament] = {}
    volumes: dict[tuple[int, int], list[float]] = {}
    used: dict[int, UsedDensity] = {}
    for part in parts:
        record = filament_for(part.colour)
        slot = slots.setdefault(record.id, len(slots) + 1)
        records[slot] = record
        if part.volume_mm3 is not None:
            # Only a colour that weighs something needs its filament's density.
            if slot not in used:
                used[slot] = used_density(record)
            volumes.setdefault((part.plate, slot), []).append(part.volume_mm3)
    plates: list[Plate] = []
    total = Decimal(0)
    for index in sorted({part.plate for part in parts}):
        weighed: list[PlateFilament] = []
        for slot in sorted(records):
            if (index, slot) not in volumes:
                continue
            grams = estimate_grams(volumes[index, slot], used[slot].density_g_cm3, factor)
            total += grams
            weighed.append(PlateFilament(slot=slot, used_g=grams))
        plates.append(Plate(index=index, print_seconds=0, filaments=weighed))
    priced = price(plates, records, policy, currency=currency, quantity=quantity, estimated=True)
    return RoughEstimate(
        grams=format(total, "f"),
        filament_minor=sum(line.amount_minor for line in priced.lines if line.kind == "filament"),
        currency=currency,
        minor_exponent=minor_exponent(currency),
        factor=factor,
        parts=list(parts),
        unknown_parts=sum(1 for part in parts if part.volume_mm3 is None),
        price_minor=priced.total_minor,
        lines=priced.lines,
        densities=[used[slot] for slot in sorted(used)],
    )
