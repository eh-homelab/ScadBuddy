"""The catalog's cost calculator (#7152): a spool's price, tax, shipping and net weight per
kg, on `Decimal` with the invoice's rounding."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from scadbuddy.filament.cost_calc import CostQuery, CostTooHighError, calculate
from scadbuddy.filament.invoice_money import spool_cost_per_kg


def _query(**over: object) -> CostQuery:
    return CostQuery.model_validate({"price": "19.99", "net_weight_g": 1000} | over)


def test_one_spool_of_a_kilo_costs_its_price() -> None:
    found = calculate(_query(), "USD")
    assert found.currency == "USD"
    assert found.cost_per_kg == Decimal("19.9900")
    assert found.price_per_kg == Decimal("19.9900")
    assert found.total == Decimal("19.99")


def test_tax_and_shipping_go_into_the_cost_and_only_tax_into_the_price() -> None:
    found = calculate(
        _query(price="40", tax="3.20", shipping="5", quantity=2, net_weight_g=750), "EUR"
    )
    assert found.total == Decimal("48.20")
    # 48.20 / 2 / 0.75
    assert found.cost_per_kg == Decimal("32.1333")
    # 43.20 / 2 / 0.75
    assert found.price_per_kg == Decimal("28.8000")


def test_the_cost_is_an_invoice_lines_cost() -> None:
    """R-7152-2: the same sum an invoice line gives its spools."""
    query = _query(price="23.47", tax="1.88", shipping="4.99", quantity=3, net_weight_g=1000)
    found = calculate(query, "USD")
    assert found.cost_per_kg == spool_cost_per_kg(Decimal("30.34"), 3, 1000)


def test_rounds_half_up_to_four_places() -> None:
    # 1 / 3 / 1 = 0.33333…; 2 / 3 = 0.66666… rounds up.
    assert calculate(_query(price="1", net_weight_g=3000), "USD").cost_per_kg == Decimal("0.3333")
    assert calculate(_query(price="2", net_weight_g=3000), "USD").cost_per_kg == Decimal("0.6667")


def test_a_cost_past_the_column_is_refused() -> None:
    with pytest.raises(CostTooHighError):
        calculate(_query(price="99999999.9999", net_weight_g=1), "USD")


@pytest.mark.parametrize(
    "over",
    [
        {"price": "-1"},
        {"price": "1.00001"},
        {"tax": "-0.01"},
        {"shipping": "1e13"},
        {"quantity": 0},
        {"quantity": 1001},
        {"net_weight_g": 0},
        {"net_weight_g": 100_001},
        {"currency": "EUR"},
    ],
)
def test_bad_inputs_are_refused(over: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _query(**over)


def test_money_is_never_a_float() -> None:
    found = calculate(_query(price="0.1", tax="0.2", net_weight_g=1000), "USD")
    assert found.total == Decimal("0.3")
    assert isinstance(found.cost_per_kg, Decimal)
