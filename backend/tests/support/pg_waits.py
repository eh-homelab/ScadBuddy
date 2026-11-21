"""Wait for other sessions to be blocked on a lock, by asking Postgres, so a test pins an
order without a sleep."""

from __future__ import annotations

import time

from tests.conftest import PgPool


def wait_for_blocked(pool: PgPool, count: int = 1, timeout: float = 30.0) -> None:
    """Return once at least ``count`` sessions of this database wait on a lock."""
    deadline = time.monotonic() + timeout
    while True:
        with pool.connection() as conn:
            row = conn.execute(
                "SELECT count(*) AS n FROM pg_stat_activity"
                " WHERE datname = current_database() AND wait_event_type = 'Lock'"
            ).fetchone()
        if row is not None and row["n"] >= count:
            return
        if time.monotonic() > deadline:
            raise TimeoutError(f"fewer than {count} sessions are waiting on a lock")
        time.sleep(0.02)
