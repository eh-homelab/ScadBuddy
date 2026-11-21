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
    _CATALOG_FIELDS,
    _FILAMENT_FIELDS,
    _LOCATION_FIELDS,
    _SUPPLIER_FIELDS,
    UNSEEN,
    UNSEEN_KEY,
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
from scadbuddy.filament.metrics_store import MetricsSettings, MetricsStore
from scadbuddy.filament.models import (
    LocationCreate,
    LocationKind,
    LocationPatch,
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
        metrics=MetricsStore(pool, spools, MetricsSettings),
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
            " bb_seen, bb_seen_at, bb_k_profiles, bb_seeded)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                spool,
                bb_id,
                _T0,
                Jsonb(pushed or {}),
                None if seen is None else Jsonb(seen),
                seen_at,
                None if k is None else Jsonb(k),
                seen is not None,
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


def test_retries_cut_by_the_cap_do_not_count_deferred(pg_pool: PgPool) -> None:
    """R-T4-15: 250 creates Bambuddy refused earlier fill the cap but are not new work, so
    the plan does not count them ``deferred`` (which would set ``more`` and run the next
    pass at once, for ever)."""
    f = _filament(pg_pool)
    kind = _type(f)
    old = datetime.now(UTC) - timedelta(hours=1)
    for _ in range(250):
        _refuse(pg_pool, "spool", _spool(f, kind), old)
    result = _plan(f, _snap([]))
    assert (len(result.writes), result.deferred) == (200, 0)


def test_new_work_cut_by_the_cap_counts_deferred_beside_retries(pg_pool: PgPool) -> None:
    """R-T4-15: only the new spools the cap cut count; the retries it cut do not."""
    f = _filament(pg_pool)
    kind = _type(f)
    old = datetime.now(UTC) - timedelta(hours=1)
    for _ in range(3):
        _refuse(pg_pool, "spool", _spool(f, kind), old)
    for _ in range(3):
        _spool(f, kind)
    result = _plan(f, _snap([]), limit=2)
    assert len(result.writes) == 2
    assert result.deferred == 1


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


# -- intents that close, and seen copies that are seeded (R-T4-19, R-T4-20, R-T4-22) ------


def _seen(pool: PgPool, table: str, key: str, sb_id: UUID) -> tuple[Any, datetime | None]:
    with pool.connection() as conn:
        row = conn.execute(
            f"SELECT bb_seen, bb_seen_at FROM {table} WHERE {key} = %s",
            (sb_id,),
        ).fetchone()
    assert row is not None
    return row["bb_seen"], row["bb_seen_at"]


@pytest.mark.parametrize("ref_kind", ["location", "insert_type"])
def test_an_archived_reference_with_an_open_intent_stops_waiting(
    pg_pool: PgPool, ref_kind: str
) -> None:
    """Re-review I-1: a pass made the reference's create and its intent, and died before
    ``results``. Then the owner archived the reference, while a spool still names it.
    ``rows`` never plans an archived row, so its intent never closed, and the spool was
    ``deferred`` on every pass. Now the spool counts ``blocked`` (R-T4-19), the intent
    expires (R-T4-20), and later passes write the spool's other fields without it."""
    f = _filament(pg_pool)
    spool = _spool(f, _type(f))
    if ref_kind == "location":
        ref = _place(pg_pool, f, "Shelf A")
        _put(pg_pool, spool, ref)
        sync = SPOOLS.model_copy(update={"locations": "sb_to_bb"})
        lists: dict[str, Any] = {"locations": []}
        table = "filament_locations"
    else:
        ref = uuid4()
        with pg_pool.connection() as conn:
            conn.execute(
                "INSERT INTO filament_insert_types (id, name, empty_weight_g)"
                " VALUES (%s, 'Core X', 250)",
                (ref,),
            )
            conn.execute(
                "UPDATE filament_spools SET insert_type_id = %s WHERE id = %s", (ref, spool)
            )
        sync = SPOOLS
        lists = {"catalog": []}
        table = "filament_insert_types"
    desired = _matching(pg_pool, spool, 50, sync)
    row = desired | {"core_weight_catalog_id": None, "location_id": None, "color_name": "Other"}
    _link(pg_pool, spool, 50)
    start = datetime.now(UTC)
    first = _plan(f, _snap([row], read_at=start - timedelta(minutes=1), **lists), sync)
    assert first.deferred == 1
    assert (ref_kind, ref) in _intents(pg_pool)
    with pg_pool.connection() as conn:
        conn.execute(f"UPDATE {table} SET archived_at = now() WHERE id = %s", (ref,))
    passes = [
        _plan(f, _snap([row], read_at=start + timedelta(minutes=n), **lists), sync)
        for n in (1, 2, 3)
    ]
    assert [(p.deferred, p.blocked) for p in passes] == [(0, 1), (0, 0), (0, 0)]
    assert (ref_kind, ref) not in _intents(pg_pool)
    for later in passes[1:]:
        (update,) = later.writes
        assert (update.sb_id, update.op, update.body) == (spool, "update", {"color_name": None})


def test_an_intent_that_matches_nothing_expires(pg_pool: PgPool) -> None:
    """R-T4-20: a full pass newer than an intent finds no Bambuddy row it made: the intent
    closes, and the item is planned again as a fresh create. An event pass keeps it."""
    f = _filament(pg_pool)
    shelf = f.locations.create(LocationCreate(name="Shelf A", kind_id=_kind(f, "Shelf")))
    spool = _spool(f, _type(f))
    old = datetime.now(UTC) - timedelta(hours=1)
    with pg_pool.connection() as conn:
        for kind, sb_id in (("location", shelf.id), ("spool", spool)):
            conn.execute(
                "INSERT INTO filament_bb_creates (kind, sb_id, started_at) VALUES (%s, %s, %s)",
                (kind, sb_id, old),
            )
    sync = SPOOLS.model_copy(update={"locations": "sb_to_bb"})
    now = datetime.now(UTC)
    event = _plan(f, _snap([], locations=[], full=False, read_at=now), sync, limit=0)
    assert (event.writes, event.deferred) == ([], 2)
    assert len(_intents(pg_pool)) == 2
    full = _plan(f, _snap([], locations=[], read_at=now), sync, limit=0)
    assert (full.writes, full.deferred) == ([], 2)
    assert _intents(pg_pool) == []
    again = _plan(f, _snap([], locations=[], read_at=now), sync)
    assert [(w.kind, w.op) for w in again.writes] == [("location", "create"), ("spool", "create")]
    with pg_pool.connection() as conn:
        starts = [
            r["started_at"] for r in conn.execute("SELECT started_at FROM filament_bb_creates")
        ]
    assert len(starts) == 2 and all(s > old for s in starts)


def test_seeding_records_what_bambuddy_holds(pg_pool: PgPool) -> None:
    """R-T4-22: a legacy link (no ``bb_seen``) gets its seen copy from a full pass's
    snapshot, at ``snap.read_at``. A field that differs from SB's value is left out of
    it, so SB's older note is not pushed over Bambuddy's; a field that agrees is in it,
    so a later SB change of that field is pushed."""
    f = _filament(pg_pool)
    spool = _spool(f, _type(f))
    f.spools.update(spool, SpoolPatch(note="SB's note"), actor="person")
    two_way = SPOOLS.model_copy(update={"spools": "two_way"})
    later = (datetime.now(UTC) + timedelta(hours=1)).replace(tzinfo=None).isoformat()
    row = _matching(pg_pool, spool, 50, two_way) | {"note": "BB's note", "updated_at": later}
    _link(pg_pool, spool, 50)
    read_at = datetime.now(UTC)
    assert _plan(f, _snap([row], read_at=read_at), two_way).writes == []
    seen, seen_at = _seen(pg_pool, "filament_bb_links", "spool_id", spool)
    assert seen_at == read_at
    assert "note" not in seen
    assert seen["material_number"] == "GFA00-RED" and seen["color_name"] is None
    f.spools.update(spool, SpoolPatch(colour_name="Brick"), actor="person")
    (update,) = _plan(f, _snap([row]), two_way).writes
    assert update.body == {"color_name": "Brick"}


@pytest.mark.parametrize("direction", ["two_way", "sb_to_bb"])
def test_a_field_that_differs_at_seeding_waits_for_an_sb_change(
    pg_pool: PgPool, direction: SyncDirection
) -> None:
    """R-T4-22 on a row with no Bambuddy time (a location): under ``two_way`` a field
    that differs when the seen copy is seeded is not pushed until SB changes it after
    ``bb_seen_at``. Under ``sb_to_bb`` the direction's rule pushes every difference, so it
    goes at once (decision 73). The seen copy holds only the fields that agree."""
    f = _filament(pg_pool)
    shelf = f.locations.create(
        LocationCreate(name="Shelf A", kind_id=_kind(f, "Shelf"), identifier="A1")
    )
    with pg_pool.connection() as conn:
        conn.execute(
            "UPDATE filament_locations SET bb_location_id = 4,"
            " updated_at = now() - interval '1 hour' WHERE id = %s",
            (shelf.id,),
        )
    sync = SPOOLS.model_copy(update={"spools": "off", "locations": direction})
    bb = [{"id": 4, "name": "Shelf A", "identifier": "B7"}]
    read_at = datetime.now(UTC) - timedelta(minutes=10)
    first = _plan(f, _snap([], locations=bb, read_at=read_at), sync)
    assert _seen(pg_pool, "filament_locations", "id", shelf.id) == ({"name": "Shelf A"}, read_at)
    if direction == "two_way":
        assert first.writes == []
        assert _plan(f, _snap([], locations=bb), sync).writes == []
    else:
        (update,) = first.writes
        assert update.body == {"identifier": "A1"}
    f.locations.update(shelf.id, LocationPatch(identifier="C3"))
    (update,) = _plan(f, _snap([], locations=bb), sync).writes
    assert (update.op, update.bb_id, update.body) == ("update", 4, {"identifier": "C3"})


def test_an_unseen_field_with_a_seeding_time_waits_for_a_later_sb_change() -> None:
    """R-T4-22 in `decide`: with no Bambuddy time, a field missing from a seeded copy
    pushes only when SB changed it after ``bb_seen_at``; a Bambuddy time still decides
    when there is one (R-T4-4)."""
    late = _T0 + timedelta(seconds=1)
    kw: dict[str, Any] = {"sb": "Red", "bb_now": "Blue", "bb_seen": UNSEEN, "direction": "two_way"}
    assert not decide(**kw, sb_at=_T0, bb_at=None, seen_at=late)
    assert not decide(**kw, sb_at=_T0, bb_at=None, seen_at=_T0)
    assert decide(**kw, sb_at=late, bb_at=None, seen_at=_T0)
    assert decide(**kw, sb_at=late, bb_at=_T0, seen_at=late)
    assert not decide(**kw, sb_at=_T0, bb_at=late, seen_at=None)


# -- R-T4-27: a seeded field is pushed only when SB changes it ------------------------


def _shelf_seeded(
    pool: PgPool, f: Filament, bb: dict[str, Any]
) -> tuple[UUID, FilamentSync, BbSnapshot]:
    """A legacy location link under ``two_way``: SB identifier ``A1``, Bambuddy's row
    ``bb``. A full pass seeds its seen copy; the returned snapshot is that pass's."""
    shelf = f.locations.create(
        LocationCreate(name="Shelf A", kind_id=_kind(f, "Shelf"), identifier="A1")
    )
    with pool.connection() as conn:
        conn.execute(
            "UPDATE filament_locations SET bb_location_id = 4,"
            " updated_at = now() - interval '1 hour' WHERE id = %s",
            (shelf.id,),
        )
    sync = SPOOLS.model_copy(update={"spools": "off", "locations": "two_way"})
    snap = _snap([], locations=[bb], read_at=datetime.now(UTC) - timedelta(minutes=10))
    assert _plan(f, snap, sync).writes == []
    return shelf.id, sync, snap


def _bb_pushed(pool: PgPool, table: str, sb_id: UUID) -> dict[str, Any]:
    with pool.connection() as conn:
        row = conn.execute(f"SELECT bb_pushed FROM {table} WHERE id = %s", (sb_id,)).fetchone()
    assert row is not None
    return dict(row["bb_pushed"])


def _ago(minutes: int) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes)).replace(tzinfo=None).isoformat()


