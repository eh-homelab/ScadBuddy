"""A dry box holds one spool, and a spool keeps its home box (#7167, Task 2)."""

from __future__ import annotations

import threading
from pathlib import Path
from uuid import UUID

import pytest

from scadbuddy.bambuddy.models import BbLocation
from scadbuddy.filament.bambuddy_pull import pull
from scadbuddy.filament.catalog import CatalogStore
from scadbuddy.filament.csv_import import CsvRow, ImportRefusedError, import_rows
from scadbuddy.filament.dry_box import BoxFullError, BoxHasManyError, is_dry_box, occupant_on
from scadbuddy.filament.locations import LocationStore
from scadbuddy.filament.models import (
    LocationCreate,
    LocationKind,
    LocationKindCreate,
    LocationKindPatch,
    LocationPatch,
    Material,
    SpoolBulkCreate,
    SpoolCreate,
    SpoolPatch,
)
from scadbuddy.filament.spools import FilamentSpoolStore
from tests.conftest import PgPool
from tests.filament.test_bambuddy_pull import NOW, bb
from tests.filament.test_bambuddy_pull_refs import ROWS, _bb_time
from tests.filament.test_bb_sync_plan import _place as _printer_place
from tests.filament.test_bb_sync_plan import _printer
from tests.filament.test_csv_import import _filament

pytestmark = pytest.mark.requires_postgres

_MIGRATION = (
    Path(__file__).parents[2] / "scadbuddy/migrations/20261127T0754Z_filament_dry_boxes.sql"
)


def _place(pool: PgPool, name: str, kind: str, capacity: int | None) -> UUID:
    kinds = CatalogStore(pool).list("location-kinds")
    kind_id = next(r.id for r in kinds if isinstance(r, LocationKind) and r.name == kind)
    made = LocationStore(pool).create(LocationCreate(name=name, kind_id=kind_id, capacity=capacity))
    return made.id


def _box(pool: PgPool, name: str = "Box A") -> UUID:
    return _place(pool, name, "Dry box", 1)


def _spool(pool: PgPool) -> UUID:
    material = next(
        r.id
        for r in CatalogStore(pool).list("materials")
        if isinstance(r, Material) and r.name == "PLA"
    )
    return FilamentSpoolStore(pool).create(SpoolCreate(material_id=material), actor="ann").id


def _row(pool: PgPool, spool: UUID) -> dict[str, object]:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT location_id, home_location_id, home_asked_at FROM filament_spools"
            " WHERE id = %s",
            (spool,),
        ).fetchone()
    assert row is not None
    return dict(row)


def _moved(pool: PgPool, spool: UUID) -> int:
    return sum(1 for e in FilamentSpoolStore(pool).events(spool) if e.kind == "moved")


def test_move_into_empty_box_sets_home(pg_pool: PgPool) -> None:
    box, spool = _box(pg_pool), _spool(pg_pool)
    LocationStore(pg_pool).move(spool, box, actor="ann")
    row = _row(pg_pool, spool)
    assert row["location_id"] == box
    assert row["home_location_id"] == box


def test_move_into_full_box_raises_box_full(pg_pool: PgPool) -> None:
    box, first, second = _box(pg_pool), _spool(pg_pool), _spool(pg_pool)
    store = LocationStore(pg_pool)
    store.move(first, box, actor="ann")
    with pytest.raises(BoxFullError) as full:
        store.move(second, box, actor="ann")
    assert full.value.occupant == first
    row = _row(pg_pool, second)
    assert (row["location_id"], row["home_location_id"]) == (None, None)
    assert _moved(pg_pool, second) == 0


def test_move_to_shelf_keeps_home(pg_pool: PgPool) -> None:
    box, spool = _box(pg_pool), _spool(pg_pool)
    shelf = _place(pg_pool, "Shelf A", "Shelf", None)
    store = LocationStore(pg_pool)
    store.move(spool, box, actor="ann")
    with pg_pool.connection() as conn:
        conn.execute("UPDATE filament_spools SET home_asked_at = now() WHERE id = %s", (spool,))
    store.move(spool, shelf, actor="ann")
    row = _row(pg_pool, spool)
    assert row["location_id"] == shelf
    assert row["home_location_id"] == box
    assert row["home_asked_at"] is None
    store.move(spool, None, actor="ann")
    assert _row(pg_pool, spool)["home_location_id"] == box


def test_same_spool_into_its_own_box_is_not_full(pg_pool: PgPool) -> None:
    box, spool = _box(pg_pool), _spool(pg_pool)
    store = LocationStore(pg_pool)
    store.move(spool, box, actor="ann")
    assert store.move(spool, box, actor="ann") is None
    with pg_pool.connection() as conn:
        assert occupant_on(conn, box, but=spool) is None
        assert occupant_on(conn, box) == spool


