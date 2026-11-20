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
from scadbuddy.filament.bambuddy_pull import Pulls, pull, read_and_pull
from scadbuddy.filament.bb_sync_plan import UNSEEN_KEY, field_hash
from scadbuddy.filament.models import SpoolKValueCreate
from scadbuddy.filament.purchasing_models import SupplierCreate
from scadbuddy.library.settings_store import StoredSettings
from tests.conftest import PgPool
from tests.filament.test_bambuddy_pull import BB, NOW, _filament, bb
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