@pytest.mark.parametrize("bb_time", [None, 30], ids=["no-bb-time", "older-bb-time"])
def test_a_rename_leaves_a_seeded_differing_identifier_alone(
    pg_pool: PgPool, bb_time: int | None
) -> None:
    """R-T4-27, the re-review's measured scenario (I-1): Bambuddy's identifier ``B7``
    differs from SB's ``A1`` at seeding. A rename in SB changes the row's ``updated_at``,
    not the identifier, so only the name is pushed. With a Bambuddy time older than the
    rename, the row times still say nothing about the identifier."""
    f = _filament(pg_pool)
    bb: dict[str, Any] = {"id": 4, "name": "Shelf A", "identifier": "B7"}
    if bb_time is not None:
        bb["updated_at"] = _ago(bb_time)
    shelf, sync, _ = _shelf_seeded(pg_pool, f, bb)
    f.locations.update(shelf, LocationPatch(name="Shelf Z"))
    (update,) = _plan(f, _snap([], locations=[bb]), sync).writes
    assert update.body == {"name": "Shelf Z"}


def test_an_sb_identifier_change_after_seeding_is_pushed(pg_pool: PgPool) -> None:
    """R-T4-27: seeding records SB's hash of the differing field (and Bambuddy's) under
    the reserved key ``unseen`` of ``bb_pushed``. When SB really changes the field, its
    hash differs from the record, and the field is pushed."""
    f = _filament(pg_pool)
    bb = {"id": 4, "name": "Shelf A", "identifier": "B7"}
    shelf, sync, _ = _shelf_seeded(pg_pool, f, bb)
    assert _bb_pushed(pg_pool, "filament_locations", shelf) == {
        UNSEEN_KEY: {"identifier": {"sb": field_hash("A1"), "bb": field_hash("B7")}}
    }
    f.locations.update(shelf, LocationPatch(identifier="C3"))
    (update,) = _plan(f, _snap([], locations=[bb]), sync).writes
    assert (update.op, update.bb_id, update.body) == ("update", 4, {"identifier": "C3"})


