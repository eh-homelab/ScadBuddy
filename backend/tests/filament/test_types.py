"""Spool types and effective values (#6261)."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from psycopg import errors
from pydantic import ValidationError

from scadbuddy.filament.catalog import CatalogExistsError, CatalogStore
from scadbuddy.filament.models import (
    Material,
    SpoolCreate,
    SpoolEvent,
    SpoolPatch,
    SpoolPresetCreate,
    SpoolTypeCreate,
    SpoolTypePatch,
    TypeKValue,
    TypePreset,
)
from scadbuddy.filament.spools import FilamentSpoolStore
from scadbuddy.filament.types import SpoolTypeStore, TypeTempError, effective, overridden
from scadbuddy.orders.models import FilamentPatch
from scadbuddy.orders.store import OrderStore
from tests.conftest import PgPool

pg = pytest.mark.requires_postgres


def test_effective_takes_spool_then_type_then_subtype_then_material() -> None:
    material = {"density_g_cm3": Decimal("1.24"), "nozzle_temp_min": 190}
    subtype = {"density_g_cm3": None, "nozzle_temp_min": 200, "material_id": UUID(int=1)}
    assert effective({}, {}, None, material).density_g_cm3 == Decimal("1.24")
    assert effective({}, {}, subtype, material).nozzle_temp_min == 200
    assert effective({}, {"nozzle_temp_min": 210}, subtype, material).nozzle_temp_min == 210
    spool = {"nozzle_temp_min": 220, "density_g_cm3": Decimal("1.30")}
    one = effective(spool, {"density_g_cm3": Decimal("1.27")}, subtype, material)
    assert one.density_g_cm3 == Decimal("1.30")
    assert one.nozzle_temp_min == 220
    assert "density_g_cm3" in overridden(spool)
    assert overridden({"density_g_cm3": None}) == []


def test_a_subtype_does_not_supply_the_material() -> None:
    subtype = {"material_id": UUID(int=1)}
    assert effective({}, {"material_id": UUID(int=2)}, subtype, None).material_id == UUID(int=2)
    assert effective({}, {}, subtype, None).material_id is None


def _material(pool: PgPool, name: str = "PLA") -> UUID:
    rows = CatalogStore(pool).list("materials")
    return next(r.id for r in rows if isinstance(r, Material) and r.name == name)


def _body(pool: PgPool, **over: object) -> SpoolTypeCreate:
    values: dict[str, object] = {
        "code": "PLA-WHT",
        "material_id": _material(pool),
        "rgba": "#FFFFFF",
        "label_weight_g": 1000,
    }
    return SpoolTypeCreate.model_validate(values | over)


@pg
def test_a_priced_type_writes_its_price_book_row(pg_pool: PgPool) -> None:
    store = SpoolTypeStore(pg_pool)
    orders = OrderStore(pg_pool)
    made = store.create(_body(pg_pool, cost_per_kg="19.99"))
    assert made.filament_id is not None
    book = orders.filament(made.filament_id)
    assert book is not None
    assert (book.material, book.colour, Decimal(book.cost_per_kg)) == (
        "PLA",
        "#FFFFFF",
        Decimal("19.99"),
    )
    changed = store.update(made.id, SpoolTypePatch(cost_per_kg=Decimal("21.5")), actor="ann")
    assert changed is not None
    assert changed.filament_id == made.filament_id
    book = orders.filament(made.filament_id)
    assert book is not None
    assert Decimal(book.cost_per_kg) == Decimal("21.5")


@pg
def test_a_new_type_takes_the_next_shared_number(pg_pool: PgPool) -> None:
    store = SpoolTypeStore(pg_pool)
    with pg_pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO items (kind_id, name) VALUES ('hardware', 'Grease') RETURNING number"
        ).fetchone()
    assert row is not None
    first = store.create(_body(pg_pool))
    second = store.create(_body(pg_pool, code="OTHER"))
    assert first.number > row["number"]
    assert second.number > first.number
    got = store.get(first.id)
    assert got is not None
    assert got.number == first.number


@pg
def test_an_unpriced_type_makes_no_price_book_row(pg_pool: PgPool) -> None:
    made = SpoolTypeStore(pg_pool).create(_body(pg_pool))
    assert made.filament_id is None


@pg
def test_a_type_joins_an_existing_price_book_row(pg_pool: PgPool) -> None:
    store = SpoolTypeStore(pg_pool)
    first = store.create(_body(pg_pool, cost_per_kg="10"))
    second = store.create(_body(pg_pool, code="OTHER", rgba="#ffffff", cost_per_kg="12"))
    assert second.filament_id == first.filament_id


@pg
def test_a_translucent_type_makes_a_translucent_book_row(pg_pool: PgPool) -> None:
    """The book row keeps the type's alpha, stops and effect (#6209 landed in #6211)."""
    rows = [r.model_dump() for r in CatalogStore(pg_pool).list("effects")]
    effect = next(r["id"] for r in rows if r["name"] == "Dual Color")
    made = SpoolTypeStore(pg_pool).create(
        _body(
            pg_pool,
            rgba="#FFFFFF80",
            extra_colours=["#FF0000", "#00FF00"],
            effect_id=effect,
            cost_per_kg="10",
        )
    )
    book = OrderStore(pg_pool).filament(made.filament_id or "")
    assert book is not None
    assert (book.colour, book.alpha, book.extra_colours, book.effect) == (
        "#FFFFFF",
        0x80,
        ["#FF0000FF", "#00FF00FF"],
        "dual-color",
    )


