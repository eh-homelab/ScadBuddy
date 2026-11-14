"""One follow of a background slice across the API replicas (#6158).

Each replica that serves a read of a slice would otherwise follow it itself: prod runs
three API replicas (eh-homelab/clusters `applications/scadbuddy/scadbuddy.yaml`, read on
2026-11-13: ``replicas: 3``), so a slice was read up to three times every
`FOLLOW_INTERVAL` and each change went out once per replica. A follow now first takes a
session advisory lock on its slice. The replica that holds it follows; a read on any
other replica answers ``followed: true`` and starts nothing, because the follower's
events reach every replica's sockets over the Postgres bus (`core/pg_events.py`:
``NOTIFY scadbuddy_events``, heard on each process's ``LISTEN`` connection,
`core/pg_listener.py`).

Every lock of one process is on one connection of its own, opened at the first claim,
so the claims cost one connection however many slices are followed (`MAX_FOLLOWS`).
Never a pooled one: a lock held for minutes must not hold a connection the routes need.
Closing the connection ends every lock on it, so a process that stops frees its slices.

A claim whose connection fails answers that this process follows the slice: the dialog
then hears of its changes, at worst twice while Postgres cannot be reached. A lock lost
with its connection lets another replica follow the slice too, until one follow ends.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Protocol

import psycopg

from scadbuddy.core.pg_keepalive import TCP_KEEPALIVE
from scadbuddy.core.pg_pool import record_connection

logger = logging.getLogger(__name__)

#: The claims' connection, as `core/pg_pool.py`'s registry and ``pg_stat_activity`` name it.
CLAIMS_NAME = "scadbuddy-preview-follows"
CONNECT_TIMEOUT = 5


class FollowClaims(Protocol):
    """Who follows a slice. The calls block: run them on a thread."""

    def claim(self, key: str) -> bool:
        """Whether this process now holds ``key``: False when another process holds it."""
        ...

    def release(self, key: str) -> None: ...

    def close(self) -> None: ...


class PgFollowClaims:
    """`FollowClaims` as session advisory locks on one connection of this process."""

    name = CLAIMS_NAME
    max_size = 1

    def __init__(self, conninfo: str) -> None:
        self.conninfo = conninfo
        self._conn: psycopg.Connection[Any] | None = None
        #: Serialises the connection's statements: claims come from several threads.
        self._lock = threading.Lock()
        record_connection(self)

    def open_connections(self) -> int:
        return 0 if self._conn is None else 1

    def claim(self, key: str) -> bool:
        with self._lock:
            try:
                if self._conn is None:
                    self._conn = psycopg.connect(
                        self.conninfo,
                        autocommit=True,
                        connect_timeout=CONNECT_TIMEOUT,
                        application_name=CLAIMS_NAME,
                        # A lock may be held for minutes: a peer that went away is found
                        # in about 30 s, and the server then frees its locks.
                        **TCP_KEEPALIVE,  # type: ignore[arg-type]
                    )
                row = self._conn.execute(
                    "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", (_lock_key(key),)
                ).fetchone()
            except psycopg.Error:
                logger.warning("a background slice's follow could not be claimed", exc_info=True)
                self._drop()
                return True
            return bool(row and row[0])

    def release(self, key: str) -> None:
        with self._lock:
            if self._conn is None:
                return
            try:
                self._conn.execute(
                    "SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (_lock_key(key),)
                )
            except psycopg.Error:
                # Closing the connection frees the lock, and every other one on it.
                logger.warning("a background slice's follow could not be released", exc_info=True)
                self._drop()

    def close(self) -> None:
        with self._lock:
            self._drop()

    def _drop(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


def _lock_key(key: str) -> str:
    return f"scadbuddy:preview-follow:{key}"
