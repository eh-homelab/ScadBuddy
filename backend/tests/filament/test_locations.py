"""The location tree and spool moves (#6261)."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from scadbuddy.bambuddy.trays import EXTERNAL
from scadbuddy.filament.catalog import CatalogRefError, CatalogStore
from scadbuddy.filament.labels import printer_labels
from scadbuddy.filament.locations import (
    LocationCycleError,
    LocationExistsError,
    LocationStore,
    MoveTargetError,
    NoPrinterKindError,
    repoint_printer_locations,
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
from tests.filament.lock_support import waits_for_the_printer_lock

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


def _printer_named(pool: PgPool, name: str) -> UUID:
    printer = uuid4()
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO printers (id, serial, name) VALUES (%s, %s, %s)",
            (printer, f"SN{printer.hex[:8]}", name),
        )
    return printer


def test_printer_place_is_made_once_and_named_as_the_printer(pg_pool: PgPool) -> None:
    store = LocationStore(pg_pool)
    printer = _printer_named(pg_pool, "H2C")
    first = store.ensure_printer(printer)
    assert store.ensure_printer(printer) == first
    place = store.get(first)
    assert place is not None
    assert (place.name, place.printer_id, place.ams_id, place.tray_id) == (
        "H2C",
        printer,
        None,
        None,
    )
    assert place.kind_id == _kind(pg_pool, "Printer")
    with pytest.raises(CatalogRefError):
        store.ensure_printer(uuid4())


def test_two_threads_make_one_printer_place(pg_pool: PgPool) -> None:
    store = LocationStore(pg_pool)
    printer = _printer(pg_pool)
    barrier = threading.Barrier(4)

    def call() -> UUID:
        barrier.wait()
        return store.ensure_printer(printer)

    with ThreadPoolExecutor(4) as ex:
        ids = {f.result() for f in [ex.submit(call) for _ in range(4)]}
    assert len(ids) == 1


def test_unit_places_made_before_hang_under_the_printer(pg_pool: PgPool) -> None:
    store = LocationStore(pg_pool)
    printer = _printer(pg_pool)
    tray = store.ensure_ams(printer, 0, 1, label="T1")
    with pg_pool.connection() as conn:
        assert tray.parent_id is not None
        conn.execute(
            "UPDATE filament_locations SET parent_id = NULL WHERE id = %s", (tray.parent_id,)
        )
    unit = tray.parent_id
    assert unit is not None
    root = store.ensure_printer(printer)
    placed = store.get(unit)
    assert placed is not None and placed.parent_id == root
    again = store.get(tray.id)
    assert again is not None and again.parent_id == unit


def test_a_new_root_takes_the_units_of_an_archived_one(pg_pool: PgPool) -> None:
    store = LocationStore(pg_pool)
    printer = _printer(pg_pool)
    tray = store.ensure_ams(printer, 0, 1, label="T1")
    external = store.ensure_ams(printer, EXTERNAL, 0, label="Ext")
    old = store.ensure_printer(printer)
    store.update(old, LocationPatch(archived=True))
    new = store.ensure_printer(printer)
    assert new != old
    assert tray.parent_id is not None
    unit = store.get(tray.parent_id)
    moved = store.get(external.id)
    assert unit is not None and unit.parent_id == new
    assert moved is not None and moved.parent_id == new


def test_a_beside_place_and_the_root_coexist_and_the_root_never_adopts_it(
    pg_pool: PgPool,
) -> None:
    store = LocationStore(pg_pool)
    printer = _printer(pg_pool)
    beside = store.create(LocationCreate(name="Beside", kind_id=_kind(pg_pool), printer_id=printer))
    root = store.ensure_printer(printer)
    assert root != beside.id
    other = store.create(
        LocationCreate(name="Beside 2", kind_id=_kind(pg_pool), printer_id=printer)
    )
    assert store.ensure_printer(printer) == root
    still = store.get(beside.id)
    assert still is not None and not still.printer_root and still.parent_id is None
    made = store.get(root)
    assert made is not None and made.printer_root
    assert other.printer_root is False


def test_new_unit_and_external_tray_hang_under_the_printer(pg_pool: PgPool) -> None:
    store = LocationStore(pg_pool)
    printer = _printer(pg_pool)
    tray = store.ensure_ams(printer, 0, 2, label="T")
    external = store.ensure_ams(printer, EXTERNAL, 0, label="Ext")
    root = store.ensure_printer(printer)
    unit = store.get(tray.parent_id) if tray.parent_id else None
    assert unit is not None and unit.parent_id == root
    assert external.parent_id == root


def test_a_missing_printer_kind_leaves_the_unit_with_no_parent(pg_pool: PgPool) -> None:
    store = LocationStore(pg_pool)
    printer = _printer(pg_pool)
    with pg_pool.connection() as conn:
        conn.execute(
            "UPDATE filament_location_kinds SET archived_at = now() WHERE name = 'Printer'"
        )
    tray = store.ensure_ams(printer, 0, 0, label="T")
    unit = store.get(tray.parent_id) if tray.parent_id else None
    assert unit is not None and unit.parent_id is None
    with pytest.raises(NoPrinterKindError):
        store.ensure_printer(printer)


def test_merge_keeps_the_kept_printers_place(pg_pool: PgPool) -> None:
    store = LocationStore(pg_pool)
    merged, into = _printer(pg_pool), _printer(pg_pool)
    clashing = store.ensure_ams(merged, 0, 0, label="T")
    store.ensure_ams(into, 0, 0, label="T")
    live = store.ensure_ams(merged, 1, 0, label="T live")
    beside = store.create(
        LocationCreate(name="Beside merged", kind_id=_kind(pg_pool), printer_id=merged)
    )
    root_merged, root_into = store.ensure_printer(merged), store.ensure_printer(into)
    with pg_pool.connection() as conn, conn.transaction():
        repoint_printer_locations(conn, merged, into)
    old = store.get(root_merged)
    assert old is not None and old.archived_at is not None
    kept = store.get(root_into)
    assert kept is not None and kept.archived_at is None
    assert clashing.parent_id is not None and live.parent_id is not None
    gone_unit = store.get(clashing.parent_id)
    assert gone_unit is not None and gone_unit.archived_at is not None
    live_unit = store.get(live.parent_id)
    assert live_unit is not None and live_unit.archived_at is None
    assert live_unit.parent_id == root_into and live_unit.printer_id == into
    moved = store.get(beside.id)
    assert moved is not None and moved.archived_at is None and moved.printer_id == into


def test_a_merged_root_moved_to_the_kept_printer_takes_its_name(pg_pool: PgPool) -> None:
    """#7081: only the merged printer has a printer place, so it moves to ``into`` and is
    named as ``into``, in the merge's transaction."""
    store = LocationStore(pg_pool)
    merged, into = _printer_named(pg_pool, "Old H2C"), _printer_named(pg_pool, "H2C")
    root = store.ensure_printer(merged)
    with pg_pool.connection() as conn, conn.transaction():
        repoint_printer_locations(conn, merged, into)
    moved = store.get(root)
    assert moved is not None and moved.archived_at is None
    assert (moved.printer_id, moved.name) == (into, "H2C")
    assert store.ensure_printer(into) == root


