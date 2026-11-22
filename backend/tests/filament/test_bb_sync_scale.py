"""The Bambuddy sync at prod's size (#6902): a full ``two_way`` round of 200 writes on
about 110 spools and 100k ledger events, read in a fixed number of queries and recorded
in one commit; one plan or results call of an instance at a time; and a call whose caller
went away rolls back whole.

The bounds are query and commit counts, never times, so a slow runner cannot fail them.
The times on a developer machine are in the PR that added this file."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from fastapi import Request

from scadbuddy.api.disconnect import CALLER_GONE, until_disconnect
from scadbuddy.core.cancel import CancelledError, CancelToken
from scadbuddy.core.problems import ApiError
from scadbuddy.filament.bambuddy_pull import parse_rows, pull
from scadbuddy.filament.bb_sync_models import BbWrite, BbWriteResult
from scadbuddy.filament.bb_sync_plan import (
    SYNC_LOCK,
    BbRefs,
    SpoolFacts,
    SyncBusyError,
    desired_and_unmapped,
    plan,
)
from scadbuddy.filament.bb_sync_results import LocalFilamentSync, apply_results
from scadbuddy.filament.component import Filament
from tests.conftest import PgPool
from tests.filament import bb_sync_scale as scale
from tests.filament.test_bb_sync_plan import INSTANCE, _filament

pytestmark = pytest.mark.requires_postgres

_AT = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


def _round(f: Filament, made: scale.Scale) -> tuple[list[BbWrite], list[BbWriteResult]]:
    planned = plan(f, made.snap, scale.TWO_WAY, interval_s=300.0, limit=200)
    return planned.writes, scale.ok_results(made.snap, planned.writes, _AT)


def _apply(
    f: Filament,
    writes: list[BbWrite],
    results: list[BbWriteResult],
    cancel: CancelToken | None = None,
) -> Any:
    return apply_results(
        f, writes, results, instance=INSTANCE, now=_AT, pass_id="p1", cancel=cancel
    )


def _committed(pool: PgPool) -> tuple[int, Any]:
    """The echo events and the links' seen copies: what a recorded round changes."""
    with pool.connection() as conn:
        events = conn.execute(
            "SELECT count(*) AS n FROM filament_spool_events WHERE kind = 'synced_from_bb'"
        ).fetchone()
        seen = conn.execute(
            "SELECT spool_id, bb_seen, pushed_fields FROM filament_bb_links ORDER BY spool_id"
        ).fetchall()
    assert events is not None
    return events["n"], seen


@contextmanager
def _held_spool(pool: PgPool, spool: UUID) -> Iterator[None]:
    """Another transaction holds ``spool``'s row while the block runs."""
    with pool.connection() as conn, conn.transaction():
        conn.execute("SELECT 1 FROM filament_spools WHERE id = %s FOR UPDATE", (spool,))
        yield


def _wait_for_lock_wait(pool: PgPool) -> None:
    """Until a backend waits on a row lock (the round reached the held spool)."""
    for _ in range(200):
        with pool.connection() as conn:
            row = conn.execute(
                "SELECT count(*) AS n FROM pg_stat_activity"
                " WHERE wait_event_type = 'Lock' AND datname = current_database()"
            ).fetchone()
        if row is not None and row["n"] > 0:
            return
        threading.Event().wait(0.05)
    raise AssertionError("the round never waited on the held spool")