def test_the_reserved_key_is_no_field_name() -> None:
    """R-T4-27: ``bb_pushed`` maps field names to hashes, so its reserved key must never
    be the name of a field the plan pushes."""
    for fields in (_SUPPLIER_FIELDS, _LOCATION_FIELDS, _CATALOG_FIELDS, _FILAMENT_FIELDS):
        assert UNSEEN_KEY not in fields


def test_a_seeded_field_bambuddy_changed_again_is_not_pushed_blind(pg_pool: PgPool) -> None:
    """Bambuddy changed the seeded field again (``B7`` to ``B9``) and has no time: its
    value's age is unknown, so SB's later change is not pushed over it. A Bambuddy time
    older than SB's row time changes nothing: the row time does not show when SB changed
    the identifier, so both sides changed it, and the plan counts a conflict (R-T4-33)."""
    f = _filament(pg_pool)
    shelf, sync, _ = _shelf_seeded(pg_pool, f, {"id": 4, "name": "Shelf A", "identifier": "B7"})
    f.locations.update(shelf, LocationPatch(identifier="C3"))
    moved = {"id": 4, "name": "Shelf A", "identifier": "B9"}
    assert _plan(f, _snap([], locations=[moved]), sync).writes == []
    result = _plan(f, _snap([], locations=[moved | {"updated_at": _ago(30)}]), sync)
    assert (result.writes, result.conflicts) == ([], 1)


def test_a_seen_field_bambuddy_changed_is_not_pushed_for_another_sb_change(
    pg_pool: PgPool,
) -> None:
    """A row with no time per field (a location): Bambuddy changed the identifier after
    SB last saw it, and SB renamed the row later. SB's identifier is still what SB saw,
    so SB did not change it, and Bambuddy's newer value stays."""
    f = _filament(pg_pool)
    shelf = f.locations.create(
        LocationCreate(name="Shelf A", kind_id=_kind(f, "Shelf"), identifier="A1")
    )
    with pg_pool.connection() as conn:
        conn.execute(
            "UPDATE filament_locations SET bb_location_id = 4, bb_seen = %s,"
            " bb_seen_at = now() - interval '1 hour' WHERE id = %s",
            (Jsonb({"name": "Shelf A", "identifier": "A1"}), shelf.id),
        )
    sync = SPOOLS.model_copy(update={"spools": "off", "locations": "two_way"})
    f.locations.update(shelf.id, LocationPatch(name="Shelf Z"))
    bb = {"id": 4, "name": "Shelf A", "identifier": "B7", "updated_at": _ago(30)}
    (update,) = _plan(f, _snap([], locations=[bb]), sync).writes
    assert update.body == {"name": "Shelf Z"}


