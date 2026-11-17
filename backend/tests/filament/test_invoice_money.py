from decimal import Decimal as Dec

import pytest

from scadbuddy.filament.invoice_money import (
    MixedLineTaxError,
    allocate,
    difference,
    price_per_kg,
    price_per_pack,
    spool_cost_per_kg,
    spread,
    tax_per_line,
)


def test_spread_adds_up_exactly() -> None:
    shares = spread(Dec("5.00"), [Dec("10"), Dec("10"), Dec("10")])
    assert sum(shares) == Dec("5.0000")
    assert shares == [Dec("1.6667"), Dec("1.6667"), Dec("1.6666")]
    assert sum(spread(Dec("0.01"), [Dec(1), Dec(1), Dec(1)])) == Dec("0.0100")


def test_a_single_line_takes_all_of_it() -> None:
    assert spread(Dec("4.50"), [Dec("9")]) == [Dec("4.5000")]


def test_a_free_line_takes_no_share() -> None:
    assert spread(Dec("3"), [Dec("0"), Dec("30")]) == [Dec("0.0000"), Dec("3.0000")]
    assert spread(Dec("3"), [Dec("0"), Dec("0")]) == [Dec("1.5000"), Dec("1.5000")]


def test_receipt_wide_tax_names_its_rate() -> None:
    assert tax_per_line([Dec("40"), Dec("60")], [None, None], Dec("7")) == (
        [Dec("2.8000"), Dec("4.2000")],
        "receipt-wide 7.0%",
    )


def test_line_tax_is_kept_as_given() -> None:
    assert tax_per_line([Dec("20"), Dec("30")], [Dec("1.40"), Dec("2.10")], Dec("0")) == (
        [Dec("1.4000"), Dec("2.1000")],
        "line",
    )


def test_mixed_line_tax_is_refused() -> None:
    with pytest.raises(MixedLineTaxError):
        tax_per_line([Dec("20"), Dec("30")], [Dec("1"), None], Dec("0"))


def test_no_tax_has_basis_none() -> None:
    assert tax_per_line([Dec("20"), Dec("30")], [None, None], Dec("0")) == (
        [Dec("0.0000"), Dec("0.0000")],
        "none",
    )


def test_allocation_adds_shipping_by_value() -> None:
    assert allocate([Dec("30"), Dec("70")], Dec("10")) == [Dec("33.0000"), Dec("77.0000")]


def test_spool_cost_per_kg() -> None:
    assert spool_cost_per_kg(Dec("21.4000"), 1, 1000) == Dec("21.4000")
    assert spool_cost_per_kg(Dec("30"), 2, 500) == Dec("30.0000")


def test_price_per_pack_and_kg() -> None:
    assert price_per_pack(Dec("60"), 3, 2) == Dec("40.0000")
    assert price_per_kg(Dec("40"), 2, 1000) == Dec("20.0000")


def test_difference() -> None:
    lines = [Dec("30"), Dec("70")]
    assert difference(Dec("110"), lines, Dec("10")) == Dec("0.0000")
    assert difference(Dec("110.04"), lines, Dec("10")) == Dec("0.0400")
