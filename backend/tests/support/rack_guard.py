"""Spec 2026-10-01 §5: ``choose_rack`` swallows every exception, so tests must see which."""

from __future__ import annotations

import logging
from collections.abc import Iterable

import httpx
import psycopg
import psycopg.errors  # the SQLSTATE subclasses counted below

from scadbuddy.bambuddy.print_run import RACK_FALLBACKS, RACK_USAGE_FALLBACK
from scadbuddy.core.problems import ApiError

#: The error type names a rack pick may legitimately fall back on: Bambuddy's answers
#: and transport failures. Never ``StopIteration``: a bare ``next()`` on an empty
#: iterator is a real bug, so a stub that runs out must raise ``ApiError`` instead.
_EXPECTED = {ApiError.__name__} | {
    name
    for name, value in vars(httpx).items()
    if isinstance(value, type) and issubclass(value, Exception)
}


def _subclass_names(root: type[Exception]) -> set[str]:
    names, todo = set(), [root]
    while todo:
        cls = todo.pop()
        names.add(cls.__name__)
        todo.extend(cls.__subclasses__())
    return names


#: The usage read is a Postgres read (#1086 review): an outage, a pool wait or the
#: store's statement timeout is infrastructure there, not a bug. Only those branches:
#: a ``ProgrammingError`` (a renamed column, a typo) or ``DataError`` is a bug.
_EXPECTED_BY_MESSAGE = {
    RACK_USAGE_FALLBACK: _EXPECTED
    | _subclass_names(psycopg.OperationalError)  # QueryCanceled and PoolTimeout too
    | _subclass_names(psycopg.InterfaceError)
}

#: Every rack fallback that swallows an exception and logs its type (#1081): the pick,
#: the /check preview, and a usage read ranked without. Production's own constants, so a
#: reworded message cannot silently drop out of the guard.
MESSAGES = RACK_FALLBACKS


def foreign_rack_errors(records: Iterable[logging.LogRecord]) -> list[str]:
    """The logged error type of every rack fallback that is not an expected one."""
    return [
        str(getattr(record, "error", None))
        for record in records
        if record.getMessage() in MESSAGES
        and getattr(record, "error", None)
        not in _EXPECTED_BY_MESSAGE.get(record.getMessage(), _EXPECTED)
    ]