def _spool_of_late_write(writes: list[BbWrite]) -> UUID:
    """A spool whose write comes after many others in the round."""
    spool_writes = [w for w in writes if w.kind == "spool"]
    return spool_writes[len(spool_writes) // 2].sb_id


def _free(pool: PgPool) -> None:
    """The sync's lock and the state row's lock are free, and so is every connection."""
    with pool.connection() as conn, conn.transaction():
        held = conn.execute(
            "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0)) AS held",
            (SYNC_LOCK + INSTANCE,),
        ).fetchone()
        assert held is not None and held["held"]
        conn.execute("SELECT instance FROM filament_bb_sync_state FOR UPDATE NOWAIT")
    stats = pool.get_stats()
    assert stats.get("pool_size", 0) - stats.get("pool_available", 0) == 0


def test_a_full_round_at_prods_size(pg_pool: PgPool) -> None:
    """Prod's 2026-11-21 shape: 110 spools, 99k ledger events, every data type
    ``two_way``. The plan reads every spool in a fixed number of queries (it made 2448
    before #6902), and the round's 200 results are one commit (200 before)."""
    f = _filament(pg_pool)
    made = scale.build(pg_pool, f)

    with scale.counting() as planned:
        writes, results = _round(f, made)
    with scale.counting() as recorded:
        report = _apply(f, writes, results)

    assert len(writes) == 200
    assert planned.transactions == 1
    assert planned.statements < 250
    assert recorded.transactions == 1
    assert recorded.statements < 10 * len(results)
    assert report.updated == 200
    # Sent again (a lost answer): the same counts, nothing booked twice.
    events, _ = _committed(pg_pool)
    assert _apply(f, writes, results).updated == 200
    assert _committed(pg_pool)[0] == events


def test_the_read_once_values_are_each_spools_own(pg_pool: PgPool) -> None:
    """`SpoolFacts` read for every spool at once gives each spool the values its own read
    gives (the plan's output is unchanged by #6902)."""
    f = _filament(pg_pool)
    made = scale.build(pg_pool, f, spools=30, archived=5, events=40)
    with pg_pool.connection() as conn:
        refs = BbRefs.read(conn)
        facts = SpoolFacts.read(conn, made.spools, scale.TWO_WAY)
        for spool in made.spools:
            alone = desired_and_unmapped(conn, spool, refs, scale.TWO_WAY)
            assert desired_and_unmapped(conn, spool, refs, scale.TWO_WAY, facts) == alone


def test_a_second_call_while_one_runs_is_busy(pg_pool: PgPool) -> None:
    """A results call still running (it waits on a spool another writer holds) keeps the
    instance's sync: a second results call, and a plan, are `SyncBusyError` at once and
    write nothing; the first then records its round alone."""
    f = _filament(pg_pool)
    made = scale.build(pg_pool, f, spools=12, archived=2, events=20)
    writes, results = _round(f, made)
    before = _committed(pg_pool)
    first: list[Any] = []
    with _held_spool(pg_pool, _spool_of_late_write(writes)):
        runner = threading.Thread(target=lambda: first.append(_apply(f, writes, results)))
        runner.start()
        _wait_for_lock_wait(pg_pool)
        with pytest.raises(SyncBusyError):
            _apply(f, writes, results)
        with pytest.raises(SyncBusyError):
            plan(f, made.snap, scale.TWO_WAY, interval_s=300.0)
        assert _committed(pg_pool) == before
    runner.join(timeout=30)
    assert first and first[0].updated == len(writes)
    _free(pg_pool)


def _request(disconnect_after: threading.Event) -> Request:
    """A request whose client disconnects once ``disconnect_after`` is set."""

    async def receive() -> dict[str, Any]:
        while not disconnect_after.is_set():
            await asyncio.sleep(0.02)
        return {"type": "http.disconnect"}

    scope = {
        "type": "http",
        "method": "POST",
        "scheme": "http",
        "server": ("test", 80),
        "path": "/api/v1/internal/crud/print/filament/sync/results",
        "query_string": b"",
        "headers": [],
        "root_path": "",
    }
    return Request(scope, receive)


class _Settings:
    def read(self, _names: Any) -> dict[str, Any]:
        return {"bambuddy_url": None}


def test_a_caller_that_goes_away_rolls_the_round_back(pg_pool: PgPool) -> None:
    """The results route's caller disconnects while the round waits mid-way on a spool
    another writer holds: the running statement is cancelled, the whole round rolls back
    (none of the results before it stays), and the sync's lock, the state row and the
    connection are free again. The same results sent again then apply in full."""
    f = _filament(pg_pool)
    made = scale.build(pg_pool, f, spools=12, archived=2, events=20)
    writes, results = _round(f, made)
    before = _committed(pg_pool)
    sync = LocalFilamentSync(f, _Settings())  # type: ignore[arg-type]
    gone = threading.Event()

    async def call() -> Any:
        return await until_disconnect(
            _request(gone),
            lambda token: sync.results(INSTANCE, writes, results, "p1", (), token),
        )

    def hang_up() -> None:
        _wait_for_lock_wait(pg_pool)
        gone.set()

    with _held_spool(pg_pool, _spool_of_late_write(writes)):
        threading.Thread(target=hang_up).start()
        with pytest.raises(ApiError) as refused:
            asyncio.run(call())
    assert refused.value.extensions.get("reason") == CALLER_GONE
    assert _committed(pg_pool) == before
    _free(pg_pool)
    assert _apply(f, writes, results).updated == len(writes)


def test_a_cancelled_plan_writes_nothing(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    made = scale.build(pg_pool, f, spools=6, archived=1, events=20)
    token = CancelToken()
    token.cancel()
    with pytest.raises(CancelledError):
        plan(f, made.snap, scale.TWO_WAY, interval_s=300.0, cancel=token)
    _free(pg_pool)


def test_a_cancelled_pull_stops_before_the_next_spool(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    made = scale.build(pg_pool, f, spools=6, archived=1, events=20)
    spools, _ = parse_rows(made.snap.spools)
    token = CancelToken()
    token.cancel()
    with pytest.raises(CancelledError):
        pull(f, spools, now=_AT + timedelta(days=1), instance=INSTANCE, cancel=token)