def test_a_value_bambuddy_changed_with_no_time_is_not_overwritten() -> None:
    """Bambuddy moved off what SB last saw and gives no time: its value's age is
    unknown, so SB's value is not pushed, however late SB's change."""
    assert not decide(
        sb="Red",
        bb_now="Blue",
        bb_seen="Green",
        sb_at=_T0 + timedelta(days=1),
        bb_at=None,
        direction="two_way",
    )


def test_a_fresh_seen_copy_without_a_field_reads_the_snapshot(pg_pool: PgPool) -> None:
    """A copy ``results`` stored in this pass holds only the fields its write sent
    (R-T4-28). A field it lacks is read from the snapshot's row, not taken as missing,
    so the identifier SB never changed is not pushed."""
    f = _filament(pg_pool)
    shelf = f.locations.create(
        LocationCreate(name="Shelf Z", kind_id=_kind(f, "Shelf"), identifier="A1")
    )
    with pg_pool.connection() as conn:
        conn.execute(
            "UPDATE filament_locations SET bb_location_id = 4, bb_seen = %s,"
            " bb_seen_at = now(), bb_pushed = %s WHERE id = %s",
            (Jsonb({"name": "Shelf Z"}), Jsonb({"name": field_hash("Shelf Z")}), shelf.id),
        )
    sync = SPOOLS.model_copy(update={"spools": "off", "locations": "two_way"})
    bb = {"id": 4, "name": "Shelf A", "identifier": "B7"}
    read_at = datetime.now(UTC) - timedelta(minutes=1)
    assert _plan(f, _snap([], locations=[bb], read_at=read_at), sync).writes == []


def test_a_legacy_link_does_not_put_an_empty_preset_list_under_two_way(pg_pool: PgPool) -> None:
    """Bambuddy's spool rows do not carry the preset list, so a legacy link's list in
    Bambuddy has an unknown value and age. Under ``two_way`` no ``PUT []`` goes over it:
    an event pass before seeding sends nothing, and seeding records SB's hash."""
    f = _filament(pg_pool)
    spool = _spool(f, _type(f))
    sync = SPOOLS.model_copy(update={"spools": "off", "presets": "two_way"})
    row = _matching(pg_pool, spool, 50, sync)
    _link(pg_pool, spool, 50)
    assert "presets" not in [w.op for w in _plan(f, _snap([row], full=False), sync).writes]
    assert "presets" not in [w.op for w in _plan(f, _snap([row]), sync).writes]
    with pg_pool.connection() as conn:
        link = conn.execute(
            "SELECT pushed_fields FROM filament_bb_links WHERE spool_id = %s", (spool,)
        ).fetchone()
    assert link is not None
    assert link["pushed_fields"]["filament_presets"] == field_hash(
        compare_form("filament_presets", _desired(pg_pool, spool, sync)["filament_presets"])
    )


# -- fix round 5: R-T4-30 to R-T4-33 -------------------------------------------------


def test_round_two_reads_bambuddys_newer_value_over_an_old_seen_one(pg_pool: PgPool) -> None:
    """R-T4-30 (re-review 3 I-A), rows: round 1 pushed the name, and ``results`` merged its
    answer (R-T4-28). The copy keeps the old seen identifier ``A1``, because Bambuddy's
    ``B7`` is not SB's ``C3``. Round 2 plans from the same snapshot. The old seen value is
    not SB's value, so it does not stand over the snapshot's ``B7``, and SB's ``C3`` is not
    pushed over Bambuddy's newer value."""
    f = _filament(pg_pool)
    shelf = f.locations.create(
        LocationCreate(name="Shelf Z", kind_id=_kind(f, "Shelf"), identifier="C3")
    )
    with pg_pool.connection() as conn:
        conn.execute(
            "UPDATE filament_locations SET bb_location_id = 4, bb_seen = %s,"
            " bb_seen_at = now(), bb_pushed = %s WHERE id = %s",
            (
                Jsonb({"name": "Shelf Z", "identifier": "A1"}),
                Jsonb({"name": field_hash("Shelf Z")}),
                shelf.id,
            ),
        )
    sync = SPOOLS.model_copy(update={"spools": "off", "locations": "two_way"})
    bb = {"id": 4, "name": "Shelf A", "identifier": "B7", "updated_at": _ago(5)}
    read_at = datetime.now(UTC) - timedelta(minutes=1)
    assert _plan(f, _snap([], locations=[bb], read_at=read_at), sync).writes == []


def test_round_two_reads_a_spools_newer_note_over_an_old_seen_one(pg_pool: PgPool) -> None:
    """R-T4-30 (re-review 3 I-A), spools: the merged copy keeps the old seen note, and the
    snapshot holds Bambuddy's newer one. SB's note changed before round 1's answer, so it
    is not pushed over Bambuddy's note in round 2."""
    f = _filament(pg_pool)
    spool = _spool(f, _type(f))
    f.spools.update(spool, SpoolPatch(note="SB's note"), actor="person")
    two_way = SPOOLS.model_copy(update={"spools": "two_way"})
    row = _matching(pg_pool, spool, 50, two_way)
    _link(pg_pool, spool, 50, seen=seen_row(BbSpool.model_validate(row | {"note": "Old note"})))
    with pg_pool.connection() as conn:
        conn.execute(
            "UPDATE filament_bb_links SET bb_seen_at = now(), bb_updated_at = now()"
            " WHERE spool_id = %s",
            (spool,),
        )
    read_at = datetime.now(UTC) - timedelta(minutes=1)
    snap = _snap([row | {"note": "BB's note", "updated_at": _ago(5)}], read_at=read_at)
    assert _plan(f, snap, two_way).writes == []


