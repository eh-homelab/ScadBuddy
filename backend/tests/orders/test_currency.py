"""The minor unit's table (#4250, plan 2026-10-22 Ruling 5): ISO 4217's current codes
(https://en.wikipedia.org/wiki/ISO_4217#Active_codes), 2 unless listed."""

from __future__ import annotations

import pytest

from scadbuddy.orders.currency import CURRENCY_EXPONENTS, NO_MINOR_UNIT, minor_exponent


@pytest.mark.parametrize(
    ("code", "exponent"),
    [
        ("USD", 2),
        ("EUR", 2),
        # CLDR (and so `Intl`) says 0 for these; ISO 4217 says 2.
        ("IRR", 2),
        ("LAK", 2),
        ("MMK", 2),
        ("ALL", 2),
        ("JPY", 0),
        ("KRW", 0),
        ("ISK", 0),
        ("XOF", 0),
        ("KWD", 3),
        # CLDR says 0; ISO 4217 says 3.
        ("IQD", 3),
        ("BHD", 3),
        ("CLF", 4),
        ("UYW", 4),
        ("ZZZ", 2),
    ],
)
def test_the_exponent_is_iso_4217s(code: str, exponent: int) -> None:
    assert minor_exponent(code) == exponent


def test_the_table_lists_every_non_two_code_once() -> None:
    zero = {code for code, places in CURRENCY_EXPONENTS.items() if places == 0}
    assert zero - NO_MINOR_UNIT == {
        "BIF", "CLP", "DJF", "GNF", "ISK", "JPY", "KMF", "KRW", "PYG",
        "RWF", "UGX", "UYI", "VND", "VUV", "XAF", "XOF", "XPF",
    }  # fmt: skip
    three = {code for code, places in CURRENCY_EXPONENTS.items() if places == 3}
    assert three == {"BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"}
    assert {code for code, places in CURRENCY_EXPONENTS.items() if places == 4} == {"CLF", "UYW"}
    assert 2 not in CURRENCY_EXPONENTS.values()


def test_the_codes_with_no_minor_unit_are_iso_4217s() -> None:
    listed = {
        "XAG", "XAU", "XBA", "XBB", "XBC", "XBD", "XDR", "XPD", "XPT", "XSU", "XTS", "XUA", "XXX",
    }  # fmt: skip
    assert listed == NO_MINOR_UNIT
