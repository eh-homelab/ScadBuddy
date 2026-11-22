from __future__ import annotations

import pytest

from scadbuddy.codes.parse import Code, format_code, parse_code


@pytest.mark.parametrize("text", ["L-12", "l12", "L12", " l-12 ", "L-012"])
def test_parse_accepts(text: str) -> None:
    assert parse_code(text) == Code("L", 12)


@pytest.mark.parametrize(
    "text", ["", "L-", "-12", "L-1x", "12", "L--12", "L-0", "L-99999999999", "L-" + "1" * 5000]
)
def test_parse_refuses(text: str) -> None:
    assert parse_code(text) is None


def test_the_largest_integer_parses_and_one_more_does_not() -> None:
    assert parse_code("S-2147483647") == Code("S", 2**31 - 1)
    assert parse_code("S-2147483648") is None


def test_format() -> None:
    assert format_code("S", 142) == "S-142"
