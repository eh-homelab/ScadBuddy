"""What a native LAN print uses of SB spools (#6262 Task 6, from #4643 part 2): each LAN
start's grams per slot, booked as it starts against the SB spool the slot's tray holds
(``sb_spool_id``) as a ``print_started`` estimate, and settled once, when the run's
print settles: its sliced grams for a start that ended ``done``, else 0
(`filament/print_uses.py`)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from psycopg.types.json import Jsonb

from scadbuddy.bambuddy.filaments import SpoolOption
from scadbuddy.bambuddy.print_run import PreparedPlates
from scadbuddy.bambuddy.progress import CopyProgress, PrintProgress
from scadbuddy.bambuddy.stages import Stage
from scadbuddy.core.components import Components, Core
from scadbuddy.filament import component as filament_component
from scadbuddy.filament import ledger
from scadbuddy.filament.catalog import CatalogStore
from scadbuddy.filament.models import Material, SpoolCreate
from scadbuddy.filament.print_uses import (
    PoolPrintUses,
    SlotUse,
    print_use_hook,
    record_uses,
    settle_uses,
    slot_uses,
)
from scadbuddy.filament.spools import FilamentSpoolStore
from scadbuddy.workflows.print_activities import PrintActivities, PrintDeps
from tests.conftest import PgPool

RUN = "run-6262"
STARTED = datetime(2026, 11, 15, 9, 0, tzinfo=UTC)


def _option(spool_id: int, held: UUID | None) -> SpoolOption:
    """A tray holding SB spool ``held`` (#6262 Task 5), or none."""
    return SpoolOption(spool_id=spool_id, material="PLA", tray_only=True, sb_spool_id=held)


def test_a_slot_uses_the_sb_spool_in_its_tray() -> None:
    red, blue = uuid.uuid4(), uuid.uuid4()
    uses = slot_uses(
        {1: _option(-1, red), 2: _option(-2, None), 3: _option(-3, blue), 4: _option(-4, red)},
        {1: 12.5, 2: 3.0, 3: 0.0, 4: 1.5},
        STARTED,
    )
    # Slot 2's tray holds no SB spool, slot 3 printed nothing; slot 4 shares slot 1's
    # spool, and each slot is its own use.
    assert uses == [
        SlotUse(slot_id=1, spool_id=red, grams=12.5, plate_started_at=STARTED),
        SlotUse(slot_id=4, spool_id=red, grams=1.5, plate_started_at=STARTED),
    ]
    assert slot_uses({1: _option(-1, red)}, {}, STARTED) == []


pg = pytest.mark.requires_postgres


def _spool(pool: PgPool, grams: float | None = 100.0) -> UUID:
    material = next(
        r.id
        for r in CatalogStore(pool).list("materials")
        if isinstance(r, Material) and r.name == "PLA"
    )
    made = FilamentSpoolStore(pool).create(
        SpoolCreate(material_id=material, grams_left=grams), actor="test"
    )
    return made.id


def _grams(pool: PgPool, spool_id: UUID) -> float | None:
    spool = FilamentSpoolStore(pool).get(spool_id)
    assert spool is not None
    return spool.grams_left


def _print_events(pool: PgPool, spool_id: UUID) -> list[tuple[str, str | None, float | None]]:
    return [
        (e.kind, e.source, e.delta_g)
        for e in FilamentSpoolStore(pool).events(spool_id)
        if e.ref_kind == ledger.PRINT_RUN
    ]


def _run(pool: PgPool, starts: list[dict[str, Any]]) -> None:
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO print_runs (id, output_id, idempotency_key, status, result)"
            " VALUES (%s, 'out', 'k', 'succeeded', %s)",
            (RUN, Jsonb({"lan": starts})),
        )


def _start(name: str, ended: Stage | None = None) -> dict[str, Any]:
    return {"subtask_name": name, "started_at": "2026-11-15T09:00:00Z", "ended": ended}


def _use(spool_id: UUID, grams: float, slot_id: int = 1, at: datetime | None = None) -> SlotUse:
    """A use whose plate started ``at``, by default now: after the spool's ``created``
    reading, which a plate started before it would not be taken off."""
    started = at or datetime.now(UTC)
    return SlotUse(slot_id=slot_id, spool_id=spool_id, grams=grams, plate_started_at=started)


@pg
def test_a_lan_start_books_estimates_on_sb_spools(pg_pool: PgPool) -> None:
    """The slot's tray holds SB spool #N; a start of 12 g books one ``print_started``
    estimate of -12 on #N, at the plate's start, which the ledger takes off at once."""
    held = _spool(pg_pool)
    _run(pg_pool, [_start("a")])
    started_at = datetime.now(UTC)
    uses = slot_uses({1: _option(-1, held), 2: _option(-2, None)}, {1: 12, 2: 5}, started_at)

    assert record_uses(pg_pool, RUN, "a", uses) == 1
    # An attempt that starts again books nothing new.
    assert record_uses(pg_pool, RUN, "a", [_use(held, 99)]) == 0

    assert _print_events(pg_pool, held) == [("print_started", "print_estimate", -12.0)]
    (started,) = [e for e in FilamentSpoolStore(pg_pool).events(held) if e.kind == "print_started"]
    assert started.at == started_at and started.ref_id == f"{RUN}/a/1"
    assert _grams(pg_pool, held) == 88


