"""The home return (#7167, Task 4): the spool the person identified goes into the dry box.
With ``replace``, the box's own spool is archived at 0 g and its grams are booked as
non-print waste; the server never picks the spool."""

from __future__ import annotations

from collections.abc import Callable
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
from tests.filament.lock_support import waits_for

pytestmark = pytest.mark.requires_postgres

ASKED = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
#: A spool the caller expected in a box that is empty now (a replace names one).
GONE = UUID(int=0x7167)


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
    expected: UUID | None = None,
    *,
    expected_location_id: UUID | None = None,
    check_location: bool = False,
) -> m.SpoolEvent | None:
    return homes.move(
        FilamentSpoolStore(pool),
        spool,
        place,
        if_occupied=if_occupied,
        answers=answers,
        expected_occupant_id=expected,
        expected_location_id=expected_location_id,
        check_location=check_location,
        actor="ann",
    )


def _idle_only_defaults(pool: PgPool) -> Any:
    """A settings reader that fails when a pool connection is in use: the store's
    ``defaults`` may take a connection of its own, so it must be read before one is held."""
    from scadbuddy.filament.spools import _setting_defaults

    def read() -> Any:
        stats = pool.get_stats()
        assert stats["pool_size"] == stats["pool_available"], "defaults read inside a transaction"
        return _setting_defaults()

    return read


def test_a_replace_reads_the_settings_before_it_takes_a_connection(pg_pool: PgPool) -> None:
    """#8270 review: the replace's archive reads the settings before the move's
    transaction, never on a second connection inside it."""
    box = _place(pg_pool, "Box A", "Dry box", 1)
    old, new = _spool(pg_pool, 80), _spool(pg_pool)
    LocationStore(pg_pool).move(old, box, actor="ann")
    homes.move(
        FilamentSpoolStore(pg_pool, _idle_only_defaults(pg_pool)),
        new,
        box,
        if_occupied="replace",
        expected_occupant_id=old,
        actor="ann",
    )
    assert _row(pg_pool, old)["archived_at"] is not None


def test_replace_archives_the_occupant_and_books_waste(pg_pool: PgPool) -> None:
    box = _place(pg_pool, "Box A", "Dry box", 1)
    old, new = _spool(pg_pool, 80), _spool(pg_pool)
    LocationStore(pg_pool).move(old, box, actor="ann")
    moved = _move(pg_pool, new, box, "replace", expected=old)
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
    assert _move(pg_pool, spool, box, "replace", expected=GONE) is not None
    assert _row(pg_pool, spool)["location_id"] == box
    assert [e.kind for e in FilamentSpoolStore(pg_pool).events(spool)].count("archived") == 0
    assert _kinds(pg_pool, spool, "non_print_waste") == []


def test_replace_on_a_shelf_archives_nothing(pg_pool: PgPool) -> None:
    shelf = _place(pg_pool, "Shelf", "Shelf", None)
    first, second = _spool(pg_pool), _spool(pg_pool)
    LocationStore(pg_pool).move(first, shelf, actor="ann")
    _move(pg_pool, second, shelf, "replace", expected=first)
    assert _row(pg_pool, first)["archived_at"] is None
    assert _row(pg_pool, second)["location_id"] == shelf


def test_identified_spool_is_the_occupant_is_a_noop(pg_pool: PgPool) -> None:
    box = _place(pg_pool, "Box C", "Dry box", 1)
    spool = _spool(pg_pool)
    LocationStore(pg_pool).move(spool, box, actor="ann")
    before = len(FilamentSpoolStore(pg_pool).events(spool))
    assert _move(pg_pool, spool, box, "replace", expected=spool) is None
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
        _move(pg_pool, new, box, "replace", expected=old)
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


