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
from datetime import UTC, datetime, timedelta
from typing import Protocol

from psycopg import Connection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from scadbuddy.bambuddy.client import client_for
from scadbuddy.bambuddy.follow import SettledHook
from scadbuddy.bambuddy.models import ArchiveDetail, PrinterStatus
from scadbuddy.bambuddy.print_links import PrintLink, PrintLinkStore
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
#: How long one archive's read from Bambuddy may take in a settle (#1086 review), so a
#: stalled read costs that archive and not the ones after it. The write that follows is
#: bounded by ``STATEMENT_TIMEOUT_MS`` instead: a write cut off here would run on in its
#: thread and could land after a warning that said it had not. Read, write and a wait
#: for a pool connection can take ~35 s, so the follow's ``SETTLE_TIMEOUT`` (60 s)
#: covers one or two slow archives; it can cut the hook off mid-write, and that
#: archive's write then still lands (``tests/rack/test_settle.py``).
ARCHIVE_TIMEOUT = 15.0
#: The whole budget of a settle's settings read, its wait for a connection included (#1111):
#: well inside the follow's ``SETTLE_TIMEOUT``. It covers a read Postgres is slow to
#: answer, not a connection that gets no reply at all (#1226). A settle whose read is cut
#: off records nothing: its archives are recorded only by that output's next settle,
#: which a one-off output may never have. That loss is the price of freeing the thread.
SETTINGS_READ_TIMEOUT = 10.0