def _presets_two_way_after_off(pg_pool: PgPool, f: Filament) -> tuple[UUID, FilamentSync, Any]:
    """A link seeded by a full pass while ``presets`` was ``off``, then ``presets`` set to
    ``two_way``."""
    spool = _spool(f, _type(f))
    off = SPOOLS.model_copy(update={"spools": "two_way", "presets": "off"})
    row = _matching(pg_pool, spool, 50, off)
    _link(pg_pool, spool, 50)
    assert _plan(f, _snap([row]), off).writes == []
    assert _seen(pg_pool, "filament_bb_links", "spool_id", spool)[0] is not None
    return spool, off.model_copy(update={"presets": "two_way"}), row


def test_a_seeded_link_does_not_put_its_preset_list_after_a_switch(pg_pool: PgPool) -> None:
    """R-T4-31 (re-review 3 I-B): the link has a seen copy but never pushed the preset
    list, so SB does not know Bambuddy's list. Neither an event pass nor a full pass puts
    SB's list over it; a full pass records SB's hash."""
    f = _filament(pg_pool)
    spool, sync, row = _presets_two_way_after_off(pg_pool, f)
    assert "presets" not in [w.op for w in _plan(f, _snap([row], full=False), sync).writes]
    assert "presets" not in [w.op for w in _plan(f, _snap([row]), sync).writes]
    with pg_pool.connection() as conn:
        link = conn.execute(
            "SELECT pushed_fields FROM filament_bb_links WHERE spool_id = %s", (spool,)
        ).fetchone()
    assert link is not None
    assert link["pushed_fields"]["filament_presets"] == field_hash(
        compare_form("filament_presets", _desired(pg_pool, spool, sync)["filament_presets"])
    )


def test_a_create_records_bambuddys_empty_preset_list(pg_pool: PgPool) -> None:
    """R-T4-31: Bambuddy starts a new spool with an empty preset list, so the create's
    hashes hold the hash of ``[]``; the list then pushes by hash when SB's differs."""
    f = _filament(pg_pool)
    _spool(f, _type(f))
    sync = SPOOLS.model_copy(update={"presets": "two_way"})
    create = _only(_plan(f, _snap([]), sync).writes, "create")
    assert create.hashes["filament_presets"] == field_hash(compare_form("filament_presets", []))


@pytest.mark.parametrize("intent", [False, True], ids=["person-made", "sb-made"])
def test_adopting_a_row_a_person_made_pushes_no_differing_field(
    pg_pool: PgPool, intent: bool
) -> None:
    """R-T4-32 (re-review 3 open path a): under ``two_way``, an unlinked SB location adopts
    Bambuddy's row of the same name. With no intent a person made that row, so its
    differing identifier is not sent; the adoption records the seeding marks instead. With
    an old intent SB made the row (a lost answer), and SB's value is sent."""
    f = _filament(pg_pool)
    shelf = f.locations.create(
        LocationCreate(name="Shelf A", kind_id=_kind(f, "Shelf"), identifier="A1")
    )
    if intent:
        # SB's create sent ``B7``; SB has changed it to ``A1`` since, and Bambuddy still
        # holds what SB sent (R-T4-35).
        sent = {"name": field_hash("Shelf A"), "identifier": field_hash("B7")}
        with pg_pool.connection() as conn:
            conn.execute(
                "INSERT INTO filament_bb_creates (kind, sb_id, started_at, sent)"
                " VALUES ('location', %s, now() - interval '1 hour', %s)",
                (shelf.id, Jsonb(sent)),
            )
    sync = SPOOLS.model_copy(update={"spools": "off", "locations": "two_way"})
    bb = [{"id": 4, "name": "Shelf A", "identifier": "B7"}]
    (write,) = _plan(f, _snap([], locations=bb), sync).writes
    assert (write.op, write.bb_id) == ("update", 4)
    assert _intents(pg_pool) == [("location", shelf.id)]
    if intent:
        assert write.body == {"identifier": "A1"}
        return
    assert write.body == {}
    assert write.hashes == {"name": field_hash("Shelf A")}
    marks = {UNSEEN_KEY: {"identifier": {"sb": field_hash("A1"), "bb": field_hash("B7")}}}
    assert _bb_pushed(pg_pool, "filament_locations", shelf.id) == marks
    # The answer was lost: the adoption's own intent does not make the row SB's.
    (again,) = _plan(f, _snap([], locations=bb), sync).writes
    assert (again.bb_id, again.body) == (4, {})


def test_a_person_made_row_with_no_difference_stays_the_persons(pg_pool: PgPool) -> None:
    """R-T4-32: an adoption with nothing to hold back still marks the row as a person's.
    When its answer is lost and a person then changes the row in Bambuddy, the retry
    does not push SB's value over it."""
    f = _filament(pg_pool)
    f.locations.create(LocationCreate(name="Shelf A", kind_id=_kind(f, "Shelf"), identifier="A1"))
    sync = SPOOLS.model_copy(update={"spools": "off", "locations": "two_way"})
    (write,) = _plan(
        f, _snap([], locations=[{"id": 4, "name": "Shelf A", "identifier": "A1"}]), sync
    ).writes
    assert write.body == {}
    bb = [{"id": 4, "name": "Shelf A", "identifier": "B7"}]
    (again,) = _plan(f, _snap([], locations=bb), sync).writes
    assert (again.bb_id, again.body) == (4, {})