@pg
def test_an_opaque_and_a_translucent_type_of_one_hue_get_two_rows(pg_pool: PgPool) -> None:
    store = SpoolTypeStore(pg_pool)
    orders = OrderStore(pg_pool)
    solid = store.create(_body(pg_pool, cost_per_kg="10"))
    clear = store.create(_body(pg_pool, code="CLEAR", rgba="#FFFFFF80", cost_per_kg="12"))
    assert solid.filament_id is not None
    assert clear.filament_id is not None
    assert clear.filament_id != solid.filament_id
    books = [orders.filament(solid.filament_id), orders.filament(clear.filament_id)]
    assert [b.alpha if b is not None else None for b in books] == [0xFF, 0x80]
    again = store.create(_body(pg_pool, code="CLEAR2", rgba="#FFFFFF80", cost_per_kg="12"))
    assert again.filament_id == clear.filament_id


@pg
def test_match_compares_alpha(pg_pool: PgPool) -> None:
    store = SpoolTypeStore(pg_pool)
    store.create(_body(pg_pool, code="SOLID", rgba="#FFFFFFFF"))
    clear = store.create(_body(pg_pool, code="CLEAR", rgba="#FFFFFF80"))
    found = store.match(
        brand=None, material="PLA", subtype=None, rgba="#FFFFFF80", label_weight_g=1000
    )
    assert found is not None
    assert found.id == clear.id


@pg
def test_rrggbb_input_matches_rgba(pg_pool: PgPool) -> None:
    store = SpoolTypeStore(pg_pool)
    solid = store.create(_body(pg_pool, code="SOLID", rgba="#ffffffff"))
    found = store.match(
        brand=None, material="pla", subtype=None, rgba="ffffff", label_weight_g=1000
    )
    assert found is not None
    assert found.id == solid.id
    assert (
        store.match(brand=None, material="PLA", subtype=None, rgba="#FFFFFF", label_weight_g=250)
        is None
    )


