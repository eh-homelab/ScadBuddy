"""A native (LAN) rack print's usage (#4543): its picks are keyed by the run and the
start's ``subtask_name``, since a LAN start has no Bambuddy queue item, and settled from
the printer's report by the native follow, since it has no Bambuddy archive either.
Invented serials only."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from prometheus_client import CollectorRegistry, Counter

from scadbuddy.bambuddy.progress import PrintProgress
from scadbuddy.printers.models import PrinterCreate
from scadbuddy.printers.store import PrinterStore
from scadbuddy.rack.usage import (
    PickedHotend,
    PickedSpool,
    RackUsageStore,
    lan_settle_hook,
    save_lan_picks,
    settle_lan_end,
    settle_lan_print,
)
from tests.conftest import PgPool
from tests.printers.conftest import KEK
from tests.rack.helpers import serial

pytestmark = pytest.mark.requires_postgres

PRINTER_SERIAL = "01P00A000005138"
A, B = serial(17), serial(18)
RUN = "run-4543"
FIRST, SECOND = "scadbuddy-run-4543-p1", "scadbuddy-run-4543-p1-c2"
GREEN = PickedSpool(slot_id=1, spool_id=7, label="Green", material="PLA", grams=10.0)
BLACK = PickedSpool(slot_id=2, spool_id=8, label="Black", material="PLA", grams=2.5)


@pytest.fixture
async def store(pg_conninfo: str) -> AsyncIterator[RackUsageStore]:
    opened = RackUsageStore(pg_conninfo)
    try:
        yield opened
    finally:
        opened.close()


def _picked(serial: str, *spools: PickedSpool, group_id: int = 0) -> PickedHotend:
    return PickedHotend(group_id=group_id, position=2, serial=serial, spools=list(spools))


def _picked_ago(conninfo: str, seconds: int) -> None:
    """Every LAN pick made ``seconds`` ago, as a print that ran that long."""
    with psycopg.connect(conninfo) as conn:
        conn.execute(
            "UPDATE rack_nozzle_picks SET picked_at = now() - make_interval(secs => %s)"
            " WHERE lan_run_id IS NOT NULL",
            (seconds,),
        )


async def test_a_lan_pick_is_pending_until_its_start_settles(
    store: RackUsageStore, pg_conninfo: str
) -> None:
    """Least used sees the open pick of a LAN start, and counts its wear once the start
    settles: its time from the pick, its grams the group's sliced grams."""
    written = await store.record_lan_picks(
        RUN, FIRST, None, [_picked(A, GREEN, BLACK), _picked(B, GREEN, group_id=1)]
    )
    assert written == 2
    usage = await store.usage([A, B])
    assert (usage[A].pending, usage[A].prints) == (1, 0)
    _picked_ago(pg_conninfo, 600)

    assert await store.settle_lan(RUN, FIRST) == 2

    usage = await store.usage([A, B])
    assert (usage[A].pending, usage[A].prints) == (0, 1)
    assert 595 <= usage[A].print_seconds <= 660
    assert usage[A].grams == 12.5 and usage[B].grams == 10.0
    assert usage[A].last_used_at is not None
    history = await store.spool_history([A])
    assert sorted((use.spool_id, use.prints, use.grams) for use in history[A]) == [
        (7, 1, 10.0),
        (8, 1, 2.5),
    ]


async def test_settling_a_start_again_changes_nothing(store: RackUsageStore) -> None:
    await store.record_lan_picks(RUN, FIRST, None, [_picked(A)])
    assert await store.settle_lan(RUN, FIRST) == 1
    assert await store.settle_lan(RUN, FIRST) == 0
    assert await store.settle_lan(RUN) == 0
    assert (await store.usage([A]))[A].prints == 1


async def test_each_copy_is_a_start_of_its_own(store: RackUsageStore) -> None:
    """Each copy of a LAN run is its own start and its own pick: the first settles while
    the second is still open, and the run's settle closes what is left."""
    await store.record_lan_picks(RUN, FIRST, None, [_picked(A)])
    await store.record_lan_picks(RUN, SECOND, None, [_picked(A)])
    assert (await store.usage([A]))[A].pending == 2

    assert await store.settle_lan(RUN, FIRST) == 1
    assert ((await store.usage([A]))[A].prints, (await store.usage([A]))[A].pending) == (1, 1)

    assert await store.settle_lan(RUN) == 1
    assert ((await store.usage([A]))[A].prints, (await store.usage([A]))[A].pending) == (2, 0)


