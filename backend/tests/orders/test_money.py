"""The money rules of deposits, shipping and tax (#5281, storefront spec §8, §9): pure,
on integer minor units and decimal-string percents."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from scadbuddy.orders.models import ChargeInput
from scadbuddy.orders.money import (
    DepositRule,
    ShippingOption,
    deposit_for,
    tax_base,
    tax_minor,
)


def test_tax_rounds_once_half_up() -> None:
    assert tax_minor(1005, Decimal("10")) == 101
    assert tax_minor(1004, Decimal("10")) == 100
    assert tax_minor(999, Decimal("0")) == 0
    assert tax_minor(1000, Decimal("8.25")) == 83


def test_shipping_is_taxed_only_when_asked() -> None:
    charges = [
        ChargeInput(kind="shipping", label="Post", amount_minor=500),
        ChargeInput(kind="discount", label="-", amount_minor=-200),
    ]
    assert tax_base(1000, charges, tax_shipping=False) == 1000
    assert tax_base(1000, charges, tax_shipping=True) == 1500


@pytest.mark.parametrize(
    ("rules", "ctx", "want"),
    [
        ([DepositRule(criterion="has_request", amount_minor=2000)], {"has_request": True}, 2000),
        ([DepositRule(criterion="has_request", amount_minor=2000)], {"has_request": False}, None),
        (
            [DepositRule(criterion="total_above", above_minor=10000, percent="50")],
            {"total_minor": 10001},
            5001,
        ),
        (
            [DepositRule(criterion="total_above", above_minor=10000, percent="50")],
            {"total_minor": 10000},
            None,
        ),
        (
            [DepositRule(criterion="first_order", percent="25")],
            {"first_order": True, "total_minor": 999},
            250,
        ),
        (
            [
                DepositRule(criterion="first_order", amount_minor=500),
                DepositRule(criterion="has_request", amount_minor=800),
            ],
            {"first_order": True, "has_request": True},
            800,
        ),
        (
            [DepositRule(criterion="first_order", amount_minor=500)],
            {"first_order": True, "currency": "EUR"},
            None,
        ),
        # A percent applies in any currency; `total_above` only in the shop's.
        (
            [DepositRule(criterion="first_order", percent="10")],
            {"first_order": True, "total_minor": 5000, "currency": "EUR"},
            500,
        ),
        (
            [DepositRule(criterion="total_above", above_minor=100, percent="10")],
            {"total_minor": 5000, "currency": "EUR"},
            None,
        ),
        # A percent of nothing is no deposit.
        ([DepositRule(criterion="first_order", percent="25")], {"first_order": True}, None),
    ],
)
def test_the_largest_rule_that_applies_wins(
    rules: list[DepositRule], ctx: dict[str, Any], want: int | None
) -> None:
    context: dict[str, Any] = {
        "total_minor": 0,
        "has_request": False,
        "first_order": False,
        "currency": "USD",
        "shop_currency": "USD",
        **ctx,
    }
    assert deposit_for(rules, **context) == want


def test_a_rule_needs_one_amount() -> None:
    with pytest.raises(ValidationError):
        DepositRule(criterion="first_order", percent="10", amount_minor=5)
    with pytest.raises(ValidationError):
        DepositRule(criterion="first_order")
    with pytest.raises(ValidationError):
        DepositRule(criterion="total_above", amount_minor=5)
    with pytest.raises(ValidationError):
        DepositRule(criterion="has_request", above_minor=5, amount_minor=5)


@pytest.mark.parametrize("percent", ["0", "100.01", "-5", "lots"])
def test_a_percent_is_above_0_and_at_most_100(percent: str) -> None:
    with pytest.raises(ValidationError):
        DepositRule(criterion="first_order", percent=percent)
    assert DepositRule(criterion="first_order", percent="100").percent == "100"


def test_a_shipping_option_is_checked() -> None:
    assert ShippingOption(id="pickup", label="Pick up", amount_minor=0, ships=False).ships is False
    with pytest.raises(ValidationError):
        ShippingOption(id="Pick Up", label="x", amount_minor=0, ships=False)
    with pytest.raises(ValidationError):
        ShippingOption(id="post", label="Post", amount_minor=-1, ships=True)
    with pytest.raises(ValidationError):
        ShippingOption.model_validate(
            {"id": "post", "label": "Post", "amount_minor": 1, "ships": True, "x": 1}
        )
