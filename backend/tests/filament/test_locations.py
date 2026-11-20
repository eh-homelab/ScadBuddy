"""The location tree and spool moves (#6261)."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from scadbuddy.filament.catalog import CatalogStore
from scadbuddy.filament.locations import (
    LocationCycleError,
    LocationExistsError,
    LocationStore,
    MoveTargetError,
)
from scadbuddy.filament.models import (
    LocationCreate,
    LocationKind,
    LocationNode,
    LocationPatch,
    Material,
    SpoolCreate,
)
from scadbuddy.filament.spools import FilamentSpoolStore
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_postgres


def _kind(pool: PgPool, name: str = "Shelf") -> UUID:
    rows = CatalogStore(pool).list("location-kinds")
    return next(r.id for r in rows if isinstance(r, LocationKind) and r.name == name)


def _place(store: LocationStore, pool: PgPool, name: str, parent: UUID | None = None) -> UUID:
    return store.create(LocationCreate(name=name, kind_id=_kind(pool), parent_id=parent)).id


def _spool(pool: PgPool) -> UUID:
    rows = CatalogStore(pool).list("materials")
    material = next(r.id for r in rows if isinstance(r, Material) and r.name == "PLA")
    return FilamentSpoolStore(pool).create(SpoolCreate(material_id=material), actor="ann").id


def _printer(pool: PgPool) -> UUID:
    printer = uuid4()
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO printers (id, serial, name) VALUES (%s, %s, 'P')",
            (printer, f"SN{printer.hex[:8]}"),
        )
    return printer


def _names(nodes: list[LocationNode]) -> list[Any]:
    return [(n.name, n.spool_count, _names(n.children)) for n in nodes]


def test_the_tree_nests_and_counts(pg_pool: PgPool) -> None:
    store = LocationStore(pg_pool)
    room = _place(store, pg_pool, "Room")
    shelf = _place(store, pg_pool, "Shelf A", room)
    _place(store, pg_pool, "Shelf B", room)
    gone = _place(store, pg_pool, "Old", room)
    one, two, archived = _spool(pg_pool), _spool(pg_pool), _spool(pg_pool)
    spools = FilamentSpoolStore(pg_pool)
    for spool in (one, two, archived):
        store.move(spool, shelf, actor="ann")
    spools.archive(archived, True, actor="ann")
    store.update(gone, LocationPatch(archived=True))
    assert _names(store.tree()) == [
        ("Room", 0, [("Shelf A", 2, []), ("Shelf B", 0, [])]),
    ]
    everything = _names(store.tree(include_archived=True))
    assert everything == [("Room", 0, [("Old", 0, []), ("Shelf A", 2, []), ("Shelf B", 0, [])])]


def test_a_parent_cycle_is_refused(pg_pool: PgPool) -> None:
    store = LocationStore(pg_pool)
    a = _place(store, pg_pool, "A")
    b = _place(store, pg_pool, "B", a)
    with pytest.raises(LocationCycleError):
        store.update(a, LocationPatch(parent_id=b))
    with pytest.raises(LocationCycleError):
        store.update(a, LocationPatch(parent_id=a))
    assert store.update(b, LocationPatch(parent_id=None)) is not None


def test_opposite_reparents_never_make_a_cycle(pg_pool: PgPool) -> None:
    store = LocationStore(pg_pool)
    a, b = _place(store, pg_pool, "A"), _place(store, pg_pool, "B")
    barrier = threading.Barrier(2)

    def reparent(child: UUID, parent: UUID) -> bool:
        barrier.wait()
        try:
            store.update(child, LocationPatch(parent_id=parent))
        except LocationCycleError:
            return False
        return True

    with ThreadPoolExecutor(2) as ex:
        done = [ex.submit(reparent, a, b), ex.submit(reparent, b, a)]
        results = [f.result() for f in done]
    assert sorted(results) == [False, True]


def test_a_name_is_unique_under_its_parent(pg_pool: PgPool) -> None:
    store = LocationStore(pg_pool)
    _place(store, pg_pool, "A")
    with pytest.raises(LocationExistsError):
        _place(store, pg_pool, "a")


def test_a_second_place_for_an_ams_unit_names_the_unit(pg_pool: PgPool) -> None:
    """#6679 item 1: the 409 for a second hand-made place of one AMS unit says so."""
    store = LocationStore(pg_pool)
    printer = _printer(pg_pool)
    unit = LocationCreate(name="AMS A", kind_id=_kind(pg_pool), printer_id=printer, ams_id=0)
    store.create(unit)
    with pytest.raises(LocationExistsError, match="that AMS unit"):
        store.create(unit.model_copy(update={"name": "AMS B"}))


def test_a_move_is_a_ledger_event(pg_pool: PgPool) -> None:
    store = LocationStore(pg_pool)
    first, second = _place(store, pg_pool, "A"), _place(store, pg_pool, "B")
    spool = _spool(pg_pool)
    spools = FilamentSpoolStore(pg_pool)
    event = store.move(spool, first, actor="ann")
    assert event is not None
    assert event.kind == "moved"
    assert event.actor == "ann"
    assert event.data == {"from": None, "to": str(first)}
    moved = store.move(spool, second, actor="ann")
    assert moved is not None
    assert moved.data == {"from": str(first), "to": str(second)}
    found = spools.get(spool)
    assert found is not None
    assert found.location_id == second
    assert [e.kind for e in spools.events(spool)] == ["created", "moved", "moved"]


def test_moving_to_where_a_spool_is_writes_nothing(pg_pool: PgPool) -> None:
    store = LocationStore(pg_pool)
    place = _place(store, pg_pool, "A")
    spool = _spool(pg_pool)
    store.move(spool, place, actor="ann")
    assert store.move(spool, place, actor="ann") is None
    assert [e.kind for e in FilamentSpoolStore(pg_pool).events(spool)] == ["created", "moved"]


