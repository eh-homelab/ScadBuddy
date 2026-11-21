"""The pull of Bambuddy's K values, suppliers and locations (#6265 Task 9): BB's
K-profiles become a linked spool's own K values, BB's suppliers and locations become SB's
rows, and a linked spool follows BB's location, never out of a printer's place."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
import respx
from psycopg.types.json import Jsonb

from scadbuddy.bambuddy.models import BbLocation, BbSupplier
from scadbuddy.core.settings import FilamentSync
from scadbuddy.filament.bambuddy_pull import Pulls, pull
from scadbuddy.filament.bb_sync_plan import UNSEEN_KEY, field_hash
from scadbuddy.filament.component import Filament
from scadbuddy.filament.models import SpoolKValueCreate
from scadbuddy.filament.purchasing_models import SupplierCreate
from scadbuddy.library.settings_store import StoredSettings
from tests.conftest import PgPool
from tests.filament.pull_support import read_and_pull
from tests.filament.test_bambuddy_pull import (
    BB,
    NOW,
    _filament,
    _forget_seen,
    _link_seen,
    bb,
)
from tests.filament.test_bb_sync_plan import _kind, _place, _plan, _printer, _put, _snap

pytestmark = pytest.mark.requires_postgres

#: Suppliers and locations pull; the rest keep their defaults.
ROWS = Pulls.of(FilamentSync(suppliers="bb_to_sb", locations="bb_to_sb"))


def _bb_time(hours: float) -> str:
    """A Bambuddy time (naive UTC) ``hours`` from now."""
    return (datetime.now(UTC) + timedelta(hours=hours)).replace(tzinfo=None).isoformat()


def _k(printer: int, k: float, *, nozzle: str = "0.4", extruder: int = 0) -> dict[str, Any]:
    return {"printer_id": printer, "extruder": extruder, "nozzle_diameter": nozzle, "k_value": k}


def _own_k(pool: PgPool, spool: UUID) -> list[tuple[UUID, int, str, Decimal]]:
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT printer_id, extruder, nozzle_diameter, k_value FROM filament_spool_k_values"
            " WHERE spool_id = %s ORDER BY extruder, nozzle_diameter",
            (spool,),
        ).fetchall()
    return [(r["printer_id"], r["extruder"], r["nozzle_diameter"], r["k_value"]) for r in rows]


def _row(pool: PgPool, table: str, column: str, value: Any) -> dict[str, Any] | None:
    with pool.connection() as conn:
        row = conn.execute(f"SELECT * FROM {table} WHERE {column} = %s", (value,)).fetchone()
    return None if row is None else dict(row)


# -- K values ----------------------------------------------------------------------------


def test_bb_k_profiles_become_sb_k_values(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    h2c = _printer(pg_pool, bb_id=1)
    first = bb(k_profiles=[_k(1, 0.02), _k(99, 0.05)], updated_at=_bb_time(-2))
    (made,) = pull(f, [first], now=NOW).created_ids
    # A printer no SB printer links (99) is left out.
    assert _own_k(pg_pool, made) == [(h2c, 0, "0.4", Decimal("0.0200"))]
    link = _row(pg_pool, "filament_bb_links", "spool_id", made)
    assert link is not None
    assert [r["k_value"] for r in link["bb_k_profiles"]] == [0.02, 0.05]
    changed = bb(k_profiles=[_k(1, 0.025), _k(1, 0.03, nozzle="0.6")], updated_at=_bb_time(-1))
    report = pull(f, [changed], now=NOW)
    assert report.updated_ids == [made]
    assert _own_k(pg_pool, made) == [
        (h2c, 0, "0.4", Decimal("0.0250")),
        (h2c, 0, "0.6", Decimal("0.0300")),
    ]
    # With presets off nothing moves.
    cleared = bb(k_profiles=[], updated_at=_bb_time(-0.5))
    pull(f, [cleared], now=NOW, pulls=Pulls(presets=False))
    assert len(_own_k(pg_pool, made)) == 2
    # A clear in Bambuddy deletes the rows that still hold its last values.
    pull(f, [cleared], now=NOW)
    assert _own_k(pg_pool, made) == []


def test_a_persons_k_value_is_kept(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    h2c = _printer(pg_pool, bb_id=1)
    (made,) = pull(f, [bb(k_profiles=[_k(1, 0.02)], updated_at=_bb_time(-2))], now=NOW).created_ids
    f.spools.set_k_values(
        made,
        [
            SpoolKValueCreate(
                printer_id=h2c, extruder=0, nozzle_diameter="0.4", k_value=Decimal("0.031")
            )
        ],
        actor="ann",
    )
    moved = bb(k_profiles=[_k(1, 0.025), _k(1, 0.04, extruder=1)], updated_at=_bb_time(-1))
    pull(f, [moved], now=NOW)
    # The person's row stays; a key the spool has no row for takes Bambuddy's.
    assert _own_k(pg_pool, made) == [
        (h2c, 0, "0.4", Decimal("0.0310")),
        (h2c, 1, "0.4", Decimal("0.0400")),
    ]


# -- suppliers ---------------------------------------------------------------------------


def test_a_bb_supplier_links_by_name(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    amazon = f.suppliers.create(SupplierCreate(name="Amazon", website="https://amazon.com"))
    suppliers = [
        {"id": 3, "name": "AMAZON", "website": None, "updated_at": "2026-01-01T00:00:00"},
        {"id": 4, "name": "Micro Center", "customer_number": "C-1", "updated_at": None},
    ]
    spool = bb(suppliers=[{"supplier_id": 4, "quoted_price_per_kg": 19.99}])
    report = pull(f, [spool], now=NOW, pulls=ROWS, suppliers=_suppliers(suppliers))
    linked = _row(pg_pool, "filament_suppliers", "id", amazon.id)
    assert linked is not None
    # Linked by its upper case name; SB's later values stay.
    assert (linked["bb_supplier_id"], linked["name"], linked["website"]) == (
        3,
        "Amazon",
        "https://amazon.com",
    )
    made = _row(pg_pool, "filament_suppliers", "bb_supplier_id", 4)
    assert made is not None
    assert (made["name"], made["customer_number"]) == ("Micro Center", "C-1")
    assert made["bb_seen"] == {
        "name": "Micro Center",
        "website": None,
        "customer_number": "C-1",
        "note": None,
    }
    assert report.suppliers == 2
    # A spool's supplier links and prices are never pulled (Decision 15).
    with pg_pool.connection() as conn:
        products = conn.execute("SELECT count(*) AS n FROM filament_supplier_products").fetchone()
    assert products == {"n": 0}
    # A linked supplier takes Bambuddy's later change.
    later = [suppliers[0] | {"note": "prime", "updated_at": _bb_time(1)}, suppliers[1]]
    pull(f, [spool], now=NOW, pulls=ROWS, suppliers=_suppliers(later))
    again = _row(pg_pool, "filament_suppliers", "id", amazon.id)
    assert again is not None
    assert (again["note"], again["name"]) == ("prime", "Amazon")


def _suppliers(rows: list[dict[str, Any]]) -> list[BbSupplier]:
    return [BbSupplier.model_validate(r) for r in rows]


def _locations(rows: list[dict[str, Any]]) -> list[BbLocation]:
    return [BbLocation.model_validate(r) for r in rows]


# -- locations ---------------------------------------------------------------------------


def test_a_bb_location_becomes_a_root_shelf(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    (made,) = pull(f, [bb(updated_at=_bb_time(-3))], now=NOW).created_ids
    garage = _locations([{"id": 5, "name": "Garage", "identifier": "G1"}])
    moved = bb(location_id=5, updated_at=_bb_time(-1))
    report = pull(f, [moved], now=NOW, pulls=ROWS, locations=garage)
    place = _row(pg_pool, "filament_locations", "bb_location_id", 5)
    assert place is not None
    assert (place["name"], place["identifier"], place["parent_id"]) == ("Garage", "G1", None)
    assert place["kind_id"] == _kind(f, "Shelf")
    assert report.locations == 1
    # The linked spool follows Bambuddy's place, as Bambuddy's move.
    spool = f.spools.get(made)
    assert spool is not None and spool.location_id == place["id"]
    (event,) = [e for e in f.spools.events(made) if e.kind == "moved"]
    assert event.actor == "bambuddy"
    assert report.moved == 1


def test_an_unlinked_root_location_of_the_name_is_linked(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    shelf = _place(pg_pool, f, "Garage")
    pull(f, [], now=NOW, pulls=ROWS, locations=_locations([{"id": 5, "name": "GARAGE"}]))
    linked = _row(pg_pool, "filament_locations", "id", shelf)
    assert linked is not None and linked["bb_location_id"] == 5
    with pg_pool.connection() as conn:
        count = conn.execute("SELECT count(*) AS n FROM filament_locations").fetchone()
    assert count == {"n": 1}


@pytest.mark.parametrize("tray", [1, 255])
def test_a_spool_in_a_printer_place_is_not_moved_by_bambuddy(pg_pool: PgPool, tray: int) -> None:
    """F18: an AMS tray (1) and the external spool place (255) are never left by a BB
    move."""
    f = _filament(pg_pool)
    printer = _printer(pg_pool, bb_id=1)
    if tray == 255:
        place = _place(pg_pool, f, "External", printer=printer, ams=255, tray=0)
    else:
        place = _place(pg_pool, f, "AMS A1", printer=printer, ams=0, tray=tray)
    (made,) = pull(f, [bb(updated_at=_bb_time(-3))], now=NOW).created_ids
    _put(pg_pool, made, place)
    garage = _locations([{"id": 5, "name": "Garage"}])
    moved = bb(location_id=5, updated_at=_bb_time(1))
    report = pull(f, [moved], now=NOW, pulls=ROWS, locations=garage)
    spool = f.spools.get(made)
    assert spool is not None and spool.location_id == place
    assert report.moved == 0


def test_a_move_in_sb_after_bbs_is_kept(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    shelf = _place(pg_pool, f, "Shelf A")
    (made,) = pull(f, [bb(updated_at=_bb_time(-3))], now=NOW).created_ids
    f.locations.move(made, shelf, actor="ann")
    garage = _locations([{"id": 5, "name": "Garage"}])
    pull(f, [bb(location_id=5, updated_at=_bb_time(-1))], now=NOW, pulls=ROWS, locations=garage)
    spool = f.spools.get(made)
    assert spool is not None and spool.location_id == shelf


def test_a_spool_the_pull_makes_takes_bbs_place(pg_pool: PgPool) -> None:
    """A spool the pull makes is placed where Bambuddy has it: a creation, not a move of
    an SB spool, so R-T9-3 does not hold it back."""
    f = _filament(pg_pool)
    garage = _locations([{"id": 5, "name": "Garage"}])
    report = pull(f, [bb(location_id=5)], now=NOW, pulls=ROWS, locations=garage)
    place = _row(pg_pool, "filament_locations", "bb_location_id", 5)
    (made,) = report.created_ids
    spool = f.spools.get(made)
    assert place is not None and spool is not None and spool.location_id == place["id"]
    assert _link_seen(pg_pool, made)["location_id"] == 5


def _unseen_place(pg_pool: PgPool, f: Filament) -> tuple[UUID, UUID]:
    """A linked spool on an SB shelf whose link has no seen location (a legacy link)."""
    shelf = _place(pg_pool, f, "Shelf A")
    (made,) = pull(f, [bb(updated_at=_bb_time(-3))], now=NOW).created_ids
    _put(pg_pool, made, shelf)
    _forget_seen(pg_pool, made, "location_id")
    return made, shelf


def test_a_link_with_no_seen_location_is_not_moved_and_is_seeded(pg_pool: PgPool) -> None:
    """R-T9-3: the first pull of a link with no seen location never moves the SB spool.
    It seeds the seen location with Bambuddy's."""
    f = _filament(pg_pool)
    made, shelf = _unseen_place(pg_pool, f)
    garage = _locations([{"id": 5, "name": "Garage"}])
    moved = bb(location_id=5, updated_at=_bb_time(1))
    report = pull(f, [moved], now=NOW, pulls=ROWS, locations=garage)
    spool = f.spools.get(made)
    assert spool is not None and spool.location_id == shelf
    assert report.moved == 0
    assert _link_seen(pg_pool, made)["location_id"] == 5