def test_archived_occupant_does_not_fill_the_box(pg_pool: PgPool) -> None:
    box, old, new = _box(pg_pool), _spool(pg_pool), _spool(pg_pool)
    store = LocationStore(pg_pool)
    store.move(old, box, actor="ann")
    with pg_pool.connection() as conn:
        conn.execute("UPDATE filament_spools SET archived_at = now() WHERE id = %s", (old,))
    store.move(new, box, actor="ann")
    assert _row(pg_pool, new)["location_id"] == box


def test_a_place_with_more_capacity_or_another_kind_is_no_box(pg_pool: PgPool) -> None:
    roomy = _place(pg_pool, "Roomy", "Dry box", 4)
    shelf = _place(pg_pool, "Shelf", "Shelf", 1)
    unsized = _place(pg_pool, "Unsized", "Dry box", None)
    a, b = _spool(pg_pool), _spool(pg_pool)
    store = LocationStore(pg_pool)
    with pg_pool.connection() as conn:
        assert not any(is_dry_box(conn, p) for p in (roomy, shelf, unsized))
    for place in (roomy, shelf, unsized):
        store.move(a, place, actor="ann")
        store.move(b, place, actor="ann")
        store.move(b, None, actor="ann")
    assert _row(pg_pool, a)["home_location_id"] is None


def test_every_location_writer_respects_a_full_box(pg_pool: PgPool) -> None:
    """Review Focus 1: the bulk create and the CSV import write a place through
    `move_on`; the invoice confirm writes no place at all."""
    f = _filament(pg_pool)
    box = _box(pg_pool)
    holder = _spool(pg_pool)
    f.locations.move(holder, box, actor="ann")
    material = next(
        r.id for r in f.catalog.list("materials") if isinstance(r, Material) and r.name == "PLA"
    )
    # A create with a place.
    draft = SpoolBulkCreate(spool=SpoolCreate(material_id=material), location_id=box)
    with pg_pool.connection() as conn, pytest.raises(BoxFullError), conn.transaction():
        f.spools.bulk_on(conn, draft, actor="ann")
    # Two spools of one create into an empty box: the second finds the first.
    other = _box(pg_pool, "Box B")
    twin = SpoolBulkCreate(spool=SpoolCreate(material_id=material), location_id=other, quantity=2)
    with pg_pool.connection() as conn, pytest.raises(BoxFullError), conn.transaction():
        f.spools.bulk_on(conn, twin, actor="ann")
    # A CSV row with a place.
    before = len(f.spools.list())
    with pytest.raises(ImportRefusedError) as refused:
        import_rows(f, [CsvRow(material="PLA", location="Box A")], dry_run=False, actor="ann")
    assert str(holder) in (refused.value.preview.rows[0].error or "")
    assert len(f.spools.list()) == before
    # A CSV row that moves an existing spool.
    mover = _spool(pg_pool)
    number = next(s.number for s in f.spools.list() if s.id == mover)
    with pytest.raises(ImportRefusedError):
        import_rows(f, [CsvRow(number=str(number), location="Box A")], dry_run=False, actor="ann")
    moved = f.spools.get(mover)
    assert moved is not None and moved.location_id is None


def test_two_concurrent_moves_into_one_box_one_wins(pg_pool: PgPool) -> None:
    """Review Focus 2: the box's row is locked before the occupant is read, so the second
    mover waits for the first's commit and then sees its spool."""
    box = _box(pg_pool)
    spools = [_spool(pg_pool), _spool(pg_pool)]
    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    guard = threading.Lock()

    def go(spool: UUID) -> None:
        try:
            with pg_pool.connection() as conn, conn.transaction():
                barrier.wait(timeout=30)
                LocationStore.move_on(conn, spool, box, actor="ann")
            result = "moved"
        except BoxFullError:
            result = "full"
        with guard:
            outcomes.append(result)

    threads = [threading.Thread(target=go, args=(s,)) for s in spools]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert sorted(outcomes) == ["full", "moved"]
    with pg_pool.connection() as conn:
        rows = conn.execute(
            "SELECT count(*) AS n FROM filament_spools WHERE location_id = %s", (box,)
        ).fetchone()
    assert rows == {"n": 1}


def test_cannot_make_a_place_with_two_spools_a_box(pg_pool: PgPool) -> None:
    drawer = _place(pg_pool, "Drawer 1", "Dry box", 3)
    a, b = _spool(pg_pool), _spool(pg_pool)
    store = LocationStore(pg_pool)
    store.move(a, drawer, actor="ann")
    store.move(b, drawer, actor="ann")
    with pytest.raises(BoxHasManyError):
        store.update(drawer, LocationPatch(capacity=1))
    found = store.get(drawer)
    assert found is not None and found.capacity == 3
    # One spool is fine.
    store.move(b, None, actor="ann")
    changed = store.update(drawer, LocationPatch(capacity=1))
    assert changed is not None and changed.capacity == 1


