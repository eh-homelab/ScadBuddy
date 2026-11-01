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
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Protocol

from psycopg import Connection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from scadbuddy.bambuddy.client import client_for
from scadbuddy.bambuddy.filaments import SpoolOption, normalise_colour, spool_label
from scadbuddy.bambuddy.follow import ReadHook, SettledHook
from scadbuddy.bambuddy.models import ArchiveDetail, FilamentRequirement, PrinterStatus
from scadbuddy.bambuddy.print_links import PrintLink, PrintLinkStore
from scadbuddy.bambuddy.progress import PrintProgress
from scadbuddy.bambuddy.subject import PrintSubject
from scadbuddy.core.pg_keepalive import TCP_KEEPALIVE
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
#: off records nothing: its archives are recorded only by that subject's next settle,
#: which a one-off print may never have. That loss is the price of freeing the thread.
SETTINGS_READ_TIMEOUT = 10.0
#: How long an open pick (one whose print has not settled) counts toward Least used
#: (#1079, spec §4). A cancelled queue item never settles, so its pick ages out here; a
#: print still waiting or running past it stops counting until it settles.
PENDING_PICK_MAX_AGE = timedelta(days=3)

#: What each advisory store write or read below logs when it swallows an exception, by
#: type (#1112). Named so the tests' programming-error guard reads the same strings.
RACK_SEEN_FALLBACK = "could not record the rack's hotends"
RACK_PICKS_FALLBACK = "could not record the rack picks"
RACK_SETTLE_READ_FALLBACK = "could not read a settled print's rack picks"
RACK_SETTLE_FALLBACK = "could not record a rack nozzle's print"
#: Logged when the follow cuts a settle off (#1113): it is not retried (spec §4), so
#: this names, by id, the archives it had not recorded. Not a fallback: it re-raises.
RACK_SETTLE_CUT_OFF = "a rack settle was cut off with archives unrecorded"
#: Logged when a settle's settings read fails or is cut off at ``SETTINGS_READ_TIMEOUT``
#: (#1262): that settle records nothing, and nothing retries it. Not a fallback: it
#: re-raises.
RACK_SETTLE_SETTINGS_DROPPED = (
    "a rack settle could not read the settings; its usage is not recorded"
)
RACK_STORE_FALLBACKS = frozenset(
    {RACK_SEEN_FALLBACK, RACK_PICKS_FALLBACK, RACK_SETTLE_READ_FALLBACK, RACK_SETTLE_FALLBACK}
)


class PickedSpool(BaseModel):
    """One filament slot of a picked group, and the spool it printed from (#2170)."""

    slot_id: int
    #: Bambuddy's inventory spool; ``None`` when the slot had none.
    spool_id: int | None = None
    label: str | None = None
    material: str | None = None
    #: ``#RRGGBB``.
    colour: str | None = None
    #: The slicer's grams for one plate; ``None`` when it did not say.
    grams: float | None = None


class PickedHotend(BaseModel):
    """A sent pick with the hotend it named. Carries a serial: backend only (spec §7)."""

    group_id: int
    position: int
    serial: str = Field(repr=False)
    #: The spools the group printed from (#2170), recorded with the pick.
    spools: list[PickedSpool] = Field(default_factory=list)


class SpoolUse(BaseModel):
    """One spool's settled prints on one hotend (#2170). A slot that printed from no
    inventory spool is one row per label, with ``spool_id`` ``None``."""

    spool_id: int | None
    label: str | None
    material: str | None
    colour: str | None
    prints: int
    grams: float
    last_used_at: datetime | None


