"""A quote's arithmetic (#4250, spec 2026-10-18 §3.6, §5.2 step 4; plan Ruling 5): pure,
on ``Decimal``, never a float.

Each line's amount is rounded to the currency's minor unit (:func:`minor_exponent`, the
backend's one table) half up, once, at the line
(https://docs.python.org/3/library/decimal.html#rounding-modes), and the totals add the
rounded lines. The lines are, in order: one ``filament`` line per slot (its grams summed
over every plate, times the record's cost per kilogram), one ``machine_time`` line (every
plate's seconds), one ``setup`` line per plate, one ``labour`` line, one ``markup`` line (a
percentage of the lines above it) and, under ``to_major``, a ``rounding`` line that takes
the unit price up to the next whole currency unit (0 when it is whole already), so the
lines always add to the unit price. A policy rate of 0 adds no line. Each line carries the
rate it was priced at in its label (and a setup line its fee as ``unit_minor``), so a later
change to the book never moves a recorded quote (§5.1).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

from pydantic import BaseModel, Field

from scadbuddy.orders.currency import minor_exponent
from scadbuddy.orders.models import Filament, Money, PricingPolicy, QuoteLine

SECONDS_PER_HOUR = Decimal(3600)
GRAMS_PER_KG = Decimal(1000)
MM3_PER_CM3 = Decimal(1000)
#: The estimate's grams are shown to the hundredth.
GRAMS_SHOWN = Decimal("0.01")


class PlateFilament(BaseModel):
    """One slot's grams on one plate, as the sliced file says (``read_sliced``)."""

    slot: int
    used_g: Decimal


class Plate(BaseModel):
    """One sliced plate, as a quote's ``plates`` stores it (§3.4)."""

    index: int
    print_seconds: int
    filaments: list[PlateFilament] = Field(default_factory=list)
    #: The plate's own sliced file (Ruling 3): its blob key and digest; ``None`` on a
    #: plate priced without a slice.
    blob_key: str | None = None
    sha256: str | None = None


class Priced(BaseModel):
    lines: list[QuoteLine]
    unit_cost_minor: Money
    unit_price_minor: Money
    total_minor: Money


class PricingError(ValueError):
    """Nothing would price this honestly: a slot with no filament (``slot``), or a plate
    whose seconds are unknown (``plate``)."""

    def __init__(self, message: str, *, slot: int | None = None, plate: int | None = None):
        super().__init__(message)
        self.slot = slot
        self.plate = plate


def _text(number: Decimal) -> str:
    """The shortest decimal string of ``number``: ``1.50`` stays as typed elsewhere, but
    a computed ``1.5000`` reads ``1.5``."""
    return format(number.normalize(), "f")


def to_minor(amount: Decimal, currency: str) -> Money:
    """``amount`` in major units as integer minor units of ``currency``, rounded half up."""
    scaled = amount.scaleb(minor_exponent(currency))
    return int(scaled.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def filament_name(record: Filament) -> str:
    """``PLA Basic``, with the colour's name when the record has one."""
    return " ".join(part for part in (record.material, record.subtype, record.colour_name) if part)


def _line(kind: str, label: str, amount: Money) -> QuoteLine:
    """Every line is one of itself (a filament's grams are on its label, not its
    integer ``quantity``; a setup line is one plate's fee)."""
    return QuoteLine.model_validate(
        {"kind": kind, "label": label, "quantity": 1, "unit_minor": amount, "amount_minor": amount}
    )


def price(
    plates: Sequence[Plate],
    filaments: Mapping[int, Filament],
    policy: PricingPolicy,
    *,
    currency: str,
    quantity: int,
) -> Priced:
    """The lines and totals of ``plates`` priced under ``policy`` with the slot's
    ``filaments`` (keyed by the plates' slot numbers). :class:`PricingError` for a
    slot no record covers, or a plate with no seconds."""
    for plate in plates:
        if plate.print_seconds <= 0:
            raise PricingError(f"plate {plate.index}'s print time is unknown", plate=plate.index)
    grams: dict[int, Decimal] = {}
    for plate in plates:
        for used in plate.filaments:
            grams[used.slot] = grams.get(used.slot, Decimal(0)) + used.used_g
    lines: list[QuoteLine] = []
    for slot in sorted(grams):
        record = filaments.get(slot)
        if record is None:
            raise PricingError(f"slot {slot} has no filament to price it with", slot=slot)
        rate = Decimal(record.cost_per_kg)
        amount = to_minor(grams[slot] * rate / GRAMS_PER_KG, currency)
        label = f"{filament_name(record)}, {_text(grams[slot])} g at {record.cost_per_kg} per kg"
        lines.append(_line("filament", label, amount))

    machine_rate = Decimal(policy.machine_rate_per_hour)
    if machine_rate:
        hours = Decimal(sum(plate.print_seconds for plate in plates)) / SECONDS_PER_HOUR
        label = f"Machine time, {_text(hours)} h at {policy.machine_rate_per_hour} per hour"
        lines.append(_line("machine_time", label, to_minor(hours * machine_rate, currency)))
    unit_cost = sum(line.amount_minor for line in lines)

    setup_fee = Decimal(policy.setup_fee)
    if setup_fee:
        fee = to_minor(setup_fee, currency)
        for plate in plates:
            lines.append(_line("setup", f"Setup, plate {plate.index}", fee))
    labour = Decimal(policy.labour_per_item)
    if labour:
        lines.append(_line("labour", "Labour", to_minor(labour, currency)))
    markup = Decimal(policy.markup_percent)
    if markup:
        base = Decimal(sum(line.amount_minor for line in lines))
        # Already in minor units: a percentage of them, rounded half up once.
        amount = int((base * markup / 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        lines.append(_line("markup", f"Markup, {policy.markup_percent} %", amount))

    unit_price = sum(line.amount_minor for line in lines)
    if policy.rounding == "to_major":
        exponent = minor_exponent(currency)
        major = Decimal(unit_price).scaleb(-exponent)
        whole = int(major.quantize(Decimal(1), rounding=ROUND_CEILING).scaleb(exponent))
        lines.append(_line("rounding", "Rounded up to a whole unit", whole - unit_price))
        unit_price = whole
    return Priced(
        lines=lines,
        unit_cost_minor=unit_cost,
        unit_price_minor=unit_price,
        total_minor=unit_price * quantity,
    )


def estimate_grams(
    volumes_mm3: Iterable[Decimal | float], density_g_cm3: Decimal | str, factor: Decimal | str
) -> Decimal:
    """§5.3's rough grams: the parts' enclosed volume in cm³ (mm³ / 1000) times the
    filament's density and the shop's factor for walls, infill and purge, to 0.01 g. A
    float volume (as ``render/geometry.py`` measures one) enters through ``str``."""
    volume = sum((Decimal(str(v)) for v in volumes_mm3), Decimal(0)) / MM3_PER_CM3
    grams = volume * Decimal(density_g_cm3) * Decimal(factor)
    return grams.quantize(GRAMS_SHOWN, rounding=ROUND_HALF_UP)
