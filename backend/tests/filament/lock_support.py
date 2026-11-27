"""A call that must wait on a printer's place lock (`LocationStore.lock_printer_on`,
#7081), for the tests of the labels and capture paths, or on any lock another transaction
holds (`waits_for`)."""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import UUID

from scadbuddy.filament.locations import LocationStore
from tests.conftest import PgPool


def _waiters(pool: PgPool, holder: int) -> int:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) AS n FROM pg_stat_activity WHERE %s = ANY(pg_blocking_pids(pid))",
            (holder,),
        ).fetchone()
    return 0 if row is None else int(row["n"])


def waits_for_the_printer_lock(pool: PgPool, printer: UUID, call: Callable[[], Any]) -> Any:
    """Run ``call`` in a thread while another transaction holds ``printer``'s place lock:
    it must wait on that lock (``pg_blocking_pids`` names the holder) and finish once the
    holder commits. Gives ``call``'s result."""
    return waits_for(pool, lambda holder: LocationStore.lock_printer_on(holder, printer), call)


def waits_for(pool: PgPool, hold: Callable[[Any], object], call: Callable[[], Any]) -> Any:
    """Run ``call`` in a thread while another transaction has run ``hold`` and not yet
    committed: ``call`` must wait on a lock that transaction holds (``pg_blocking_pids``
    names it) and finish once it commits (#7167 reuses it for a replace). Gives
    ``call``'s result."""
    with pool.connection() as holder, ThreadPoolExecutor(1) as ex:
        waited = False
        with holder.transaction():
            hold(holder)
            done = ex.submit(call)
            for _ in range(200):
                if _waiters(pool, holder.info.backend_pid) > 0:
                    waited = True
                    break
                if done.done():
                    break
                threading.Event().wait(0.05)
        result = done.result(timeout=30)
    assert waited, "the call never waited on the holder's lock"
    return result