@pg
def test_match_by_code_ignores_case_and_archived(pg_pool: PgPool) -> None:
    store = SpoolTypeStore(pg_pool)
    made = store.create(_body(pg_pool, code="Abc-1"))
    found = store.match(
        code="aBC-1", brand=None, material="x", subtype=None, rgba="#000000", label_weight_g=1
    )
    assert found is not None
    assert found.id == made.id
    store.archive(made.id, True, actor="ann")
    assert (
        store.match(
            code="abc-1", brand=None, material="x", subtype=None, rgba="#000000", label_weight_g=1
        )
        is None
    )


@pg
def test_a_taken_code_is_refused(pg_pool: PgPool) -> None:
    store = SpoolTypeStore(pg_pool)
    store.create(_body(pg_pool))
    with pytest.raises(CatalogExistsError):
        store.create(_body(pg_pool, code="pla-wht"))


@pg
def test_a_temperature_range_must_not_be_backwards(pg_pool: PgPool) -> None:
    store = SpoolTypeStore(pg_pool)
    with pytest.raises(TypeTempError):
        store.create(_body(pg_pool, nozzle_temp_min=250, nozzle_temp_max=200))
    made = store.create(_body(pg_pool, bed_temp_min=50, bed_temp_max=60))
    with pytest.raises(TypeTempError):
        store.update(made.id, SpoolTypePatch(bed_temp_max=40), actor="ann")
    unchanged = store.get(made.id)
    assert unchanged is not None
    assert unchanged.bed_temp_max == 60


@pg
def test_presets_and_k_values_are_replaced_whole(pg_pool: PgPool) -> None:
    store = SpoolTypeStore(pg_pool)
    made = store.create(_body(pg_pool))
    one = TypePreset(
        type_id=made.id,
        printer_model="H2C",
        nozzle_diameter="0.4",
        slicer_filament="GFA00",
        slicer_filament_name=None,
    )
    assert store.set_presets(made.id, [one], actor="ann") == [one]
    assert store.presets(made.id) == [one]
    assert store.set_presets(made.id, [], actor="ann") == []
    assert store.presets(made.id) == []
    assert store.set_presets(UUID(int=9), [one], actor="ann") is None
    unknown_printer = TypeKValue(
        type_id=made.id,
        printer_id=UUID(int=9),
        extruder=0,
        nozzle_diameter="0.4",
        nozzle_type=None,
        k_value=Decimal("0.02"),
        name=None,
        cali_idx=None,
        setting_id=None,
    )
    with pytest.raises(ValueError, match="points at"):
        store.set_k_values(made.id, [unknown_printer], actor="ann")


@pg
def test_list_hides_archived_types(pg_pool: PgPool) -> None:
    store = SpoolTypeStore(pg_pool)
    made = store.create(_body(pg_pool))
    assert [t.id for t in store.list()] == [made.id]
    store.archive(made.id, True, actor="ann")
    assert store.list() == []
    assert [t.id for t in store.list(include_archived=True)] == [made.id]


@pg
def test_changing_a_type_colour_never_rewrites_a_shared_row(pg_pool: PgPool) -> None:
    store = SpoolTypeStore(pg_pool)
    orders = OrderStore(pg_pool)
    a = store.create(_body(pg_pool, code="A", rgba="#FFFFFFFF", cost_per_kg="10"))
    b = store.create(_body(pg_pool, code="B", rgba="#FFFFFF", cost_per_kg="10"))
    assert a.filament_id == b.filament_id
    shared = a.filament_id
    assert shared is not None
    changed = store.update(a.id, SpoolTypePatch(rgba="#FF0000"), actor="ann")
    assert changed is not None
    assert changed.filament_id != shared
    kept = orders.filament(shared)
    assert kept is not None
    assert kept.colour == "#FFFFFF"
    moved = store.get(b.id)
    assert moved is not None
    assert moved.filament_id == shared
    fresh = orders.filament(changed.filament_id or "")
    assert fresh is not None
    assert fresh.colour == "#FF0000"