def test_the_bambuddy_pull_leaves_a_spool_unplaced_when_the_box_is_full(pg_pool: PgPool) -> None:
    """BB moves a linked spool into a box that holds another: the pull logs it, leaves the
    spool where it was and does not fail."""
    f = _filament(pg_pool)
    garage = _place(pg_pool, "Garage", "Dry box", 1)
    holder = _spool(pg_pool)
    f.locations.move(holder, garage, actor="ann")
    (made,) = pull(f, [bb(updated_at=_bb_time(-3))], now=NOW).created_ids
    places = [BbLocation.model_validate({"id": 5, "name": "Garage"})]
    report = pull(
        f, [bb(location_id=5, updated_at=_bb_time(-1))], now=NOW, pulls=ROWS, locations=places
    )
    spool = f.spools.get(made)
    assert spool is not None and spool.location_id is None
    assert report.moved == 0
    assert _row(pg_pool, holder)["location_id"] == garage


def test_a_printer_delete_style_move_to_none_still_works(pg_pool: PgPool) -> None:
    box, spool = _box(pg_pool), _spool(pg_pool)
    store = LocationStore(pg_pool)
    store.move(spool, box, actor="ann")
    with pg_pool.connection() as conn, conn.transaction():
        LocationStore.move_on(conn, spool, None, actor="system")
    assert _row(pg_pool, spool)["location_id"] is None


# -- fix round 1 ------------------------------------------------------------------------


def _kind(pool: PgPool, name: str, *, dry: bool) -> UUID:
    made = CatalogStore(pool).create(
        "location-kinds", LocationKindCreate(name=name, dry_storage=dry)
    )
    assert isinstance(made, LocationKind)
    return made.id


def _dry_flag(pool: PgPool, kind: UUID) -> bool:
    found = next(
        r
        for r in CatalogStore(pool).list("location-kinds")
        if isinstance(r, LocationKind) and r.id == kind
    )
    return found.dry_storage


def test_a_kind_flip_to_dry_storage_is_refused_for_a_place_with_two_spools(
    pg_pool: PgPool,
) -> None:
    kind = _kind(pg_pool, "Cabinet", dry=False)
    cabinet = LocationStore(pg_pool).create(LocationCreate(name="Cab", kind_id=kind, capacity=1)).id
    a, b = _spool(pg_pool), _spool(pg_pool)
    store = LocationStore(pg_pool)
    # A cabinet that is no box can hold two.
    with pg_pool.connection() as conn:
        conn.execute(
            "UPDATE filament_spools SET location_id = %s WHERE id = ANY(%s)", (cabinet, [a, b])
        )
    flip = LocationKindPatch(dry_storage=True)
    with pytest.raises(BoxHasManyError):
        CatalogStore(pg_pool).update("location-kinds", kind, flip)
    assert _dry_flag(pg_pool, kind) is False
    store.move(b, None, actor="ann")
    CatalogStore(pg_pool).update("location-kinds", kind, flip)
    assert _dry_flag(pg_pool, kind) is True
    with pg_pool.connection() as conn:
        assert is_dry_box(conn, cabinet)


def test_a_copy_of_a_boxed_spool_does_not_keep_the_box(pg_pool: PgPool) -> None:
    box, boxed, shelved = _box(pg_pool), _spool(pg_pool), _spool(pg_pool)
    shelf = _place(pg_pool, "Shelf A", "Shelf", None)
    store = LocationStore(pg_pool)
    store.move(boxed, box, actor="ann")
    store.move(shelved, shelf, actor="ann")
    spools = FilamentSpoolStore(pg_pool)
    draft = spools.copy(boxed)
    assert draft is not None and draft.location_id is None
    other = spools.copy(shelved)
    assert other is not None and other.location_id == shelf


def _boxed_pair(pool: PgPool) -> tuple[UUID, UUID, UUID]:
    """An archived spool that keeps its box, and a live spool that now holds it."""
    box, old, new = _box(pool), _spool(pool), _spool(pool)
    store = LocationStore(pool)
    store.move(old, box, actor="ann")
    FilamentSpoolStore(pool).archive(old, True, actor="ann", reason="other")
    store.move(new, box, actor="ann")
    return box, old, new


