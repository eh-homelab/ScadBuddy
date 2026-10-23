"""A quote's arithmetic (#4250, spec 2026-10-18 §3.6, §5.2 step 4; plan Ruling 5):
on ``Decimal``, each line rounded half up to the currency's minor unit once, the totals
the sum of rounded lines, and ``to_major``'s difference a line of its own. Literal
plates, no slice, no Postgres."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from scadbuddy.orders.models import Filament, PricingPolicy
from scadbuddy.orders.pricing import (
    Plate,
    PlateFilament,
    PricingError,
    estimate_grams,
    price,
    to_minor,
)

NOW = datetime(2026, 10, 22, tzinfo=UTC)


def filament(material: str, cost: str, *, subtype: str | None = None) -> Filament:
    return Filament(
        id=f"f-{material}-{cost}",
        material=material,
        subtype=subtype,
        brand=None,
        colour_name=None,
        colour="#FFFFFF",
        cost_per_kg=cost,
        density_g_cm3=None,
        archived_at=None,
        created_at=NOW,
        updated_at=NOW,
    )


def plates() -> list[Plate]:
    """Two plates: slot 1 on both, slot 2 on the first only."""
    return [
        Plate(
            index=1,
            print_seconds=3600,
            filaments=[
                PlateFilament(slot=1, used_g=Decimal("12.345")),
                PlateFilament(slot=2, used_g=Decimal("3.0")),
            ],
        ),
        Plate(index=2, print_seconds=1800, filaments=[PlateFilament(slot=1, used_g=Decimal("10"))]),
    ]


POLICY = PricingPolicy(
    machine_rate_per_hour="1.50",
    setup_fee="0.75",
    labour_per_item="2",
    markup_percent="15",
)
USD_BOOK = {1: filament("PLA", "25.50", subtype="Basic"), 2: filament("PETG", "19.99")}


def test_each_line_rounds_once_and_totals_add_lines() -> None:
    priced = price(plates(), USD_BOOK, POLICY, currency="USD", quantity=3)
    amounts = [(line.kind, line.amount_minor) for line in priced.lines]
    assert amounts == [
        # 22.345 g x 25.50 / 1000 = 0.5697975 -> 0.57
        ("filament", 57),
        # 3.0 g x 19.99 / 1000 = 0.05997 -> 0.06
        ("filament", 6),
        # 5400 s = 1.5 h x 1.50 = 2.25
        ("machine_time", 225),
        # One per plate the item prints (Ruling 2).
        ("setup", 75),
        ("setup", 75),
        ("labour", 200),
        # 15 % of the rounded lines above (638) = 95.7 -> 96
        ("markup", 96),
    ]
    assert priced.unit_cost_minor == 57 + 6 + 225
    assert priced.unit_price_minor == sum(amount for _, amount in amounts) == 734
    assert priced.total_minor == 734 * 3

    # Two lines of half a cent each: each rounds up on its own, so the lines say 2 where
    # the unrounded sum (0.010) would say 1. The totals are the lines'.
    halves = price(
        [
            Plate(
                index=1,
                print_seconds=60,
                filaments=[
                    PlateFilament(slot=1, used_g=Decimal("5")),
                    PlateFilament(slot=2, used_g=Decimal("5")),
                ],
            )
        ],
        {1: filament("PLA", "1"), 2: filament("PETG", "1")},
        PricingPolicy(),
        currency="USD",
        quantity=1,
    )
    assert [line.amount_minor for line in halves.lines] == [1, 1]
    assert halves.unit_price_minor == 2


def test_to_major_rounds_the_unit_price_up_as_a_rounding_line() -> None:
    up = POLICY.model_copy(update={"rounding": "to_major"})
    priced = price(plates(), USD_BOOK, up, currency="USD", quantity=2)
    assert priced.lines[-1].kind == "rounding"
    # 7.34 up to 8.00: the difference is its own line, so the lines still add up.
    assert priced.lines[-1].amount_minor == 66
    assert priced.unit_price_minor == 800 == sum(line.amount_minor for line in priced.lines)
    assert priced.total_minor == 1600
    # The cost is the filament and the machine's time, whatever the rounding.
    assert priced.unit_cost_minor == 288

    # A unit price that is whole already gets a rounding line of 0.
    whole = price(
        [Plate(index=1, print_seconds=3600, filaments=[])],
        {},
        PricingPolicy(machine_rate_per_hour="3", rounding="to_major"),
        currency="USD",
        quantity=1,
    )
    assert [(line.kind, line.amount_minor) for line in whole.lines] == [
        ("machine_time", 300),
        ("rounding", 0),
    ]


def test_zero_decimal_and_three_decimal_currencies() -> None:
    yen = price(
        plates(),
        {1: filament("PLA", "3000"), 2: filament("PETG", "2500")},
        PricingPolicy(machine_rate_per_hour="200", setup_fee="100", markup_percent="10"),
        currency="JPY",
        quantity=1,
    )
    assert [(line.kind, line.amount_minor) for line in yen.lines] == [
        # 67.035 yen -> 67; 7.5 yen -> 8 (half up, to whole yen)
        ("filament", 67),
        ("filament", 8),
        ("machine_time", 300),
        ("setup", 100),
        ("setup", 100),
        # 10 % of 575 = 57.5 -> 58
        ("markup", 58),
    ]
    assert yen.unit_price_minor == 633

    dinar = price(
        plates(),
        {1: filament("PLA", "7.5"), 2: filament("PETG", "6")},
        PricingPolicy(machine_rate_per_hour="0.5"),
        currency="KWD",
        quantity=1,
    )
    # In fils, thousandths: 0.1675875 -> 0.168, 0.018, 0.75.
    assert [line.amount_minor for line in dinar.lines] == [168, 18, 750]
    assert dinar.unit_price_minor == 936


def test_a_slot_with_no_filament_is_an_error() -> None:
    with pytest.raises(PricingError) as raised:
        price(plates(), {1: USD_BOOK[1]}, POLICY, currency="USD", quantity=1)
    assert raised.value.slot == 2
    assert "slot 2" in str(raised.value)


def test_a_plate_with_no_seconds_is_an_error() -> None:
    unknown = [*plates(), Plate(index=3, print_seconds=0, filaments=[])]
    with pytest.raises(PricingError) as raised:
        price(unknown, USD_BOOK, POLICY, currency="USD", quantity=1)
    assert raised.value.plate == 3
    assert "plate 3" in str(raised.value)


def test_the_lines_carry_the_rates_they_were_priced_at() -> None:
    """§5.1: the book is copied into each line, so a later edit never moves a quote."""
    priced = price(plates(), USD_BOOK, POLICY, currency="USD", quantity=1)
    lines = {(line.kind, line.label): line for line in priced.lines}
    shown = [(line.kind, line.label, line.quantity, line.unit_minor) for line in priced.lines]
    assert shown == [
        ("filament", "PLA Basic, 22.345 g at 25.50 per kg", 1, 57),
        ("filament", "PETG, 3 g at 19.99 per kg", 1, 6),
        ("machine_time", "Machine time, 1.5 h at 1.50 per hour", 1, 225),
        ("setup", "Setup, plate 1", 1, 75),
        ("setup", "Setup, plate 2", 1, 75),
        ("labour", "Labour", 1, 200),
        ("markup", "Markup, 15 %", 1, 96),
    ]
    assert all(line.unit_minor * line.quantity == line.amount_minor for line in lines.values())


def test_a_rate_of_zero_adds_no_line() -> None:
    priced = price(plates(), USD_BOOK, PricingPolicy(), currency="USD", quantity=1)
    assert [line.kind for line in priced.lines] == ["filament", "filament"]


def test_the_rough_estimate_in_grams() -> None:
    """§5.3: volume (mm3 / 1000 = cm3) x density x factor."""
    grams = estimate_grams([Decimal("10000"), 5000.0], Decimal("1.24"), Decimal("1.15"))
    assert grams == Decimal("21.39")
    assert to_minor(grams * Decimal("25.5") / 1000, "USD") == 55