@pg
def test_a_patch_without_price_or_identity_leaves_the_row_alone(pg_pool: PgPool) -> None:
    store = SpoolTypeStore(pg_pool)
    orders = OrderStore(pg_pool)
    made = store.create(_body(pg_pool, colour_name="Snow", cost_per_kg="10"))
    assert made.filament_id is not None
    book = orders.filament(made.filament_id)
    assert book is not None
    orders.update_filament(made.filament_id, FilamentPatch(colour_name="Mine"))
    store.update(made.id, SpoolTypePatch(note="x"), actor="ann")
    store.archive(made.id, True, actor="ann")
    again = orders.filament(made.filament_id)
    assert again is not None
    assert again.colour_name == "Mine"
    assert again.archived_at is None


@pg
def test_a_failure_after_the_price_book_write_rolls_back_the_type(
    pg_pool: PgPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SpoolTypeStore(pg_pool)
    real = OrderStore.insert_filament

    def inserted_then_failed(self: OrderStore, *args: Any, **kwargs: Any) -> Any:
        real(self, *args, **kwargs)
        raise RuntimeError("boom")

    monkeypatch.setattr(OrderStore, "insert_filament", inserted_then_failed)
    with pytest.raises(RuntimeError):
        store.create(_body(pg_pool, cost_per_kg="10"))
    assert store.list(include_archived=True) == []
    assert OrderStore(pg_pool).filaments() == []


@pg
def test_type_keeps_lead_time_and_safety_margin(pg_pool: PgPool) -> None:
    store = SpoolTypeStore(pg_pool)
    made = store.create(_body(pg_pool, lead_time_days=10, safety_margin_days=2))
    got = store.get(made.id)
    assert got is not None
    assert (got.lead_time_days, got.safety_margin_days) == (10, 2)
    store.update(made.id, SpoolTypePatch(lead_time_days=None), actor="ann")
    after = store.get(made.id)
    assert after is not None
    assert (after.lead_time_days, after.safety_margin_days) == (None, 2)


def test_negative_lead_time_is_refused() -> None:
    with pytest.raises(ValidationError):
        SpoolTypePatch(lead_time_days=-1)


def _edits(spools: FilamentSpoolStore, spool_id: UUID, after: int = 0) -> list[SpoolEvent]:
    return [e for e in spools.events(spool_id) if e.kind == "edited"][after:]


@pg
def test_type_edit_writes_edited_on_spools_that_inherit(pg_pool: PgPool) -> None:
    store = SpoolTypeStore(pg_pool)
    spools = FilamentSpoolStore(pg_pool)
    made = store.create(_body(pg_pool, cost_per_kg="20"))
    a = spools.create(SpoolCreate(type_id=made.id), actor="ann").id
    b = spools.create(SpoolCreate(type_id=made.id, cost_per_kg=Decimal(30)), actor="ann").id
    c = spools.create(SpoolCreate(type_id=made.id), actor="ann").id
    spools.update(c, SpoolPatch(archived=True), actor="ann")
    before = {s: len(_edits(spools, s)) for s in (a, b, c)}
    store.update(made.id, SpoolTypePatch(cost_per_kg=Decimal(25)), actor="user:bo")
    [event] = _edits(spools, a, before[a])
    changes = event.data["changes"]
    assert list(changes) == ["cost_per_kg"]
    assert [Decimal(v) for v in changes["cost_per_kg"]] == [20, 25]
    assert event.data["type_id"] == str(made.id)
    assert event.actor == "user:bo"
    assert _edits(spools, b, before[b]) == []
    assert _edits(spools, c, before[c]) == []


@pg
def test_type_edit_of_a_field_not_overridable_writes_nothing(pg_pool: PgPool) -> None:
    store = SpoolTypeStore(pg_pool)
    spools = FilamentSpoolStore(pg_pool)
    made = store.create(_body(pg_pool))
    a = spools.create(SpoolCreate(type_id=made.id), actor="ann").id
    before = len(spools.events(a))
    store.update(made.id, SpoolTypePatch(note="x", lead_time_days=5), actor="user:bo")
    assert len(spools.events(a)) == before


@pg
def test_type_label_weight_edit_writes_edited_on_inheriting_spools(pg_pool: PgPool) -> None:
    store = SpoolTypeStore(pg_pool)
    spools = FilamentSpoolStore(pg_pool)
    made = store.create(_body(pg_pool))
    a = spools.create(SpoolCreate(type_id=made.id), actor="ann").id
    store.update(made.id, SpoolTypePatch(label_weight_g=800), actor="user:bo")
    [event] = _edits(spools, a)
    assert event.data["changes"]["label_weight_g"] == [1000, 800]


@pg
def test_type_presets_and_k_values_edit_writes_edited_on_spools_without_their_own(
    pg_pool: PgPool,
) -> None:
    store = SpoolTypeStore(pg_pool)
    spools = FilamentSpoolStore(pg_pool)
    made = store.create(_body(pg_pool))
    a = spools.create(SpoolCreate(type_id=made.id), actor="ann").id
    b = spools.create(SpoolCreate(type_id=made.id), actor="ann").id
    own = SpoolPresetCreate(printer_model="H2C", nozzle_diameter="0.4", slicer_filament="MINE")
    spools.set_presets(b, [own], actor="ann")
    before = {s: len(_edits(spools, s)) for s in (a, b)}
    row = TypePreset(
        type_id=made.id,
        printer_model="H2C",
        nozzle_diameter="0.4",
        slicer_filament="GFA00",
        slicer_filament_name=None,
    )
    store.set_presets(made.id, [row], actor="user:bo")
    [event] = _edits(spools, a, before[a])
    old, new = event.data["changes"]["presets"]
    assert old == []
    assert [r["slicer_filament"] for r in new] == ["GFA00"]
    assert event.data["type_id"] == str(made.id)
    assert event.actor == "user:bo"
    assert _edits(spools, b, before[b]) == []
    store.set_k_values(made.id, [], actor="user:bo")
    assert _edits(spools, a, before[a] + 1) == []


def _printer(pool: PgPool) -> UUID:
    printer = UUID(int=0x6269)
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO printers (id, serial, name) VALUES (%s, 'S6269', 'Garage')", (printer,)
        )
    return printer