def test_a_field_both_sides_changed_is_a_conflict(pg_pool: PgPool) -> None:
    """R-T4-33 (re-review 3 open path b): SB and Bambuddy both moved the identifier off
    the seen ``A1``. SB's row time is after Bambuddy's, but it does not show when SB
    changed the identifier, so nothing is pushed and the plan counts one conflict."""
    f = _filament(pg_pool)
    shelf = f.locations.create(
        LocationCreate(name="Shelf A", kind_id=_kind(f, "Shelf"), identifier="A1")
    )
    with pg_pool.connection() as conn:
        conn.execute(
            "UPDATE filament_locations SET bb_location_id = 4, bb_seen = %s,"
            " bb_seen_at = now() - interval '1 hour' WHERE id = %s",
            (Jsonb({"name": "Shelf A", "identifier": "A1"}), shelf.id),
        )
    f.locations.update(shelf.id, LocationPatch(identifier="C3"))
    sync = SPOOLS.model_copy(update={"spools": "off", "locations": "two_way"})
    bb = {"id": 4, "name": "Shelf A", "identifier": "B7", "updated_at": _ago(30)}
    result = _plan(f, _snap([], locations=[bb]), sync)
    assert (result.writes, result.conflicts) == ([], 1)


# -- Task 5: R-T4-35 and R-T4-25 -----------------------------------------------------------


def _lost_location_create(pg_pool: PgPool, f: Filament, identifier: str) -> UUID:
    """SB location ``Shelf A`` whose create (with ``identifier``) was planned and whose
    answer was lost: its intent holds the hashes the create sent."""
    shelf = f.locations.create(
        LocationCreate(name="Shelf A", kind_id=_kind(f, "Shelf"), identifier=identifier)
    )
    sync = SPOOLS.model_copy(update={"spools": "off", "locations": "two_way"})
    (create,) = _plan(f, _snap([], locations=[]), sync).writes
    assert create.op == "create"
    return shelf.id


def _later() -> datetime:
    return datetime.now(UTC) + timedelta(minutes=1)


def test_a_person_edit_of_a_row_sb_made_is_not_pushed(pg_pool: PgPool) -> None:
    """R-T4-35: SB's create sent ``A1`` and its answer was lost. Bambuddy's row now holds
    ``B7``, so a person changed it after the create: the adoption does not send ``A1``
    over it, and records the field's marks as seeding does."""
    f = _filament(pg_pool)
    shelf = _lost_location_create(pg_pool, f, "A1")
    sync = SPOOLS.model_copy(update={"spools": "off", "locations": "two_way"})
    bb = [{"id": 4, "name": "Shelf A", "identifier": "B7"}]
    (adopt,) = _plan(f, _snap([], locations=bb, read_at=_later()), sync).writes
    assert (adopt.op, adopt.bb_id, adopt.body) == ("update", 4, {})
    assert adopt.hashes == {"name": field_hash("Shelf A")}
    assert _bb_pushed(pg_pool, "filament_locations", shelf) == {
        UNSEEN_KEY: {"identifier": {"sb": field_hash("A1"), "bb": field_hash("B7")}}
    }


def test_an_sb_change_after_a_lost_create_is_pushed(pg_pool: PgPool) -> None:
    """R-T4-35: Bambuddy still holds what SB's lost create sent (``B7``), and SB changed
    it to ``A1`` since: SB's value is newer, so the adoption sends it."""
    f = _filament(pg_pool)
    shelf = _lost_location_create(pg_pool, f, "B7")
    f.locations.update(shelf, LocationPatch(identifier="A1"))
    sync = SPOOLS.model_copy(update={"spools": "off", "locations": "two_way"})
    bb = [{"id": 4, "name": "Shelf A", "identifier": "B7"}]
    (adopt,) = _plan(f, _snap([], locations=bb, read_at=_later()), sync).writes
    assert (adopt.op, adopt.bb_id, adopt.body) == ("update", 4, {"identifier": "A1"})
    assert UNSEEN_KEY not in _bb_pushed(pg_pool, "filament_locations", shelf)


@pytest.mark.parametrize("person", [True, False], ids=["person-edit", "sb-edit"])
def test_a_lost_spool_create_pushes_only_what_bambuddy_kept(pg_pool: PgPool, person: bool) -> None:
    """R-T4-35 for spools: the create sent the note ``n1``. A person changed it in
    Bambuddy: the adoption leaves it, and its hash out. SB changed it, and Bambuddy still
    holds ``n1``: the adoption sends SB's note."""
    f = _filament(pg_pool)
    spool = _spool(f, _type(f))
    f.spools.update(spool, SpoolPatch(note="n1"), actor="person")
    sync = SPOOLS.model_copy(update={"spools": "two_way"})
    create = _only(_plan(f, _snap([]), sync).writes, "create")
    assert isinstance(create.body, dict) and create.body["note"] == "n1"
    made = (datetime.now(UTC) + timedelta(seconds=1)).replace(tzinfo=None).isoformat()
    row = _row(60, **create.body, created_at=made)
    if person:
        row["note"] = "a person's"
    else:
        f.spools.update(spool, SpoolPatch(note="n2"), actor="person")
    (adopt,) = _plan(f, _snap([row], read_at=_later()), sync).writes
    assert (adopt.op, adopt.bb_id) == ("update", 60)
    if person:
        assert adopt.body == {}
        assert "note" not in adopt.hashes
    else:
        assert adopt.body == {"note": "n2"}
        assert adopt.hashes["note"] == field_hash("n2")