def test_a_bb_move_after_the_seeded_place_moves_the_spool(pg_pool: PgPool) -> None:
    """R-T9-3: once seeded, Bambuddy's next move is a real move off the seen place."""
    f = _filament(pg_pool)
    made, _ = _unseen_place(pg_pool, f)
    places = _locations([{"id": 5, "name": "Garage"}, {"id": 6, "name": "Attic"}])
    pull(f, [bb(location_id=5, updated_at=_bb_time(1))], now=NOW, pulls=ROWS, locations=places)
    moved = bb(location_id=6, updated_at=_bb_time(2))
    report = pull(f, [moved], now=NOW, pulls=ROWS, locations=places)
    attic = _row(pg_pool, "filament_locations", "bb_location_id", 6)
    spool = f.spools.get(made)
    assert attic is not None and spool is not None and spool.location_id == attic["id"]
    assert report.moved == 1


def test_an_unmapped_bb_place_is_not_seeded(pg_pool: PgPool) -> None:
    """R-T9-3 (decision 164): a seen location is seeded only when BB's place maps to an SB
    location; the pull that cannot map it leaves it out."""
    f = _filament(pg_pool)
    made, shelf = _unseen_place(pg_pool, f)
    report = pull(f, [bb(location_id=5, updated_at=_bb_time(1))], now=NOW, pulls=ROWS)
    spool = f.spools.get(made)
    assert spool is not None and spool.location_id == shelf
    assert report.moved == 0
    assert "location_id" not in _link_seen(pg_pool, made)