#: What each advisory store write or read below logs when it swallows an exception, by
#: type (#1112). Named so the tests' programming-error guard reads the same strings.
RACK_SEEN_FALLBACK = "could not record the rack's hotends"
RACK_PICKS_FALLBACK = "could not record the rack picks"
RACK_SETTLE_READ_FALLBACK = "could not read a settled print's rack picks"
RACK_SETTLE_FALLBACK = "could not record a rack nozzle's print"
#: Logged when the follow cuts a settle off (#1113): it is not retried (spec §4), so
#: this names, by id, the archives it had not recorded. Not a fallback: it re-raises.
RACK_SETTLE_CUT_OFF = "a rack settle was cut off with archives unrecorded"
RACK_STORE_FALLBACKS = frozenset(
    {RACK_SEEN_FALLBACK, RACK_PICKS_FALLBACK, RACK_SETTLE_READ_FALLBACK, RACK_SETTLE_FALLBACK}
)


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
    """Lower the connection's statement timeout to ``STATEMENT_TIMEOUT_MS``; one the
    conninfo already sets lower is kept (#1086 review)."""
    row = conn.execute(
        "SELECT current_setting('statement_timeout')::interval AS current"
    ).fetchone()
    current = row["current"] if row is not None else timedelta(0)
    if not current or current > timedelta(milliseconds=STATEMENT_TIMEOUT_MS):
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
            # conninfo already carries (a search_path, say). It only ever lowers one.
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
                # Unbounded while migrating, deliberately (#1086 review): the wait for
                # another process's migration lock, and a slow migration, count toward
                # it. This also lifts a timeout the conninfo sets, so the first rack call
                # in a process (on the print path too) waits as long as the lock is
                # held. The bound it had is restored afterwards.
                row = conn.execute("SHOW statement_timeout").fetchone()
                bound = row["statement_timeout"] if row is not None else "0"
                conn.execute("SET statement_timeout = 0")
                try:
                    migrate(conn)
                finally:
                    # Not on a broken connection: the pool drops it, and a second error
                    # here would replace the migration's own (#1086 review).
                    if not conn.broken:
                        conn.execute("SELECT set_config('statement_timeout', %s, false)", (bound,))
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
                # debounced re-check (#1082). A row this skips makes no new version, but
                # is still locked, so a re-check is cheap rather than free.
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
            RACK_SEEN_FALLBACK,
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
            RACK_PICKS_FALLBACK,
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
    archive_timeout: float = ARCHIVE_TIMEOUT,
) -> int:
    """One ``rack_nozzle_prints`` row per linked archive and picked group (spec §4); the
    rows written. Every ended archive counts, whatever the print's outcome: the hotend
    wore either way (spec §10). One still running is left for its own settle. An
    archive linked by hash has no queue item and is not counted. Idempotent, so a
    settle seen twice writes nothing the second time. Each failure is logged by type
    and ids and skipped, as is an archive read that stalls past ``archive_timeout``.
    Nothing is retried now: an archive skipped here is recorded by the output's next
    settle, which reads every linked archive not yet recorded. A settle cut off by the
    follow logs the ids of those it had not recorded (``RACK_SETTLE_CUT_OFF``, stage
    ``archives``). One cut off during the initial reads logs stage ``read`` with the
    links read so far, which may include archives already recorded. A cut-off before
    this function starts (the hook's settings load) is not logged here."""
    linked: list[tuple[int, int]] = []
    try:
        linked = [
            (link.archive_id, link.queue_item_id)
            for link in await links.for_output(output_id)
            if link.queue_item_id is not None
        ]
        picked = await store.picked_items(item for _, item in linked)
        recorded = await store.recorded_archives(archive for archive, _ in linked)
    except asyncio.CancelledError:
        # Cut off before the loop: the ids are the links read so far (candidates, not yet
        # filtered to the unrecorded), none if the link read itself was in flight.
        logger.warning(
            RACK_SETTLE_CUT_OFF,
            extra={
                "output_id": output_id,
                "stage": "read",
                "archive_ids": [archive for archive, _ in linked],
            },
        )
        raise
    except Exception as exc:
        logger.warning(
            RACK_SETTLE_READ_FALLBACK,
            extra={"output_id": output_id, "error": type(exc).__name__},
        )
        return 0

    async def record_one(archive_id: int, queue_item_id: int) -> int:
        archive = await asyncio.wait_for(client.archive(archive_id), timeout=archive_timeout)
        if archive.status not in SETTLED_STATUSES:
            # Another print of this output, still running: its own settle counts it,
            # with its real time, which DO NOTHING would never let in after this.
            return 0
        seconds = (
            archive.actual_time_seconds
            if archive.actual_time_seconds is not None
            else archive.print_time_seconds
        )
        return await store.record_prints(
            archive_id=archive_id,
            queue_item_id=queue_item_id,
            settled_at=now(),
            print_seconds=seconds,
            grams=archive.filament_used_grams,
        )

    written = 0
    pending = [
        (archive_id, queue_item_id)
        for archive_id, queue_item_id in linked
        if queue_item_id in picked and archive_id not in recorded
    ]
    for index, (archive_id, queue_item_id) in enumerate(pending):
        try:
            written += await record_one(archive_id, queue_item_id)
        except asyncio.CancelledError:
            # Ids only (spec §7). The one in flight is named too: its write may still
            # land, its read did not.
            logger.warning(
                RACK_SETTLE_CUT_OFF,
                extra={
                    "output_id": output_id,
                    "stage": "archives",
                    "archive_ids": [archive for archive, _ in pending[index:]],
                },
            )
            raise
        except Exception as exc:
            logger.warning(
                RACK_SETTLE_FALLBACK,
                extra={
                    "output_id": output_id,
                    "archive_id": archive_id,
                    "error": type(exc).__name__,
                },
            )
    return written


def settle_hook(
    store: RackUsage, links: PrintLinkStore, load: Callable[[float], StoredSettings]
) -> SettledHook:
    """The print follow's ``on_settled`` hook for the rack (spec §4).

    It links nothing itself: the follow's settled read is a ``progress_for`` call, which
    records each queue item's ``archive_id`` before it returns the settled progress, and
    the hook runs after that read. So a print dispatched and settled between two polls is
    linked by the read that finds it settled (``tests/rack/test_settle.py``)."""

    async def hook(meta: OutputMeta) -> None:
        if not links.available:
            return
        # A settings read is a database read: off the event loop, so the follow stops
        # waiting on it at its timeout (#1083), and bounded itself (#1111), so a read
        # stuck on a slow Postgres gives its thread back to the shared executor rather
        # than holding it until Postgres answers.
        settings = await asyncio.to_thread(load, SETTINGS_READ_TIMEOUT)
        async with client_for(settings) as client:
            await record_settled(meta.id, client=client, links=links, store=store)

    return hook