class RackUsage(Protocol):
    async def seen(self, printer_id: int, serials: Iterable[str]) -> None: ...

    async def usage(self, serials: Iterable[str]) -> dict[str, Usage]: ...

    async def record_picks(
        self,
        queue_item_id: int,
        printer_id: int,
        picks: Sequence[PickedHotend],
        *,
        copies: int = 1,
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


class LanUsage(Protocol):
    """A LAN start's picks (#4543), keyed by its run and ``subtask_name``."""

    async def record_lan_picks(
        self,
        run_id: str,
        subtask_name: str,
        printer_id: int | None,
        picks: Sequence[PickedHotend],
    ) -> int: ...

    async def settle_lan(
        self, run_id: str, subtask_name: str | None = None, *, timed: bool = True
    ) -> int: ...


class LanRackUsage(RackUsage, LanUsage, Protocol):
    """What a LAN start ranks its pick with and saves it to (`printers.print_activities`)."""


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
                **TCP_KEEPALIVE,
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
        """One :class:`Usage` per non-empty serial; zeros for one with no rows (spec §4).
        ``pending`` counts its open picks younger than ``PENDING_PICK_MAX_AGE`` (#1079),
        one per copy of the pick's queue item not yet settled (#2134)."""
        unique = sorted({serial for serial in serials if serial})
        if not unique:
            return {}
        return await asyncio.to_thread(self._usage, unique)

    def _usage(self, unique: list[str]) -> dict[str, Usage]:
        with self._ready().connection() as conn:
            rows = conn.execute(
                # A LAN print's row has no archive (#4543): every row is one print.
                "SELECT s.serial, seen.first_seen_at, count(p.serial) AS prints,"
                " coalesce(sum(p.print_seconds), 0) AS print_seconds,"
                " coalesce(sum(p.grams), 0) AS grams,"
                " max(p.settled_at) AS last_used_at,"
                # Open picks (#1079): each of a pick's copies with no print row for its
                # item (or its LAN start, #4543) and group yet, as every copy settles its
                # own (#2134).
                " (SELECT coalesce(sum(greatest(k.copies - (SELECT count(*)"
                "   FROM rack_nozzle_prints AS done"
                "   WHERE (done.queue_item_id = k.queue_item_id"
                "    OR (done.lan_run_id = k.lan_run_id AND done.lan_subtask = k.lan_subtask))"
                "   AND done.group_id = k.group_id), 0)), 0)"
                "  FROM rack_nozzle_picks AS k"
                "  WHERE k.serial = s.serial AND k.picked_at > now() - %s) AS pending"
                " FROM unnest(%s::text[]) AS s(serial)"
                " LEFT JOIN rack_nozzle_seen AS seen ON seen.serial = s.serial"
                " LEFT JOIN rack_nozzle_prints AS p ON p.serial = s.serial"
                " GROUP BY s.serial, seen.first_seen_at",
                (PENDING_PICK_MAX_AGE, unique),
            ).fetchall()
        return {
            str(row["serial"]): Usage(
                prints=int(row["prints"]),
                print_seconds=int(row["print_seconds"]),
                grams=float(row["grams"]),
                first_seen_at=row["first_seen_at"],
                pending=int(row["pending"]),
                last_used_at=row["last_used_at"],
            )
            for row in rows
        }

    async def spool_history(self, serials: Iterable[str]) -> dict[str, list[SpoolUse]]:
        """Each serial's settled spools, most used first (#2170). A serial with none is
        missing from the answer."""
        unique = sorted({serial for serial in serials if serial})
        if not unique:
            return {}
        return await asyncio.to_thread(self._spool_history, unique)

    def _spool_history(self, unique: list[str]) -> dict[str, list[SpoolUse]]:
        with self._ready().connection() as conn:
            rows = conn.execute(
                "SELECT p.serial, s.spool_id,"
                # A spool's newest label, colour and material: it may have been renamed.
                " (array_agg(s.label ORDER BY p.settled_at DESC))[1] AS label,"
                " (array_agg(s.material ORDER BY p.settled_at DESC))[1] AS material,"
                " (array_agg(s.colour ORDER BY p.settled_at DESC))[1] AS colour,"
                # A print is its archive, or its LAN start (#4543).
                " count(DISTINCT coalesce(p.archive_id::text,"
                "  p.lan_run_id || '/' || p.lan_subtask)) AS prints,"
                " coalesce(sum(s.grams), 0) AS grams,"
                " max(p.settled_at) AS last_used_at"
                " FROM rack_nozzle_prints AS p"
                " JOIN rack_nozzle_pick_spools AS s"
                "  ON s.group_id = p.group_id AND (s.queue_item_id = p.queue_item_id"
                "  OR (s.lan_run_id = p.lan_run_id AND s.lan_subtask = p.lan_subtask))"
                " WHERE p.serial = ANY(%s)"
                # No inventory spool: one row per label instead.
                " GROUP BY p.serial, s.spool_id,"
                "  CASE WHEN s.spool_id IS NULL THEN s.label END"
                " ORDER BY p.serial, prints DESC, last_used_at DESC",
                (unique,),
            ).fetchall()
        history: dict[str, list[SpoolUse]] = {}
        for row in rows:
            history.setdefault(str(row["serial"]), []).append(
                SpoolUse(
                    spool_id=row["spool_id"],
                    label=row["label"],
                    material=row["material"],
                    colour=row["colour"],
                    prints=int(row["prints"]),
                    grams=float(row["grams"]),
                    last_used_at=row["last_used_at"],
                )
            )
        return history

    async def record_picks(
        self,
        queue_item_id: int,
        printer_id: int,
        picks: Sequence[PickedHotend],
        *,
        copies: int = 1,
    ) -> int:
        """The picks for one queue item of ``copies`` copies, with their spools; the
        pick rows actually written (#1015)."""
        if not picks:
            return 0
        return await asyncio.to_thread(
            self._record_picks, queue_item_id, printer_id, list(picks), copies
        )

    def _record_picks(
        self, queue_item_id: int, printer_id: int, picks: list[PickedHotend], copies: int
    ) -> int:
        written = 0
        with self._ready().connection() as conn, conn.transaction():
            for pick in picks:
                cursor = conn.execute(
                    "INSERT INTO rack_nozzle_picks"
                    " (queue_item_id, group_id, printer_id, serial, copies)"
                    " VALUES (%s, %s, %s, %s, %s)"
                    " ON CONFLICT (queue_item_id, group_id) DO NOTHING",
                    (queue_item_id, pick.group_id, printer_id, pick.serial, copies),
                )
                written += cursor.rowcount
                for spool in pick.spools:
                    conn.execute(
                        "INSERT INTO rack_nozzle_pick_spools"
                        " (queue_item_id, group_id, slot_id, spool_id, label, material,"
                        " colour, grams) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
                        " ON CONFLICT (queue_item_id, group_id, slot_id) DO NOTHING",
                        (
                            queue_item_id,
                            pick.group_id,
                            spool.slot_id,
                            spool.spool_id,
                            spool.label,
                            spool.material,
                            spool.colour,
                            spool.grams,
                        ),
                    )
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
                " (archive_id, group_id, serial, settled_at, print_seconds, grams, queue_item_id)"
                " SELECT %s, group_id, serial, %s, %s, %s, queue_item_id FROM rack_nozzle_picks"
                " WHERE queue_item_id = %s"
                " ON CONFLICT (archive_id, group_id) DO NOTHING",
                (archive_id, settled_at, print_seconds, grams, queue_item_id),
            )
        return cursor.rowcount

    async def record_lan_picks(
        self,
        run_id: str,
        subtask_name: str,
        printer_id: int | None,
        picks: Sequence[PickedHotend],
    ) -> int:
        """The picks sent with one LAN start (#4543), keyed by its run and its
        ``subtask_name``, with their spools; the pick rows written. A start picked again
        (its start may not have been taken, so it was sent again) keeps the newer pick:
        that one prints. ``printer_id`` is the Bambuddy printer the rack tables key on,
        ``None`` for a LAN-only printer."""
        if not picks:
            return 0
        return await asyncio.to_thread(
            self._record_lan_picks, run_id, subtask_name, printer_id, list(picks)
        )

    def _record_lan_picks(
        self, run_id: str, subtask: str, printer_id: int | None, picks: list[PickedHotend]
    ) -> int:
        written = 0
        with self._ready().connection() as conn, conn.transaction():
            # The earlier pick's spools go with it: the newer pick may print others.
            conn.execute(
                "DELETE FROM rack_nozzle_pick_spools WHERE lan_run_id = %s AND lan_subtask = %s",
                (run_id, subtask),
            )
            conn.execute(
                "DELETE FROM rack_nozzle_picks WHERE lan_run_id = %s AND lan_subtask = %s"
                " AND group_id <> ALL(%s)",
                (run_id, subtask, [pick.group_id for pick in picks]),
            )
            for pick in picks:
                cursor = conn.execute(
                    "INSERT INTO rack_nozzle_picks"
                    " (lan_run_id, lan_subtask, group_id, printer_id, serial)"
                    " VALUES (%s, %s, %s, %s, %s)"
                    " ON CONFLICT (lan_run_id, lan_subtask, group_id) DO UPDATE SET"
                    " printer_id = EXCLUDED.printer_id, serial = EXCLUDED.serial,"
                    " picked_at = now()",
                    (run_id, subtask, pick.group_id, printer_id, pick.serial),
                )
                written += cursor.rowcount
                for spool in pick.spools:
                    conn.execute(
                        "INSERT INTO rack_nozzle_pick_spools"
                        " (lan_run_id, lan_subtask, group_id, slot_id, spool_id, label,"
                        " material, colour, grams) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
                        " ON CONFLICT (lan_run_id, lan_subtask, group_id, slot_id) DO NOTHING",
                        (
                            run_id,
                            subtask,
                            pick.group_id,
                            spool.slot_id,
                            spool.spool_id,
                            spool.label,
                            spool.material,
                            spool.colour,
                            spool.grams,
                        ),
                    )
        return written

    async def settle_lan(
        self, run_id: str, subtask_name: str | None = None, *, timed: bool = True
    ) -> int:
        """One ``rack_nozzle_prints`` row per open pick of a LAN run (#4543): of one start,
        or with ``subtask_name`` ``None`` of every start of the run; the rows written. Its
        time is from the pick (made as the printer took the start) to now, on the
        database's clock, or none when not ``timed`` (the report never said it ended);
        its grams the group's sliced grams. A start settled already writes nothing, so
        each copy's own settle and the run's last one never count it twice."""
        return await asyncio.to_thread(self._settle_lan, run_id, subtask_name, timed)

    def _settle_lan(self, run_id: str, subtask: str | None, timed: bool) -> int:
        with self._ready().connection() as conn:
            cursor = conn.execute(
                "INSERT INTO rack_nozzle_prints"
                " (lan_run_id, lan_subtask, group_id, serial, settled_at, print_seconds, grams)"
                " SELECT k.lan_run_id, k.lan_subtask, k.group_id, k.serial, now(),"
                "  CASE WHEN %s THEN greatest(0, extract(epoch FROM now() - k.picked_at))::bigint"
                "  END,"
                "  (SELECT sum(s.grams) FROM rack_nozzle_pick_spools AS s"
                "   WHERE s.lan_run_id = k.lan_run_id AND s.lan_subtask = k.lan_subtask"
                "   AND s.group_id = k.group_id)"
                " FROM rack_nozzle_picks AS k"
                " WHERE k.lan_run_id = %s AND (%s::text IS NULL OR k.lan_subtask = %s)"
                " ON CONFLICT (lan_run_id, lan_subtask, group_id) DO NOTHING",
                (timed, run_id, subtask, subtask),
            )
        return cursor.rowcount


