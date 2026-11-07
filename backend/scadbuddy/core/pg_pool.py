"""Every Postgres pool in the backend, made here (#5226).

On 2026-11-03 prod's ``scadbuddy-db`` ran out of connection slots: each API pod held
23, almost all idle, because every feature kept a pool of its own with ``min_size=1``
and psycopg_pool's default ``max_idle`` of ten minutes. So a pool made here:

- holds no connection it does not use (``min_size`` 0 unless the caller says so) and
  closes an idle one after :data:`IDLE_SECONDS` (psycopg_pool closes one per
  ``max_idle`` interval while connections stayed unused through it, ``ShrinkPool``);
- names itself in ``pg_stat_activity`` (``application_name`` is the pool's name);
- is recorded in this process's registry, which :func:`pools` reads: the
  ``scadbuddy_db_pool_*`` gauges (`core/metrics.py`) and the per-process budget test
  (``tests/test_pg_pools.py``) sum it.

No module outside this one constructs a ``ConnectionPool`` or ``AsyncConnectionPool``;
the test checks that too. The ``LISTEN`` connection (`core/pg_listener.py`) is not a
pool, but it records itself here as one connection.
"""

from __future__ import annotations

import threading
import weakref
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from psycopg import AsyncConnection, Connection
from psycopg.rows import DictRow, TupleRow, dict_row
from psycopg_pool import AsyncConnectionPool, ConnectionPool

from scadbuddy.core.pg_keepalive import TCP_KEEPALIVE

#: How long a connection may sit unused before its pool closes it.
IDLE_SECONDS = 60.0


class _Counted(Protocol):
    name: str
    conninfo: str

    @property
    def max_size(self) -> int: ...

    def open_connections(self) -> int: ...


@dataclass(frozen=True)
class PoolUse:
    """One pool's (or the listener's) connections now, and the most it may hold."""

    name: str
    conninfo: str
    open: int
    max_size: int
    #: Callers queued for a connection (psycopg_pool's ``requests_waiting``).
    waiting: int = 0


class _RowPool(ConnectionPool[Connection[DictRow]]):
    """Remembers that it was opened, so the registry tells unopened from closed."""

    was_opened = False

    def open(self, wait: bool = False, timeout: float = 30.0) -> None:
        self.was_opened = True
        super().open(wait=wait, timeout=timeout)


class _AsyncPool(AsyncConnectionPool[AsyncConnection[TupleRow]]):
    was_opened = False

    async def open(self, wait: bool = False, timeout: float = 30.0) -> None:
        self.was_opened = True
        await super().open(wait=wait, timeout=timeout)


class _PoolEntry:
    def __init__(self, pool: _RowPool | _AsyncPool) -> None:
        self._pool = weakref.ref(pool)

    def use(self) -> PoolUse | None:
        pool = self._pool()
        # A pool not yet opened counts at its most; one closed after opening is gone
        # (psycopg_pool never reopens one).
        if pool is None or (pool.closed and pool.was_opened):
            return None
        stats = pool.get_stats() if not pool.closed else {}
        return PoolUse(
            name=pool.name,
            conninfo=pool.conninfo if isinstance(pool.conninfo, str) else "",
            open=stats.get("pool_size", 0),
            max_size=pool.max_size,
            waiting=stats.get("requests_waiting", 0),
        )


class _CountedEntry:
    def __init__(self, counted: _Counted) -> None:
        self._counted = weakref.ref(counted)

    def use(self) -> PoolUse | None:
        counted = self._counted()
        if counted is None:
            return None
        return PoolUse(
            name=counted.name,
            conninfo=counted.conninfo,
            open=counted.open_connections(),
            max_size=counted.max_size,
        )


_lock = threading.Lock()
_entries: list[_PoolEntry | _CountedEntry] = []


def _record(entry: _PoolEntry | _CountedEntry) -> None:
    with _lock:
        _entries[:] = [e for e in _entries if e.use() is not None]
        _entries.append(entry)


def record_connection(counted: _Counted) -> None:
    """Record a connection that is not a pool's (the ``LISTEN`` one), held weakly."""
    _record(_CountedEntry(counted))


def pools() -> Iterator[PoolUse]:
    """Every pool of this process not yet closed for good, with the listener."""
    with _lock:
        entries = list(_entries)
    for entry in entries:
        use = entry.use()
        if use is not None:
            yield use


def row_pool(
    conninfo: str,
    *,
    name: str,
    max_size: int,
    min_size: int = 0,
    timeout: float = 30.0,
    connect_timeout: float | None = None,
    configure: Callable[[Connection[DictRow]], None] | None = None,
    kwargs: Mapping[str, Any] | None = None,
) -> ConnectionPool[Connection[DictRow]]:
    """An autocommit ``dict_row`` pool, unopened (``open=False``: nothing connects
    until its owner opens it). ``timeout`` bounds the wait for a connection and
    ``connect_timeout``, when given, each connect."""
    pool = _RowPool(
        conninfo,
        min_size=min_size,
        max_size=max_size,
        max_idle=IDLE_SECONDS,
        open=False,
        timeout=timeout,
        connection_class=Connection[DictRow],
        kwargs=_kwargs(name, connect_timeout, {"row_factory": dict_row, **(kwargs or {})}),
        configure=configure,
        name=name,
    )
    _record(_PoolEntry(pool))
    return pool


def async_pool(
    conninfo: str, *, name: str, max_size: int, min_size: int = 0
) -> AsyncConnectionPool[AsyncConnection[TupleRow]]:
    """An autocommit async pool of tuple rows, unopened."""
    pool = _AsyncPool(
        conninfo,
        min_size=min_size,
        max_size=max_size,
        max_idle=IDLE_SECONDS,
        open=False,
        kwargs=_kwargs(name, None, {}),
        name=name,
    )
    _record(_PoolEntry(pool))
    return pool


def _kwargs(name: str, connect_timeout: float | None, extra: Mapping[str, Any]) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "autocommit": True,
        "application_name": name,
        **TCP_KEEPALIVE,
        **extra,
    }
    if connect_timeout is not None:
        kwargs["connect_timeout"] = max(1, int(connect_timeout))
    return kwargs