def _preset(type_id: UUID, nozzle: str, slicer: str) -> TypePreset:
    return TypePreset(
        type_id=type_id,
        printer_model="H2C",
        nozzle_diameter=nozzle,
        slicer_filament=slicer,
        slicer_filament_name=None,
    )


@pg
def test_type_preset_edit_reaches_a_spool_that_sets_another_key(pg_pool: PgPool) -> None:
    store = SpoolTypeStore(pg_pool)
    spools = FilamentSpoolStore(pg_pool)
    made = store.create(_body(pg_pool))
    a = spools.create(SpoolCreate(type_id=made.id), actor="ann").id
    own = SpoolPresetCreate(printer_model="H2C", nozzle_diameter="0.4", slicer_filament="MINE")
    spools.set_presets(a, [own], actor="ann")
    before = len(_edits(spools, a))
    store.set_presets(made.id, [_preset(made.id, "0.6", "GFA06")], actor="user:bo")
    [event] = _edits(spools, a, before)
    assert event.data["type_id"] == str(made.id)
    store.set_presets(
        made.id,
        [_preset(made.id, "0.4", "GFA04"), _preset(made.id, "0.6", "GFA06")],
        actor="user:bo",
    )
    assert _edits(spools, a, before + 1) == []


@pg
def test_type_k_value_edit_writes_edited_on_an_inheriting_spool(pg_pool: PgPool) -> None:
    store = SpoolTypeStore(pg_pool)
    spools = FilamentSpoolStore(pg_pool)
    printer = _printer(pg_pool)
    made = store.create(_body(pg_pool))
    a = spools.create(SpoolCreate(type_id=made.id), actor="ann").id
    before = len(_edits(spools, a))
    row = TypeKValue(
        type_id=made.id,
        printer_id=printer,
        extruder=0,
        nozzle_diameter="0.4",
        nozzle_type=None,
        k_value=Decimal("0.02"),
        name=None,
        cali_idx=None,
        setting_id=None,
    )
    store.set_k_values(made.id, [row], actor="user:bo")
    [event] = _edits(spools, a, before)
    old, new = event.data["changes"]["k_values"]
    assert old == []
    assert [Decimal(r["k_value"]) for r in new] == [Decimal("0.02")]
    assert event.data["type_id"] == str(made.id)
    assert event.actor == "user:bo"