def test_a_place_that_maps_later_is_seeded_then(pg_pool: PgPool) -> None:
    """R-T9-3 (decision 164): BB's place changed while the place was unmapped; the pull
    that maps it seeds BB's value then, and does not move the spool."""
    f = _filament(pg_pool)
    made, shelf = _unseen_place(pg_pool, f)
    pull(f, [bb(location_id=5, updated_at=_bb_time(1))], now=NOW, pulls=ROWS)
    places = _locations([{"id": 5, "name": "Garage"}, {"id": 6, "name": "Attic"}])
    moved = bb(location_id=6, updated_at=_bb_time(2))
    report = pull(f, [moved], now=NOW, pulls=ROWS, locations=places)
    spool = f.spools.get(made)
    assert spool is not None and spool.location_id == shelf
    assert report.moved == 0
    assert _link_seen(pg_pool, made)["location_id"] == 6


def _map_place(pg_pool: PgPool, place: UUID, bb_id: int, *, archived: bool = False) -> None:
    with pg_pool.connection() as conn:
        conn.execute(
            "UPDATE filament_locations SET bb_location_id = %s,"
            " archived_at = CASE WHEN %s THEN now() END WHERE id = %s",
            (bb_id, archived, place),
        )


def test_a_place_mapping_only_to_an_archived_location_is_not_seeded(pg_pool: PgPool) -> None:
    """R-T9-3 (decision 164): an archived SB location is no mapping, so no seed."""
    f = _filament(pg_pool)
    made, _ = _unseen_place(pg_pool, f)
    _map_place(pg_pool, _place(pg_pool, f, "Old shelf"), 5, archived=True)
    report = pull(f, [bb(location_id=5, updated_at=_bb_time(1))], now=NOW, pulls=ROWS)
    assert report.moved == 0
    assert "location_id" not in _link_seen(pg_pool, made)


