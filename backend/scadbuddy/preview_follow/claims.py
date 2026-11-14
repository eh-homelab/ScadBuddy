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
then hears of its changes, at worst twice while Postgres cannot be reached. After a failed
claim or release, the claims answer the same with no connect for `CONNECT_BACKOFF` seconds, so
during an outage reads of new slices do not queue behind each other's connects, each up
to `CONNECT_TIMEOUT` seconds (#6162). A lock lost with its connection lets another
replica follow the slice too, until one follow ends.

A process that stops frees its locks at once when its kernel closes the socket (an exit
or a crash of the process). A host that vanishes with no FIN or RST (a node lost, a
partition) leaves the server a half-open session, and its locks stay held, with no
follower, until the server sees the session is dead. The client's `TCP_KEEPALIVE` does
not help there: it only lets this process see a dead server. The server sees a dead
client only on its own timers: ``tcp_keepalives_idle``, ``tcp_keepalives_interval`` and
``tcp_keepalives_count``, and ``tcp_user_timeout`` while it has unacknowledged data
(https://www.postgresql.org/docs/18/runtime-config-connection.html#GUC-TCP-KEEPALIVES-IDLE).
Each is 0 by default, which takes the operating system's value; on Linux that is 7200 s
idle, then 9 probes 75 s apart, about 2 h 11 min
(https://docs.kernel.org/networking/ip-sysctl.html, ``tcp_keepalive_*``).
So those server settings bound how long such a slice can stay "held elsewhere" with no
follower. Until then its dialogs hear no event, and see the slice only when they read it
again for another reason.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol

import psycopg

from scadbuddy.core.pg_keepalive import TCP_KEEPALIVE
from scadbuddy.core.pg_pool import record_connection

logger = logging.getLogger(__name__)

#: The claims' connection, as `core/pg_pool.py`'s registry and ``pg_stat_activity`` name it.
CLAIMS_NAME = "scadbuddy-preview-follows"
CONNECT_TIMEOUT = 5
#: Seconds after a failed claim in which a claim answers True with no connect.
CONNECT_BACKOFF = 10.0


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

    def __init__(self, conninfo: str, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.conninfo = conninfo
        self._clock = clock
        self._conn: psycopg.Connection[Any] | None = None
        #: When a claim may connect again after a failed one.
        self._retry_at = 0.0
        #: Serialises the connection's statements: claims come from several threads.
        self._lock = threading.Lock()
        record_connection(self)

    def open_connections(self) -> int:
        return 0 if self._conn is None else 1

    def claim(self, key: str) -> bool:
        with self._lock:
            if self._conn is None and self._clock() < self._retry_at:
                return True
            try:
                if self._conn is None:
                    self._conn = psycopg.connect(
                        self.conninfo,
                        autocommit=True,
                        connect_timeout=CONNECT_TIMEOUT,
                        application_name=CLAIMS_NAME,
                        # A server that went away is found in about 30 s. These are the
                        # client's timers only: when this host vanishes, the server frees
                        # its locks on its own tcp_keepalives_* (the module docstring).
                        **TCP_KEEPALIVE,  # type: ignore[arg-type]
                    )
                row = self._conn.execute(
                    "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", (_lock_key(key),)
                ).fetchone()
            except psycopg.Error:
                logger.warning("a background slice's follow could not be claimed", exc_info=True)
                self._drop()
                self._retry_at = self._clock() + CONNECT_BACKOFF
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
                # #6174: as after a failed claim, the next claim does not connect at once.
                self._retry_at = self._clock() + CONNECT_BACKOFF

    def close(self) -> None:
        with self._lock:
            self._drop()

    def _drop(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


def _lock_key(key: str) -> str:
    return f"scadbuddy:preview-follow:{key}"
