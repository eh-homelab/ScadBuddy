"""``RackUsageStore`` on Postgres (#836, spec 2026-10-01 §4). Invented serials only."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import psycopg
import pytest

from scadbuddy.rack.rank import Usage, rank_rack
from scadbuddy.rack.usage import STATEMENT_TIMEOUT_MS, PickedHotend, RackUsageStore
from tests.rack.helpers import group, serial, slot

pytestmark = pytest.mark.requires_postgres

AT = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
A, B = serial(17), serial(18)


@pytest.fixture
async def store(pg_conninfo: str) -> AsyncIterator[RackUsageStore]:
    opened = RackUsageStore(pg_conninfo)
    try:
        yield opened
    finally:
        opened.close()


def _seen_rows(conninfo: str) -> dict[str, tuple[int, datetime]]:
    with psycopg.connect(conninfo) as conn:
        rows = conn.execute(
            "SELECT serial, printer_id, first_seen_at FROM rack_nozzle_seen"
        ).fetchall()
    return {row[0]: (row[1], row[2]) for row in rows}


async def test_a_hotend_moved_to_another_printer_keeps_its_age_and_history(
    store: RackUsageStore, pg_conninfo: str
) -> None:
    await store.seen(1, [A])
    first = _seen_rows(pg_conninfo)[A]
    await store.record_picks(51, 1, [PickedHotend(group_id=0, position=2, serial=A)])
    await store.record_prints(
        archive_id=101, queue_item_id=51, settled_at=AT, print_seconds=600, grams=12.5
    )

    await store.seen(2, [A])

    printer_id, first_seen = _seen_rows(pg_conninfo)[A]
    assert (printer_id, first_seen) == (2, first[1])
    assert (await store.usage([A]))[A] == Usage(
        prints=1, print_seconds=600, grams=12.5, first_seen_at=first[1]
    )


def _row_version(conninfo: str, serial: str) -> str:
    with psycopg.connect(conninfo) as conn:
        row = conn.execute(
            "SELECT xmin::text FROM rack_nozzle_seen WHERE serial = %s", (serial,)
        ).fetchone()
    assert row is not None
    return str(row[0])


async def test_seeing_a_hotend_again_on_the_same_printer_writes_nothing(
    store: RackUsageStore, pg_conninfo: str
) -> None:
    """#1082: /check records the rack on every debounced re-check, so an unchanged row
    must not be rewritten; a move to another printer still is."""
    await store.seen(1, [A])
    before = _row_version(pg_conninfo, A)
    await store.seen(1, [A])
    assert _row_version(pg_conninfo, A) == before
    await store.seen(2, [A])
    assert _row_version(pg_conninfo, A) != before


async def test_seen_skips_empty_and_repeated_serials(
    store: RackUsageStore, pg_conninfo: str
) -> None:
    """Review Focus 4."""
    await store.seen(1, ["", A, A, ""])
    assert list(_seen_rows(pg_conninfo)) == [A]


async def test_a_serial_with_no_rows_is_zeros_and_ranks_before_a_used_one(
    store: RackUsageStore,
) -> None:
    """Spec §4: ``sum`` over no rows is NULL, which an ascending sort puts last."""
    await store.record_picks(51, 1, [PickedHotend(group_id=0, position=2, serial=A)])
    await store.record_prints(
        archive_id=101, queue_item_id=51, settled_at=AT, print_seconds=60, grams=None
    )

    usage = await store.usage([A, B])

    assert usage[B] == Usage()
    assert usage[A].prints == 1 and usage[A].grams == 0.0
    picks = rank_rack([group()], [slot(2), slot(3)], "least_used", usage, {})
    assert picks[0].position == 3


async def test_a_pick_written_twice_writes_once_and_says_so(store: RackUsageStore) -> None:
    """#1015: never expected, but a second write is visible in the count, not an error."""
    picks = [
        PickedHotend(group_id=0, position=2, serial=A),
        PickedHotend(group_id=1, position=3, serial=B),
    ]
    assert await store.record_picks(51, 1, picks) == 2
    assert await store.record_picks(51, 1, picks) == 0
    assert await store.picked_items([51, 52]) == {51}


async def test_a_print_copies_the_picks_serial_and_a_second_settle_changes_nothing(
    store: RackUsageStore,
) -> None:
    await store.record_picks(51, 1, [PickedHotend(group_id=0, position=2, serial=A)])
    assert (
        await store.record_prints(
            archive_id=101, queue_item_id=51, settled_at=AT, print_seconds=60, grams=3.0
        )
        == 1
    )
    assert (
        await store.record_prints(
            archive_id=101, queue_item_id=51, settled_at=AT, print_seconds=99, grams=9.0
        )
        == 0
    )
    assert (await store.usage([A]))[A].print_seconds == 60


async def test_an_item_with_no_picks_records_no_print(store: RackUsageStore) -> None:
    assert (
        await store.record_prints(
            archive_id=101, queue_item_id=77, settled_at=AT, print_seconds=60, grams=1.0
        )
        == 0
    )


async def test_every_query_on_the_store_is_bounded(store: RackUsageStore) -> None:
    """#1086 review: a stuck read must release its thread even after the watcher's
    timeout has stopped waiting on it."""
    await store.seen(1, [A])  # opens the pool
    with store._ready().connection() as conn:
        row = conn.execute("SHOW statement_timeout").fetchone()
    assert row is not None and row["statement_timeout"] == f"{STATEMENT_TIMEOUT_MS // 1000}s"