def group_spools(
    filaments: Sequence[FilamentRequirement],
    spools_by_slot: Mapping[int, SpoolOption],
    group_id: int,
) -> list[PickedSpool]:
    """The used slots of one sliced group and the spool each printed from (#2170)."""
    picked: list[PickedSpool] = []
    for filament in filaments:
        if not filament.used_in_plate or filament.group_id != group_id:
            continue
        spool = spools_by_slot.get(filament.slot_id)
        picked.append(
            PickedSpool(
                slot_id=filament.slot_id,
                spool_id=spool.spool_id if spool else None,
                label=spool_label(spool) if spool else filament.type,
                material=(spool.material if spool else None) or filament.type,
                colour=normalise_colour(spool.colour if spool and spool.colour else filament.color),
                # 0 is "not sliced", not "none" (``FilamentRequirement``).
                grams=filament.used_grams or None,
            )
        )
    return picked


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
    *,
    copies: int = 1,
) -> None:
    """Write the sent picks against each queue item of ``copies`` copies, right after
    ``POST /queue/`` (spec §5). Advisory: the item is queued, so a failure is logged by
    type and dropped, and that print's use goes uncounted. A pick of a hotend with no
    serial is skipped: it was sent, but nothing can be attributed to it."""
    attributable = [pick for pick in picks if pick.serial]
    if store is None or not attributable:
        return
    try:
        for queue_item_id in queue_item_ids:
            written = await store.record_picks(
                queue_item_id, printer_id, attributable, copies=copies
            )
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