def test_a_filament_entry_is_seeded_with_its_marks(pg_pool: PgPool) -> None:
    """R-T4-25: a linked filament catalog entry with no seen copy is seeded on a full
    pass, as suppliers and locations are: the fields that agree are seen, the others get
    their marks, and none of them is pushed blind under ``two_way``."""
    f = _filament(pg_pool)
    kind = _type(f)
    with pg_pool.connection() as conn:
        conn.execute("UPDATE filament_spool_types SET bb_filament_id = 8 WHERE id = %s", (kind,))
    sync = SPOOLS.model_copy(update={"spools": "two_way"})
    bb = {"id": 8, "name": "GFA00-RED", "type": "PLA", "brand": None, "color_hex": "#ff0000",
          "density": 9.9}  # fmt: skip
    writes = _plan(f, _snap([], filaments=[bb]), sync).writes
    # The fields Bambuddy's listing does not carry compare by hash (decision 48).
    for write in writes:
        assert not (_FILAMENT_FIELDS & set(write.body))
    seen, seen_at = _seen(pg_pool, "filament_spool_types", "id", kind)
    assert seen == {"name": "GFA00-RED", "type": "PLA", "brand": None, "color_hex": "#FF0000"}
    assert seen_at is not None
    marks = _bb_pushed(pg_pool, "filament_spool_types", kind)[UNSEEN_KEY]
    assert set(marks) == {"density"}


def test_an_sb_edit_after_a_lost_create_adopts_instead_of_creating_again(
    pg_pool: PgPool,
) -> None:
    """R-T5-6: SB changed a snapshot field (the colour name) after a create whose answer
    was lost. The retry matches Bambuddy's row against what the create sent, so it adopts
    that row and sends the change, and makes no duplicate."""
    f = _filament(pg_pool)
    spool = _spool(f, _type(f))
    create = _only(_plan(f, _snap([])).writes, "create")
    f.spools.update(spool, SpoolPatch(colour_name="Brick"), actor="person")
    made = (datetime.now(UTC) + timedelta(seconds=1)).replace(tzinfo=None).isoformat()
    assert isinstance(create.body, dict)
    row = _row(60, **create.body, created_at=made)
    (write,) = _plan(f, _snap([row], read_at=_later())).writes
    assert (write.op, write.bb_id, write.sb_id) == ("update", 60, spool)
    assert write.body == {"color_name": "Brick"}


def test_a_double_lost_answer_still_adopts(pg_pool: PgPool) -> None:
    """R-T5-9: the create's answer was lost, the retry's adoption sent SB's later colour
    and its answer was lost too. Bambuddy's row now holds SB's current value, not the
    sent one: it is still adopted, not created again."""
    f = _filament(pg_pool)
    spool = _spool(f, _type(f))
    create = _only(_plan(f, _snap([])).writes, "create")
    f.spools.update(spool, SpoolPatch(colour_name="Brick"), actor="person")
    made = (datetime.now(UTC) + timedelta(seconds=1)).replace(tzinfo=None).isoformat()
    assert isinstance(create.body, dict)
    row = _row(60, **(create.body | {"color_name": "Brick"}), created_at=made)
    (write,) = _plan(f, _snap([row], read_at=_later())).writes
    assert (write.op, write.bb_id, write.body) == ("update", 60, {})


# -- linked rows Bambuddy deleted (#6688, decisions 172-177) ------------------------------

#: Every data type that a row kind follows pushes.
_ROWS_PUSH = SPOOLS.model_copy(update={"suppliers": "sb_to_bb", "locations": "sb_to_bb"})
#: Each row kind's listing in the snapshot.
_LISTING = {
    "supplier": "suppliers",
    "location": "locations",
    "insert_type": "catalog",
    "filament": "filaments",
}


def _linked(pool: PgPool, f: Filament, kind: str, bb_id: int = 40) -> UUID:
    """An SB row of ``kind`` linked to Bambuddy's row ``bb_id``."""
    if kind == "supplier":
        sb_id = f.suppliers.create(SupplierCreate(name="Shop")).id
        sql = "UPDATE filament_suppliers SET bb_supplier_id = %s WHERE id = %s"
    elif kind == "location":
        sb_id = f.locations.create(LocationCreate(name="Shelf A", kind_id=_kind(f, "Shelf"))).id
        sql = "UPDATE filament_locations SET bb_location_id = %s WHERE id = %s"
    elif kind == "insert_type":
        sb_id = uuid4()
        with pool.connection() as conn:
            conn.execute(
                "INSERT INTO filament_insert_types (id, name, empty_weight_g)"
                " VALUES (%s, 'Cardboard', 250)",
                (sb_id,),
            )
        sql = "UPDATE filament_insert_types SET bb_catalog_id = %s WHERE id = %s"
    else:
        sb_id = _type(f)
        sql = "UPDATE filament_spool_types SET bb_filament_id = %s WHERE id = %s"
    with pool.connection() as conn:
        conn.execute(sql, (bb_id, sb_id))
    return sb_id