def test_replace_after_the_occupant_moved_out_archives_nothing(pg_pool: PgPool) -> None:
    """#7167 review (Important): one transaction moves the box's spool to a shelf and has
    not committed; the replace reads it in the box, waits on its row, and runs after the
    commit. The spool left, so the replace is a plain move into the empty box."""
    box, shelf = _place(pg_pool, "Box M", "Dry box", 1), _place(pg_pool, "Shelf M", "Shelf", None)
    old, new = _spool(pg_pool, 80), _spool(pg_pool)
    LocationStore(pg_pool).move(old, box, actor="ann")

    def move_out(conn: Any) -> None:
        LocationStore.move_on(conn, old, shelf, actor="bob")

    waits_for(pg_pool, move_out, lambda: _move(pg_pool, new, box, "replace", expected=old))
    kept = _row(pg_pool, old)
    assert kept["archived_at"] is None and kept["grams_left"] == 80
    assert kept["location_id"] == shelf
    assert _kinds(pg_pool, old, "non_print_waste") == []
    assert _row(pg_pool, new)["location_id"] == box


def test_replace_refuses_an_occupant_the_person_did_not_see(pg_pool: PgPool) -> None:
    """#7167 Task 6 review (M4): the dialog showed ``old`` in the box, and ``other`` took its
    place before the click. ``replace`` with ``expected_occupant_id=old`` answers
    `BoxFullError` naming ``other`` and writes nothing: ``other`` is not archived."""
    box, shelf = _place(pg_pool, "Box N", "Dry box", 1), _place(pg_pool, "Shelf N", "Shelf", None)
    old, other, new = _spool(pg_pool, 80), _spool(pg_pool, 90), _spool(pg_pool)
    LocationStore(pg_pool).move(old, box, actor="ann")
    LocationStore(pg_pool).move(old, shelf, actor="bob")
    LocationStore(pg_pool).move(other, box, actor="bob")
    with pytest.raises(BoxFullError) as full:
        _move(pg_pool, new, box, "replace", expected=old)
    assert full.value.occupant == other
    assert _row(pg_pool, other)["archived_at"] is None
    assert _row(pg_pool, other)["location_id"] == box
    assert _kinds(pg_pool, other, "non_print_waste") == []
    assert _row(pg_pool, new)["location_id"] is None


def test_replace_of_the_expected_occupant_archives_it(pg_pool: PgPool) -> None:
    box = _place(pg_pool, "Box O", "Dry box", 1)
    old, new = _spool(pg_pool, 80), _spool(pg_pool)
    LocationStore(pg_pool).move(old, box, actor="ann")
    assert _move(pg_pool, new, box, "replace", expected=old) is not None
    assert _row(pg_pool, old)["archived_at"] is not None
    assert _row(pg_pool, new)["location_id"] == box


def test_spool_move_takes_an_expected_occupant() -> None:
    occupant = UUID(int=7)
    body = m.SpoolMove.model_validate({"location_id": None, "expected_occupant_id": str(occupant)})
    assert body.expected_occupant_id == occupant
    assert m.SpoolMove(location_id=None).expected_occupant_id is None


def test_replace_without_an_expected_occupant_is_refused(pg_pool: PgPool) -> None:
    """#7167 fix round 2: ScadBuddy never archives a spool the caller did not name."""
    box = _place(pg_pool, "Box P", "Dry box", 1)
    old, new = _spool(pg_pool, 80), _spool(pg_pool)
    LocationStore(pg_pool).move(old, box, actor="ann")
    with pytest.raises(homes.ExpectedOccupantRequiredError):
        _move(pg_pool, new, box, "replace")
    assert _row(pg_pool, old)["archived_at"] is None
    assert _row(pg_pool, new)["location_id"] is None


# -- the stale refusal (#7168): the move carries where the caller read the spool -------


def _shelf(pool: PgPool, name: str) -> UUID:
    return _place(pool, name, "Shelf", None)


def test_a_move_whose_spool_left_its_place_is_stale(pg_pool: PgPool) -> None:
    shelf_a, shelf_b = _shelf(pg_pool, "Shelf A1"), _shelf(pg_pool, "Shelf B1")
    box = _place(pg_pool, "Box S1", "Dry box", 1)
    spool = _spool(pg_pool)
    LocationStore(pg_pool).move(spool, shelf_a, actor="ann")
    LocationStore(pg_pool).move(spool, shelf_b, actor="bob")
    with pytest.raises(homes.SpoolMovedError) as err:
        _move(pg_pool, spool, box, expected_location_id=shelf_a, check_location=True)
    assert err.value.location_id == shelf_b and err.value.archived is False
    assert _row(pg_pool, spool)["location_id"] == shelf_b