@pg
def test_a_type_keeps_its_tdfp_id(pg_pool: PgPool) -> None:
    store = SpoolTypeStore(pg_pool)
    made = store.create(_body(pg_pool, tdfp_id=30173))
    assert made.tdfp_id == 30173
    got = store.get(made.id)
    assert got is not None
    assert got.tdfp_id == 30173
    assert store.create(_body(pg_pool, code="NONE")).tdfp_id is None
    changed = store.update(made.id, SpoolTypePatch(tdfp_id=5), actor="ann")
    assert changed is not None
    assert changed.tdfp_id == 5
    with pytest.raises(ValueError, match="greater than 0"):
        _body(pg_pool, tdfp_id=0)
    with pytest.raises(ValueError, match="greater than 0"):
        SpoolTypePatch(tdfp_id=0)


def _fetch(pool: PgPool, type_id: UUID, status: str) -> None:
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO filament_profile_fetches (type_id, tdfp_id, targets, status)"
            " VALUES (%s, 1, '[]', %s)",
            (type_id, status),
        )


@pg
def test_one_running_fetch_per_type(pg_pool: PgPool) -> None:
    made = SpoolTypeStore(pg_pool).create(_body(pg_pool))
    _fetch(pg_pool, made.id, "running")
    _fetch(pg_pool, made.id, "failed")
    with pytest.raises(errors.UniqueViolation):
        _fetch(pg_pool, made.id, "running")


@pg
def test_deleting_a_fetch_releases_its_blobs(pg_pool: PgPool) -> None:
    made = SpoolTypeStore(pg_pool).create(_body(pg_pool))
    with pg_pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO filament_profile_fetches (type_id, tdfp_id, targets, status)"
            " VALUES (%s, 1, '[]', 'imported') RETURNING id",
            (made.id,),
        ).fetchone()
        assert row is not None
        conn.execute(
            "INSERT INTO blob_refs (key, holder_kind, holder_id) VALUES ('pf', %s, %s)",
            ("profile_fetch", str(row["id"])),
        )
        conn.execute("DELETE FROM filament_spool_types WHERE id = %s", (made.id,))
        held = conn.execute(
            "SELECT count(*) AS n FROM blob_refs WHERE holder_kind = 'profile_fetch'"
        ).fetchone()
    assert held is not None
    assert held["n"] == 0


class _RacedTdfp(SpoolTypeStore):
    """Both callers reach their write together (a `threading.Barrier`, as
    `test_invoices_race.py`), so the row's lock is where the two meet."""

    def __init__(self, pool: PgPool) -> None:
        super().__init__(pool)
        self._gate = threading.Barrier(2, timeout=20)

    def set_tdfp_id(self, type_id: UUID, tdfp_id: int) -> bool:
        self._gate.wait()
        return super().set_tdfp_id(type_id, tdfp_id)