async def test_a_copy_lan_wait_settled_is_not_settled_again_by_the_follow(
    store: RackUsageStore, pg_conninfo: str
) -> None:
    """``lan_wait`` settles copy 1 as the printer ends it; the follow's run-wide settle
    then writes only copy 2's row, never a second one for copy 1."""
    await store.record_lan_picks(RUN, FIRST, None, [_picked(A, GREEN)])
    await store.record_lan_picks(RUN, SECOND, None, [_picked(A, GREEN)])
    await settle_lan_print(store, RUN, FIRST)
    await lan_settle_hook(store)("o" * 32, _lan("done", settled=True))

    with psycopg.connect(pg_conninfo) as conn:
        rows = conn.execute(
            "SELECT lan_subtask, count(*) FROM rack_nozzle_prints WHERE lan_run_id = %s"
            " GROUP BY lan_subtask ORDER BY lan_subtask",
            (RUN,),
        ).fetchall()
    assert rows == [(FIRST, 1), (SECOND, 1)]
    usage = (await store.usage([A]))[A]
    assert (usage.prints, usage.pending) == (2, 0)
    assert [(use.prints, use.grams) for use in (await store.spool_history([A]))[A]] == [(2, 20.0)]


async def test_an_untimed_settle_counts_the_print_without_a_time(
    store: RackUsageStore, pg_conninfo: str
) -> None:
    """A start whose end the report never said (the printer went quiet, or moved on)
    still wore the hotend, but its time is not known."""
    await store.record_lan_picks(RUN, FIRST, None, [_picked(A)])
    _picked_ago(pg_conninfo, 86_400)
    await store.settle_lan(RUN, timed=False)
    usage = (await store.usage([A]))[A]
    assert (usage.prints, usage.print_seconds) == (1, 0)


async def test_a_start_picked_again_keeps_the_newer_pick(store: RackUsageStore) -> None:
    """A start that may not have been taken is started again, and picked again: the
    pick that was sent last is the one that prints."""
    await store.record_lan_picks(RUN, FIRST, None, [_picked(A, GREEN)])
    await store.record_lan_picks(RUN, FIRST, None, [_picked(B, BLACK)])
    usage = await store.usage([A, B])
    assert (usage[A].pending, usage[B].pending) == (0, 1)
    await store.settle_lan(RUN, FIRST)
    assert [use.spool_id for use in (await store.spool_history([B]))[B]] == [8]
    assert (await store.spool_history([A])) == {}


async def test_a_lan_pick_never_collides_with_a_queue_items(store: RackUsageStore) -> None:
    await store.record_picks(51, None, [_picked(A)])
    await store.record_lan_picks(RUN, FIRST, None, [_picked(A)])
    assert (await store.usage([A]))[A].pending == 2
    await store.record_prints(
        archive_id=101,
        queue_item_id=51,
        settled_at=datetime.now(UTC),
        print_seconds=60,
        grams=1.0,
    )
    assert (await store.usage([A]))[A].pending == 1


@pytest.fixture
def printer(pg_pool: PgPool) -> str:
    """A registry printer the picks name (`rack_nozzle_picks.printer`)."""
    return PrinterStore(pg_pool, KEK).create(PrinterCreate(serial=PRINTER_SERIAL, name="H2C")).id


def _start_picked_ago(conninfo: str, subtask: str, seconds: int) -> None:
    with psycopg.connect(conninfo) as conn:
        conn.execute(
            "UPDATE rack_nozzle_picks SET picked_at = now() - make_interval(secs => %s)"
            " WHERE lan_subtask = %s",
            (seconds, subtask),
        )


async def test_the_watchs_settle_times_a_start_to_its_reports_end(
    store: RackUsageStore, pg_conninfo: str, printer: str
) -> None:
    """#5142: the printer watch settles a start as its report ends it, timed to that
    report, however late anything else settles it; the follow's settle after it then
    writes nothing."""
    await store.record_lan_picks(RUN, FIRST, printer, [_picked(A, GREEN)])
    _picked_ago(pg_conninfo, 3600)

    ended_at = datetime.now(UTC) - timedelta(seconds=3000)
    assert await store.settle_lan_ended(printer, FIRST, ended_at) == 1
    assert await store.settle_lan(RUN) == 0

    usage = (await store.usage([A]))[A]
    assert (usage.prints, usage.pending) == (1, 0)
    assert 595 <= usage.print_seconds <= 660