def test_a_spool_read_in_no_place_is_checked_too(pg_pool: PgPool) -> None:
    shelf_b = _shelf(pg_pool, "Shelf B2")
    box = _place(pg_pool, "Box S2", "Dry box", 1)
    spool = _spool(pg_pool)
    LocationStore(pg_pool).move(spool, shelf_b, actor="bob")
    with pytest.raises(homes.SpoolMovedError) as err:
        _move(pg_pool, spool, box, expected_location_id=None, check_location=True)
    assert err.value.location_id == shelf_b and err.value.archived is False
    assert _row(pg_pool, spool)["location_id"] == shelf_b


def test_an_archived_spool_is_stale_though_its_place_is_the_same(pg_pool: PgPool) -> None:
    shelf_a = _shelf(pg_pool, "Shelf A3")
    box = _place(pg_pool, "Box S3", "Dry box", 1)
    spool = _spool(pg_pool)
    LocationStore(pg_pool).move(spool, shelf_a, actor="ann")
    with pg_pool.connection() as conn:
        conn.execute("UPDATE filament_spools SET archived_at = now() WHERE id = %s", (spool,))
    with pytest.raises(homes.SpoolMovedError) as err:
        _move(pg_pool, spool, box, expected_location_id=shelf_a, check_location=True)
    assert err.value.location_id == shelf_a and err.value.archived is True
    assert _row(pg_pool, spool)["location_id"] == shelf_a


def test_a_stale_replace_archives_nothing(pg_pool: PgPool) -> None:
    shelf_a, shelf_b = _shelf(pg_pool, "Shelf A4"), _shelf(pg_pool, "Shelf B4")
    box = _place(pg_pool, "Box S4", "Dry box", 1)
    occupant, mover = _spool(pg_pool, 80), _spool(pg_pool)
    LocationStore(pg_pool).move(occupant, box, actor="ann")
    LocationStore(pg_pool).move(mover, shelf_a, actor="ann")
    LocationStore(pg_pool).move(mover, shelf_b, actor="bob")
    with pytest.raises(homes.SpoolMovedError):
        _move(
            pg_pool,
            mover,
            box,
            "replace",
            expected=occupant,
            expected_location_id=shelf_a,
            check_location=True,
        )
    kept = _row(pg_pool, occupant)
    assert kept["archived_at"] is None and kept["location_id"] == box
    assert _kinds(pg_pool, occupant, "non_print_waste") == []
    assert _row(pg_pool, mover)["location_id"] == shelf_b


def test_no_expected_location_is_todays_move(pg_pool: PgPool) -> None:
    shelf_a, shelf_b = _shelf(pg_pool, "Shelf A5"), _shelf(pg_pool, "Shelf B5")
    box = _place(pg_pool, "Box S5", "Dry box", 1)
    spool = _spool(pg_pool)
    LocationStore(pg_pool).move(spool, shelf_b, actor="bob")
    assert _move(pg_pool, spool, box, expected_location_id=shelf_a) is not None
    assert _row(pg_pool, spool)["location_id"] == box


def test_a_matching_expected_location_moves(pg_pool: PgPool) -> None:
    shelf_a = _shelf(pg_pool, "Shelf A6")
    box = _place(pg_pool, "Box S6", "Dry box", 1)
    spool = _spool(pg_pool)
    LocationStore(pg_pool).move(spool, shelf_a, actor="ann")
    assert _move(pg_pool, spool, box, expected_location_id=shelf_a, check_location=True)
    assert _row(pg_pool, spool)["location_id"] == box


def test_spool_move_takes_an_expected_location() -> None:
    place = UUID(int=9)
    body = m.SpoolMove.model_validate({"location_id": None, "expected_location_id": str(place)})
    assert body.expected_location_id == place
    assert "expected_location_id" in body.model_fields_set
    bare = m.SpoolMove(location_id=None)
    assert bare.expected_location_id is None
    assert "expected_location_id" not in bare.model_fields_set