async def save_lan_picks(
    store: LanUsage | None,
    run_id: str,
    subtask_name: str,
    printer_id: int | None,
    picks: Sequence[PickedHotend],
) -> None:
    """Write the picks sent with a LAN start (#4543), keyed by its run and its
    ``subtask_name``. Advisory, as `save_picks`: the start is sent, so a failure is
    logged by type and dropped. A pick of a hotend with no serial is skipped."""
    attributable = [pick for pick in picks if pick.serial]
    if store is None or not attributable:
        return
    try:
        await store.record_lan_picks(run_id, subtask_name, printer_id, attributable)
    except Exception as exc:
        logger.warning(
            RACK_PICKS_FALLBACK,
            extra={"run_id": run_id, "error": type(exc).__name__},
        )


async def settle_lan_print(
    store: LanUsage | None, run_id: str, subtask_name: str | None = None, *, timed: bool = True
) -> None:
    """Settle a LAN run's open picks (#4543): one start's, or every start's with
    ``subtask_name`` ``None``. Advisory: a failure is logged by type, and the run's last
    settle (the follow's) or the pick's age (`PENDING_PICK_MAX_AGE`) closes it."""
    if store is None:
        return
    try:
        await store.settle_lan(run_id, subtask_name, timed=timed)
    except Exception as exc:
        logger.warning(
            RACK_SETTLE_FALLBACK,
            extra={"run_id": run_id, "error": type(exc).__name__},
        )