async def test_the_watchs_settle_names_only_that_printers_start(
    store: RackUsageStore, printer: str
) -> None:
    await store.record_lan_picks(RUN, FIRST, printer, [_picked(A)])
    assert await store.settle_lan_ended(printer, SECOND, datetime.now(UTC)) == 0
    other = "0" * 8 + "-0000-4000-8000-" + "0" * 12
    assert await store.settle_lan_ended(other, FIRST, datetime.now(UTC)) == 0
    assert await store.settle_lan_ended(printer, FIRST, datetime.now(UTC), timed=False) == 1
    usage = (await store.usage([A]))[A]
    assert (usage.prints, usage.print_seconds) == (1, 0)


async def test_a_run_settle_stops_an_earlier_copy_at_the_printers_next_start(
    store: RackUsageStore, pg_conninfo: str, printer: str
) -> None:
    """#5142: a copy the watch and ``lan_wait`` both missed is settled by the run's
    last settle, but the printer had ended it to take the next copy: its time stops at
    that copy's pick, not at the run's end."""
    await store.record_lan_picks(RUN, FIRST, printer, [_picked(A)])
    await store.record_lan_picks(RUN, SECOND, printer, [_picked(B)])
    _start_picked_ago(pg_conninfo, FIRST, 3600)
    _start_picked_ago(pg_conninfo, SECOND, 600)

    assert await store.settle_lan(RUN) == 2

    usage = await store.usage([A, B])
    assert 2995 <= usage[A].print_seconds <= 3060
    assert 595 <= usage[B].print_seconds <= 660


class _Recording:
    def __init__(self, *, fail: bool = False) -> None:
        self.picks: list[tuple[str, str, str | None, list[PickedHotend]]] = []
        self.settled: list[tuple[str, str | None, bool]] = []
        self.fail = fail

    async def record_lan_picks(
        self,
        run_id: str,
        subtask_name: str,
        printer: str | None,
        picks: list[PickedHotend],
    ) -> int:
        if self.fail:
            raise psycopg.OperationalError("the database is gone")
        self.picks.append((run_id, subtask_name, printer, list(picks)))
        return len(picks)

    async def settle_lan(
        self, run_id: str, subtask_name: str | None = None, *, timed: bool = True
    ) -> int:
        if self.fail:
            raise psycopg.OperationalError("the database is gone")
        self.settled.append((run_id, subtask_name, timed))
        return 1

    async def settle_lan_ended(
        self, printer: str, subtask_name: str, ended_at: datetime, *, timed: bool = True
    ) -> int:
        if self.fail:
            raise psycopg.OperationalError("the database is gone")
        return 1


async def test_saving_skips_a_pick_with_no_serial_and_never_raises() -> None:
    recording = _Recording()
    await save_lan_picks(
        recording,  # type: ignore[arg-type]
        RUN,
        FIRST,
        None,
        [_picked(A), _picked("", group_id=1)],
    )
    [(run, start, printer, picks)] = recording.picks
    assert (run, start, printer, [p.serial for p in picks]) == (RUN, FIRST, None, [A])

    await save_lan_picks(_Recording(fail=True), RUN, FIRST, None, [_picked(A)])  # type: ignore[arg-type]
    await settle_lan_print(_Recording(fail=True), RUN, FIRST)  # type: ignore[arg-type]
    failed = Counter("failed", "test", registry=CollectorRegistry())
    await settle_lan_end(_Recording(fail=True), "p", FIRST, datetime.now(UTC), failed=failed)  # type: ignore[arg-type]
    assert failed._value.get() == 1


def _lan(stage: str, *, settled: bool, run_id: str | None = RUN) -> PrintProgress:
    return PrintProgress(
        route="lan",
        stage=stage,  # type: ignore[arg-type]
        settled=settled,
        bambuddy_url="",
        run_id=run_id,
    )


@pytest.mark.parametrize(
    ("progress", "settled"),
    [
        (_lan("done", settled=True), [(RUN, None, True)]),
        (_lan("failed", settled=True), [(RUN, None, True)]),
        (_lan("unknown", settled=True), [(RUN, None, False)]),
        (_lan("running", settled=False), []),
        (_lan("done", settled=True, run_id=None), []),
        (
            PrintProgress(route="slice_queue", stage="done", settled=True, bambuddy_url="x"),
            [],
        ),
    ],
    ids=["done", "failed", "unknown", "running", "no run", "bambuddy"],
)
async def test_the_follow_settles_a_lan_runs_picks_once_its_print_settles(
    progress: PrintProgress, settled: list[tuple[str, str | None, bool]]
) -> None:
    recording = _Recording()
    await lan_settle_hook(recording)("o" * 32, progress)  # type: ignore[arg-type]
    assert recording.settled == settled