def test_a_place_mapping_only_to_a_printers_place_is_not_seeded(pg_pool: PgPool) -> None:
    """R-T9-3 (decision 164): a printer's place is no mapping, so no seed."""
    f = _filament(pg_pool)
    made, _ = _unseen_place(pg_pool, f)
    tray = _place(pg_pool, f, "Tray", printer=_printer(pg_pool), ams=0, tray=0)
    _map_place(pg_pool, tray, 5)
    report = pull(f, [bb(location_id=5, updated_at=_bb_time(1))], now=NOW, pulls=ROWS)
    assert report.moved == 0
    assert "location_id" not in _link_seen(pg_pool, made)


def test_an_old_unmapped_seed_is_replaced_once_it_maps(pg_pool: PgPool) -> None:
    """Decision 171: a link seeded (#6265) with a place that does not map holds no seed. BB's
    move to a mapped place seeds it fresh and moves nothing; the next move moves the spool."""
    f = _filament(pg_pool)
    made, shelf = _unseen_place(pg_pool, f)
    with pg_pool.connection() as conn:
        conn.execute(
            "UPDATE filament_bb_links SET bb_seen = bb_seen || '{\"location_id\": 5}'"
            " WHERE spool_id = %s",
            (made,),
        )
    places = _locations([{"id": 6, "name": "Attic"}, {"id": 7, "name": "Cellar"}])
    first = pull(
        f, [bb(location_id=6, updated_at=_bb_time(1))], now=NOW, pulls=ROWS, locations=places
    )
    spool = f.spools.get(made)
    assert spool is not None and spool.location_id == shelf
    assert first.moved == 0
    assert _link_seen(pg_pool, made)["location_id"] == 6
    report = pull(f, [bb(location_id=7, updated_at=_bb_time(2))], now=NOW, pulls=ROWS)
    assert report.moved == 1


