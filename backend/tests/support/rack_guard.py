"""Spec 2026-10-01 §5: ``choose_rack`` swallows every exception, so tests must see which."""

from __future__ import annotations

import logging
from collections.abc import Iterable

import httpx

from scadbuddy.core.problems import ApiError

#: The error type names a rack pick may legitimately fall back on: Bambuddy's answers,
#: transport failures and a stub's exhausted response list.
_EXPECTED = {ApiError.__name__, StopIteration.__name__} | {
    name
    for name, value in vars(httpx).items()
    if isinstance(value, type) and issubclass(value, Exception)
}

MESSAGE = "rack pick left to Bambuddy"


def foreign_rack_errors(records: Iterable[logging.LogRecord]) -> list[str]:
    """The logged error type of every rack-pick fallback that is not an expected one."""
    return [
        str(getattr(record, "error", None))
        for record in records
        if record.getMessage() == MESSAGE and getattr(record, "error", None) not in _EXPECTED
    ]