def test_a_restore_into_a_box_another_spool_holds_is_refused(pg_pool: PgPool) -> None:
    box, old, new = _boxed_pair(pg_pool)
    spools = FilamentSpoolStore(pg_pool)
    with pytest.raises(BoxFullError) as full:
        spools.archive(old, False, actor="ann")
    assert full.value.occupant == new
    with pytest.raises(BoxFullError):
        spools.update(old, SpoolPatch(archived=False), actor="ann")
    with pytest.raises(BoxFullError):
        spools.bulk_archive([old], False, actor="ann")
    kept = spools.get(old)
    assert kept is not None and kept.archived_at is not None and kept.location_id == box


def test_a_restore_into_an_empty_box_keeps_the_box(pg_pool: PgPool) -> None:
    box, old = _box(pg_pool), _spool(pg_pool)
    LocationStore(pg_pool).move(old, box, actor="ann")
    spools = FilamentSpoolStore(pg_pool)
    spools.archive(old, True, actor="ann", reason="other")
    back = spools.archive(old, False, actor="ann")
    assert back is not None and back.archived_at is None and back.location_id == box


def test_the_bambuddy_pull_restores_a_spool_to_no_place_when_its_box_is_taken(
    pg_pool: PgPool,
) -> None:
    f = _filament(pg_pool)
    (made,) = pull(f, [bb(updated_at=_bb_time(-3))], now=NOW).created_ids
    box = _box(pg_pool)
    f.locations.move(made, box, actor="ann")
    pull(f, [bb(archived_at=_bb_time(-2), updated_at=_bb_time(-2))], now=NOW)
    archived = f.spools.get(made)
    assert archived is not None and archived.archived_at is not None
    taker = _spool(pg_pool)
    f.locations.move(taker, box, actor="ann")
    pull(f, [bb(updated_at=_bb_time(1))], now=NOW)
    restored = f.spools.get(made)
    assert restored is not None
    assert restored.archived_at is None
    assert restored.location_id is None
    held = f.spools.get(taker)
    assert held is not None and held.location_id == box


def test_a_tray_place_is_no_box_whatever_its_kind_and_capacity(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    tray = _printer_place(pg_pool, f, "Tray 1", printer=_printer(pg_pool), ams=0, tray=1)
    kinds = CatalogStore(pg_pool).list("location-kinds")
    dry = next(r.id for r in kinds if isinstance(r, LocationKind) and r.name == "Dry box")
    with pg_pool.connection() as conn:
        conn.execute(
            "UPDATE filament_locations SET kind_id = %s, capacity = 1 WHERE id = %s", (dry, tray)
        )
        assert not is_dry_box(conn, tray)
    a, b = _spool(pg_pool), _spool(pg_pool)
    LocationStore(pg_pool).move(a, tray, actor="ann")
    LocationStore(pg_pool).move(b, tray, actor="ann")


def test_the_migration_backfill_skips_a_printer_place(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    tray = _printer_place(pg_pool, f, "Tray 1", printer=_printer(pg_pool), ams=0, tray=1)
    box, on_tray, on_box = _box(pg_pool), _spool(pg_pool), _spool(pg_pool)
    kinds = CatalogStore(pg_pool).list("location-kinds")
    dry = next(r.id for r in kinds if isinstance(r, LocationKind) and r.name == "Dry box")
    store = LocationStore(pg_pool)
    store.move(on_tray, tray, actor="ann")
    store.move(on_box, box, actor="ann")
    with pg_pool.connection() as conn:
        conn.execute(
            "UPDATE filament_locations SET kind_id = %s, capacity = 1 WHERE id = %s", (dry, tray)
        )
        conn.execute("UPDATE filament_spools SET home_location_id = NULL")
        sql = _MIGRATION.read_text()
        conn.execute(sql[sql.index("UPDATE filament_spools s SET home_location_id") :])
    assert _row(pg_pool, on_tray)["home_location_id"] is None
    assert _row(pg_pool, on_box)["home_location_id"] == box


def test_the_bambuddy_pull_keeps_a_shelved_spool_where_it_was_when_the_box_is_full(
    pg_pool: PgPool,
) -> None:
    f = _filament(pg_pool)
    garage = _place(pg_pool, "Garage", "Dry box", 1)
    f.locations.move(_spool(pg_pool), garage, actor="ann")
    (made,) = pull(f, [bb(updated_at=_bb_time(-3))], now=NOW).created_ids
    shelf = _place(pg_pool, "Shelf Z", "Shelf", None)
    f.locations.move(made, shelf, actor="ann")
    places = [BbLocation.model_validate({"id": 5, "name": "Garage"})]
    report = pull(
        f, [bb(location_id=5, updated_at=_bb_time(1))], now=NOW, pulls=ROWS, locations=places
    )
    spool = f.spools.get(made)
    assert spool is not None and spool.location_id == shelf
    assert report.moved == 0