def test_dropping_an_unmapped_seed_moves_bb_seen_at(pg_pool: PgPool) -> None:
    """Decision 171 with review M2: ``bb_seen_at`` moves whenever ``bb_seen`` changes, and
    dropping a never-mapped seed changes it, so a running push pass sees the newer stamp."""
    f = _filament(pg_pool)
    made, _ = _unseen_place(pg_pool, f)
    with pg_pool.connection() as conn:
        conn.execute(
            "UPDATE filament_bb_links SET bb_seen = bb_seen || '{\"location_id\": 5}',"
            " bb_seen_at = %s WHERE spool_id = %s",
            (NOW - timedelta(days=1), made),
        )
    pull(f, [bb(location_id=5, updated_at=_bb_time(1))], now=NOW, pulls=ROWS)
    with pg_pool.connection() as conn:
        row = conn.execute(
            "SELECT bb_seen, bb_seen_at FROM filament_bb_links WHERE spool_id = %s", (made,)
        ).fetchone()
    assert row is not None
    assert "location_id" not in row["bb_seen"]
    assert row["bb_seen_at"] == NOW


def test_a_seed_of_a_place_archived_since_still_counts(pg_pool: PgPool) -> None:
    """Decision 171: a seed of a place SB once mapped stays a real seen value after its SB
    location is archived, so Bambuddy's move to a mapped place still moves the spool."""
    f = _filament(pg_pool)
    made, shelf = _unseen_place(pg_pool, f)
    places = _locations([{"id": 5, "name": "Garage"}, {"id": 6, "name": "Attic"}])
    pull(f, [bb(location_id=5, updated_at=_bb_time(1))], now=NOW, pulls=ROWS, locations=places)
    assert _link_seen(pg_pool, made)["location_id"] == 5
    with pg_pool.connection() as conn:
        conn.execute("UPDATE filament_locations SET archived_at = now() WHERE bb_location_id = 5")
    kept = pull(f, [bb(location_id=5, updated_at=_bb_time(1))], now=NOW, pulls=ROWS)
    assert kept.moved == 0
    assert _link_seen(pg_pool, made)["location_id"] == 5
    report = pull(f, [bb(location_id=6, updated_at=_bb_time(2))], now=NOW, pulls=ROWS)
    attic = _row(pg_pool, "filament_locations", "bb_location_id", 6)
    spool = f.spools.get(made)
    assert attic is not None and spool is not None and spool.location_id == attic["id"]
    assert spool.location_id != shelf
    assert report.moved == 1


