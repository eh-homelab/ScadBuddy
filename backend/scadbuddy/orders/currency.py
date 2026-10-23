"""The minor unit of each currency (#4250, plan 2026-10-22 Ruling 5): how many decimal
places an amount in it has, from ISO 4217's current codes list
(https://en.wikipedia.org/wiki/ISO_4217#Active_codes).

This is the one table: a quote's lines are quantised to it, and it is published as
``minor_exponent`` on every order and on the price book, so the browser takes the digits
it is given and never asks ``Intl`` for them (CLDR's digits differ from ISO 4217's for
several codes: ``IQD`` is 0 there and 3 here). A code not listed has two decimals.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

#: The codes ISO 4217 lists with no minor unit ("N.A."): precious metals, bond-market
#: and drawing units, the testing and no-currency codes. Nothing is priced in them:
#: ``orders_currency`` refuses them.
NO_MINOR_UNIT: Final = frozenset(
    {
        "XAG",
        "XAU",
        "XBA",
        "XBB",
        "XBC",
        "XBD",
        "XDR",
        "XPD",
        "XPT",
        "XSU",
        "XTS",
        "XUA",
        "XXX",
    }
)

#: The exponents other than 2, by code.
CURRENCY_EXPONENTS: Final[Mapping[str, int]] = MappingProxyType(
    {
        **dict.fromkeys(
            (
                "BIF",
                "CLP",
                "DJF",
                "GNF",
                "ISK",
                "JPY",
                "KMF",
                "KRW",
                "PYG",
                "RWF",
                "UGX",
                "UYI",
                "VND",
                "VUV",
                "XAF",
                "XOF",
                "XPF",
            ),
            0,
        ),
        **dict.fromkeys(("BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"), 3),
        **dict.fromkeys(("CLF", "UYW"), 4),
        # An order made in one before Task 1's check refused them has whole units only.
        **dict.fromkeys(NO_MINOR_UNIT, 0),
    }
)

DEFAULT_EXPONENT: Final = 2


def minor_exponent(code: str) -> int:
    """Decimal places of ``code``'s minor unit: 100 cents to the dollar is 2."""
    return CURRENCY_EXPONENTS.get(code.upper(), DEFAULT_EXPONENT)
