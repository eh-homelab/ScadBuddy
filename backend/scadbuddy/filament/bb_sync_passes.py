"""The sync's pass history (#6905): one ``filament_bb_sync_passes`` row per pass, failed
ones too.

`bb_sync_results.record_pass` inserts the row (`insert_on`) in the transaction that writes
the state row, through the CRUD area ``filament-sync`` on the print worker.
`SyncPassStore` (``Filament.sync_passes``) reads them newest first for
``GET /filament/sync/passes`` (`SyncPassStore.page`) and deletes the rows older than the
setting ``filament_sync_pass_ttl_days`` (`PASS_TTL_DAYS_DEFAULT` when unset), at most
`PRUNE_BATCH` a run, from the housekeeping's activity sweep (`SyncPassStore.prune`,
`main._sweep_activity_logged`).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from psycopg import Connection

from scadbuddy.filament.bb_sync_models import SyncPass, SyncPassPage, SyncPassResult
from scadbuddy.filament.spools import FilamentSpoolStore

Conn = Connection[dict[str, Any]]

#: How long a pass's row is kept when ``filament_sync_pass_ttl_days`` is unset.
PASS_TTL_DAYS_DEFAULT = 30
#: The most rows one prune deletes. A pass every 5 minutes is 288 rows a day, so a sweep
#: catches up after a long gap in a few runs, and no run holds a long delete.
PRUNE_BATCH = 5000
#: The most rows one page holds (`GET /filament/sync/passes` ``limit``).
PAGE_MAX = 200

_COLUMNS = ", ".join(SyncPass.model_fields)


class BadPassCursorError(ValueError):
    """A ``cursor`` that no page of the pass history gave."""

    def __init__(self) -> None:
        super().__init__("the cursor is not one this pass history gave")


def insert_on(conn: Conn, result: SyncPassResult) -> None:
    """The pass's row. A result with no ``pass_id`` (an older worker's) gets a new one; a
    second record of the same id (the CRUD client's retry) adds nothing."""
    conn.execute(
        "INSERT INTO filament_bb_sync_passes (id, instance, trigger, started_at, written,"
        " failed, deferred, conflicts, unmapped_k, skipped, instance_changed, error)"
        " VALUES (%s, %s, %s, COALESCE(%s, now()), %s, %s, %s, %s, %s, %s, %s, %s)"
        " ON CONFLICT (id) DO NOTHING",
        (
            result.pass_id or uuid4(),
            result.instance,
            result.trigger,
            result.started_at,
            result.written,
            result.failed,
            result.deferred,
            result.conflicts,
            result.unmapped_k,
            result.skipped,
            result.instance_changed,
            result.error,
        ),
    )


def _encode(ended_at: datetime, pass_id: UUID) -> str:
    return f"{ended_at.isoformat()}|{pass_id}".encode().hex()


def _decode(cursor: str) -> tuple[datetime, UUID]:
    try:
        ended_at, pass_id = bytes.fromhex(cursor).decode().rsplit("|", 1)
        return datetime.fromisoformat(ended_at), UUID(pass_id)
    except ValueError:
        raise BadPassCursorError() from None


class SyncPassStore:
    """The pass rows' reads and prune, on the spools store's connections."""

    def __init__(self, spools: FilamentSpoolStore) -> None:
        self._spools = spools

    def page(self, *, cursor: str | None = None, limit: int = 20) -> SyncPassPage:
        """One page of at most ``limit`` passes, newest first by ``(ended_at, id)``. The
        cursor names the last row of the page before, so two passes of one time never lose
        or repeat a row at the page's edge. `BadPassCursorError` for a cursor no page
        gave."""
        where, args = "", []
        if cursor is not None:
            ended_at, pass_id = _decode(cursor)
            where, args = " WHERE (ended_at, id) < (%s, %s)", [ended_at, pass_id]
        with self._spools.transaction() as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM filament_bb_sync_passes{where}"
                " ORDER BY ended_at DESC, id DESC LIMIT %s",
                [*args, limit + 1],
            ).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        return SyncPassPage(
            passes=[SyncPass.model_validate(row) for row in rows],
            next_cursor=_encode(rows[-1]["ended_at"], rows[-1]["id"]) if more else None,
        )

    def prune(self, ttl_days: int | None, batch: int = PRUNE_BATCH) -> int:
        """Delete up to ``batch`` rows that ended more than ``ttl_days`` days ago
        (`PASS_TTL_DAYS_DEFAULT` when ``None``), oldest first. Answers how many went."""
        with self._spools.transaction() as conn:
            return conn.execute(
                "DELETE FROM filament_bb_sync_passes WHERE id IN"
                " (SELECT id FROM filament_bb_sync_passes"
                " WHERE ended_at < now() - make_interval(days => %s)"
                " ORDER BY ended_at LIMIT %s)",
                (ttl_days or PASS_TTL_DAYS_DEFAULT, batch),
            ).rowcount
