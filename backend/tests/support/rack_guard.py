"""Spec 2026-10-01 §5: the rack fallbacks swallow every exception, so tests must see which."""

from __future__ import annotations

import logging
from collections.abc import Iterable

import httpx
import psycopg
import psycopg.errors  # the SQLSTATE subclasses counted below
import psycopg_pool  # noqa: F401  (PoolTimeout, counted below whatever else imports)

from scadbuddy.bambuddy.print_run import RACK_FALLBACKS, RACK_USAGE_FALLBACK
from scadbuddy.core.problems import ApiError
from scadbuddy.crud.client import CrudUnavailableError
from scadbuddy.rack.usage import RACK_SETTLE_FALLBACK, RACK_STORE_FALLBACKS

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


#: What a Postgres read or write may legitimately fail with (#1086 review): an outage, a
#: pool wait or the store's statement timeout is infrastructure, not a bug. Only those
#: branches: a ``ProgrammingError`` (a renamed column, a typo) or ``DataError`` is a bug.
_POSTGRES = (
    _subclass_names(psycopg.OperationalError)  # QueryCanceled and PoolTimeout too
    | _subclass_names(psycopg.InterfaceError)
)

#: The print worker's rack store is the CRUD service's client (#5298 Task 30). Its
#: `CrudUnavailableError` is an outage only when the API was not reached, did not answer
#: (a transport failure, a read timeout among them) or its database was out of reach: the
#: ``failure`` words `rack.usage.error_facts` logs. A missing route, a route's 5xx and a
#: refused credential are bugs, as is any refusal (`CrudRefusedError`, a 4xx).
CRUD_OUTAGES = frozenset({"unreached", "transport", "database_unavailable"})


def _crud_outage(record: logging.LogRecord) -> bool:
    return (
        getattr(record, "error", None) == CrudUnavailableError.__name__
        and getattr(record, "failure", None) in CRUD_OUTAGES
    )


#: The usage read is a Postgres call made inside a pick that also reads Bambuddy. The
#: store's own advisory writes and reads (#1112) call only Postgres, so a Bambuddy or
#: httpx error there can only be a bug. Only the per-archive settle also reads Bambuddy,
#: under a timeout that raises the builtin ``TimeoutError``.
_EXPECTED_BY_MESSAGE = {
    RACK_USAGE_FALLBACK: _EXPECTED | _POSTGRES,
    **{message: _POSTGRES for message in RACK_STORE_FALLBACKS},
    RACK_SETTLE_FALLBACK: _EXPECTED | _POSTGRES | {TimeoutError.__name__},
}

#: Every rack fallback that swallows an exception and logs its type: the pick, the /check
#: preview and a usage read ranked without (#1081), and the store's advisory writes and
#: reads (#1112). Production's own constants, so a reworded message cannot silently drop
#: out of the guard.
MESSAGES = RACK_FALLBACKS | RACK_STORE_FALLBACKS
#: The fallbacks around a rack store call, which on the print worker is a CRUD call: the
#: usage read and the store's own writes and reads.
_CRUD_MESSAGES = RACK_STORE_FALLBACKS | {RACK_USAGE_FALLBACK}


def foreign_rack_errors(records: Iterable[logging.LogRecord]) -> list[str]:
    """The logged error type of every rack fallback that is not an expected one."""
    return [
        str(getattr(record, "error", None))
        for record in records
        if record.getMessage() in MESSAGES
        and getattr(record, "error", None)
        not in _EXPECTED_BY_MESSAGE.get(record.getMessage(), _EXPECTED)
        and not (record.getMessage() in _CRUD_MESSAGES and _crud_outage(record))
    ]