@pg
def test_two_sets_of_a_types_tdfp_id_keep_the_first(pg_pool: PgPool) -> None:
    """R-7025-14: two creates for one type, under different keys, set its ``tdfp_id`` at
    once; one write wins, and the other finds that id and writes nothing."""
    made = SpoolTypeStore(pg_pool).create(_body(pg_pool))
    store = _RacedTdfp(pg_pool)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda i: store.set_tdfp_id(made.id, i), [40001, 40002]))
    assert sorted(results) == [False, True]
    won = 40001 if results[0] else 40002
    kept = SpoolTypeStore(pg_pool).get(made.id)
    assert kept is not None and kept.tdfp_id == won


@pg
def test_a_tdfp_id_set_again_holds_and_another_does_not_replace_it(pg_pool: PgPool) -> None:
    store = SpoolTypeStore(pg_pool)
    made = store.create(_body(pg_pool))
    assert store.set_tdfp_id(made.id, 40001)
    assert store.set_tdfp_id(made.id, 40001)
    assert not store.set_tdfp_id(made.id, 40002)
    kept = store.get(made.id)
    assert kept is not None and kept.tdfp_id == 40001
    assert not store.set_tdfp_id(UUID(int=1), 40001)


def _rows(pool: PgPool, kind: str) -> dict[str, dict[str, Any]]:
    listed = CatalogStore(pool).list(kind, include_archived=True)
    return {row["name"]: row for row in (r.model_dump() for r in listed)}


def _names(pool: PgPool, kind: str) -> list[str]:
    return [r.model_dump()["name"] for r in CatalogStore(pool).list(kind, include_archived=True)]


@pg
def test_a_type_create_adds_and_links_a_named_brand_material_and_subtype(pg_pool: PgPool) -> None:
    store = SpoolTypeStore(pg_pool)
    body = _body(pg_pool, material_id=None, material_name="Nylon X", brand_name="Acme")
    made = store.create(body.model_copy(update={"subtype_name": "Glossy X"}))
    material = _rows(pg_pool, "materials")["Nylon X"]
    subtype = _rows(pg_pool, "subtypes")["Glossy X"]
    assert made.material_id == material["id"]
    assert made.brand_id == _rows(pg_pool, "brands")["Acme"]["id"]
    assert made.subtype_id == subtype["id"]
    assert subtype["material_id"] == material["id"]


@pg
def test_a_type_create_resolves_an_existing_name_to_its_row(pg_pool: PgPool) -> None:
    store = SpoolTypeStore(pg_pool)
    first = store.create(_body(pg_pool, brand_name="Acme"))
    again = store.create(_body(pg_pool, code="PLA-BLK", brand_name="acme"))
    assert again.brand_id == first.brand_id
    assert store.create(_body(pg_pool, code="PLA-RED", material_id=None, material_name="pla"))
    assert _names(pg_pool, "brands").count("Acme") == 1
    assert [n for n in _names(pg_pool, "materials") if n.upper() == "PLA"] == ["PLA"]


@pg
def test_a_refused_type_create_adds_no_named_row(pg_pool: PgPool) -> None:
    store = SpoolTypeStore(pg_pool)
    store.create(_body(pg_pool))
    with pytest.raises(CatalogExistsError):
        store.create(_body(pg_pool, brand_name="Acme", subtype_name="Glossy X"))
    assert "Acme" not in _names(pg_pool, "brands")
    assert "Glossy X" not in _names(pg_pool, "subtypes")


def test_a_type_create_takes_an_id_or_a_name_not_both() -> None:
    base = {"code": "X", "rgba": "#FFFFFF", "label_weight_g": 1000}
    one = UUID(int=1)
    with pytest.raises(ValidationError, match="material_id or material_name"):
        SpoolTypeCreate.model_validate(base)
    for noun in ("brand", "material", "subtype"):
        body = base | {"material_id": one, f"{noun}_id": one, f"{noun}_name": "A"}
        with pytest.raises(ValidationError, match=f"{noun}_id or {noun}_name, not both"):
            SpoolTypeCreate.model_validate(body)
    assert SpoolTypeCreate.model_validate(base | {"material_name": "PLA"}).material_id is None
