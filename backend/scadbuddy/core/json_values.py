"""What a JSON value may hold, so that Postgres can store it as ``jsonb`` or ``text``."""

from __future__ import annotations

import math
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Final

#: How many objects and arrays a value may nest: a deeper one cannot be written.
MAX_DEPTH: Final = 32
#: CPython reads and writes no integer of more than 4300 digits.
_TOO_LARGE: Final = 10**4299
_UNSTORABLE: Final = re.compile(r"[\x00\ud800-\udfff]")


@dataclass(frozen=True)
class Unstorable:
    """Where the value is, and why it cannot be stored. Never the value itself."""

    path: tuple[str | int, ...]
    message: str


def _scalar_problem(value: Any) -> str | None:
    if isinstance(value, str) and _UNSTORABLE.search(value):
        return "holds a character that cannot be stored"
    if isinstance(value, float) and not math.isfinite(value):
        return "is not a finite number"
    if isinstance(value, int) and abs(value) >= _TOO_LARGE:
        return "is too large a number"
    return None


def unstorable(value: Any, *, max_depth: int = MAX_DEPTH) -> Iterator[Unstorable]:
    """Every part of ``value`` that cannot be stored, walked without recursion. A key's
    problem is named at its object. Nothing below ``max_depth`` is walked."""
    stack: list[tuple[tuple[str | int, ...], Any]] = [((), value)]
    while stack:
        path, one = stack.pop()
        if isinstance(one, dict | list) and len(path) >= max_depth:
            yield Unstorable(path, "is nested too deeply")
        elif isinstance(one, dict):
            for key in one:
                if (problem := _scalar_problem(key)) is not None:
                    yield Unstorable(path, problem)
            stack += [((*path, key), item) for key, item in reversed(one.items())]
        elif isinstance(one, list):
            stack += [((*path, i), one[i]) for i in range(len(one) - 1, -1, -1)]
        elif (problem := _scalar_problem(one)) is not None:
            yield Unstorable(path, problem)