def test_a_move_to_nowhere_clears_the_place(pg_pool: PgPool) -> None:
    store = LocationStore(pg_pool)
    place = _place(store, pg_pool, "A")
    spool = _spool(pg_pool)
    store.move(spool, place, actor="ann")
    event = store.move(spool, None, actor="ann")
    assert event is not None
    assert event.data == {"from": str(place), "to": None}


def test_a_move_to_a_missing_or_archived_place_is_refused(pg_pool: PgPool) -> None:
    store = LocationStore(pg_pool)
    place = _place(store, pg_pool, "A")
    store.update(place, LocationPatch(archived=True))
    spool = _spool(pg_pool)
    with pytest.raises(MoveTargetError):
        store.move(spool, uuid4(), actor="ann")
    with pytest.raises(MoveTargetError):
        store.move(spool, place, actor="ann")
    assert [e.kind for e in FilamentSpoolStore(pg_pool).events(spool)] == ["created"]


def test_a_move_of_no_spool_raises_lookup_error(pg_pool: PgPool) -> None:
    store = LocationStore(pg_pool)
    place = _place(store, pg_pool, "A")
    with pytest.raises(LookupError):
        store.move(uuid4(), place, actor="ann")


def test_ensure_ams_is_idempotent(pg_pool: PgPool) -> None:
    store = LocationStore(pg_pool)
    printer = _printer(pg_pool)
    first = store.ensure_ams(printer, 0, 1, label="H2C AMS 0 tray 1")
    again = store.ensure_ams(printer, 0, 1, label="another label")
    assert again.id == first.id
    assert (first.printer_id, first.ams_id, first.tray_id) == (printer, 0, 1)
    assert first.kind_id == _kind(pg_pool, "AMS")
    assert store.ensure_ams(printer, 0, 2, label="H2C AMS 0 tray 2").id != first.id


def test_ensure_ams_survives_a_concurrent_call(pg_pool: PgPool) -> None:
    store = LocationStore(pg_pool)
    printer = _printer(pg_pool)
    barrier = threading.Barrier(4)

    def call(i: int) -> UUID:
        barrier.wait()
        return store.ensure_ams(printer, 1, 0, label=f"tray {i}").id

    with ThreadPoolExecutor(4) as ex:
        ids = {f.result() for f in [ex.submit(call, i) for i in range(4)]}
    assert len(ids) == 1


def test_ensure_ams_makes_a_fresh_row_after_the_old_one_is_archived(pg_pool: PgPool) -> None:
    store = LocationStore(pg_pool)
    printer = _printer(pg_pool)
    first = store.ensure_ams(printer, 0, 0, label="T")
    store.update(first.id, LocationPatch(archived=True))
    assert store.ensure_ams(printer, 0, 0, label="T").id != first.id


def test_a_tray_label_never_clashes_with_a_name(pg_pool: PgPool) -> None:
    store = LocationStore(pg_pool)
    _place(store, pg_pool, "AMS 0 tray 0")
    one, two = _printer(pg_pool), _printer(pg_pool)
    first = store.ensure_ams(one, 0, 0, label="AMS 0 tray 0")
    second = store.ensure_ams(two, 0, 0, label="AMS 0 tray 0")
    assert first.id != second.id
    assert first.name == second.name == "AMS 0 tray 0"


def test_move_on_and_ensure_ams_on_match_the_pool_methods(pg_pool: PgPool) -> None:
    store = LocationStore(pg_pool)
    printer, spool = _printer(pg_pool), _spool(pg_pool)
    shelf = _place(store, pg_pool, "Shelf A")
    at = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
    with pg_pool.connection() as conn, conn.transaction():
        tray = LocationStore.ensure_ams_on(conn, printer, 0, 2, label="P AMS 1 slot 3")
        again = LocationStore.ensure_ams_on(conn, printer, 0, 2, label="other")
        moved = LocationStore.move_on(conn, spool, tray.id, actor="w", at=at)
        assert LocationStore.move_on(conn, spool, tray.id, actor="w", at=at) is None
        conn.execute("UPDATE filament_spools SET tray_fingerprint = 'x' WHERE id = %s", (spool,))
        LocationStore.move_on(conn, spool, shelf, actor="w")
        cleared = conn.execute(
            "SELECT tray_fingerprint FROM filament_spools WHERE id = %s", (spool,)
        ).fetchone()
    assert again == tray == store.ensure_ams(printer, 0, 2, label="x")
    assert moved is not None and moved.at == at and moved.data["to"] == str(tray.id)
    assert cleared == {"tray_fingerprint": None}


def test_a_spool_cannot_move_into_an_ams_unit_place(pg_pool: PgPool) -> None:
    printer = _printer(pg_pool)
    store = LocationStore(pg_pool)
    spool = _spool(pg_pool)
    with pg_pool.connection() as conn:
        unit = LocationStore.ensure_ams_unit_on(conn, printer, 0)
    with pytest.raises(MoveTargetError):
        store.move(spool, unit, actor="ann")
    tray = store.ensure_ams(printer, 0, 1, label="T1")
    assert store.move(spool, tray.id, actor="ann") is not None


def test_a_spool_can_move_into_a_printer_place_with_no_ams(pg_pool: PgPool) -> None:
    """A place beside a printer (``printer_id`` and no ``ams_id``) is not a unit's
    place (Decision 16), so it takes spools."""
    printer = _printer(pg_pool)
    store = LocationStore(pg_pool)
    spool = _spool(pg_pool)
    beside = store.create(
        LocationCreate(name="Beside the H2C", kind_id=_kind(pg_pool), printer_id=printer)
    ).id
    assert store.move(spool, beside, actor="ann") is not None
