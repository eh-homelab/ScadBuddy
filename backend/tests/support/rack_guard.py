"""Spec 2026-10-01 §5: ``choose_rack`` swallows every exception, so tests must see which."""

from __future__ import annotations

import logging
from collections.abc import Iterable

import httpx

from scadbuddy.core.problems import ApiError

#: The error type names a rack pick may legitimately fall back on: Bambuddy's answers
#: and transport failures. Never ``StopIteration``: a bare ``next()`` on an empty
#: iterator is a real bug, so a stub that runs out must raise ``ApiError`` instead.
_EXPECTED = {ApiError.__name__} | {
    name
    for name, value in vars(httpx).items()
    if isinstance(value, type) and issubclass(value, Exception)
}

MESSAGE = "rack pick left to Bambuddy"
#: Every rack fallback that swallows an exception and logs its type (#1081): the pick,
#: the /check preview, and a usage read ranked without.
MESSAGES = frozenset(
    {MESSAGE, "the rack preview could not be built", "rack usage unreadable; ranked without it"}
)


def foreign_rack_errors(records: Iterable[logging.LogRecord]) -> list[str]:
    """The logged error type of every rack fallback that is not an expected one."""
    return [
        str(getattr(record, "error", None))
        for record in records
        if record.getMessage() in MESSAGES and getattr(record, "error", None) not in _EXPECTED
    ]
