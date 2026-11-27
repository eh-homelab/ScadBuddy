"""The home return (#7167, Task 4): the spool the person identified goes into the dry box.
With ``replace``, the box's own spool is archived at 0 g and its grams are booked as
non-print waste; the server never picks the spool."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from scadbuddy.filament import homes
from scadbuddy.filament import models as m
from scadbuddy.filament.catalog import CatalogStore
from scadbuddy.filament.dry_box import BoxFullError
from scadbuddy.filament.locations import LocationStore
from scadbuddy.filament.spools import FilamentSpoolStore
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_postgres

ASKED = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


def _place(pool: PgPool, name: str, kind: str, capacity: int | None) -> UUID:
    kinds = CatalogStore(pool).list("location-kinds")
    kind_id = next(r.id for r in kinds if isinstance(r, m.LocationKind) and r.name == kind)
    made = LocationStore(pool).create(
        m.LocationCreate(name=name, kind_id=kind_id, capacity=capacity)
    )
    return made.id


def _spool(pool: PgPool, grams: float = 100) -> UUID:
    material = next(
        r.id
        for r in CatalogStore(pool).list("materials")
        if isinstance(r, m.Material) and r.name == "PLA"
    )
    body = m.SpoolCreate.model_validate(
        {"material_id": material, "label_weight_g": 1000, "grams_left": grams}
    )
    return FilamentSpoolStore(pool).create(body, actor="ann").id


def _row(pool: PgPool, spool: UUID) -> dict[str, Any]:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT location_id, home_location_id, home_asked_at, archived_at, grams_left"
            " FROM filament_spools WHERE id = %s",
            (spool,),
        ).fetchone()
    assert row is not None
    return dict(row)


def _ask(pool: PgPool, spool: UUID) -> None:
    """The spool came out of a tray and is asked about its home (as capture parks it)."""
    with pool.connection() as conn:
        conn.execute(
            "UPDATE filament_spools SET location_id = NULL, home_asked_at = %s WHERE id = %s",
            (ASKED, spool),
        )


def _kinds(pool: PgPool, spool: UUID, kind: str) -> list[m.SpoolEvent]:
    return [e for e in FilamentSpoolStore(pool).events(spool) if e.kind == kind]


def _move(
    pool: PgPool,
    spool: UUID,
    place: UUID | None,
    if_occupied: m.IfOccupied = "refuse",
    answers: UUID | None = None,
) -> m.SpoolEvent | None:
    return homes.move(
        FilamentSpoolStore(pool),
        spool,
        place,
        if_occupied=if_occupied,
        answers=answers,
        actor="ann",
    )


def test_replace_archives_the_occupant_and_books_waste(pg_pool: PgPool) -> None:
    box = _place(pg_pool, "Box A", "Dry box", 1)
    old, new = _spool(pg_pool, 80), _spool(pg_pool)
    LocationStore(pg_pool).move(old, box, actor="ann")
    moved = _move(pg_pool, new, box, "replace")
    assert moved is not None and moved.data["to"] == str(box)
    gone = _row(pg_pool, old)
    assert gone["archived_at"] is not None and gone["grams_left"] == 0
    [waste] = _kinds(pg_pool, old, "non_print_waste")
    assert waste.data["cause"] == "replaced" and waste.data["waste_g"] == 80
    assert _row(pg_pool, new)["location_id"] == box
    assert _row(pg_pool, new)["home_location_id"] == box


def test_replace_with_empty_box_is_a_plain_move(pg_pool: PgPool) -> None:
    box = _place(pg_pool, "Box B", "Dry box", 1)
    spool = _spool(pg_pool)
    assert _move(pg_pool, spool, box, "replace") is not None
    assert _row(pg_pool, spool)["location_id"] == box
    assert [e.kind for e in FilamentSpoolStore(pg_pool).events(spool)].count("archived") == 0
    assert _kinds(pg_pool, spool, "non_print_waste") == []


def test_replace_on_a_shelf_archives_nothing(pg_pool: PgPool) -> None:
    shelf = _place(pg_pool, "Shelf", "Shelf", None)
    first, second = _spool(pg_pool), _spool(pg_pool)
    LocationStore(pg_pool).move(first, shelf, actor="ann")
    _move(pg_pool, second, shelf, "replace")
    assert _row(pg_pool, first)["archived_at"] is None
    assert _row(pg_pool, second)["location_id"] == shelf


def test_identified_spool_is_the_occupant_is_a_noop(pg_pool: PgPool) -> None:
    box = _place(pg_pool, "Box C", "Dry box", 1)
    spool = _spool(pg_pool)
    LocationStore(pg_pool).move(spool, box, actor="ann")
    before = len(FilamentSpoolStore(pg_pool).events(spool))
    assert _move(pg_pool, spool, box, "replace") is None
    assert _row(pg_pool, spool)["archived_at"] is None
    assert _row(pg_pool, spool)["location_id"] == box
    assert len(FilamentSpoolStore(pg_pool).events(spool)) == before


def test_refuse_is_the_default_and_answers_box_full(pg_pool: PgPool) -> None:
    assert m.SpoolMove(location_id=None).if_occupied == "refuse"
    box = _place(pg_pool, "Box D", "Dry box", 1)
    old, new = _spool(pg_pool), _spool(pg_pool)
    LocationStore(pg_pool).move(old, box, actor="ann")
    with pytest.raises(BoxFullError) as full:
        _move(pg_pool, new, box)
    assert full.value.occupant == old
    assert _row(pg_pool, old)["archived_at"] is None
    assert _row(pg_pool, new)["location_id"] is None


def test_replace_is_atomic(pg_pool: PgPool, monkeypatch: pytest.MonkeyPatch) -> None:
    box = _place(pg_pool, "Box E", "Dry box", 1)
    old, new = _spool(pg_pool, 80), _spool(pg_pool)
    LocationStore(pg_pool).move(old, box, actor="ann")

    def broken(*_: object, **__: object) -> None:
        raise RuntimeError("the move failed")

    monkeypatch.setattr(LocationStore, "move_on", staticmethod(broken))
    with pytest.raises(RuntimeError):
        _move(pg_pool, new, box, "replace")
    kept = _row(pg_pool, old)
    assert kept["archived_at"] is None and kept["grams_left"] == 80
    assert kept["location_id"] == box
    assert _kinds(pg_pool, old, "non_print_waste") == []


def test_answering_with_another_spool_clears_the_unloaded_spools_prompt(
    pg_pool: PgPool,
) -> None:
    box = _place(pg_pool, "Box F", "Dry box", 1)
    unloaded, other = _spool(pg_pool), _spool(pg_pool)
    LocationStore(pg_pool).move(unloaded, box, actor="ann")
    _ask(pg_pool, unloaded)
    _move(pg_pool, other, box, "refuse", answers=unloaded)
    asked = _row(pg_pool, unloaded)
    assert asked["home_asked_at"] is None and asked["location_id"] is None
    assert asked["archived_at"] is None
    assert _row(pg_pool, other)["location_id"] == box
    awaiting = FilamentSpoolStore(pg_pool).list(awaiting_home=True)
    assert [s.id for s in awaiting] == []


def test_answering_with_the_unloaded_spool_puts_it_home(pg_pool: PgPool) -> None:
    box = _place(pg_pool, "Box G", "Dry box", 1)
    spool = _spool(pg_pool)
    LocationStore(pg_pool).move(spool, box, actor="ann")
    _ask(pg_pool, spool)
    _move(pg_pool, spool, box, "refuse", answers=spool)
    row = _row(pg_pool, spool)
    assert row["location_id"] == box and row["home_asked_at"] is None


def test_answers_names_a_spool_whose_home_is_another_place(pg_pool: PgPool) -> None:
    box, other_box = _place(pg_pool, "Box H", "Dry box", 1), _place(pg_pool, "Box I", "Dry box", 1)
    unloaded, other = _spool(pg_pool), _spool(pg_pool)
    LocationStore(pg_pool).move(unloaded, box, actor="ann")
    _ask(pg_pool, unloaded)
    with pytest.raises(homes.NotItsHomeError):
        _move(pg_pool, other, other_box, "refuse", answers=unloaded)
    assert _row(pg_pool, unloaded)["home_asked_at"] == ASKED
    assert _row(pg_pool, other)["location_id"] is None


def test_answers_names_no_spool(pg_pool: PgPool) -> None:
    box = _place(pg_pool, "Box J", "Dry box", 1)
    spool = _spool(pg_pool)
    missing = UUID(int=7)
    with pytest.raises(LookupError, match=str(missing)):
        _move(pg_pool, spool, box, "refuse", answers=missing)
    assert _row(pg_pool, spool)["location_id"] is None


def test_awaiting_home_lists_only_asked_spools_with_a_live_box(pg_pool: PgPool) -> None:
    box, gone_box = _place(pg_pool, "Box K", "Dry box", 1), _place(pg_pool, "Box L", "Dry box", 1)
    asked, plain, orphan = _spool(pg_pool), _spool(pg_pool), _spool(pg_pool)
    LocationStore(pg_pool).move(asked, box, actor="ann")
    LocationStore(pg_pool).move(orphan, gone_box, actor="ann")
    _ask(pg_pool, asked)
    _ask(pg_pool, orphan)
    LocationStore(pg_pool).update(gone_box, m.LocationPatch(archived=True))
    store = FilamentSpoolStore(pg_pool)
    assert [s.id for s in store.list(awaiting_home=True)] == [asked]
    rest = {s.id for s in store.list(awaiting_home=False)}
    assert asked not in rest and {plain, orphan} <= rest