def _wizard(
    pool: PgPool,
    spool: UUID,
    place: UUID,
    expected: UUID | None,
    if_occupied: m.IfOccupied = "refuse",
    occupant: UUID | None = None,
) -> Callable[[Any], object]:
    """A wizard's move on the caller's connection, in its open transaction. The settings
    are read here, before any connection is taken."""
    spools = FilamentSpoolStore(pool)
    defaults = spools.defaults()

    def run(conn: Any) -> object:
        return homes.move_into(
            conn,
            spool,
            place,
            if_occupied=if_occupied,
            actor="ann",
            spools=spools,
            defaults=defaults,
            expected_occupant_id=occupant,
            expected_location_id=expected,
            check_location=True,
        )

    return run


def _in_own_transaction(pool: PgPool, step: Callable[[Any], object]) -> Callable[[], str]:
    """``step`` in a transaction of its own, its outcome as a word: ``moved``,
    ``stale:<place>:<archived>`` or ``full:<occupant>``."""

    def run() -> str:
        try:
            with pool.connection() as conn, conn.transaction():
                step(conn)
        except homes.SpoolMovedError as exc:
            return f"stale:{exc.location_id}:{exc.archived}"
        except BoxFullError as exc:
            return f"full:{exc.occupant}"
        return "moved"

    return run


def test_two_wizards_on_one_spool_the_second_is_stale(pg_pool: PgPool) -> None:
    """D13: both read S in A. Wizard 1 moves S to B and has not committed; wizard 2's move
    of S to C waits on S's row and, once wizard 1 commits, reads S in B."""
    a, b, c = _shelf(pg_pool, "Shelf A7"), _shelf(pg_pool, "Shelf B7"), _shelf(pg_pool, "C7")
    spool = _spool(pg_pool)
    LocationStore(pg_pool).move(spool, a, actor="ann")
    first = _wizard(pg_pool, spool, b, a)
    second = _in_own_transaction(pg_pool, _wizard(pg_pool, spool, c, a))
    assert waits_for(pg_pool, first, second) == f"stale:{b}:False"
    assert _row(pg_pool, spool)["location_id"] == b


def test_two_wizards_on_one_box_the_second_meets_box_full(pg_pool: PgPool) -> None:
    """D13: box X is empty. Wizard 1 moves S1 in and has not committed; wizard 2's move of
    S2 waits on X's place lock and then finds S1 there."""
    box = _place(pg_pool, "Box X8", "Dry box", 1)
    a1, a2 = _shelf(pg_pool, "Shelf A8"), _shelf(pg_pool, "Shelf B8")
    s1, s2 = _spool(pg_pool), _spool(pg_pool)
    LocationStore(pg_pool).move(s1, a1, actor="ann")
    LocationStore(pg_pool).move(s2, a2, actor="ann")
    first = _wizard(pg_pool, s1, box, a1)
    second = _in_own_transaction(pg_pool, _wizard(pg_pool, s2, box, a2))
    assert waits_for(pg_pool, first, second) == f"full:{s1}"
    assert _row(pg_pool, s1)["location_id"] == box
    assert _row(pg_pool, s2)["location_id"] == a2


def test_a_replace_and_a_move_of_the_occupant_race(pg_pool: PgPool) -> None:
    """D13: box X holds O. Wizard 1 replaces it with S and has not committed (O is
    archived); wizard 2's move of O, read in X, waits on O's row and is stale, archived."""
    box = _place(pg_pool, "Box X9", "Dry box", 1)
    a, shelf = _shelf(pg_pool, "Shelf A9"), _shelf(pg_pool, "Shelf B9")
    occupant, spool = _spool(pg_pool, 80), _spool(pg_pool)
    LocationStore(pg_pool).move(occupant, box, actor="ann")
    LocationStore(pg_pool).move(spool, a, actor="ann")
    first = _wizard(pg_pool, spool, box, a, "replace", occupant)
    second = _in_own_transaction(pg_pool, _wizard(pg_pool, occupant, shelf, box))
    # An archive keeps the spool's place, so the stale report names the box.
    assert waits_for(pg_pool, first, second) == f"stale:{box}:True"
    assert _row(pg_pool, occupant)["archived_at"] is not None
    assert _row(pg_pool, spool)["location_id"] == box