def test_printer_labels_take_the_printer_lock_before_any_place(pg_pool: PgPool) -> None:
    """#7081: the labels route takes the printer's place lock first, as capture does
    (`tests/filament/test_capture.py`), so the two never take place locks in another
    order and never deadlock. While it is held the route makes nothing."""
    store = LocationStore(pg_pool)
    printer = _printer_named(pg_pool, "H2C")
    found, _ = waits_for_the_printer_lock(
        pg_pool, printer, lambda: printer_labels(store, [printer])
    )
    assert [label.name for label in found] == ["H2C"]


def test_every_place_maker_takes_the_printer_lock(pg_pool: PgPool) -> None:
    """Each `ensure_*` takes the lock, so every caller's first place write comes after it,
    also with no printer kind (a capture never fails for a missing kind)."""
    printer = _printer(pg_pool)

    def in_transaction(make: Any) -> Any:
        def run() -> Any:
            with pg_pool.connection() as conn, conn.transaction():
                return make(conn)

        return run

    waits_for_the_printer_lock(
        pg_pool, printer, in_transaction(lambda c: LocationStore.ensure_printer_on(c, printer))
    )
    waits_for_the_printer_lock(
        pg_pool, printer, in_transaction(lambda c: LocationStore.ensure_ams_unit_on(c, printer, 0))
    )
    with pg_pool.connection() as conn:
        conn.execute(
            "UPDATE filament_location_kinds SET archived_at = now() WHERE name = 'Printer'"
        )
    waits_for_the_printer_lock(
        pg_pool,
        printer,
        in_transaction(lambda c: LocationStore.ensure_ams_on(c, printer, 1, 0, label="T")),
    )


def test_rehanging_never_makes_a_cycle_through_the_root(pg_pool: PgPool) -> None:
    """Review of #7038: a person hangs the printer's place under its own unit place, then
    the next capture rehangs the parentless unit. The unit stays where it is, and the
    place page still reads."""
    store = LocationStore(pg_pool)
    printer = _printer(pg_pool)
    tray = store.ensure_ams(printer, 0, 1, label="T1")
    root = store.ensure_printer(printer)
    unit = tray.parent_id
    assert unit is not None
    store.update(unit, LocationPatch(parent_id=None))
    store.update(root, LocationPatch(parent_id=unit))
    assert store.ensure_printer(printer) == root
    with pg_pool.connection() as conn:
        assert LocationStore.ensure_ams_unit_on(conn, printer, 0) == unit
    store.ensure_ams(printer, 0, 1, label="T1")
    left = store.get(unit)
    assert left is not None and left.parent_id is None
    detail = store.detail(root)
    assert detail is not None and [p.id for p in detail.path] == [unit]


def test_the_external_holder_never_makes_a_cycle_through_the_root(pg_pool: PgPool) -> None:
    """The same for the external holder, which hangs straight under the printer's place."""
    store = LocationStore(pg_pool)
    printer = _printer(pg_pool)
    holder = store.ensure_ams(printer, EXTERNAL, 0, label="Ext")
    root = store.ensure_printer(printer)
    store.update(holder.id, LocationPatch(parent_id=None))
    store.update(root, LocationPatch(parent_id=holder.id))
    again = store.ensure_ams(printer, EXTERNAL, 0, label="Ext")
    assert again.id == holder.id and again.parent_id is None
    assert store.ensure_printer(printer) == root
    left = store.get(holder.id)
    assert left is not None and left.parent_id is None


def test_the_place_page_reads_through_a_cycle(pg_pool: PgPool) -> None:
    """A cycle a bad row made ends the ancestor walk instead of looping."""
    store = LocationStore(pg_pool)
    a = _place(store, pg_pool, "A")
    b = _place(store, pg_pool, "B", parent=a)
    with pg_pool.connection() as conn:
        conn.execute("UPDATE filament_locations SET parent_id = %s WHERE id = %s", (b, a))
    detail = store.detail(b)
    assert detail is not None and [p.id for p in detail.path] == [a]
