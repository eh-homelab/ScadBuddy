"""Stopping database work in a worker thread from outside it (#6902).

A route that runs its work in ``asyncio.to_thread`` cannot cancel the thread. The work
takes a `CancelToken` instead: it calls `CancelToken.check` between its steps, and runs
its transaction under `CancelToken.watching`, so `CancelToken.cancel` also cancels the
statement running on that connection (psycopg ``cancel_safe``). Either way the work
raises (`CancelledError`, or psycopg's ``QueryCanceled``) inside its transaction, which rolls
back. `api/disconnect.py` cancels the token when the caller of a CRUD route goes away.

A connection is watched only while the work holds it: `watching` stops watching it
before the transaction ends, under the token's lock, so a cancel never reaches a
connection that is back in the pool and serves another caller.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger(__name__)

#: How long `CancelToken.cancel` waits for the server to take a cancel request.
CANCEL_TIMEOUT_S = 5.0


class CancelledError(Exception):
    """The work's caller went away; raised inside the work, so its transaction rolls back."""


class CancelToken:
    """One request's cancel, as the module says. Thread-safe."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._conns: list[Any] = []

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        """Raise `CancelledError` once the token is cancelled."""
        if self._event.is_set():
            raise CancelledError

    def cancel(self) -> None:
        """Cancel the work: the next `check` raises, and the statement running on each
        watched connection is cancelled. It blocks while the server takes the cancel
        request, so call it off the event loop."""
        self._event.set()
        with self._lock:
            for conn in self._conns:
                try:
                    conn.cancel_safe(timeout=CANCEL_TIMEOUT_S)
                except Exception:
                    # The statement may have ended; `check` stops the work then.
                    logger.warning("could not cancel a statement", exc_info=True)

    @contextmanager
    def watching(self, conn: Any) -> Iterator[None]:
        """Watch ``conn`` while the block runs; `check` first."""
        self.check()
        with self._lock:
            self._conns.append(conn)
        try:
            yield
        finally:
            with self._lock:
                self._conns.remove(conn)


@contextmanager
def watched(token: CancelToken | None, conn: Any) -> Iterator[None]:
    """`CancelToken.watching`, or nothing with no token."""
    if token is None:
        yield
        return
    with token.watching(conn):
        yield


def check(token: CancelToken | None) -> None:
    """`CancelToken.check`, or nothing with no token."""
    if token is not None:
        token.check()
