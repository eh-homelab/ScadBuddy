"""A call that must wait on a printer's place lock (`LocationStore.lock_printer_on`,
#7081), for the tests of the labels and capture paths."""

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
    with pool.connection() as holder, ThreadPoolExecutor(1) as ex:
        waited = False
        with holder.transaction():
            LocationStore.lock_printer_on(holder, printer)
            done = ex.submit(call)
            for _ in range(200):
                if _waiters(pool, holder.info.backend_pid) > 0:
                    waited = True
                    break
                if done.done():
                    break
                threading.Event().wait(0.05)
        result = done.result(timeout=30)
    assert waited, "the call never waited on the printer's place lock"
    return result