@pytest.mark.parametrize("kind", list(_LISTING))
def test_a_row_missing_from_its_listing_is_probed(pg_pool: PgPool, kind: str) -> None:
    """R-6688-1: a linked row missing from its kind's listing, which is read whole or not
    at all, gets one probe with its link's Bambuddy id, and no other write."""
    f = _filament(pg_pool)
    sb_id = _linked(pg_pool, f, kind)
    listings: dict[str, Any] = {_LISTING[kind]: []}
    writes = _plan(f, _snap([], **listings), _ROWS_PUSH).writes
    probes = [w for w in writes if w.kind == kind]
    assert [(w.op, w.sb_id, w.bb_id, w.body, w.hashes) for w in probes] == [
        ("probe", sb_id, 40, {}, {})
    ]
    assert probes[0].key == f"{kind}:{sb_id}:probe"


@pytest.mark.parametrize("kind", list(_LISTING))
def test_a_row_of_an_unread_listing_is_not_probed(pg_pool: PgPool, kind: str) -> None:
    """R-6688-1: a listing this pass did not read (``None``) tells nothing: no probe."""
    f = _filament(pg_pool)
    _linked(pg_pool, f, kind)
    writes = _plan(f, _snap([]), _ROWS_PUSH).writes
    assert [w for w in writes if w.kind == kind] == []


def test_a_row_whose_kind_does_not_push_is_not_probed(pg_pool: PgPool) -> None:
    """R-6688-1: only a kind that pushes is probed; the link stays as it is."""
    f = _filament(pg_pool)
    _linked(pg_pool, f, "location")
    sync = _ROWS_PUSH.model_copy(update={"locations": "bb_to_sb"})
    writes = _plan(f, _snap([], locations=[]), sync).writes
    assert [w for w in writes if w.kind == "location"] == []


def test_a_row_written_this_pass_is_not_probed(pg_pool: PgPool) -> None:
    """R-6688-1: ``results`` stored an answer after the snapshot was read (a create or a
    probe earlier in this pass): the snapshot cannot hold it, so it is not probed."""
    f = _filament(pg_pool)
    shelf = _linked(pg_pool, f, "location")
    with pg_pool.connection() as conn:
        conn.execute(
            "UPDATE filament_locations SET bb_seen = %s, bb_seen_at = now() WHERE id = %s",
            (Jsonb({"name": "Shelf A", "identifier": None}), shelf),
        )
    read_at = datetime.now(UTC) - timedelta(minutes=1)
    assert _plan(f, _snap([], locations=[], read_at=read_at), _ROWS_PUSH).writes == []


def test_a_listed_row_is_not_probed(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    _linked(pg_pool, f, "location")
    bb = [{"id": 40, "name": "Shelf A", "identifier": None}]
    assert _plan(f, _snap([], locations=bb), _ROWS_PUSH).writes == []


def test_a_row_refused_this_pass_is_not_probed_again(pg_pool: PgPool) -> None:
    """R-6688-3: a probe that failed in this pass (its error row is newer than the
    snapshot) waits for the next pass, as any refused write does (R-T4-1)."""
    f = _filament(pg_pool)
    shelf = _linked(pg_pool, f, "location")
    read_at = datetime.now(UTC) - timedelta(minutes=1)
    _refuse(pg_pool, "location", shelf)
    result = _plan(f, _snap([], locations=[], read_at=read_at), _ROWS_PUSH)
    assert result.writes == [] and result.failed == 1


def test_a_spool_waits_for_the_probe_of_its_location(pg_pool: PgPool) -> None:
    """R-6688-5: a spool whose location is missing from Bambuddy's listing sends nothing
    while the location's probe goes: Bambuddy may have deleted it, and a spool update
    naming a deleted location fails. It waits for a later round of this pass."""
    f = _filament(pg_pool)
    spool = _spool(f, _type(f))
    shelf = _linked(pg_pool, f, "location")
    _put(pg_pool, spool, shelf)
    row = _matching(pg_pool, spool, 50, _ROWS_PUSH)
    _link(pg_pool, spool, 50)
    result = _plan(f, _snap([row | {"location_id": None}], locations=[]), _ROWS_PUSH)
    assert [(w.kind, w.op) for w in result.writes] == [("location", "probe")]
    assert (result.deferred, result.blocked) == (1, 0)


def test_a_spool_is_blocked_while_its_locations_probe_failed(pg_pool: PgPool) -> None:
    """R-6688-5: the location's probe failed in this pass, so whether Bambuddy still has
    it is not known: the spool's write waits for a later pass (``blocked``)."""
    f = _filament(pg_pool)
    spool = _spool(f, _type(f))
    shelf = _linked(pg_pool, f, "location")
    _put(pg_pool, spool, shelf)
    row = _matching(pg_pool, spool, 50, _ROWS_PUSH)
    _link(pg_pool, spool, 50)
    read_at = datetime.now(UTC) - timedelta(minutes=1)
    _refuse(pg_pool, "location", shelf)
    snap = _snap([row | {"location_id": None}], locations=[], read_at=read_at)
    result = _plan(f, snap, _ROWS_PUSH)
    assert result.writes == []
    assert (result.deferred, result.blocked) == (0, 1)


def test_an_unlinked_row_is_made_again(pg_pool: PgPool) -> None:
    """R-6688-4: once the probe's 404 dropped the link, the next round creates the row
    again under the kind's direction, and the spool waits for that create."""
    f = _filament(pg_pool)
    spool = _spool(f, _type(f))
    shelf = f.locations.create(LocationCreate(name="Shelf A", kind_id=_kind(f, "Shelf"))).id
    _put(pg_pool, spool, shelf)
    row = _matching(pg_pool, spool, 50, _ROWS_PUSH)
    _link(pg_pool, spool, 50)
    result = _plan(f, _snap([row | {"location_id": 40}], locations=[]), _ROWS_PUSH)
    assert [(w.kind, w.op, w.sb_id) for w in result.writes] == [("location", "create", shelf)]
    assert result.deferred == 1