def test_a_bb_move_after_a_late_mapped_seed_moves_the_spool(pg_pool: PgPool) -> None:
    """R-T9-3 (decision 164): after the late seed, Bambuddy's next move moves the spool."""
    f = _filament(pg_pool)
    made, _ = _unseen_place(pg_pool, f)
    pull(f, [bb(location_id=5, updated_at=_bb_time(1))], now=NOW, pulls=ROWS)
    places = _locations([{"id": 5, "name": "Garage"}, {"id": 6, "name": "Attic"}])
    seeding = pull(
        f, [bb(location_id=6, updated_at=_bb_time(2))], now=NOW, pulls=ROWS, locations=places
    )
    assert seeding.moved == 0
    report = pull(f, [bb(location_id=5, updated_at=_bb_time(3))], now=NOW, pulls=ROWS)
    garage = _row(pg_pool, "filament_locations", "bb_location_id", 5)
    spool = f.spools.get(made)
    assert garage is not None and spool is not None and spool.location_id == garage["id"]
    assert report.moved == 1


def test_a_bb_cleared_location_takes_the_spool_out_of_a_shelf(pg_pool: PgPool) -> None:
    """R-T9-4: Bambuddy clears a place SB saw, so the SB spool leaves its shelf."""
    f = _filament(pg_pool)
    (made,) = pull(f, [bb(updated_at=_bb_time(-3))], now=NOW).created_ids
    garage = _locations([{"id": 5, "name": "Garage"}])
    pull(f, [bb(location_id=5, updated_at=_bb_time(-2))], now=NOW, pulls=ROWS, locations=garage)
    spool = f.spools.get(made)
    assert spool is not None and spool.location_id is not None
    assert _link_seen(pg_pool, made)["location_id"] == 5
    # After the pull's own move (its event is at the database's now).
    cleared = bb(location_id=None, updated_at=_bb_time(1))
    report = pull(f, [cleared], now=NOW, pulls=ROWS, locations=garage)
    spool = f.spools.get(made)
    assert spool is not None and spool.location_id is None
    assert report.moved == 1
    assert _link_seen(pg_pool, made)["location_id"] is None


def test_a_bb_cleared_location_leaves_a_printer_place_alone(pg_pool: PgPool) -> None:
    """R-T9-4: a place cleared in Bambuddy never takes the SB spool out of a tray."""
    f = _filament(pg_pool)
    printer = _printer(pg_pool, bb_id=1)
    tray = _place(pg_pool, f, "AMS A1", printer=printer, ams=0, tray=1)
    (made,) = pull(f, [bb(updated_at=_bb_time(-3))], now=NOW).created_ids
    garage = _locations([{"id": 5, "name": "Garage"}])
    pull(f, [bb(location_id=5, updated_at=_bb_time(-2))], now=NOW, pulls=ROWS, locations=garage)
    _put(pg_pool, made, tray)
    cleared = bb(location_id=None, updated_at=_bb_time(1))
    report = pull(f, [cleared], now=NOW, pulls=ROWS, locations=garage)
    spool = f.spools.get(made)
    assert spool is not None and spool.location_id == tray
    assert report.moved == 0