@pg
def test_settle_replaces_the_estimate_once(pg_pool: PgPool) -> None:
    """Settled twice, one ``print_ended`` per use: -10 for a start sliced at 10 g that
    ended ``done``, 0 (its estimate dropped) for one that ``failed``."""
    done, failed = _spool(pg_pool), _spool(pg_pool)
    _run(pg_pool, [_start("a"), _start("b")])
    started_at = datetime.now(UTC)
    record_uses(pg_pool, RUN, "a", [_use(done, 10, at=started_at)])
    record_uses(pg_pool, RUN, "b", [_use(failed, 10)])

    assert settle_uses(pg_pool, RUN, ["done", "failed"]) is True
    assert settle_uses(pg_pool, RUN, ["done", "failed"]) is False

    assert _print_events(pg_pool, done) == [
        ("print_started", "print_estimate", -10.0),
        ("print_ended", "print_actual", -10.0),
    ]
    assert _print_events(pg_pool, failed) == [
        ("print_started", "print_estimate", -10.0),
        ("print_ended", "print_actual", 0.0),
    ]
    assert (_grams(pg_pool, done), _grams(pg_pool, failed)) == (90, 100)
    ended = [e for e in FilamentSpoolStore(pg_pool).events(done) if e.kind == "print_ended"]
    assert [(e.at, e.data["basis"]) for e in ended] == [(started_at, "sliced")]


@pg
def test_a_reading_after_the_plate_started_is_not_taken_off_again(pg_pool: PgPool) -> None:
    """The estimate is booked at the plate's start, so a weighing between the start and
    the booking reads as holding the plate: nothing is taken off it."""
    spool = _spool(pg_pool)
    started_at = datetime.now(UTC)
    with pg_pool.connection() as conn, conn.transaction():
        weighed = started_at + timedelta(seconds=1)
        ledger.record(conn, spool, "weighed", source="scale", absolute_g=80, at=weighed)
    _run(pg_pool, [_start("a", ended="done")])
    record_uses(pg_pool, RUN, "a", [_use(spool, 12, at=started_at)])
    settle_uses(pg_pool, RUN, ["done"])
    assert _grams(pg_pool, spool) == 80


@pg
def test_a_spool_forgotten_before_its_print_settles_takes_nothing(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool)
    _run(pg_pool, [_start("a")])
    assert record_uses(pg_pool, RUN, "a", [_use(uuid.uuid4(), 1)]) == 0
    assert record_uses(pg_pool, RUN, "a", [_use(spool, 1, slot_id=2)]) == 1
    assert FilamentSpoolStore(pg_pool).delete(spool, refusing=("print_ended",))
    assert settle_uses(pg_pool, RUN, ["done"]) is False


def _settled(*stages: Stage) -> PrintProgress:
    return PrintProgress(
        route="lan",
        stage="done",
        settled=True,
        run_id=RUN,
        copies_detail=[CopyProgress(stage=stage) for stage in stages],
        bambuddy_url="",
    )


@pg
async def test_the_follow_settles_a_settled_lan_print_once(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool)
    _run(pg_pool, [_start("a")])
    record_uses(pg_pool, RUN, "a", [_use(spool, 30)])
    hook = print_use_hook(PoolPrintUses(pg_pool))

    await hook("output:x", _settled("running").model_copy(update={"settled": False}))
    await hook("output:x", _settled("done").model_copy(update={"route": "slice_queue"}))
    # The estimate comes off as the print starts (the ledger rule, #6261).
    assert _grams(pg_pool, spool) == 70
    assert [k for k, _, _ in _print_events(pg_pool, spool)] == ["print_started"]

    await hook("output:x", _settled("done"))
    await hook("output:x", _settled("done"))
    assert _grams(pg_pool, spool) == 70
    assert [k for k, _, _ in _print_events(pg_pool, spool)] == ["print_started", "print_ended"]


async def test_a_failed_settle_never_raises_into_the_follow() -> None:
    class Broken:
        async def record_uses(self, *_: Any) -> int:
            raise AssertionError

        async def settle_uses(self, *_: Any) -> bool:
            raise ConnectionError("the API is away")

    await print_use_hook(Broken())("output:x", _settled("done"))


def test_the_filament_component_settles_from_the_follows_reads() -> None:
    """The API's follow settles a native print's uses as it reads it settled (#4643)."""
    core = SimpleNamespace(
        settings_store=SimpleNamespace(pool=object()),
        print_follower=SimpleNamespace(on_read=[]),
    )
    filament_component._build(cast(Core, core), cast(Components, SimpleNamespace()))
    (hook,) = core.print_follower.on_read
    assert hook.__qualname__ == f"{print_use_hook.__qualname__}.<locals>.hook"


class _Reads:
    """SB spool reads that must not be reached."""

    async def trays(self, *_: Any) -> Any:
        raise AssertionError("read with no printer")

    async def presets(self, *_: Any) -> Any:
        raise AssertionError("read with no printer")


class _BrokenReports:
    def get(self, printer_id: str) -> Any:
        raise ConnectionError("the API is away")


async def test_a_failed_report_read_plans_on_the_trays_alone() -> None:
    """The run's plan reads the printer's stored report first; when that read fails it
    plans on the trays alone and goes on (`PrintActivities._sb_spools`)."""
    deps = SimpleNamespace(
        sb_spools=_Reads(),
        printers=SimpleNamespace(get=lambda printer_id: object()),
        reports=_BrokenReports(),
    )
    activities = PrintActivities(cast(PrintDeps, deps))
    prepared = SimpleNamespace(lan_start=True, printer="row-1")
    assert await activities._sb_spools(cast(PreparedPlates, prepared)) is None
