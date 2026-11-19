"""The plan of the ScadBuddy-to-Bambuddy sync (#6265 Task 4): what SB wants Bambuddy to
hold, field hashes, "newer change wins", and `plan`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from psycopg.types.json import Jsonb

from scadbuddy.bambuddy.models import Spool as BbSpool
from scadbuddy.core.settings import FilamentSync, SyncDirection
from scadbuddy.filament.bb_sync_models import BbSnapshot, BbWrite, SyncPlan, seen_row
from scadbuddy.filament.bb_sync_plan import (
    UNSEEN,
    BbRefs,
    body_value,
    compare_form,
    decide,
    desired_spool,
    field_hash,
    plan,
    sb_changed_at,
)
from scadbuddy.filament.catalog import CatalogStore
from scadbuddy.filament.component import Filament
from scadbuddy.filament.inventory import FilamentInventory
from scadbuddy.filament.invoices import InvoiceStore
from scadbuddy.filament.locations import LocationStore
from scadbuddy.filament.models import (
    LocationCreate,
    LocationKind,
    Material,
    SpoolCreate,
    SpoolKValueCreate,
    SpoolPatch,
    SpoolTypeCreate,
)
from scadbuddy.filament.presets import PoolSpoolPresets
from scadbuddy.filament.purchasing_models import PricePointCreate, ProductCreate, SupplierCreate
from scadbuddy.filament.rfid_mode import RfidModeStore
from scadbuddy.filament.sensors import SensorStore
from scadbuddy.filament.shopping import ShoppingStore
from scadbuddy.filament.spools import FilamentSpoolStore
from scadbuddy.filament.suppliers import SuppliersStore
from scadbuddy.filament.types import SpoolTypeStore
from tests.bambuddy.conftest import recording
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_postgres

INSTANCE = "0123456789ab"
#: Only the spools push.
SPOOLS = FilamentSync(
    spools="sb_to_bb", weights="off", tags="off", presets="off", suppliers="off", locations="off"
)
_T0 = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)


def _filament(pool: PgPool) -> Filament:
    types = SpoolTypeStore(pool)
    spools = FilamentSpoolStore(pool)
    return Filament(
        catalog=CatalogStore(pool),
        types=types,
        spools=spools,
        locations=LocationStore(pool),
        sensors=SensorStore(pool),
        inventory=FilamentInventory(pool),
        presets=PoolSpoolPresets(pool),
        rfid=RfidModeStore(pool),
        suppliers=SuppliersStore(pool, lambda: "USD"),
        invoices=InvoiceStore(pool, lambda: "USD", spools, types),
        shopping=ShoppingStore(pool),
    )


def _type(f: Filament, code: str = "GFA00-RED", rgba: str = "#FF0000FF") -> UUID:
    pla = next(
        r.id for r in f.catalog.list("materials") if isinstance(r, Material) and r.name == "PLA"
    )
    made = f.types.create(
        SpoolTypeCreate(code=code, material_id=pla, rgba=rgba, label_weight_g=1000)
    )
    return made.id


def _spool(f: Filament, type_id: UUID, grams: float | None = 900.0) -> UUID:
    return f.spools.create(SpoolCreate(type_id=type_id, grams_left=grams), actor="person").id


def _kind(f: Filament, name: str) -> UUID:
    rows = f.catalog.list("location-kinds")
    return next(r.id for r in rows if isinstance(r, LocationKind) and r.name == name)


def _printer(pool: PgPool, bb_id: int | None = None, model: str | None = "H2C") -> UUID:
    printer = uuid4()
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO printers (id, serial, name, model, bambuddy_printer_id)"
            " VALUES (%s, %s, 'P', %s, %s)",
            (printer, f"SN{printer.hex[:8]}", model, bb_id),
        )
    return printer


def _place(
    pool: PgPool,
    f: Filament,
    name: str,
    *,
    printer: UUID | None = None,
    ams: int | None = None,
    tray: int | None = None,
) -> UUID:
    place = uuid4()
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO filament_locations (id, name, kind_id, printer_id, ams_id, tray_id)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (place, name, _kind(f, "AMS" if printer else "Shelf"), printer, ams, tray),
        )
    return place


def _put(pool: PgPool, spool: UUID, location: UUID) -> None:
    with pool.connection() as conn:
        conn.execute("UPDATE filament_spools SET location_id = %s WHERE id = %s", (location, spool))


def _desired(pool: PgPool, spool: UUID, sync: FilamentSync = SPOOLS) -> dict[str, Any]:
    with pool.connection() as conn:
        return desired_spool(conn, spool, BbRefs.read(conn), sync)


def _row(bb_id: int, **over: Any) -> dict[str, Any]:
    """A recorded Bambuddy spool row with id ``bb_id`` and ``over``, with no tags and no
    catalog entry (what SB's create leaves out)."""
    row: dict[str, Any] = next(r for r in recording("inventory-spools.json") if r["id"] == 9)
    plain = {"id": bb_id, "tag_uid": None, "tray_uuid": None, "core_weight_catalog_id": None}
    return row | plain | over


def _matching(pool: PgPool, spool: UUID, bb_id: int, sync: FilamentSync = SPOOLS) -> dict[str, Any]:
    """BB's row of ``spool`` holding every value SB wants it to."""
    desired = _desired(pool, spool, sync)
    body = {
        n: body_value(n, v)
        for n, v in desired.items()
        if n not in ("archived_at", "k_profiles", "filament_presets")
    }
    return _row(bb_id, **body)


def _hashes(pool: PgPool, spool: UUID, sync: FilamentSync = SPOOLS) -> dict[str, str]:
    return {n: field_hash(compare_form(n, v)) for n, v in _desired(pool, spool, sync).items()}


def _link(
    pool: PgPool,
    spool: UUID,
    bb_id: int,
    *,
    pushed: dict[str, str] | None = None,
    seen: dict[str, Any] | None = None,
    seen_at: datetime | None = None,
    k: list[dict[str, Any]] | None = None,
) -> None:
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO filament_bb_links (spool_id, bb_spool_id, bb_updated_at, pushed_fields,"
            " bb_seen, bb_seen_at, bb_k_profiles) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                spool,
                bb_id,
                _T0,
                Jsonb(pushed or {}),
                None if seen is None else Jsonb(seen),
                seen_at,
                None if k is None else Jsonb(k),
            ),
        )


