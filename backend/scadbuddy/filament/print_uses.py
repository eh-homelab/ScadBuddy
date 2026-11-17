"""What a native LAN print uses of SB spools (#6262 Task 6, from `spools/native.py`,
#4643 part 2).

Each LAN start books, per slot, the grams its sliced plate gives the slot against the SB
spool the slot's tray holds (`slot_uses`, the tray's ``sb_spool_id``): a
``print_started`` event of source ``print_estimate`` (`record_uses`), at the plate's
start, which the ledger takes off at once (`filament/ledger.py`). Once the run's print
settles, `settle_uses` books each start's ``print_ended``: its sliced grams as the
actual when it ended ``done``, else 0, which drops the estimate. Both are found by
``ref_kind`` ``print_run`` and ``ref_id`` ``run/subtask/slot``, so a slot is booked
once and settled once, whichever reader comes first.

The API books them in Postgres (`PoolPrintUses`); the print worker, which holds no
database (#5295), through its CRUD routes (`crud/print.py` `RemotePrintUses`). The
follow's read hook (`print_use_hook`) settles a run as it reads it settled.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool
from pydantic import BaseModel

from scadbuddy.filament import ledger

if TYPE_CHECKING:
    from scadbuddy.bambuddy.filaments import SpoolOption
    from scadbuddy.bambuddy.follow import ReadHook
    from scadbuddy.bambuddy.progress import PrintProgress

logger = logging.getLogger(__name__)

Conn = Connection[DictRow]

#: Who the print events name.
ACTOR = "print"


class SlotUse(BaseModel):
    """What one slot of a LAN start uses of an SB spool, and when its plate started."""

    slot_id: int
    spool_id: UUID
    grams: float
    plate_started_at: datetime


def slot_uses(
    spools: Mapping[int, SpoolOption], grams: Mapping[int, float], started_at: datetime
) -> list[SlotUse]:
    """Each slot's use of the SB spool in its tray, by slot: the slot's chosen tray's
    ``sb_spool_id`` (`trays.loaded_trays`: found by its tag or a person's tray link),
    and the grams the sliced plate gives the slot (`slicing.slice_info.plate_grams`). A
    slot whose tray holds no SB spool, or that prints nothing, uses none."""
    uses: list[SlotUse] = []
    for slot_id in sorted(spools):
        spool_id = spools[slot_id].sb_spool_id
        used = grams.get(slot_id, 0.0)
        if spool_id is None or used <= 0:
            continue
        uses.append(
            SlotUse(slot_id=slot_id, spool_id=spool_id, grams=used, plate_started_at=started_at)
        )
    return uses


def _ref(run_id: str, subtask: str, slot_id: int) -> str:
    """A use's ``ref_id``, as the 1302Z migration writes it."""
    return f"{run_id}/{subtask}/{slot_id}"


def _like_prefix(text: str) -> str:
    """A ``LIKE`` pattern for the strings that start with ``text``."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"{escaped}%"


def record_uses(
    pool: ConnectionPool[Conn], run_id: str, subtask: str, uses: Sequence[SlotUse]
) -> int:
    """`record_uses_in` in a transaction of its own."""
    if not uses:
        return 0
    with pool.connection() as conn, conn.transaction():
        return record_uses_in(conn, run_id, subtask, uses)


def record_uses_in(conn: Conn, run_id: str, subtask: str, uses: Sequence[SlotUse]) -> int:
    """What a LAN start uses, as the printer takes it: a ``print_started`` event with
    its ``print_estimate`` per slot, at the plate's start. A slot booked already (an
    attempt that starts again) keeps its event; a spool deleted since is left out. The
    number of events booked."""
    booked = 0
    for use in sorted(uses, key=lambda u: (u.spool_id, u.slot_id)):
        ref = _ref(run_id, subtask, use.slot_id)
        # One event per slot, whichever spool: what the old table's key held.
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (ref,))
        taken = conn.execute(
            "SELECT 1 FROM filament_spool_events WHERE kind = %s AND ref_kind = %s AND ref_id = %s",
            (ledger.PRINT_STARTED, ledger.PRINT_RUN, ref),
        ).fetchone()
        spool = conn.execute(
            "SELECT 1 FROM filament_spools WHERE id = %s", (use.spool_id,)
        ).fetchone()
        if taken is not None or spool is None:
            continue
        event = ledger.record(
            conn,
            use.spool_id,
            ledger.PRINT_STARTED,
            source=ledger.PRINT_ESTIMATE,
            delta_g=-use.grams,
            ref_kind=ledger.PRINT_RUN,
            ref_id=ref,
            actor=ACTOR,
            # When the printer took the plate: a reading after it holds the plate.
            at=use.plate_started_at,
        )
        booked += event is not None
    return booked


def settle_uses(pool: ConnectionPool[Conn], run_id: str, stages: Sequence[str]) -> bool:
    """`settle_uses_in` in a transaction of its own."""
    with pool.connection() as conn, conn.transaction():
        return settle_uses_in(conn, run_id, stages)


def settle_uses_in(conn: Conn, run_id: str, stages: Sequence[str]) -> bool:
    """Settle ``run_id``'s uses once its print has settled: ``stages`` is how each of
    its LAN starts ended, in the order its result lists them (``print_runs.result.lan``,
    as the follow's progress reads them). A start that ended ``done`` books its sliced
    grams as the actual (``print_ended``, ``print_actual``, ``data.basis`` ``sliced``);
    every other start books 0 (``basis`` ``not_applied``), which drops its estimate. A
    ``print_ended`` is booked at its ``print_started``'s time, so a reading after the
    plate started (a BB pull between two plates, or before the settle) is not taken off
    again. Each use is settled once: the ledger keeps one ``print_ended`` per
    ``ref_id``. Whether this settle booked any."""
    prefix = f"{run_id}/"
    # By `filament_spool_events_print_ref`: a prefix match, the run id escaped.
    pending = conn.execute(
        "SELECT e.spool_id, e.ref_id, e.delta_g, e.at FROM filament_spool_events e"
        " WHERE e.kind = %(started)s AND e.ref_kind = %(kind)s"
        " AND e.ref_id LIKE %(prefix)s AND NOT EXISTS (SELECT 1 FROM filament_spool_events d"
        " WHERE d.spool_id = e.spool_id AND d.kind = %(ended)s"
        " AND d.ref_kind = %(kind)s AND d.ref_id = e.ref_id)"
        " ORDER BY e.spool_id, e.ref_id",
        {
            "started": ledger.PRINT_STARTED,
            "ended": ledger.PRINT_ENDED,
            "kind": ledger.PRINT_RUN,
            "prefix": _like_prefix(prefix),
        },
    ).fetchall()
    if not pending:
        return False
    row = conn.execute(
        "SELECT result->'lan' AS lan FROM print_runs WHERE id = %s", (run_id,)
    ).fetchone()
    starts = row["lan"] if row is not None and isinstance(row["lan"], list) else []
    done = {
        start.get("subtask_name")
        for start, stage in zip(starts, stages, strict=False)
        if isinstance(start, dict) and stage == "done"
    }
    settled = False
    for use in pending:
        subtask = use["ref_id"][len(prefix) :].rsplit("/", 1)[0]
        applied = subtask in done
        grams = -(use["delta_g"] or 0.0)
        ended = ledger.record(
            conn,
            use["spool_id"],
            ledger.PRINT_ENDED,
            source=ledger.PRINT_ACTUAL,
            delta_g=-grams if applied else 0.0,
            ref_kind=ledger.PRINT_RUN,
            ref_id=use["ref_id"],
            actor=ACTOR,
            # At the plate's start: no end time is kept per plate (`LanStarted` has
            # none), and a reading taken after the start is read as holding the
            # plate, as BB's is once BB deducts it (`ledger.py`).
            at=use["at"],
            data={"basis": "sliced" if applied else "not_applied"},
        )
        settled |= ended is not None
    return settled


class PrintUses(Protocol):
    """Where a LAN start books what it uses, and where its run is settled: the API's
    Postgres (:class:`PoolPrintUses`), or the print worker's CRUD routes
    (`crud/print.py` `RemotePrintUses`)."""

    async def record_uses(self, run_id: str, subtask: str, uses: Sequence[SlotUse]) -> int: ...

    async def settle_uses(self, run_id: str, stages: Sequence[str]) -> bool: ...


class PoolPrintUses:
    """:class:`PrintUses` over a pool whose connections return dict rows (the settings
    store's): each write in a thread."""

    def __init__(self, pool: ConnectionPool[Conn]) -> None:
        self._pool = pool

    async def record_uses(self, run_id: str, subtask: str, uses: Sequence[SlotUse]) -> int:
        return await asyncio.to_thread(record_uses, self._pool, run_id, subtask, uses)

    async def settle_uses(self, run_id: str, stages: Sequence[str]) -> bool:
        return await asyncio.to_thread(settle_uses, self._pool, run_id, stages)


def print_use_hook(uses: PrintUses) -> ReadHook:
    """The print follow's read hook for what a native print used: a start's estimate
    came off its SB spools as it started (`record_uses`); once a LAN run's print has
    settled, the settle books each start's ``print_ended`` (`settle_uses`), once,
    whichever reader settles it first. Advisory: a failure is logged by its type, never
    raised into the follow."""

    async def hook(run_subject: str, progress: PrintProgress) -> None:
        if progress.route != "lan" or not progress.settled or progress.run_id is None:
            return
        stages = [copy.stage for copy in progress.copies_detail] or [progress.stage]
        try:
            await uses.settle_uses(progress.run_id, stages)
        except Exception as error:
            logger.warning(
                "could not settle what a native print used of its SB spools",
                extra={"run_id": progress.run_id, "error": type(error).__name__},
            )

    return hook
