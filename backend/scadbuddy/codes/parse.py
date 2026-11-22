"""Parse and format a typed code (#6955). Parse takes any case, with or without the dash."""

from __future__ import annotations

import re
from typing import NamedTuple

#: PostgreSQL's `integer` maximum: a larger number would fail the query on an overflow.
MAX_NUMBER = 2**31 - 1

_CODE = re.compile(r"^([A-Z])-?([0-9]+)$")


class Code(NamedTuple):
    prefix: str
    number: int


def parse_code(text: str) -> Code | None:
    found = _CODE.match(text.strip().upper())
    if found is None:
        return None
    number = int(found.group(2))
    if not 1 <= number <= MAX_NUMBER:
        return None
    return Code(found.group(1), number)


def format_code(prefix: str, number: int) -> str:
    return f"{prefix}-{number}"