async def test_suppliers_and_locations_pull_while_spools_is_off(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    settings = StoredSettings(bambuddy_url=BB, bambuddy_api_key="k")
    only_rows = Pulls.of(
        FilamentSync(
            spools="off",
            weights="off",
            tags="off",
            presets="off",
            suppliers="bb_to_sb",
            locations="two_way",
        )
    )
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{BB}/api/v1/inventory/spools").respond(json=[bb().model_dump(mode="json")])
        router.get(f"{BB}/api/v1/inventory/suppliers").respond(json=[{"id": 4, "name": "Shop"}])
        router.get(f"{BB}/api/v1/inventory/locations").respond(json=[{"id": 5, "name": "Bin"}])
        report = await read_and_pull(f, settings, pulls=only_rows)
    assert (report.created, report.suppliers, report.locations) == (0, 1, 1)
    assert f.spools.list() == []
    assert _row(pg_pool, "filament_suppliers", "bb_supplier_id", 4) is not None
    assert _row(pg_pool, "filament_locations", "bb_location_id", 5) is not None


async def test_suppliers_off_reads_no_suppliers(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    settings = StoredSettings(bambuddy_url=BB, bambuddy_api_key="k")
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{BB}/api/v1/inventory/spools").respond(json=[])
        router.get(f"{BB}/api/v1/inventory/catalog").respond(json=[])
        suppliers = router.get(f"{BB}/api/v1/inventory/suppliers").respond(json=[])
        locations = router.get(f"{BB}/api/v1/inventory/locations").respond(json=[])
        await read_and_pull(f, settings, pulls=Pulls.of(FilamentSync()))
        assert not suppliers.called
        assert not locations.called


# -- the seen copy and conflicts (R-T4-28, R-T4-33) ---------------------------------------


def _seeded_location(
    pool: PgPool, f: Any, *, identifier: str, seen: dict[str, Any], marks: dict[str, Any]
) -> UUID:
    place = _place(pool, f, "Shelf A")
    with pool.connection() as conn:
        conn.execute(
            "UPDATE filament_locations SET identifier = %s, bb_location_id = 5, bb_seen = %s,"
            " bb_seen_at = now(), bb_pushed = %s WHERE id = %s",
            (identifier, Jsonb(seen), Jsonb({UNSEEN_KEY: marks} if marks else {}), place),
        )
    return place


def test_a_pull_that_does_not_take_a_field_leaves_it_unseen(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    marks = {"identifier": {"sb": field_hash("S1"), "bb": field_hash("B9")}}
    place = _seeded_location(pg_pool, f, identifier="S1", seen={"name": "Shelf A"}, marks=marks)
    rows = [{"id": 5, "name": "Shelf A", "identifier": "B9", "updated_at": _bb_time(1)}]
    report = pull(f, [], now=NOW, pulls=ROWS, locations=_locations(rows))
    after = _row(pg_pool, "filament_locations", "id", place)
    assert after is not None
    assert after["identifier"] == "S1"
    assert after["bb_seen"] == {"name": "Shelf A"}
    assert after["bb_pushed"] == {UNSEEN_KEY: marks}
    assert report.conflicts == 0
    two_way = FilamentSync(
        spools="off", weights="off", tags="off", presets="off", locations="two_way"
    )
    assert _plan(f, _snap([], locations=rows), two_way).writes == []


def test_a_field_both_sides_changed_is_not_pulled(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    place = _seeded_location(
        pg_pool, f, identifier="C3", seen={"name": "Shelf A", "identifier": "A1"}, marks={}
    )
    rows = [{"id": 5, "name": "Shelf A", "identifier": "B7", "updated_at": _bb_time(1)}]
    report = pull(f, [], now=NOW, pulls=ROWS, locations=_locations(rows))
    after = _row(pg_pool, "filament_locations", "id", place)
    assert after is not None
    assert after["identifier"] == "C3"
    assert after["bb_seen"] == {"name": "Shelf A", "identifier": "A1"}
    assert report.conflicts == 1


def test_a_field_only_bambuddy_changed_is_pulled(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    place = _seeded_location(
        pg_pool, f, identifier="A1", seen={"name": "Shelf A", "identifier": "A1"}, marks={}
    )
    rows = [{"id": 5, "name": "Shelf A", "identifier": "B7", "updated_at": "2026-01-01T00:00:00"}]
    report = pull(f, [], now=NOW, pulls=ROWS, locations=_locations(rows))
    after = _row(pg_pool, "filament_locations", "id", place)
    assert after is not None
    assert after["identifier"] == "B7"
    assert after["bb_seen"] == {"name": "Shelf A", "identifier": "B7"}
    assert (report.locations, report.conflicts) == (1, 0)


def test_the_pulls_archive_and_move_are_booked_at_bambuddys_time(pg_pool: PgPool) -> None:
    """Review M3: the pull's ``archived`` and ``moved`` events carry Bambuddy's change
    time, not the pull's, so they never count as an SB change newer than Bambuddy's
    (`sb_changed_at`), and a later Bambuddy change still wins on both sides."""
    from scadbuddy.filament.bb_sync_plan import sb_changed_at

    f = _filament(pg_pool)
    (made,) = pull(f, [bb(updated_at=_bb_time(-3))], now=NOW).created_ids
    garage = _locations([{"id": 5, "name": "Garage"}])
    pull(f, [bb(updated_at=_bb_time(-3))], now=NOW, pulls=ROWS, locations=garage)
    # After the spool's ``created_at``, which `sb_changed_at` also counts.
    when = _bb_time(1)
    changed = bb(location_id=5, archived_at=when, updated_at=when)
    pull(f, [changed], now=NOW, pulls=ROWS, locations=garage)

    spool = f.spools.get(made)
    assert spool is not None and spool.archived_at is not None and spool.location_id is not None
    at = datetime.fromisoformat(when).replace(tzinfo=UTC)
    with pg_pool.connection() as conn:
        assert sb_changed_at(conn, made, "location_id") == at
        # Booked just after the move, which came first: the ledger keeps its order.
        assert at < sb_changed_at(conn, made, "archived_at") <= at + timedelta(microseconds=1)


def test_a_pull_books_its_move_and_archive_before_its_own_events(pg_pool: PgPool) -> None:
    """Decision 158: one Bambuddy change can move and archive a spool and change its
    K-profiles. The K values' event is booked at the pull's own time, after Bambuddy's
    change, so the move and the archive are booked first: at Bambuddy's time, not just
    after that event, where `sb_changed_at` would count them as a newer SB change."""
    from scadbuddy.filament.bb_sync_plan import sb_changed_at

    f = _filament(pg_pool)
    _printer(pg_pool, bb_id=1)
    first = bb(k_profiles=[_k(1, 0.02)], updated_at=_bb_time(-3))
    (made,) = pull(f, [first], now=NOW).created_ids
    garage = _locations([{"id": 5, "name": "Garage"}])
    pull(f, [first], now=NOW, pulls=ROWS, locations=garage)
    # Bambuddy's change: after every event so far, before the next pull's own events.
    with pg_pool.connection() as conn:
        row = conn.execute("SELECT clock_timestamp() AS at").fetchone()
    assert row is not None
    at: datetime = row["at"]
    when = at.astimezone(UTC).replace(tzinfo=None).isoformat()
    changed = bb(location_id=5, archived_at=when, updated_at=when, k_profiles=[_k(1, 0.03)])
    pull(f, [changed], now=NOW, pulls=ROWS, locations=garage)

    spool = f.spools.get(made)
    assert spool is not None and spool.archived_at is not None and spool.location_id is not None
    with pg_pool.connection() as conn:
        k_at = conn.execute(
            "SELECT max(at) AS at FROM filament_spool_events WHERE spool_id = %s"
            " AND kind = 'synced_from_bb' AND data -> 'changes' ? 'k_values'",
            (made,),
        ).fetchone()
        assert k_at is not None and k_at["at"] > at
        assert sb_changed_at(conn, made, "location_id") == at
        # Just after the move, which came first; both before the K values' event.
        assert at < sb_changed_at(conn, made, "archived_at") <= at + timedelta(microseconds=1)
