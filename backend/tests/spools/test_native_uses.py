"""A native print's filament taken off ScadBuddy's own spools (#4643 part 2): each LAN
start's grams per slot, saved as it starts against the native record the overlay put in
the slot's tray, and taken off once, when the run's print settles with that start done."""

from __future__ import annotations

import uuid
from typing import Any
from uuid import UUID

import pytest
from psycopg.types.json import Jsonb

from scadbuddy.bambuddy.filaments import SpoolOption
from scadbuddy.bambuddy.progress import CopyProgress, PrintProgress
from scadbuddy.bambuddy.stages import Stage
from scadbuddy.spools.component import Spools
from scadbuddy.spools.models import SpoolSnapshot
from scadbuddy.spools.native import (
    NativeSpoolCreate,
    NativeSpoolPatch,
    NativeSpoolStore,
    SpoolUse,
    spool_uses,
)
from scadbuddy.spools.spool_providers import NativeProvider, native_use_hook
from scadbuddy.spools.store import SpoolStore
from tests.conftest import PgPool

RUN = "run-4643"


def _option(spool_id: int, native: UUID | None) -> SpoolOption:
    records = {} if native is None else {"native": SpoolSnapshot(record_id=str(native))}
    records["bambuddy"] = SpoolSnapshot(record_id="16")
    return SpoolOption(spool_id=spool_id, material="PLA", tray_only=True, records=records)


def test_a_slot_uses_the_native_record_in_its_tray() -> None:
    red, blue = uuid.uuid4(), uuid.uuid4()
    uses = spool_uses(
        {1: _option(-1, red), 2: _option(-2, None), 3: _option(-3, blue), 4: _option(-4, red)},
        {1: 12.5, 2: 3.0, 3: 0.0, 4: 1.5},
    )
    # Slot 2's tray has no native record, slot 3 printed nothing; slot 4 shares slot 1's
    # spool, and each slot is its own use.
    assert uses == [
        SpoolUse(slot_id=1, spool_id=red, grams=12.5),
        SpoolUse(slot_id=4, spool_id=red, grams=1.5),
    ]
    assert spool_uses({1: _option(-1, red)}, {}) == []
    not_native = _option(-1, None)
    not_native.records["native"] = SpoolSnapshot(record_id="not-a-uuid")
    assert spool_uses({1: not_native}, {1: 1.0}) == []


pg = pytest.mark.requires_postgres


def _run(pool: PgPool, starts: list[dict[str, Any]]) -> None:
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO print_runs (id, output_id, idempotency_key, status, result)"
            " VALUES (%s, 'out', 'k', 'succeeded', %s)",
            (RUN, Jsonb({"lan": starts})),
        )


def _start(name: str, ended: Stage | None = None) -> dict[str, Any]:
    return {"subtask_name": name, "started_at": "2026-11-01T12:00:00Z", "ended": ended}


def _settled(*stages: Stage) -> PrintProgress:
    return PrintProgress(
        route="lan",
        stage="done",
        settled=True,
        run_id=RUN,
        copies_detail=[CopyProgress(stage=stage) for stage in stages],
        bambuddy_url="",
    )


@pytest.fixture
def native(pg_pool: PgPool) -> NativeSpoolStore:
    return NativeSpoolStore(pg_pool)


@pg
def test_a_done_start_is_taken_off_once_and_a_failed_one_never(
    native: NativeSpoolStore, pg_pool: PgPool
) -> None:
    red = native.create(NativeSpoolCreate(material="PLA", remaining_g=100))
    blue = native.create(NativeSpoolCreate(material="PLA", remaining_g=5))
    untracked = native.create(NativeSpoolCreate(material="PLA"))
    _run(pg_pool, [_start("a", ended="done"), _start("b"), _start("c")])
    native.record_uses(RUN, "a", [SpoolUse(slot_id=1, spool_id=red.id, grams=12.5)])
    # An attempt that starts again records nothing new.
    native.record_uses(RUN, "a", [SpoolUse(slot_id=1, spool_id=red.id, grams=99)])
    native.record_uses(
        RUN,
        "b",
        [
            SpoolUse(slot_id=1, spool_id=red.id, grams=10),
            SpoolUse(slot_id=2, spool_id=blue.id, grams=8),
            SpoolUse(slot_id=3, spool_id=untracked.id, grams=8),
        ],
    )
    native.record_uses(RUN, "c", [SpoolUse(slot_id=1, spool_id=red.id, grams=40)])

    assert native.settle_uses(RUN, ["done", "done", "failed"]) == {red.id, blue.id}
    remaining = {s.id: s.remaining_g for s in native.list()}
    # 100 - 12.5 - 10; never below nothing; a spool whose weight is not kept stays so.
    assert remaining == {red.id: 77.5, blue.id: 0.0, untracked.id: None}
    assert native.settle_uses(RUN, ["done", "done", "done"]) == set()
    assert native.get(red.id).remaining_g == 77.5  # type: ignore[union-attr]


@pg
def test_a_spool_forgotten_before_its_print_settles_takes_nothing(
    native: NativeSpoolStore, pg_pool: PgPool
) -> None:
    red = native.create(NativeSpoolCreate(remaining_g=100))
    _run(pg_pool, [_start("a")])
    native.record_uses(RUN, "a", [SpoolUse(slot_id=1, spool_id=uuid.uuid4(), grams=1)])
    native.record_uses(RUN, "a", [SpoolUse(slot_id=2, spool_id=red.id, grams=1)])
    assert native.delete(red.id)
    assert native.settle_uses(RUN, ["done"]) == set()


@pg
async def test_the_follow_takes_a_settled_lan_print_off_and_recaches(
    native: NativeSpoolStore, pg_pool: PgPool
) -> None:
    red = native.create(NativeSpoolCreate(material="PLA", remaining_g=100))
    gone = native.create(NativeSpoolCreate(material="ABS", remaining_g=1))
    native.update(gone.id, NativeSpoolPatch(archived=True))
    spools = Spools(store=SpoolStore(pg_pool), providers=[NativeProvider(native)])
    _run(pg_pool, [_start("a")])
    native.record_uses(RUN, "a", [SpoolUse(slot_id=1, spool_id=red.id, grams=30)])
    hook = native_use_hook(spools)

    running = _settled("running").model_copy(update={"settled": False})
    await hook("output:x", running)
    await hook("output:x", _settled("done").model_copy(update={"route": "slice_queue"}))
    assert native.get(red.id).remaining_g == 100  # type: ignore[union-attr]

    await hook("output:x", _settled("done"))
    await hook("output:x", _settled("done"))
    assert native.get(red.id).remaining_g == 70  # type: ignore[union-attr]
    with pg_pool.connection() as conn:
        cached = conn.execute(
            "SELECT snapshot FROM spool_provider_records WHERE provider = 'native'"
        ).fetchall()
    assert [row["snapshot"]["remaining_g"] for row in cached] == [70]
