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
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from typing import Protocol

from psycopg import Connection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from scadbuddy.bambuddy.client import client_for
from scadbuddy.bambuddy.models import ArchiveDetail, PrinterStatus
from scadbuddy.bambuddy.print_links import PrintLink, PrintLinkStore
from scadbuddy.bambuddy.watcher import SettledHook
from scadbuddy.library.outputs import OutputMeta
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.rack.rank import Usage, rack_serials
from scadbuddy.render.pg_store import migrate

logger = logging.getLogger(__name__)

#: As the decision store's: a request is told the store is unavailable rather than hang.
CONNECT_TIMEOUT = 5.0
#: Bounds every query on this store's connections, so a stuck read releases its thread
#: and connection even after an awaiting caller has stopped waiting (#1086 review).
STATEMENT_TIMEOUT_MS = 15_000


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

    async def recorded_archives(self, archive_ids: Iterable[int]) -> set[int]: ...

    async def record_prints(
        self,
        *,
        archive_id: int,
        queue_item_id: int,
        settled_at: datetime,
        print_seconds: int | None,
        grams: float | None,
    ) -> int: ...


def _bound_statements(conn: Connection[DictRow]) -> None:
    conn.execute(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS}")


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
            # Set per connection rather than as ``options``, which would replace any the
            # conninfo already carries (a search_path, say).
            configure=_bound_statements,
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
                " ON CONFLICT (serial) DO UPDATE SET printer_id = excluded.printer_id"
                # Rewrite only a hotend that moved: /check re-records the rack on every
                # debounced re-check (#1082).
                " WHERE rack_nozzle_seen.printer_id IS DISTINCT FROM excluded.printer_id",
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

    async def recorded_archives(self, archive_ids: Iterable[int]) -> set[int]:
        """Which of these archives already have print rows, so a settle need not read
        them again (claude-review on #1043)."""
        ids = sorted(set(archive_ids))
        if not ids:
            return set()
        return await asyncio.to_thread(self._recorded_archives, ids)

    def _recorded_archives(self, ids: list[int]) -> set[int]:
        with self._ready().connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT archive_id FROM rack_nozzle_prints WHERE archive_id = ANY(%s)",
                (ids,),
            ).fetchall()
        return {int(row["archive_id"]) for row in rows}

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


async def save_picks(
    store: RackUsage | None,
    printer_id: int,
    queue_item_ids: Sequence[int],
    picks: Sequence[PickedHotend],
) -> None:
    """Write the sent picks against each queue item, right after ``POST /queue/`` (spec
    §5). Advisory: the item is queued, so a failure is logged by type and dropped, and
    that print's use goes uncounted. A pick of a hotend with no serial is skipped: it was
    sent, but nothing can be attributed to it."""
    attributable = [pick for pick in picks if pick.serial]
    if store is None or not attributable:
        return
    try:
        for queue_item_id in queue_item_ids:
            written = await store.record_picks(queue_item_id, printer_id, attributable)
            if written != len(attributable):
                logger.warning(
                    "a rack pick was already recorded for this queue item",
                    extra={
                        "queue_item_id": queue_item_id,
                        "sent": len(attributable),
                        "written": written,
                    },
                )
    except Exception as exc:
        logger.warning(
            "could not record the rack picks",
            extra={"printer_id": printer_id, "error": type(exc).__name__},
        )


class ArchiveReader(Protocol):
    async def archive(self, archive_id: int) -> ArchiveDetail: ...


class LinkReader(Protocol):
    async def for_output(self, output_id: str) -> list[PrintLink]: ...


def _now() -> datetime:
    return datetime.now(UTC)


#: An archive's ``status`` once its print has ended, however it ended (spec §10).
SETTLED_STATUSES = frozenset({"completed", "failed", "cancelled"})


async def record_settled(
    output_id: str,
    *,
    client: ArchiveReader,
    links: LinkReader,
    store: RackUsage,
    now: Callable[[], datetime] = _now,
) -> int:
    """One ``rack_nozzle_prints`` row per linked archive and picked group (spec §4); the
    rows written. Every ended archive counts, whatever the print's outcome: the hotend
    wore either way (spec §10). One still running is left for its own settle. An
    archive linked by hash has no queue item and is not counted. Idempotent, so a
    settle seen twice writes nothing the second time. Each failure is logged by type
    and ids and skipped; nothing is retried."""
    try:
        linked = [
            (link.archive_id, link.queue_item_id)
            for link in await links.for_output(output_id)
            if link.queue_item_id is not None
        ]
        picked = await store.picked_items(item for _, item in linked)
        recorded = await store.recorded_archives(archive for archive, _ in linked)
    except Exception as exc:
        logger.warning(
            "could not read a settled print's rack picks",
            extra={"output_id": output_id, "error": type(exc).__name__},
        )
        return 0
    written = 0
    for archive_id, queue_item_id in linked:
        if queue_item_id not in picked or archive_id in recorded:
            continue
        try:
            archive = await client.archive(archive_id)
            if archive.status not in SETTLED_STATUSES:
                # Another print of this output, still running: its own settle counts it,
                # with its real time, which DO NOTHING would never let in after this.
                continue
            seconds = (
                archive.actual_time_seconds
                if archive.actual_time_seconds is not None
                else archive.print_time_seconds
            )
            written += await store.record_prints(
                archive_id=archive_id,
                queue_item_id=queue_item_id,
                settled_at=now(),
                print_seconds=seconds,
                grams=archive.filament_used_grams,
            )
        except Exception as exc:
            logger.warning(
                "could not record a rack nozzle's print",
                extra={
                    "output_id": output_id,
                    "archive_id": archive_id,
                    "error": type(exc).__name__,
                },
            )
    return written


def settle_hook(
    store: RackUsage, links: PrintLinkStore, load: Callable[[], StoredSettings]
) -> SettledHook:
    """The watcher's ``on_settled`` hook for the rack (spec §4).

    It links nothing itself: the watcher's settled read is a ``progress_for`` call, which
    records each queue item's ``archive_id`` before it returns the settled progress, and
    the hook runs after that read. So a print dispatched and settled between two polls is
    linked by the read that finds it settled (``tests/rack/test_settle.py``)."""

    async def hook(meta: OutputMeta) -> None:
        if not links.available:
            return
        # A settings read is a database read: off the event loop, so the watcher stops
        # waiting on it at its timeout (#1083). The thread itself runs on; the store's
        # statement timeout is what bounds a stuck query.
        settings = await asyncio.to_thread(load)
        async with client_for(settings) as client:
            await record_settled(meta.id, client=client, links=links, store=store)

    return hook
