"""Rack hotend usage (#836, spec 2026-10-01 §4): ``rack_nozzle_seen``,
``rack_nozzle_picks`` and ``rack_nozzle_prints`` (``migrations/*_rack_nozzle_usage.sql``).

The one store that owns the three tables. It connects on first use and applies the
backend's migrations itself, as ``analyzers.decisions.PostgresDecisionStore`` does. Every
public method is a coroutine that runs its query in a worker thread.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Protocol

from psycopg import Connection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from scadbuddy.bambuddy.models import PrinterStatus
from scadbuddy.rack.rank import Usage, rack_serials
from scadbuddy.render.pg_store import migrate

logger = logging.getLogger(__name__)

#: As the decision store's: a request is told the store is unavailable rather than hang.
CONNECT_TIMEOUT = 5.0


class PickedHotend(BaseModel):
    """A sent pick with the hotend it named. Carries a serial: backend only (spec §7)."""

    group_id: int
    position: int
    serial: str = Field(repr=False)


class RackUsage(Protocol):
    async def seen(self, printer_id: int, serials: Iterable[str]) -> None: ...

    async def usage(self, serials: Iterable[str]) -> dict[str, Usage]: ...

    async def record_picks(
        self, queue_item_id: int, printer_id: int, picks: Sequence[PickedHotend]
    ) -> int: ...

    async def picked_items(self, queue_item_ids: Iterable[int]) -> set[int]: ...

    async def record_prints(
        self,
        *,
        archive_id: int,
        queue_item_id: int,
        settled_at: datetime,
        print_seconds: int | None,
        grams: float | None,
    ) -> int: ...


class RackUsageStore:
    def __init__(
        self, conninfo: str, *, pool_size: int = 2, connect_timeout: float = CONNECT_TIMEOUT
    ) -> None:
        self._pool: ConnectionPool[Connection[DictRow]] = ConnectionPool(
            conninfo,
            min_size=1,
            max_size=pool_size,
            open=False,
            timeout=connect_timeout,
            connection_class=Connection[DictRow],
            kwargs={
                "autocommit": True,
                "row_factory": dict_row,
                "connect_timeout": max(1, int(connect_timeout)),
            },
            name="scadbuddy-rack-usage",
        )
        self._pool_open = False
        self._migrated = False
        self._open_lock = threading.Lock()

    def _ready(self) -> ConnectionPool[Connection[DictRow]]:
        with self._open_lock:
            if not self._pool_open:
                self._pool.open(wait=False)
                self._pool_open = True
        if not self._migrated:
            with self._pool.connection() as conn:
                migrate(conn)
            self._migrated = True
        return self._pool

    def close(self) -> None:
        with self._open_lock:
            if self._pool_open:
                self._pool.close()
                self._pool_open = False

    async def seen(self, printer_id: int, serials: Iterable[str]) -> None:
        """Upsert each non-empty serial once: moves its printer, never its
        ``first_seen_at`` (spec §4)."""
        unique = sorted({serial for serial in serials if serial})
        if unique:
            await asyncio.to_thread(self._seen, printer_id, unique)

    def _seen(self, printer_id: int, unique: list[str]) -> None:
        with self._ready().connection() as conn:
            conn.execute(
                "INSERT INTO rack_nozzle_seen (serial, printer_id)"
                " SELECT serial, %s FROM unnest(%s::text[]) AS serial"
                " ON CONFLICT (serial) DO UPDATE SET printer_id = excluded.printer_id",
                (printer_id, unique),
            )

    async def usage(self, serials: Iterable[str]) -> dict[str, Usage]:
        """One :class:`Usage` per non-empty serial; zeros for one with no rows (spec §4)."""
        unique = sorted({serial for serial in serials if serial})
        if not unique:
            return {}
        return await asyncio.to_thread(self._usage, unique)

    def _usage(self, unique: list[str]) -> dict[str, Usage]:
        with self._ready().connection() as conn:
            rows = conn.execute(
                "SELECT s.serial, seen.first_seen_at, count(p.archive_id) AS prints,"
                " coalesce(sum(p.print_seconds), 0) AS print_seconds,"
                " coalesce(sum(p.grams), 0) AS grams"
                " FROM unnest(%s::text[]) AS s(serial)"
                " LEFT JOIN rack_nozzle_seen AS seen ON seen.serial = s.serial"
                " LEFT JOIN rack_nozzle_prints AS p ON p.serial = s.serial"
                " GROUP BY s.serial, seen.first_seen_at",
                (unique,),
            ).fetchall()
        return {
            str(row["serial"]): Usage(
                prints=int(row["prints"]),
                print_seconds=int(row["print_seconds"]),
                grams=float(row["grams"]),
                first_seen_at=row["first_seen_at"],
            )
            for row in rows
        }

    async def record_picks(
        self, queue_item_id: int, printer_id: int, picks: Sequence[PickedHotend]
    ) -> int:
        """The picks for one queue item; the rows actually written (#1015)."""
        if not picks:
            return 0
        return await asyncio.to_thread(self._record_picks, queue_item_id, printer_id, list(picks))

    def _record_picks(self, queue_item_id: int, printer_id: int, picks: list[PickedHotend]) -> int:
        written = 0
        with self._ready().connection() as conn, conn.transaction():
            for pick in picks:
                cursor = conn.execute(
                    "INSERT INTO rack_nozzle_picks (queue_item_id, group_id, printer_id, serial)"
                    " VALUES (%s, %s, %s, %s)"
                    " ON CONFLICT (queue_item_id, group_id) DO NOTHING",
                    (queue_item_id, pick.group_id, printer_id, pick.serial),
                )
                written += cursor.rowcount
        return written

    async def picked_items(self, queue_item_ids: Iterable[int]) -> set[int]:
        """Which of these queue items have picks."""
        ids = sorted(set(queue_item_ids))
        if not ids:
            return set()
        return await asyncio.to_thread(self._picked_items, ids)

    def _picked_items(self, ids: list[int]) -> set[int]:
        with self._ready().connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT queue_item_id FROM rack_nozzle_picks"
                " WHERE queue_item_id = ANY(%s)",
                (ids,),
            ).fetchall()
        return {int(row["queue_item_id"]) for row in rows}

    async def record_prints(
        self,
        *,
        archive_id: int,
        queue_item_id: int,
        settled_at: datetime,
        print_seconds: int | None,
        grams: float | None,
    ) -> int:
        """One row per picked group of the item, the serial copied from its pick in SQL,
        so it never passes through Python on the settle path."""
        return await asyncio.to_thread(
            self._record_prints, archive_id, queue_item_id, settled_at, print_seconds, grams
        )

    def _record_prints(
        self,
        archive_id: int,
        queue_item_id: int,
        settled_at: datetime,
        print_seconds: int | None,
        grams: float | None,
    ) -> int:
        with self._ready().connection() as conn:
            cursor = conn.execute(
                "INSERT INTO rack_nozzle_prints"
                " (archive_id, group_id, serial, settled_at, print_seconds, grams)"
                " SELECT %s, group_id, serial, %s, %s, %s FROM rack_nozzle_picks"
                " WHERE queue_item_id = %s"
                " ON CONFLICT (archive_id, group_id) DO NOTHING",
                (archive_id, settled_at, print_seconds, grams, queue_item_id),
            )
        return cursor.rowcount


async def record_seen(
    store: RackUsage | None, printer_id: int, status: PrinterStatus | None
) -> None:
    """Record the rack's hotends as seen by the print flow (spec §4). Advisory: a failure
    is logged by exception type and never stops the read that called it."""
    if store is None or status is None:
        return
    try:
        await store.seen(printer_id, rack_serials(status.nozzle_rack))
    except Exception as exc:
        logger.warning(
            "could not record the rack's hotends",
            extra={"printer_id": printer_id, "error": type(exc).__name__},
        )