def _refuse(pool: PgPool, kind: str, sb_id: UUID, at: datetime | None = None) -> None:
    """What Task 5 stores for a write Bambuddy refused: an error row; the intent stays."""
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO filament_bb_push_errors (kind, sb_id, error, at)"
            " VALUES (%s, %s, 'bambuddy-refused:422', %s)",
            (kind, sb_id, at or datetime.now(UTC)),
        )


def _snap(
    spools: list[dict[str, Any]],
    *,
    full: bool = True,
    complete: bool = True,
    read_at: datetime | None = None,
    **lists: Any,
) -> BbSnapshot:
    return BbSnapshot.model_validate(
        {
            "instance": INSTANCE,
            "full": full,
            "spools": spools,
            "spools_complete": complete,
            "suppliers": None,
            "locations": None,
            "catalog": None,
            "filaments": None,
            "read_at": read_at or datetime.now(UTC),
            **lists,
        }
    )


def _plan(f: Filament, snap: BbSnapshot, sync: FilamentSync = SPOOLS, limit: int = 200) -> SyncPlan:
    return plan(f, snap, sync, interval_s=300.0, limit=limit)


def _intents(pool: PgPool) -> list[tuple[str, UUID]]:
    with pool.connection() as conn:
        rows = conn.execute("SELECT kind, sb_id FROM filament_bb_creates").fetchall()
    return [(r["kind"], r["sb_id"]) for r in rows]


def _only(writes: list[BbWrite], op: str) -> BbWrite:
    (write,) = [w for w in writes if w.op == op]
    return write


# -- hashes and "newer wins" ----------------------------------------------------------


def test_field_hash_is_order_free() -> None:
    assert field_hash({"a": 1, "b": 2}) == field_hash({"b": 2, "a": 1})
    assert len(field_hash({"a": 1})) == 16
    assert field_hash("x") != field_hash("y")


def _decide(direction: SyncDirection, *, sb_at: datetime, bb_at: datetime | None) -> bool:
    return decide(
        sb="Red", bb_now="Blue", bb_seen="Green", sb_at=sb_at, bb_at=bb_at, direction=direction
    )


def test_two_way_skips_a_field_only_bambuddy_changed() -> None:
    """BB changed it after SB did: BB's value stands."""
    assert not _decide("two_way", sb_at=_T0, bb_at=_T0 + timedelta(seconds=1))


def test_two_way_pushes_a_field_sb_changed_later() -> None:
    assert _decide("two_way", sb_at=_T0 + timedelta(seconds=1), bb_at=_T0)


def test_a_tie_goes_to_bambuddy() -> None:
    """Both changed at one time: the pull keeps BB's value then, so the push must too."""
    assert not _decide("two_way", sb_at=_T0, bb_at=_T0)


def test_two_way_pushes_what_bambuddy_has_not_moved() -> None:
    """BB still holds what SB last saw: SB's change goes, whatever the times."""
    assert decide(
        sb="Red", bb_now="Blue", bb_seen="Blue", sb_at=_T0, bb_at=_T0 + timedelta(1),
        direction="two_way",
    )  # fmt: skip


def test_sb_to_bb_pushes_every_difference(pg_pool: PgPool) -> None:
    assert _decide("sb_to_bb", sb_at=_T0, bb_at=_T0 + timedelta(days=1))
    assert not decide(
        sb="Red", bb_now="Red", bb_seen="Blue", sb_at=_T0, bb_at=_T0, direction="sb_to_bb"
    )
    ways: tuple[SyncDirection, ...] = ("off", "bb_to_sb")
    for direction in ways:
        assert not _decide(direction, sb_at=_T0 + timedelta(1), bb_at=_T0)
    f = _filament(pg_pool)
    spool = _spool(f, _type(f))
    row = _matching(pg_pool, spool, 50) | {"color_name": "Other", "note": "BB's note"}
    _link(pg_pool, spool, 50)
    update = _only(_plan(f, _snap([row])).writes, "update")
    assert update.body == {"color_name": None, "note": None}
    assert update.bb_id == 50 and update.key == f"spool:{spool}:update"


