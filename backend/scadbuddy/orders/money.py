"""The money rules of deposits, shipping options and auto tax (#5281, storefront spec
§8, §9): pure, on integer minor units, with every percent a decimal string, never a
float. A computed amount is rounded once, half up, as ``pricing.py`` rounds a line.

The shop's rules are settings (``core/settings.py`` ``orders_tax_percent`` and
``orders_tax_shipping``, ``orders_purchase_fee_flat_minor`` and
``orders_purchase_fee_percent``; ``library/settings_store.py`` ``orders_deposit_policy`` and
``storefront_shipping_options``). :class:`OrderRules` is one read of them, which the
lifecycle takes from ``OrdersComponent.rules``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from scadbuddy.orders.models import ChargeInput, Money, Rate

#: When a deposit rule applies: the order has a design request in it, its total is above
#: ``above_minor``, or it is the customer's first accepted order.
DepositCriterion = Literal["has_request", "total_above", "first_order"]


def _percent(value: str) -> str:
    if not 0 < Decimal(value) <= 100:
        raise ValueError("a percent is above 0 and at most 100")
    return value


class DepositRule(BaseModel):
    """One rule of the deposit policy: a percent of the order's total, or a fixed amount
    in the shop's currency (``orders_currency``)."""

    model_config = ConfigDict(extra="forbid")

    criterion: DepositCriterion
    above_minor: Money | None = Field(default=None, ge=0)
    percent: Annotated[Rate, AfterValidator(_percent)] | None = None
    amount_minor: Money | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _one_amount(self) -> Self:
        if (self.percent is None) == (self.amount_minor is None):
            raise ValueError("a rule has exactly one of percent and amount_minor")
        if (self.above_minor is not None) != (self.criterion == "total_above"):
            raise ValueError("above_minor is set exactly when the criterion is total_above")
        return self


class ShippingOption(BaseModel):
    """A way an order reaches its customer, at a fixed price in the shop's currency. One
    that ``ships`` needs the order's shipping address; a pickup needs none."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9_-]{1,40}$")
    label: str = Field(min_length=1, max_length=200)
    amount_minor: Money = Field(ge=0)
    ships: bool


@dataclass(frozen=True)
class OrderRules:
    """One read of the shop's money settings."""

    shop_currency: str
    tax_percent: Decimal
    tax_shipping: bool
    deposit_policy: tuple[DepositRule, ...]
    shipping_options: tuple[ShippingOption, ...]
    #: The handling fee for bought items (#6133, spec 2026-11-13 §10.3): a flat amount per
    #: recorded purchase, in the shop's currency, and a percent of their sum.
    purchase_fee_flat_minor: Money = 0
    purchase_fee_percent: Decimal = Decimal(0)


#: No tax, no deposit, no shipping options: a lifecycle built without settings.
NO_RULES = OrderRules("USD", Decimal(0), False, (), ())


def _percent_of(base_minor: Money, percent: Decimal) -> Money:
    """``base * percent / 100`` in minor units, rounded half up once."""
    return int((Decimal(base_minor) * percent / 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def handling_minor(amounts: Sequence[Money], *, flat_minor: Money, percent: Decimal) -> Money:
    """The handling fee for bought items (#6133, spec 2026-11-13 §10.3): ``flat_minor``
    for each of ``amounts`` (the recorded purchases' charges), plus ``percent`` of their
    sum, rounded half up once. The caller passes 0 as ``flat_minor`` for an order not in
    the shop's currency."""
    total = Decimal(flat_minor * len(amounts)) + Decimal(sum(amounts)) * percent / 100
    return int(total.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def tax_minor(base_minor: Money, percent: Decimal) -> Money:
    """The tax on ``base_minor`` at ``percent``."""
    return _percent_of(base_minor, percent)


def tax_base(items_minor: Money, charges: Sequence[ChargeInput], *, tax_shipping: bool) -> Money:
    """What the tax is on: the items, plus the shipping charges when ``tax_shipping``.
    Fees and discounts are never in it (spec §9)."""
    shipping = sum(charge.amount_minor for charge in charges if charge.kind == "shipping")
    return items_minor + (shipping if tax_shipping else 0)


def deposit_for(
    policy: Sequence[DepositRule],
    *,
    total_minor: Money,
    has_request: bool,
    first_order: bool,
    currency: str,
    shop_currency: str,
) -> Money | None:
    """The deposit ``policy`` asks of an order: the largest positive amount among the
    rules that apply, else None. A percent is of ``total_minor`` and applies in any
    currency; a fixed amount and a ``total_above`` threshold are in the shop's currency,
    so those rules apply only to an order in ``shop_currency``."""
    same_currency = currency == shop_currency
    amounts: list[Money] = []
    for rule in policy:
        if rule.criterion == "has_request":
            applies = has_request
        elif rule.criterion == "first_order":
            applies = first_order
        else:
            assert rule.above_minor is not None
            applies = same_currency and total_minor > rule.above_minor
        if not applies:
            continue
        if rule.percent is not None:
            amounts.append(_percent_of(total_minor, Decimal(rule.percent)))
        elif same_currency:
            assert rule.amount_minor is not None
            amounts.append(rule.amount_minor)
    best = max(amounts, default=0)
    return best if best > 0 else None


def skipped_for_currency(
    policy: Sequence[DepositRule],
    *,
    has_request: bool,
    first_order: bool,
    currency: str,
    shop_currency: str,
) -> list[int]:
    """The indexes of the rules of ``policy`` that ``deposit_for`` skips only because the
    order is not in ``shop_currency`` (review of #5726): every ``total_above`` rule (its
    threshold is in the shop's currency), and a fixed amount whose criterion applies."""
    if currency == shop_currency:
        return []

    def applies(rule: DepositRule) -> bool:
        return has_request if rule.criterion == "has_request" else first_order

    return [
        index
        for index, rule in enumerate(policy)
        if rule.criterion == "total_above" or (rule.amount_minor is not None and applies(rule))
    ]
