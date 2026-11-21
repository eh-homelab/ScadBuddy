"""Time outside dry storage and needs drying (#6268, plan Decision 8, Review Focus 5)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from scadbuddy.filament import dry_hours
from scadbuddy.filament.catalog import CatalogStore
from scadbuddy.filament.dry_hours import Move
from scadbuddy.filament.ledger import record
from scadbuddy.filament.locations import LocationStore
from scadbuddy.filament.models import (
    LocationCreate,
    LocationKind,
    LocationKindCreate,
    LocationPatch,
    Material,
    SpoolCreate,
)
from scadbuddy.filament.spools import FilamentSpoolStore
from tests.conftest import PgPool

T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def _h(hours: float) -> datetime:
    return T0 + timedelta(hours=hours)


# -- the pure computation ---------------------------------------------------------------


def test_pure_counts_a_spool_with_no_place_as_outside() -> None:
    assert dry_hours.outside_dry(T0, [], set(), None, _h(5)) == pytest.approx(5)


def test_pure_a_spool_that_never_moved_follows_its_place() -> None:
    box = uuid4()
    assert dry_hours.outside_dry(T0, [], {box}, box, _h(5)) == 0
    assert dry_hours.outside_dry(T0, [], {box}, uuid4(), _h(5)) == pytest.approx(5)


def test_pure_a_move_into_nowhere_ends_dry_time() -> None:
    box = uuid4()
    moves = [Move(_h(2), box, None)]
    assert dry_hours.outside_dry(T0, moves, {box}, None, _h(5)) == pytest.approx(3)


def test_pure_a_clock_before_the_start_counts_nothing() -> None:
    assert dry_hours.outside_dry(T0, [], set(), None, _h(-1)) == 0


# -- the stores -------------------------------------------------------------------------


def _kind(pool: PgPool, name: str, dry: bool) -> UUID:
    store = CatalogStore(pool)
    found = [r for r in store.list("location-kinds") if isinstance(r, LocationKind)]
    if (row := next((r for r in found if r.name == name), None)) is not None:
        return row.id
    created = store.create("location-kinds", LocationKindCreate(name=name, dry_storage=dry))
    assert isinstance(created, LocationKind)
    return created.id


def _place(pool: PgPool, name: str, *, dry: bool) -> UUID:
    kind = _kind(pool, "Dry box" if dry else "Open shelf", dry)
    return LocationStore(pool).create(LocationCreate(name=name, kind_id=kind)).id


def _spool(pool: PgPool, *, hygroscopic: bool = True, limit: float | None = 10) -> UUID:
    material = next(
        r.id
        for r in CatalogStore(pool).list("materials")
        if isinstance(r, Material) and r.name == "PLA"
    )
    with pool.connection() as conn:
        conn.execute(
            "UPDATE filament_materials SET hygroscopic = %s, max_hours_outside_dry = %s"
            " WHERE id = %s",
            (hygroscopic, limit, material),
        )
    made = FilamentSpoolStore(pool).create(SpoolCreate(material_id=material), actor="ann")
    with pool.connection() as conn:
        conn.execute("UPDATE filament_spools SET created_at = %s WHERE id = %s", (T0, made.id))
    return made.id


def _move(pool: PgPool, spool: UUID, place: UUID | None, hours: float) -> None:
    with pool.connection() as conn, conn.transaction():
        LocationStore.move_on(conn, spool, place, actor="ann", at=_h(hours))


def _store(pool: PgPool, now: datetime) -> FilamentSpoolStore:
    return FilamentSpoolStore(pool, now=lambda: now)


@pytest.mark.requires_postgres
def test_hours_count_only_outside_dry_storage(pg_pool: PgPool) -> None:
    dry, shelf = _place(pg_pool, "Box", dry=True), _place(pg_pool, "Shelf", dry=False)
    spool = _spool(pg_pool)
    _move(pg_pool, spool, dry, 1)  # 0..1 in no place: 1 h outside
    _move(pg_pool, spool, shelf, 4)  # 1..4 dry
    detail = _store(pg_pool, _h(10)).detail(spool)  # 4..10 on the shelf: 6 h
    assert detail is not None
    assert detail.hours_outside_dry == pytest.approx(7)
    assert detail.needs_drying is False
    listed = next(s for s in _store(pg_pool, _h(10)).list() if s.id == spool)
    assert listed.hours_outside_dry == pytest.approx(7)


@pytest.mark.requires_postgres
def test_a_move_between_dry_boxes_adds_nothing(pg_pool: PgPool) -> None:
    one, two = _place(pg_pool, "Box 1", dry=True), _place(pg_pool, "Box 2", dry=True)
    spool = _spool(pg_pool)
    _move(pg_pool, spool, one, 0)
    _move(pg_pool, spool, two, 50)
    read = _store(pg_pool, _h(100)).get(spool)
    assert read is not None
    assert read.hours_outside_dry == 0
    assert read.needs_drying is False


@pytest.mark.requires_postgres
def test_drying_resets_the_count(pg_pool: PgPool) -> None:
    shelf = _place(pg_pool, "Shelf", dry=False)
    spool = _spool(pg_pool)
    _move(pg_pool, spool, shelf, 0)
    with pg_pool.connection() as conn, conn.transaction():
        record(conn, spool, "dried", at=_h(30), data={"temp_c": 55, "hours": 6})
    _move(pg_pool, spool, None, 32)
    read = _store(pg_pool, _h(34)).get(spool)
    assert read is not None
    assert read.hours_outside_dry == pytest.approx(4)


@pytest.mark.requires_postgres
def test_needs_drying_needs_a_hygroscopic_material_and_a_limit(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool, hygroscopic=True, limit=10)
    store = _store(pg_pool, _h(11))
    read = store.get(spool)
    assert read is not None
    assert read.hours_outside_dry == pytest.approx(11)
    assert read.needs_drying is True
    edge = _store(pg_pool, _h(10)).get(spool)
    assert edge is not None
    assert edge.needs_drying is False
    for hygroscopic, limit in ((False, 10), (True, None)):
        other = store.get(_spool(pg_pool, hygroscopic=hygroscopic, limit=limit))
        assert other is not None
        assert other.hours_outside_dry == pytest.approx(11)
        assert other.needs_drying is False


@pytest.mark.requires_postgres
def test_a_rekinded_place_judges_the_whole_window_by_its_current_kind(pg_pool: PgPool) -> None:
    shelf = _place(pg_pool, "Shelf", dry=False)
    spool = _spool(pg_pool)
    _move(pg_pool, spool, shelf, 0)
    before = _store(pg_pool, _h(8)).get(spool)
    assert before is not None
    assert before.hours_outside_dry == pytest.approx(8)
    LocationStore(pg_pool).update(shelf, LocationPatch(kind_id=_kind(pg_pool, "Dry box", True)))
    after = _store(pg_pool, _h(8)).get(spool)
    assert after is not None
    assert after.hours_outside_dry == 0


@pytest.mark.requires_postgres
def test_an_archived_dry_box_still_counts_as_dry(pg_pool: PgPool) -> None:
    box = _place(pg_pool, "Old box", dry=True)
    spool = _spool(pg_pool)
    _move(pg_pool, spool, box, 0)
    with pg_pool.connection() as conn:
        conn.execute("UPDATE filament_locations SET archived_at = now() WHERE id = %s", (box,))
    read = _store(pg_pool, _h(20)).get(spool)
    assert read is not None
    assert read.hours_outside_dry == 0


@pytest.mark.requires_postgres
def test_the_first_move_recovers_the_place_the_spool_started_in(pg_pool: PgPool) -> None:
    shelf, box = _place(pg_pool, "Shelf", dry=False), _place(pg_pool, "Box", dry=True)
    spool = _spool(pg_pool)
    with pg_pool.connection() as conn:  # made in the shelf: no moved event for that
        conn.execute("UPDATE filament_spools SET location_id = %s WHERE id = %s", (shelf, spool))
    _move(pg_pool, spool, box, 4)  # 0..4 on the shelf, then dry
    read = _store(pg_pool, _h(10)).get(spool)
    assert read is not None
    assert read.hours_outside_dry == pytest.approx(4)


@pytest.mark.requires_postgres
def test_the_hours_are_rounded_to_a_tenth_but_the_limit_sees_the_exact_value(
    pg_pool: PgPool,
) -> None:
    spool = _spool(pg_pool, limit=10)
    read = _store(pg_pool, _h(10.04)).get(spool)
    assert read is not None
    assert read.hours_outside_dry == 10.0
    assert read.needs_drying is True