def test_sb_changed_at_counts_the_edit_and_the_type(pg_pool: PgPool) -> None:
    """Decision 8: the newest ``edited`` event naming the field, else the spool's
    creation; an inherited value also counts its type's change."""
    f = _filament(pg_pool)
    kind = _type(f)
    spool = _spool(f, kind)
    with pg_pool.connection() as conn:
        made = sb_changed_at(conn, spool, "color_name")
    f.spools.update(spool, SpoolPatch(colour_name="Brick"), actor="person")
    with pg_pool.connection() as conn:
        edited = sb_changed_at(conn, spool, "color_name")
        assert sb_changed_at(conn, spool, "note") == made
        conn.execute("UPDATE filament_spool_types SET updated_at = now() + interval '1 hour'")
        assert sb_changed_at(conn, spool, "rgba") > edited
        assert sb_changed_at(conn, spool, "color_name") == edited
    assert edited > made


# -- what SB wants ---------------------------------------------------------------------


def test_weight_used_is_label_minus_grams_left(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    kind = _type(f)
    weights = SPOOLS.model_copy(update={"weights": "sb_to_bb"})
    assert _desired(pg_pool, _spool(f, kind, 812.34), weights)["weight_used"] == 187.7
    assert _desired(pg_pool, _spool(f, kind, 1010.0), weights)["weight_used"] == 0
    assert "weight_used" not in _desired(pg_pool, _spool(f, kind, None), weights)


def test_a_spool_in_a_printer_place_leaves_location_alone(pg_pool: PgPool) -> None:
    """An AMS unit, a tray, the external spool place and a plain printer place."""
    f = _filament(pg_pool)
    kind = _type(f)
    printer = _printer(pg_pool)
    places = [
        _place(pg_pool, f, "AMS 0", printer=printer, ams=0),
        _place(pg_pool, f, "AMS 0 tray 1", printer=printer, ams=0, tray=1),
        _place(pg_pool, f, "External", printer=printer, ams=255, tray=0),
        _place(pg_pool, f, "Beside it", printer=printer),
    ]
    sync = SPOOLS.model_copy(update={"locations": "sb_to_bb"})
    for place in places:
        spool = _spool(f, kind)
        _put(pg_pool, spool, place)
        assert "location_id" not in _desired(pg_pool, spool, sync)
    shelf = _spool(f, kind)
    assert _desired(pg_pool, shelf, sync)["location_id"] is None


def test_k_values_of_an_unmapped_printer_are_left_out(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    spool = _spool(f, _type(f))
    mapped, unmapped = _printer(pg_pool, bb_id=3), _printer(pg_pool)
    f.spools.set_k_values(
        spool,
        [
            SpoolKValueCreate(printer_id=p, extruder=0, nozzle_diameter="0.4", k_value=k)
            for p, k in ((mapped, Decimal("0.02")), (unmapped, Decimal("0.03")))
        ],
        actor="person",
    )
    presets = SPOOLS.model_copy(update={"spools": "off", "presets": "sb_to_bb"})
    _link(pg_pool, spool, 50)
    writes = _plan(f, _snap([_row(50, slicer_filament=None, slicer_filament_name=None)]), presets)
    k = _only(writes.writes, "k_profiles")
    assert [(r["printer_id"], r["k_value"]) for r in k.body] == [(3, 0.02)]  # type: ignore[index]
    assert {w.op for w in writes.writes} == {"k_profiles", "presets"}
    assert writes.unmapped_k == 1


# -- creates ---------------------------------------------------------------------------


def test_an_unlinked_spool_is_created_with_its_type_code(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    spool = _spool(f, _type(f, code="GFA00-RED"))
    result = _plan(f, _snap([]))
    create = _only(result.writes, "create")
    assert create.sb_id == spool and create.bb_id is None and create.kind == "spool"
    assert create.key == f"spool:{spool}:create"
    assert create.body["material_number"] == "GFA00-RED"  # type: ignore[call-overload]
    assert create.body["material"] == "PLA"  # type: ignore[call-overload]
    assert "tag_uid" not in create.body and "tray_uuid" not in create.body
    assert set(create.hashes) == set(create.body)
    assert _intents(pg_pool) == [("spool", spool)]
    assert result.deferred == 0


def test_an_archived_unlinked_spool_is_not_created(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    spool = _spool(f, _type(f))
    f.spools.archive(spool, True, actor="person")
    assert _plan(f, _snap([])).writes == []
    assert _intents(pg_pool) == []


def test_a_young_intent_defers_the_spool(pg_pool: PgPool) -> None:
    """A create whose answer this pass lost: no second create, no adoption, until a pass
    reads Bambuddy again (F9)."""
    f = _filament(pg_pool)
    spool = _spool(f, _type(f))
    read_at = datetime.now(UTC) - timedelta(minutes=1)
    first = _plan(f, _snap([], read_at=read_at))
    assert _only(first.writes, "create").sb_id == spool
    row = _row(60, **_only(first.writes, "create").body)  # type: ignore[arg-type]
    again = _plan(f, _snap([row], read_at=read_at))
    assert again.writes == [] and again.deferred == 1


def test_an_intent_adopts_the_spool_bambuddy_made(pg_pool: PgPool) -> None:
    """Decision 10: the create's answer was lost, and a later pass finds Bambuddy's row
    with the same values: it is linked, not made again."""
    f = _filament(pg_pool)
    spool = _spool(f, _type(f))
    create = _only(_plan(f, _snap([])).writes, "create")
    made = (datetime.now(UTC) + timedelta(seconds=1)).replace(tzinfo=None).isoformat()
    other = _row(61, material_number="OTHER", created_at=made)
    row = _row(60, **create.body, created_at=made)  # type: ignore[arg-type]
    later = _plan(f, _snap([other, row], read_at=datetime.now(UTC) + timedelta(minutes=1)))
    (write,) = later.writes
    assert write.op == "update" and write.bb_id == 60 and write.sb_id == spool
    assert write.body == {} and write.hashes == create.hashes
    assert _intents(pg_pool) == [("spool", spool)]


def test_an_old_intent_without_a_row_creates_again(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    _spool(f, _type(f))
    _plan(f, _snap([]))
    later = _plan(f, _snap([], read_at=datetime.now(UTC) + timedelta(minutes=1)))
    assert [w.op for w in later.writes] == ["create"]


def test_the_limit_bounds_the_plan(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    kind = _type(f)
    spools = [_spool(f, kind) for _ in range(3)]
    result = _plan(f, _snap([]), limit=2)
    assert [w.sb_id for w in result.writes] == spools[:2]
    assert result.deferred == 1
    assert sorted(i for _, i in _intents(pg_pool)) == sorted(spools[:2])


# -- linked spools ---------------------------------------------------------------------


def test_matching_hashes_skip_the_spool_unless_full(pg_pool: PgPool) -> None:
    """Decision 7: an event-driven pass trusts the hashes; a full one compares with what
    Bambuddy holds, and sends only what differs."""
    f = _filament(pg_pool)
    spool = _spool(f, _type(f))
    f.spools.update(spool, SpoolPatch(colour_name="Brick"), actor="person")
    row = _matching(pg_pool, spool, 50) | {"color_name": "Other"}
    _link(pg_pool, spool, 50, pushed=_hashes(pg_pool, spool))
    assert _plan(f, _snap([row], full=False)).writes == []
    (update,) = _plan(f, _snap([row], full=True)).writes
    assert update.op == "update" and update.body == {"color_name": "Brick"}
    assert update.hashes == {"color_name": field_hash("Brick")}


def test_an_event_pass_pushes_a_changed_hash(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    spool = _spool(f, _type(f))
    row = _matching(pg_pool, spool, 50)
    _link(pg_pool, spool, 50, pushed=_hashes(pg_pool, spool))
    f.spools.update(spool, SpoolPatch(colour_name="Brick"), actor="person")
    (update,) = _plan(f, _snap([row], full=False)).writes
    assert update.body == {"color_name": "Brick"}


def test_colours_compare_in_one_form(pg_pool: PgPool) -> None:
    """A translucent spool (#6654): SB's ``#AABBCC80`` is BB's ``aabbcc80``; a changed
    colour goes to Bambuddy as ``AABBCC80``, with no ``#`` (F17)."""
    f = _filament(pg_pool)
    spool = _spool(f, _type(f, rgba="#AABBCC80"))
    row = _matching(pg_pool, spool, 50) | {"rgba": "aabbcc80"}
    _link(pg_pool, spool, 50)
    assert _plan(f, _snap([row])).writes == []
    (update,) = _plan(f, _snap([row | {"rgba": "11223344"}])).writes
    assert update.body == {"rgba": "AABBCC80"}
    assert update.hashes == {"rgba": field_hash("#AABBCC80")}


def test_two_way_keeps_a_newer_bambuddy_edit(pg_pool: PgPool) -> None:
    """Both changed one field: the newer change wins (Decision 8)."""
    f = _filament(pg_pool)
    spool = _spool(f, _type(f))
    f.spools.update(spool, SpoolPatch(colour_name="Brick"), actor="person")
    two_way = SPOOLS.model_copy(update={"spools": "two_way"})
    row = _matching(pg_pool, spool, 50, two_way)
    seen = row | {"color_name": "Red"}
    _link(pg_pool, spool, 50, seen=seen_row(BbSpool.model_validate(seen)))
    later = (datetime.now(UTC) + timedelta(hours=1)).replace(tzinfo=None).isoformat()
    newer = row | {"color_name": "Teal", "updated_at": later}
    assert _plan(f, _snap([newer]), two_way).writes == []
    older = row | {"color_name": "Teal", "updated_at": "2026-01-01T00:00:00"}
    (update,) = _plan(f, _snap([older]), two_way).writes
    assert update.body == {"color_name": "Brick"}


def test_a_spool_waits_for_its_new_location(pg_pool: PgPool) -> None:
    """A spool whose place Bambuddy has no id for yet: the place's create goes first, and
    the spool waits for the round after it (Decision 11)."""
    f = _filament(pg_pool)
    spool = _spool(f, _type(f))
    shelf = _place(pg_pool, f, "Shelf A")
    sync = SPOOLS.model_copy(update={"locations": "sb_to_bb"})
    row = _matching(pg_pool, spool, 50, sync)
    _put(pg_pool, spool, shelf)
    _link(pg_pool, spool, 50)
    result = _plan(f, _snap([row], locations=[]), sync)
    (create,) = result.writes
    assert (create.kind, create.op, create.sb_id) == ("location", "create", shelf)
    assert create.body == {"name": "Shelf A", "identifier": None}
    assert result.deferred == 1
    assert ("location", shelf) in _intents(pg_pool)


def test_a_printer_place_is_never_pushed_as_a_location(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    printer = _printer(pg_pool)
    for name, ams, tray in (
        ("AMS 0", 0, None),
        ("Tray", 0, 1),
        ("Ext", 255, 0),
        ("By", None, None),
    ):
        _place(pg_pool, f, name, printer=printer, ams=ams, tray=tray)
    room = f.locations.create(LocationCreate(name="Garage", kind_id=_kind(f, "Room")))
    f.locations.create(LocationCreate(name="Shelf A", kind_id=_kind(f, "Shelf"), parent_id=room.id))
    sync = SPOOLS.model_copy(update={"spools": "off", "locations": "sb_to_bb"})
    writes = _plan(f, _snap([], locations=[]), sync).writes
    assert sorted(w.body["name"] for w in writes) == ["Garage", "Garage / Shelf A"]  # type: ignore[call-overload]
    assert all(w.kind == "location" for w in writes)


def test_a_location_of_the_same_name_is_adopted(pg_pool: PgPool) -> None:
    """Bambuddy refuses a second location of one name (409): an unlinked one of SB's name
    is linked instead, its intent recorded."""
    f = _filament(pg_pool)
    shelf = f.locations.create(LocationCreate(name="Shelf A", kind_id=_kind(f, "Shelf")))
    sync = SPOOLS.model_copy(update={"spools": "off", "locations": "sb_to_bb"})
    bb = [{"id": 4, "name": "shelf a", "identifier": None}]
    (write,) = _plan(f, _snap([], locations=bb), sync).writes
    assert (write.op, write.bb_id, write.body) == ("update", 4, {"name": "Shelf A"})
    assert _intents(pg_pool) == [("location", shelf.id)]


def test_another_instance_plans_nothing(pg_pool: PgPool) -> None:
    """Decision 12: the links belong to another Bambuddy."""
    f = _filament(pg_pool)
    _spool(f, _type(f))
    with pg_pool.connection() as conn:
        conn.execute("UPDATE filament_bb_sync_state SET instance = 'another'")
    result = _plan(f, _snap([]))
    assert result.writes == [] and result.instance_changed
    assert _intents(pg_pool) == []


def test_a_spool_missing_from_the_listing_keeps_its_link(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    spool = _spool(f, _type(f))
    _link(pg_pool, spool, 50)
    result = _plan(f, _snap([], complete=False))
    assert result.writes == []
    assert (result.blocked, result.deferred) == (1, 0)


def test_a_spool_missing_from_a_complete_listing_is_probed(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    spool = _spool(f, _type(f))
    _link(pg_pool, spool, 50)
    (probe,) = _plan(f, _snap([])).writes
    assert (probe.op, probe.bb_id, probe.body, probe.key) == (
        "probe",
        50,
        {},
        f"spool:{spool}:probe",
    )


def test_a_later_round_compares_with_the_answer(pg_pool: PgPool) -> None:
    """F11: ``results`` stored Bambuddy's answer after the snapshot was read; the
    snapshot's row is older, and round 2 does not write the field again."""
    f = _filament(pg_pool)
    spool = _spool(f, _type(f))
    row = _matching(pg_pool, spool, 50)
    read_at = datetime.now(UTC) - timedelta(minutes=1)
    _link(pg_pool, spool, 50, seen=seen_row(BbSpool.model_validate(row)), seen_at=datetime.now(UTC))
    old = row | {"color_name": "Old"}
    assert _plan(f, _snap([old], read_at=read_at)).writes == []


def test_archive_and_restore_are_pushed(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    spool = _spool(f, _type(f))
    row = _matching(pg_pool, spool, 50)
    _link(pg_pool, spool, 50)
    f.spools.archive(spool, True, actor="person")
    (archive,) = _plan(f, _snap([row | {"color_name": "Other"}])).writes
    assert (archive.op, archive.body, archive.hashes) == (
        "archive",
        {},
        {"archived_at": field_hash(False)},
    )
    f.spools.archive(spool, False, actor="person")
    archived = row | {"archived_at": "2026-11-18T10:00:00"}
    assert [w.op for w in _plan(f, _snap([archived])).writes] == ["restore"]


def test_nothing_pushes_plans_nothing(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    _spool(f, _type(f))
    sync = FilamentSync(
        spools="bb_to_sb", weights="off", tags="two_way", presets="bb_to_sb",
        suppliers="off", locations="bb_to_sb",
    )  # fmt: skip
    result = _plan(f, _snap([]), sync)
    assert (result.writes, result.deferred, result.instance_changed) == ([], 0, False)
    assert _intents(pg_pool) == []


def test_supplier_links_carry_the_sku_and_the_price(pg_pool: PgPool) -> None:
    """Decision 14: one link per supplier of the type's products, with the SKU and the
    newest per-kg price; a supplier Bambuddy has no id for leaves the list out."""
    f = _filament(pg_pool)
    kind = _type(f)
    spool = _spool(f, kind)
    shop = f.suppliers.create(SupplierCreate(name="Shop"))
    product = f.suppliers.add_product(shop.id, ProductCreate(type_id=kind, sku="A-1"))
    assert product is not None
    f.suppliers.add_price(product.id, PricePointCreate(price=Decimal("20")))
    sync = SPOOLS.model_copy(update={"spools": "off", "suppliers": "sb_to_bb"})
    _link(pg_pool, spool, 50)
    assert _plan(f, _snap([_row(50)]), sync).writes == []
    with pg_pool.connection() as conn:
        conn.execute("UPDATE filament_suppliers SET bb_supplier_id = 7")
    (write,) = _plan(f, _snap([_row(50)]), sync).writes
    assert (write.op, write.key) == ("suppliers", f"spool:{spool}:suppliers")
    assert write.body == [
        {
            "supplier_id": 7,
            "supplier_article_number": "A-1",
            "quoted_price_per_kg": 20.0,
            "is_purchase_source": False,
        }
    ]


# -- deferrals that clear, and those that do not (R-T4-1, R-T4-2) -----------------------


def test_a_refused_create_waits_for_the_next_full_pass(pg_pool: PgPool) -> None:
    """C-1 case 1: round 1 creates, Bambuddy refuses, Task 5 keeps the intent and stores
    an error row. Round 2 neither creates again nor counts it ``deferred`` (which would
    set ``more`` and start the next pass at once); an event pass leaves it; the next
    full pass tries once more."""
    f = _filament(pg_pool)
    spool = _spool(f, _type(f))
    read_at = datetime.now(UTC) - timedelta(minutes=1)
    _only(_plan(f, _snap([], read_at=read_at)).writes, "create")
    _refuse(pg_pool, "spool", spool)
    again = _plan(f, _snap([], read_at=read_at))
    assert (again.writes, again.deferred, again.failed) == ([], 0, 1)
    later = datetime.now(UTC) + timedelta(minutes=1)
    event = _plan(f, _snap([], read_at=later, full=False))
    assert (event.writes, event.deferred, event.failed) == ([], 0, 1)
    full = _plan(f, _snap([], read_at=later))
    assert [w.op for w in full.writes] == ["create"]
    assert (full.deferred, full.failed) == (0, 0)


def test_a_refused_row_is_retried_after_the_spools(pg_pool: PgPool) -> None:
    """R-T4-2: a create Bambuddy refused in an earlier pass goes after the spool writes,
    so failing catalog creates cannot spend the cap ahead of spools."""
    f = _filament(pg_pool)
    shelf = f.locations.create(LocationCreate(name="Shelf A", kind_id=_kind(f, "Shelf")))
    spool = _spool(f, _type(f))
    _refuse(pg_pool, "location", shelf.id, datetime.now(UTC) - timedelta(hours=1))
    sync = SPOOLS.model_copy(update={"locations": "sb_to_bb"})
    writes = _plan(f, _snap([], locations=[]), sync).writes
    assert [(w.kind, w.sb_id) for w in writes] == [("spool", spool), ("location", shelf.id)]


def test_an_old_intent_on_a_partial_listing_is_blocked(pg_pool: PgPool) -> None:
    """C-1 case 2: the row to adopt may be the unparsed one, so the spool waits, but as
    ``blocked``: a partial listing does not clear in a later round."""
    f = _filament(pg_pool)
    _spool(f, _type(f))
    _plan(f, _snap([]))
    later = _plan(f, _snap([], complete=False, read_at=datetime.now(UTC) + timedelta(minutes=1)))
    assert (later.writes, later.deferred, later.blocked) == ([], 0, 1)


def test_a_reference_whose_listing_was_not_read_blocks(pg_pool: PgPool) -> None:
    """C-1 case 3: the spool's place has an old intent and no error row, and this pass
    did not read the locations: the spool waits as ``blocked``, not ``deferred``."""
    f = _filament(pg_pool)
    spool = _spool(f, _type(f))
    shelf = _place(pg_pool, f, "Shelf A")
    sync = SPOOLS.model_copy(update={"locations": "sb_to_bb"})
    row = _matching(pg_pool, spool, 50, sync) | {"color_name": "Other"}
    _put(pg_pool, spool, shelf)
    _link(pg_pool, spool, 50)
    _plan(f, _snap([row], locations=[]), sync)
    assert ("location", shelf) in _intents(pg_pool)
    later = datetime.now(UTC) + timedelta(minutes=1)
    result = _plan(f, _snap([row], read_at=later), sync)
    assert (result.writes, result.deferred, result.blocked) == ([], 0, 1)


def test_failed_and_blocked_items_alone_report_no_deferral(pg_pool: PgPool) -> None:
    """A round whose only items are ``failed`` or ``blocked`` has ``deferred == 0``, so
    Task 7 sets no ``more``."""
    f = _filament(pg_pool)
    kind = _type(f)
    refused = _spool(f, kind)
    _spool(f, kind)
    _plan(f, _snap([]))
    _refuse(pg_pool, "spool", refused)
    later = datetime.now(UTC) + timedelta(minutes=1)
    result = _plan(f, _snap([], full=False, complete=False, read_at=later))
    assert result.writes == []
    assert (result.deferred, result.failed, result.blocked) == (0, 1, 1)


def test_a_refused_reference_is_left_out(pg_pool: PgPool) -> None:
    """Decision 35: the spool's place has an intent and an error row: Bambuddy refused it,
    so the spool goes without ``location_id`` and does not wait."""
    f = _filament(pg_pool)
    spool = _spool(f, _type(f))
    shelf = _place(pg_pool, f, "Shelf A")
    sync = SPOOLS.model_copy(update={"locations": "sb_to_bb"})
    row = _matching(pg_pool, spool, 50, sync) | {"color_name": "Other"}
    _put(pg_pool, spool, shelf)
    _link(pg_pool, spool, 50)
    _plan(f, _snap([row], locations=[]), sync)
    _refuse(pg_pool, "location", shelf)
    result = _plan(f, _snap([row], locations=[], full=False), sync)
    (update,) = result.writes
    assert (update.kind, update.op, update.body) == ("spool", "update", {"color_name": None})
    assert (result.deferred, result.failed) == (0, 1)


# -- a spool written earlier in this pass (R-T4-3, R-T4-5) ------------------------------


def test_a_spool_linked_this_pass_is_not_probed(pg_pool: PgPool) -> None:
    """I-1: round 1 created the spool and ``results`` stored the answer; the snapshot
    has no row for it. Round 2 plans from the answer, probes nothing, and sends the
    lists."""
    f = _filament(pg_pool)
    spool = _spool(f, _type(f))
    sync = SPOOLS.model_copy(update={"presets": "sb_to_bb"})
    answer = seen_row(BbSpool.model_validate(_matching(pg_pool, spool, 70, sync)))
    read_at = datetime.now(UTC) - timedelta(minutes=1)
    _link(pg_pool, spool, 70, seen=answer, seen_at=datetime.now(UTC), k=[])
    result = _plan(f, _snap([], read_at=read_at), sync)
    assert [(w.op, w.bb_id) for w in result.writes] == [("presets", 70)]


def test_unknown_k_profiles_are_not_written(pg_pool: PgPool) -> None:
    """R-T4-5: ``results`` stored the answer but not its K-profiles: unknown is no empty
    list, so no K write until a full read gives the list."""
    f = _filament(pg_pool)
    spool = _spool(f, _type(f))
    printer = _printer(pg_pool, bb_id=3)
    f.spools.set_k_values(
        spool,
        [
            SpoolKValueCreate(
                printer_id=printer, extruder=0, nozzle_diameter="0.4", k_value=Decimal("0.02")
            )
        ],
        actor="person",
    )
    sync = SPOOLS.model_copy(update={"presets": "sb_to_bb"})
    row = _matching(pg_pool, spool, 70, sync)
    answer = seen_row(BbSpool.model_validate(row))
    read_at = datetime.now(UTC) - timedelta(minutes=1)
    _link(pg_pool, spool, 70, seen=answer, seen_at=datetime.now(UTC))
    ops = [w.op for w in _plan(f, _snap([row], read_at=read_at), sync).writes]
    assert "k_profiles" not in ops and "presets" in ops


# -- the smaller rulings (R-T4-4, R-T4-6, R-T4-7, R-T4-8) -------------------------------


def test_an_unseen_value_is_never_pushed_without_a_newer_sb_change() -> None:
    """R-T4-4: with nothing seen, only time decides, and an unknown BB time keeps BB's."""
    late = _T0 + timedelta(seconds=1)
    for bb_now in (None, "Blue"):
        assert not decide(
            sb="Red", bb_now=bb_now, bb_seen=UNSEEN, sb_at=late, bb_at=None, direction="two_way"
        )
        assert not decide(
            sb="Red", bb_now=bb_now, bb_seen=UNSEEN, sb_at=_T0, bb_at=late, direction="two_way"
        )
        assert decide(
            sb="Red", bb_now=bb_now, bb_seen=UNSEEN, sb_at=late, bb_at=_T0, direction="two_way"
        )
    assert decide(
        sb="Red", bb_now=None, bb_seen=UNSEEN, sb_at=_T0, bb_at=late, direction="sb_to_bb"
    )


def test_a_legacy_link_keeps_a_value_bambuddy_cleared_later(pg_pool: PgPool) -> None:
    """M-1: a link made before Task 3 has no ``bb_seen``. Bambuddy cleared the note after
    SB's last edit: under ``two_way`` SB's older note is not pushed back."""
    f = _filament(pg_pool)
    spool = _spool(f, _type(f))
    f.spools.update(spool, SpoolPatch(note="SB's note"), actor="person")
    two_way = SPOOLS.model_copy(update={"spools": "two_way"})
    later = (datetime.now(UTC) + timedelta(hours=1)).replace(tzinfo=None).isoformat()
    row = _matching(pg_pool, spool, 50, two_way) | {"note": None, "updated_at": later}
    _link(pg_pool, spool, 50)
    assert _plan(f, _snap([row]), two_way).writes == []
    older = row | {"updated_at": "2026-01-01T00:00:00"}
    (update,) = _plan(f, _snap([older]), two_way).writes
    assert update.body == {"note": "SB's note"}


def test_an_archived_spool_takes_no_field_writes(pg_pool: PgPool) -> None:
    """M-3: with ``spools`` not pushing, an archived SB spool still gets no weight write
    (Decision 40)."""
    f = _filament(pg_pool)
    spool = _spool(f, _type(f), 500.0)
    weights = SPOOLS.model_copy(update={"spools": "off", "weights": "sb_to_bb"})
    row = _row(50, weight_used=0, weight_locked=False)
    _link(pg_pool, spool, 50)
    assert [w.op for w in _plan(f, _snap([row]), weights).writes] == ["update"]
    f.spools.archive(spool, True, actor="person")
    assert _plan(f, _snap([row]), weights).writes == []


def test_a_weight_write_carries_the_lock(pg_pool: PgPool) -> None:
    """Decision 38: Bambuddy locks the weight on a ``PATCH`` of ``weight_used`` unless
    ``weight_locked`` goes with it, even when the lock itself has not changed."""
    f = _filament(pg_pool)
    spool = _spool(f, _type(f), 500.0)
    weights = SPOOLS.model_copy(update={"spools": "off", "weights": "sb_to_bb"})
    row = _matching(pg_pool, spool, 50, weights) | {"weight_used": 0}
    _link(pg_pool, spool, 50)
    (update,) = _plan(f, _snap([row]), weights).writes
    assert update.body == {"weight_used": 500.0, "weight_locked": False}
    assert set(update.hashes) == {"weight_used", "weight_locked"}


def test_adoption_skips_a_row_made_before_the_intent(pg_pool: PgPool) -> None:
    """Decision 44: a BB row with the create's values but made before the intent (less
    the clocks' skew) is someone else's; the one made after it is adopted."""
    f = _filament(pg_pool)
    _spool(f, _type(f))
    create = _only(_plan(f, _snap([])).writes, "create")
    before = (datetime.now(UTC) - timedelta(minutes=5)).replace(tzinfo=None).isoformat()
    after = (datetime.now(UTC) + timedelta(seconds=1)).replace(tzinfo=None).isoformat()
    decoy = _row(61, **create.body, created_at=before)  # type: ignore[arg-type]
    made = _row(60, **create.body, created_at=after)  # type: ignore[arg-type]
    later = _plan(f, _snap([decoy, made], read_at=datetime.now(UTC) + timedelta(minutes=1)))
    (write,) = later.writes
    assert (write.op, write.bb_id) == ("update", 60)


def test_a_rename_onto_a_bambuddy_name_is_not_sent(pg_pool: PgPool) -> None:
    """R-T4-8: a linked place renamed in SB to the name of an unlinked Bambuddy place
    would get a 409 on every pass: no ``update``, and one error row."""
    f = _filament(pg_pool)
    shelf = f.locations.create(LocationCreate(name="Shelf B", kind_id=_kind(f, "Shelf")))
    with pg_pool.connection() as conn:
        conn.execute("UPDATE filament_locations SET bb_location_id = 4 WHERE id = %s", (shelf.id,))
    sync = SPOOLS.model_copy(update={"spools": "off", "locations": "sb_to_bb"})
    bb = [{"id": 4, "name": "Shelf A", "identifier": None}, {"id": 5, "name": "shelf b"}]
    for _ in range(2):
        result = _plan(f, _snap([], locations=bb), sync)
        assert (result.writes, result.failed) == ([], 1)
    with pg_pool.connection() as conn:
        rows = conn.execute(
            "SELECT kind, sb_id, error, attempts FROM filament_bb_push_errors"
        ).fetchall()
    assert [tuple(r.values()) for r in rows] == [("location", shelf.id, "name_taken", 1)]