def lan_settle_hook(store: LanUsage) -> ReadHook:
    """The print follow's read hook for a LAN rack print (#4543): once the run's print
    has settled (its last start ended, or the run stopped the rest), every pick of the
    run not settled yet is. ``lan_wait`` settles each earlier copy as the printer ends
    it; this settles the last, and any it missed. Timed unless the report never said how
    the print ended (``unknown``)."""

    async def hook(run_subject: str, progress: PrintProgress) -> None:
        if progress.route != "lan" or not progress.settled or progress.run_id is None:
            return
        await settle_lan_print(store, progress.run_id, timed=progress.stage != "unknown")

    return hook


class ArchiveReader(Protocol):
    async def archive(self, archive_id: int) -> ArchiveDetail: ...


class LinkReader(Protocol):
    async def for_subject(self, subject: PrintSubject) -> list[PrintLink]: ...


def _now() -> datetime:
    return datetime.now(UTC)


#: An archive's ``status`` once its print has ended, however it ended (spec §10).
SETTLED_STATUSES = frozenset({"completed", "failed", "cancelled"})


async def record_settled(
    subject: PrintSubject,
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
    ``subject`` is an output's or a library file's: both are linked by queue item
    (#1073). Nothing is retried now: an archive skipped here is recorded by the subject's
    next settle, which reads every linked archive not yet recorded. A settle cut off by the
    follow logs the ids of those it had not recorded (``RACK_SETTLE_CUT_OFF``, stage
    ``archives``). One cut off during the initial reads logs stage ``read`` with the
    links read so far, which may include archives already recorded. A cut-off before
    this function starts (the hook's settings load) is not logged here."""
    linked: list[tuple[int, int]] = []
    try:
        linked = [
            (link.archive_id, link.queue_item_id)
            for link in await links.for_subject(subject)
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
                "subject": subject.key,
                "stage": "read",
                "archive_ids": [archive for archive, _ in linked],
            },
        )
        raise
    except Exception as exc:
        logger.warning(
            RACK_SETTLE_READ_FALLBACK,
            extra={"subject": subject.key, "error": type(exc).__name__},
        )
        return 0

    async def record_one(archive_id: int, queue_item_id: int) -> int:
        archive = await asyncio.wait_for(client.archive(archive_id), timeout=archive_timeout)
        if archive.status not in SETTLED_STATUSES:
            # Another print of this subject, still running: its own settle counts it,
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
                    "subject": subject.key,
                    "stage": "archives",
                    "archive_ids": [archive for archive, _ in pending[index:]],
                },
            )
            raise
        except Exception as exc:
            logger.warning(
                RACK_SETTLE_FALLBACK,
                extra={
                    "subject": subject.key,
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

    async def hook(subject: PrintSubject) -> None:
        # A settings read is a database read: off the event loop, so the follow stops
        # waiting on it at its timeout (#1083), and bounded itself (#1111), so a read
        # stuck on a slow Postgres gives its thread back to the shared executor rather
        # than holding it until Postgres answers.
        try:
            settings = await asyncio.to_thread(load, SETTINGS_READ_TIMEOUT)
        except Exception as exc:
            # Type only, as every rack log (spec §7).
            logger.warning(
                RACK_SETTLE_SETTINGS_DROPPED,
                extra={"subject": subject.key, "error": type(exc).__name__},
            )
            raise
        if not settings.bambuddy_url:
            # No Bambuddy, so no archive of its to read: a LAN print links none, and its
            # follow builds no client (#3818 tier A, D3, AC2).
            return
        async with client_for(settings) as client:
            await record_settled(subject, client=client, links=links, store=store)

    return hook
