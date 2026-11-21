"""Filling spool types from their slicer profiles over the store (#6714,
`filament/profile_fill.py` `TypeFiller`): empty fields only, the refresh, the apply,
and the K values of their linked Bambuddy spools."""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx
import pytest
import respx

from scadbuddy.filament import profile_fill
from scadbuddy.filament.catalog import CatalogStore
from scadbuddy.filament.models import (
    Material,
    MaterialPatch,
    SpoolCreate,
    SpoolTypeCreate,
    SpoolTypePatch,
    TypeKValue,
    TypeKValueCreate,
    TypePreset,
)
from scadbuddy.filament.profile_fill import ProfileChangedError, TypeFiller, _rows
from scadbuddy.filament.spools import FilamentSpoolStore
from scadbuddy.filament.types import SpoolTypeStore, TypeTempError
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.orders.density import DensityTable
from scadbuddy.slicing.profiles import Profiles
from tests.conftest import PgPool
from tests.filament.test_profile_fill import BASE, TREE, local_preset

pytestmark = pytest.mark.requires_postgres

LINKED = StoredSettings(bambuddy_url=BASE, bambuddy_api_key="k")
UNLINKED = StoredSettings()
ACTOR = "person:test"
API = f"{BASE}/api/v1"


@pytest.fixture
def types(pg_pool: PgPool) -> SpoolTypeStore:
    return SpoolTypeStore(pg_pool)


@pytest.fixture
def filler(types: SpoolTypeStore) -> TypeFiller:
    table = DensityTable.build(Profiles(TREE))
    return TypeFiller(types, lambda: table)


def _type(types: SpoolTypeStore, pool: PgPool, code: str = "SILK", **over: Any) -> UUID:
    material = next(
        r.id
        for r in CatalogStore(pool).list("materials")
        if isinstance(r, Material) and r.name == "PLA"
    )
    body = {"code": code, "material_id": material, "rgba": "#FFFFFF", "label_weight_g": 1000}
    return types.create(SpoolTypeCreate.model_validate(body | over)).id


def _presets(types: SpoolTypeStore, type_id: UUID, *rows: tuple[str, str, str]) -> None:
    types.set_presets(
        type_id,
        [
            TypePreset(
                type_id=type_id,
                printer_model=model,
                nozzle_diameter=nozzle,
                slicer_filament=filament,
                slicer_filament_name=None,
            )
            for model, nozzle, filament in rows
        ],
        actor=ACTOR,
    )


async def test_only_empty_fields_are_filled_from_the_h2c_04_preset(
    filler: TypeFiller, types: SpoolTypeStore, pg_pool: PgPool
) -> None:
    type_id = _type(types, pg_pool, nozzle_temp_max=245)
    _presets(types, type_id, ("H2C", "0.2", "GFG99"), ("H2C", "0.4", "GFA05"))

    (done,) = await filler.fill(UNLINKED, [type_id], actor=ACTOR)

    assert done.reason is None
    assert done.source is not None
    assert (done.source.name, done.source.origin) == (
        "Bambu PLA Silk @BBL H2C 0.4 nozzle",
        "system",
    )
    # The maximum a person set stands; the diameter is never empty.
    assert done.filled == {
        "nozzle_temp_min": 190,
        "bed_temp_min": 55,
        "bed_temp_max": 60,
        "density_g_cm3": Decimal("1.32"),
        # The profile's drying values (#6720, R-6714-9).
        "drying_temp_c": 45,
        "drying_hours": Decimal("12"),
    }
    assert (done.written, done.cost_hint) == (True, "21.99")
    row = types.get(type_id)
    assert row is not None
    assert (row.nozzle_temp_min, row.nozzle_temp_max, row.bed_temp_min, row.bed_temp_max) == (
        190,
        245,
        55,
        60,
    )
    assert row.density_g_cm3 == Decimal("1.32")
    assert (row.drying_temp_c, row.drying_hours) == (45, Decimal("12"))

    again = await filler.fill(UNLINKED, [type_id], actor=ACTOR)
    assert (again[0].filled, again[0].written) == ({}, False)


async def test_a_fill_leaves_a_drying_field_the_material_sets_and_a_refresh_offers_it(
    filler: TypeFiller, types: SpoolTypeStore, pg_pool: PgPool
) -> None:
    """#6856 (R-6714-20): the type comes first in a spool's drying defaults, so a fill that
    wrote the profile's drying value would hide the one a person set on the material. The
    fill leaves that field empty and says why; a refresh still offers it."""
    catalog = CatalogStore(pg_pool)
    material = next(
        r.id for r in catalog.list("materials") if isinstance(r, Material) and r.name == "PLA"
    )
    catalog.update("materials", material, MaterialPatch(drying_temp_c=50))
    type_id = _type(types, pg_pool)
    _presets(types, type_id, ("H2C", "0.4", "GFA05"))

    (done,) = await filler.fill(UNLINKED, None, actor=ACTOR)

    assert "drying_temp_c" not in done.filled
    assert done.filled["drying_hours"] == Decimal("12")
    assert "material" in done.skipped["drying_temp_c"]
    row = types.get(type_id)
    assert row is not None
    assert row.drying_temp_c is None

    (preview,) = await filler.fill(UNLINKED, [type_id], actor=ACTOR, refresh=True)
    assert preview.filled == {"drying_temp_c": 45}


async def test_a_fill_writes_edited_on_spools_that_inherit_the_fields(
    filler: TypeFiller, types: SpoolTypeStore, pg_pool: PgPool
) -> None:
    """R-T9-4: a fill is a type edit; each field it writes fans out, as ``actor``, to a
    spool that does not set that field itself."""
    spools = FilamentSpoolStore(pg_pool)
    type_id = _type(types, pg_pool)
    _presets(types, type_id, ("H2C", "0.4", "GFA05"))
    heir = spools.create(SpoolCreate(type_id=type_id), actor="ann").id
    own = spools.create(SpoolCreate(type_id=type_id, nozzle_temp_min=200), actor="ann").id
    before = {s: len(spools.events(s)) for s in (heir, own)}

    await filler.fill(UNLINKED, [type_id], actor=ACTOR)

    [event] = [e for e in spools.events(heir)[before[heir] :] if e.kind == "edited"]
    assert event.actor == ACTOR
    assert event.data["changes"]["nozzle_temp_min"] == [None, 190]
    assert set(event.data["changes"]) == {
        "nozzle_temp_min",
        "nozzle_temp_max",
        "bed_temp_min",
        "bed_temp_max",
        "density_g_cm3",
        # A spool may override the drying values too (R-6714-18).
        "drying_temp_c",
        "drying_hours",
    }
    [mine] = [e for e in spools.events(own)[before[own] :] if e.kind == "edited"]
    assert "nozzle_temp_min" not in mine.data["changes"]


async def test_a_fill_that_would_leave_a_range_backwards_skips_that_pair(
    filler: TypeFiller, types: SpoolTypeStore, pg_pool: PgPool
) -> None:
    type_id = _type(types, pg_pool, nozzle_temp_max=180)
    _presets(types, type_id, ("H2C", "0.4", "GFA05"))

    (done,) = await filler.fill(UNLINKED, [type_id], actor=ACTOR)

    assert "nozzle_temp_min" not in done.filled
    assert done.skipped["nozzle_temp_min"].startswith("nozzle_temp_min 190 would be above")
    row = types.get(type_id)
    assert row is not None
    assert (row.nozzle_temp_min, row.bed_temp_min) == (None, 55)


async def test_a_range_set_meanwhile_skips_only_its_pair_and_the_rest_are_filled(
    filler: TypeFiller, types: SpoolTypeStore, pg_pool: PgPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R-6714-e (#6762): a person sets one end of a range between the read and the row
    lock; the `TypeTempError` skips only that pair, never a 500. The type's other fields
    and the types after it are filled."""
    raced = _type(types, pg_pool, "RACED")
    later = _type(types, pg_pool, "LATER")
    for type_id in (raced, later):
        _presets(types, type_id, ("H2C", "0.4", "GFA05"))
    fill_empty = types.fill_empty

    def meanwhile(type_id: UUID, values: Any, *, actor: str) -> Any:
        if type_id == raced:
            types.update(raced, SpoolTypePatch(nozzle_temp_max=150), actor=ACTOR)
        return fill_empty(type_id, values, actor=actor)

    monkeypatch.setattr(types, "fill_empty", meanwhile)

    first, second = await filler.fill(UNLINKED, [raced, later], actor=ACTOR)

    assert first.written
    assert set(first.filled) == {
        "bed_temp_min",
        "bed_temp_max",
        "density_g_cm3",
        "drying_temp_c",
        "drying_hours",
    }
    assert set(first.skipped) == {"nozzle_temp_min"}
    assert all("changed meanwhile" in why for why in first.skipped.values())
    row = types.get(raced)
    assert row is not None
    assert (row.nozzle_temp_min, row.nozzle_temp_max) == (None, 150)
    assert row.density_g_cm3 == Decimal("1.32")
    assert second.written
    assert second.filled["nozzle_temp_min"] == 190


async def test_a_range_put_back_meanwhile_is_still_retried_once(
    filler: TypeFiller, types: SpoolTypeStore, pg_pool: PgPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#6776: the first write races, but the re-read finds no backwards pair (the person
    already put the range back). The fill retries once anyway, so every field is filled."""
    raced = _type(types, pg_pool, "PUTBACK")
    _presets(types, raced, ("H2C", "0.4", "GFA05"))
    fill_empty = types.fill_empty
    calls = 0

    def meanwhile(type_id: UUID, values: Any, *, actor: str) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TypeTempError("a range was set meanwhile")
        return fill_empty(type_id, values, actor=actor)

    monkeypatch.setattr(types, "fill_empty", meanwhile)

    (done,) = await filler.fill(UNLINKED, [raced], actor=ACTOR)

    assert calls == 2
    assert done.written
    assert "nozzle_temp_min" in done.filled
    assert not done.skipped


async def test_a_second_race_skips_everything_left(
    filler: TypeFiller, types: SpoolTypeStore, pg_pool: PgPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#6776: both writes raise `TypeTempError`; nothing is written and every field left
    goes to `skipped`."""
    raced = _type(types, pg_pool, "TWICE")
    _presets(types, raced, ("H2C", "0.4", "GFA05"))
    calls = 0

    def meanwhile(type_id: UUID, values: Any, *, actor: str) -> Any:
        nonlocal calls
        calls += 1
        raise TypeTempError("raced")

    monkeypatch.setattr(types, "fill_empty", meanwhile)

    (done,) = await filler.fill(UNLINKED, [raced], actor=ACTOR)

    assert calls == 2
    assert not done.written
    assert not done.filled
    assert {"nozzle_temp_min", "bed_temp_min", "density_g_cm3"} <= set(done.skipped)
    assert all("changed meanwhile" in why for why in done.skipped.values())


async def test_a_type_deleted_meanwhile_is_not_retried_and_says_why(
    filler: TypeFiller, types: SpoolTypeStore, pg_pool: PgPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#6787: the first write races and the re-read finds the type gone. No retry; every
    wanted field is skipped with the reason."""
    gone = _type(types, pg_pool, "GONE")
    _presets(types, gone, ("H2C", "0.4", "GFA05"))
    calls = 0
    get = types.get

    def meanwhile(type_id: UUID, values: Any, *, actor: str) -> Any:
        nonlocal calls
        calls += 1
        raise TypeTempError("raced")

    monkeypatch.setattr(types, "fill_empty", meanwhile)
    monkeypatch.setattr(types, "get", lambda type_id: None if calls else get(type_id))

    (done,) = await filler.fill(UNLINKED, [gone], actor=ACTOR)

    assert calls == 1
    assert not done.written
    assert not done.filled
    assert {"nozzle_temp_min", "bed_temp_min", "density_g_cm3"} <= set(done.skipped)
    assert set(done.skipped.values()) == {"the type was deleted meanwhile"}


async def test_a_type_really_deleted_meanwhile_gets_no_k_values(
    filler: TypeFiller, types: SpoolTypeStore, pg_pool: PgPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#6787: the row is deleted between the read and the lock. The fill stops there: no
    K value is read or written for the gone type, and the result says why."""
    gone = _type(types, pg_pool, "GONE2")
    _presets(types, gone, ("H2C", "0.4", "GFA05"))
    k_calls: list[str] = []

    def meanwhile(type_id: UUID, values: Any, *, actor: str) -> Any:
        with pg_pool.connection() as conn:
            conn.execute("DELETE FROM filament_spool_types WHERE id = %s", (type_id,))
        raise TypeTempError("raced")

    monkeypatch.setattr(types, "fill_empty", meanwhile)
    for name in ("k_values", "linked_bb_spools", "fill_k_values"):
        monkeypatch.setattr(types, name, lambda *a, _n=name, **k: k_calls.append(_n))

    (done,) = await filler.fill(LINKED, [gone], actor=ACTOR)

    assert k_calls == []
    assert not done.written
    assert done.k_values == []
    assert set(done.skipped.values()) == {"the type was deleted meanwhile"}


@respx.mock
async def test_the_deleted_stop_does_not_depend_on_the_reason_text(
    filler: TypeFiller, types: SpoolTypeStore, pg_pool: PgPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#6800, #6809: `_one` stops on `_fill_empty`'s flag, not on the reason text. The
    type still exists and loses the race twice, so its skip reason equals the patched
    `_DELETED`; the K values are still read and written."""
    type_id = _type(types, pg_pool, "RACED")
    _presets(types, type_id, ("H2C", "0.4", "GFA05"))
    spool = FilamentSpoolStore(pg_pool).create(SpoolCreate(type_id=type_id), actor="ann").id
    with pg_pool.connection() as conn:
        conn.execute(
            "INSERT INTO printers (id, serial, name, bambuddy_printer_id)"
            " VALUES (gen_random_uuid(), 'TEST00000000002', 'H2C', 1)"
        )
        conn.execute(
            "INSERT INTO filament_bb_links (spool_id, bb_spool_id, linked_at)"
            " VALUES (%s, 40, now())",
            (spool,),
        )
    k = {"extruder": 0, "nozzle_diameter": "0.4", "nozzle_type": "HS", "cali_idx": 3}
    respx.get(f"{API}/inventory/spools").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": 40,
                    "material": "PLA",
                    "k_profiles": [{"printer_id": 1, "k_value": 0.03, **k}],
                }
            ],
        )
    )
    raced = 0

    def meanwhile(type_id: UUID, values: Any, *, actor: str) -> Any:
        nonlocal raced
        raced += 1
        raise TypeTempError("raced")

    monkeypatch.setattr(profile_fill, "_DELETED", "the type changed meanwhile: raced")
    monkeypatch.setattr(types, "fill_empty", meanwhile)

    (done,) = await filler.fill(LINKED, [type_id], actor=ACTOR)

    assert raced == 2
    assert set(done.skipped.values()) == {"the type changed meanwhile: raced"}
    assert [(r.extruder, r.k_value) for r in done.k_values] == [(0, Decimal("0.03"))]
    assert done.written
    assert [r.k_value for r in types.k_values(type_id)] == [Decimal("0.03")]


async def test_a_type_without_presets_or_a_profile_is_left_alone_with_a_reason(
    filler: TypeFiller, types: SpoolTypeStore, pg_pool: PgPool
) -> None:
    bare = _type(types, pg_pool, code="BARE")
    unknown = _type(types, pg_pool, code="UNKNOWN")
    _presets(types, unknown, ("H2C", "0.4", "OFDSrzZ8"))
    local = _type(types, pg_pool, code="LOCAL")
    _presets(types, local, ("H2C", "0.4", "2"))

    found = {one.code: one for one in await filler.fill(UNLINKED, None, actor=ACTOR)}

    assert found["BARE"].reason == "the type has no slicer preset"
    assert "OFDSrzZ8" in (found["UNKNOWN"].reason or "")
    assert "no Bambuddy is linked" in (found["LOCAL"].reason or "")
    assert not any(one.written for one in found.values())
    row = types.get(bare)
    assert row is not None
    assert row.density_g_cm3 is None


@respx.mock
async def test_a_local_preset_is_read_once_for_every_type_that_names_it(
    filler: TypeFiller, types: SpoolTypeStore, pg_pool: PgPool
) -> None:
    route = respx.get(f"{API}/local-presets/2").mock(
        return_value=httpx.Response(200, json=local_preset())
    )
    one, two = _type(types, pg_pool, code="ONE"), _type(types, pg_pool, code="TWO")
    _presets(types, one, ("H2C", "0.4", "2"))
    _presets(types, two, ("H2C", "0.4", "2"))

    done = await filler.fill(LINKED, [one, two], actor=ACTOR)

    assert route.call_count == 1
    assert [d.source.origin if d.source else None for d in done] == ["bambuddy_local"] * 2
    assert done[0].filled["density_g_cm3"] == Decimal("1.275")
    assert done[0].cost_hint == "30"


async def test_a_refresh_answers_the_diff_and_writes_nothing(
    filler: TypeFiller, types: SpoolTypeStore, pg_pool: PgPool
) -> None:
    type_id = _type(
        types,
        pg_pool,
        density_g_cm3="1.3",
        nozzle_temp_min=190,
        diameter_mm="2.85",
        drying_temp_c=45,
        drying_hours="12.00",
    )
    _presets(types, type_id, ("H2C", "0.4", "GFA05"))

    (diff,) = await filler.fill(UNLINKED, [type_id], actor=ACTOR, refresh=True)

    assert diff.written is False
    assert diff.filled == {
        "nozzle_temp_max": 240,
        "bed_temp_min": 55,
        "bed_temp_max": 60,
        "density_g_cm3": Decimal("1.32"),
        "diameter_mm": Decimal("1.75"),
    }
    assert diff.previous == {
        "nozzle_temp_max": None,
        "bed_temp_min": None,
        "bed_temp_max": None,
        "density_g_cm3": Decimal("1.3"),
        "diameter_mm": Decimal("2.85"),
    }
    row = types.get(type_id)
    assert row is not None
    assert (row.density_g_cm3, row.nozzle_temp_max) == (Decimal("1.3"), None)


async def test_an_apply_writes_the_confirmed_fields_only_while_the_profile_gives_them(
    filler: TypeFiller, types: SpoolTypeStore, pg_pool: PgPool
) -> None:
    type_id = _type(types, pg_pool, density_g_cm3="1.3", nozzle_temp_min=200)
    _presets(types, type_id, ("H2C", "0.4", "GFA05"))

    done = await filler.apply(UNLINKED, type_id, {"density_g_cm3": Decimal("1.32")}, actor=ACTOR)

    assert done is not None
    assert (done.filled, done.previous, done.written) == (
        {"density_g_cm3": Decimal("1.32")},
        {"density_g_cm3": Decimal("1.3")},
        True,
    )
    row = types.get(type_id)
    assert row is not None
    assert (row.density_g_cm3, row.nozzle_temp_min) == (Decimal("1.32"), 200)

    with pytest.raises(ProfileChangedError):
        await filler.apply(UNLINKED, type_id, {"nozzle_temp_min": 185}, actor=ACTOR)
    assert await filler.apply(UNLINKED, UUID(int=1), {}, actor=ACTOR) is None


@respx.mock
async def test_a_type_without_k_values_takes_its_linked_bambuddy_spools(
    filler: TypeFiller, types: SpoolTypeStore, pg_pool: PgPool
) -> None:
    type_id = _type(types, pg_pool)
    spools = FilamentSpoolStore(pg_pool)
    older = spools.create(SpoolCreate(type_id=type_id), actor="ann").id
    newer = spools.create(SpoolCreate(type_id=type_id), actor="ann").id
    with pg_pool.connection() as conn:
        printer = conn.execute(
            "INSERT INTO printers (id, serial, name, bambuddy_printer_id)"
            " VALUES (gen_random_uuid(), 'TEST00000000001', 'H2C', 1) RETURNING id"
        ).fetchone()
        assert printer is not None
        for spool, bb in ((older, 40), (newer, 41)):
            conn.execute(
                "INSERT INTO filament_bb_links (spool_id, bb_spool_id, linked_at)"
                " VALUES (%s, %s, now())",
                (spool, bb),
            )
    k = {"extruder": 0, "nozzle_diameter": "0.4", "nozzle_type": "HS", "cali_idx": 3}
    bb_rows = [
        {"id": 40, "material": "PLA", "k_profiles": [{"printer_id": 1, "k_value": 0.03, **k}]},
        {
            "id": 41,
            "material": "PLA",
            "k_profiles": [
                {"printer_id": 1, "k_value": 0.02, "name": "Silk", "setting_id": "GFSA05", **k},
                {"printer_id": 1, "k_value": 0.025, **(k | {"extruder": 1})},
                # A printer no ScadBuddy printer links.
                {"printer_id": 9, "k_value": 0.04, **k},
            ],
        },
    ]
    route = respx.get(f"{API}/inventory/spools").mock(
        return_value=httpx.Response(200, json=bb_rows)
    )

    (preview,) = await filler.fill(LINKED, [type_id], actor=ACTOR, refresh=True)
    assert [(r.extruder, r.k_value) for r in preview.k_values] == [
        (0, Decimal("0.02")),
        (1, Decimal("0.025")),
    ]
    assert types.k_values(type_id) == []

    # K values that differ from what Bambuddy now gives are refused, as a field is
    # (R-6714-g), and nothing is written.
    shown = [
        TypeKValueCreate.model_validate(r.model_dump(exclude={"type_id", "source"}))
        for r in preview.k_values
    ]
    changed = [shown[0].model_copy(update={"k_value": Decimal("0.021")}), shown[1]]
    with pytest.raises(ProfileChangedError, match="K values"):
        await filler.apply(LINKED, type_id, {}, actor=ACTOR, k_values=changed)
    assert types.k_values(type_id) == []

    # A person confirms what the refresh showed; no field, so no profile is needed.
    done = await filler.apply(LINKED, type_id, {}, actor=ACTOR, k_values=shown)
    assert done is not None
    assert done.reason == "the type has no slicer preset"
    assert (done.written, len(done.k_values)) == (True, 2)
    saved = types.k_values(type_id)
    assert [(r.extruder, r.k_value, r.name, r.cali_idx) for r in saved] == [
        (0, Decimal("0.02"), "Silk", 3),
        (1, Decimal("0.025"), None, 3),
    ]
    assert {r.printer_id for r in saved} == {printer["id"]}

    # A type that has K values keeps them, and Bambuddy is not asked again.
    calls = route.call_count
    (again,) = await filler.fill(LINKED, [type_id], actor=ACTOR)
    assert again.k_values == []
    assert route.call_count == calls


@respx.mock
async def test_a_spools_read_that_fails_is_a_k_reason(
    filler: TypeFiller, types: SpoolTypeStore, pg_pool: PgPool
) -> None:
    type_id = _type(types, pg_pool)
    spool = FilamentSpoolStore(pg_pool).create(SpoolCreate(type_id=type_id), actor="ann").id
    with pg_pool.connection() as conn:
        conn.execute(
            "INSERT INTO filament_bb_links (spool_id, bb_spool_id, linked_at)"
            " VALUES (%s, 40, now())",
            (spool,),
        )
    respx.get(f"{API}/inventory/spools").mock(return_value=httpx.Response(500))

    (done,) = await filler.fill(LINKED, [type_id], actor=ACTOR)

    assert (done.k_values, done.written) == ([], False)
    assert (done.k_reason or "").startswith("Bambuddy did not list its spools")


@respx.mock
async def test_an_apply_refused_for_a_failed_spools_read_ends_with_the_reason(
    filler: TypeFiller, types: SpoolTypeStore, pg_pool: PgPool
) -> None:
    type_id = _type(types, pg_pool)
    spool = FilamentSpoolStore(pg_pool).create(SpoolCreate(type_id=type_id), actor="ann").id
    with pg_pool.connection() as conn:
        conn.execute(
            "INSERT INTO filament_bb_links (spool_id, bb_spool_id, linked_at)"
            " VALUES (%s, 40, now())",
            (spool,),
        )
    respx.get(f"{API}/inventory/spools").mock(return_value=httpx.Response(500))

    shown = [
        TypeKValueCreate(
            printer_id=UUID(int=7), extruder=0, nozzle_diameter="0.4", k_value=Decimal("0.02")
        )
    ]
    with pytest.raises(ProfileChangedError) as refused:
        await filler.apply(LINKED, type_id, {}, actor=ACTOR, k_values=shown)

    # The reason comes before the advice: "...shown: <reason>: refresh again".
    message = str(refused.value)
    assert message.startswith("the K values now are not the ones shown: Bambuddy did not list")
    assert message.endswith(": refresh again")
    assert types.k_values(type_id) == []


async def test_an_apply_refused_for_k_values_added_meanwhile_says_so(
    filler: TypeFiller, types: SpoolTypeStore, pg_pool: PgPool
) -> None:
    type_id = _type(types, pg_pool)
    with pg_pool.connection() as conn:
        printer = conn.execute(
            "INSERT INTO printers (id, serial, name, bambuddy_printer_id)"
            " VALUES (gen_random_uuid(), 'TEST00000000002', 'H2C', 1) RETURNING id"
        ).fetchone()
    assert printer is not None
    own = TypeKValueCreate(
        printer_id=printer["id"], extruder=0, nozzle_diameter="0.4", k_value=Decimal("0.03")
    )
    types.set_k_values(type_id, _rows(type_id, [own]), actor="ann")

    with pytest.raises(ProfileChangedError) as refused:
        await filler.apply(LINKED, type_id, {}, actor=ACTOR, k_values=[own])

    assert "the type now has K values of its own, so none will be imported" in str(refused.value)


async def test_a_fill_never_writes_over_a_value_set_meanwhile(
    types: SpoolTypeStore, pg_pool: PgPool
) -> None:
    type_id = _type(types, pg_pool)
    types.update(type_id, SpoolTypePatch(density_g_cm3=Decimal("1.2")), actor=ACTOR)
    assert types.fill_empty(
        type_id, {"density_g_cm3": Decimal("1.32"), "bed_temp_min": 55}, actor=ACTOR
    ) == {"bed_temp_min": 55}
    row = types.get(type_id)
    assert row is not None
    assert row.density_g_cm3 == Decimal("1.2")
    assert types.fill_k_values(UUID(int=1), [], actor=ACTOR) is None
    with pytest.raises(ValueError, match="not a profile field"):
        types.fill_empty(type_id, {"cost_per_kg": 1}, actor=ACTOR)


def test_fill_k_values_checks_under_the_lock_that_set_k_values_takes(
    types: SpoolTypeStore, pg_pool: PgPool
) -> None:
    """R-6714-c: a K-values write that lands while the fill waits for the row is kept."""
    import threading

    type_id = _type(types, pg_pool)
    with pg_pool.connection() as conn:
        printer = UUID(int=0x6714)
        conn.execute(
            "INSERT INTO printers (id, serial, name) VALUES (%s, 'S6714', 'Garage')", (printer,)
        )
    row = TypeKValue(
        type_id=type_id,
        printer_id=printer,
        extruder=0,
        nozzle_diameter="0.4",
        nozzle_type=None,
        k_value=Decimal("0.02"),
        name=None,
        cali_idx=None,
        setting_id=None,
    )
    answers: list[Any] = []
    with pg_pool.connection() as holder, holder.transaction():
        holder.execute("SELECT id FROM filament_spool_types WHERE id = %s FOR UPDATE", (type_id,))
        thread = threading.Thread(
            target=lambda: answers.append(types.fill_k_values(type_id, [], actor=ACTOR))
        )
        thread.start()
        thread.join(timeout=1)
        assert thread.is_alive()
        holder.execute(
            "INSERT INTO filament_spool_type_k_values (type_id, printer_id, extruder,"
            " nozzle_diameter, k_value) VALUES (%s, %s, 0, '0.4', %s)",
            (type_id, printer, row.k_value),
        )
    thread.join(timeout=30)
    assert answers == [None]
    assert [k.k_value for k in types.k_values(type_id)] == [Decimal("0.02")]


# -- printer K calibrations (#6720) -----------------------------------------------------


def _h2c(pool: PgPool, serial: str = "TEST00000006720", bb_id: int = 1) -> UUID:
    with pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO printers (id, serial, name, model, bambuddy_printer_id)"
            " VALUES (gen_random_uuid(), %s, 'H2C', 'H2C', %s) RETURNING id",
            (serial, bb_id),
        ).fetchone()
    assert row is not None
    found: UUID = row["id"]
    return found


def _cal(
    extruder: int, filament: str, k: str, nozzle_id: str = "HS00-0.4", slot: int = 1
) -> dict[str, Any]:
    """A row of ``GET /printers/{id}/kprofiles/`` (Bambuddy's ``KProfile``)."""
    return {
        "slot_id": slot,
        "extruder_id": extruder,
        "nozzle_id": nozzle_id,
        "nozzle_diameter": "0.4",
        "filament_id": filament,
        "name": f"cal {filament} {extruder}",
        "k_value": k,
        "n_coef": "0.000000",
        "ams_id": 0,
        "tray_id": 0,
        "setting_id": f"S{filament}",
    }


def _k_table(*rows: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={"profiles": list(rows), "nozzle_diameter": "0.4"})


@respx.mock
async def test_a_printer_calibration_of_the_preset_filament_is_imported(
    filler: TypeFiller, types: SpoolTypeStore, pg_pool: PgPool
) -> None:
    """R-6714-12: each calibration whose ``filament_id`` is a preset's ``slicer_filament``
    becomes the type's K value for that printer, extruder and nozzle, with the nozzle type
    and slot it names."""
    printer = _h2c(pg_pool)
    type_id = _type(types, pg_pool)
    _presets(types, type_id, ("H2C", "0.4", "GFA05"))
    route = respx.get(f"{API}/printers/1/kprofiles/").mock(
        return_value=_k_table(
            _cal(0, "GFA05", "0.020", slot=4),
            _cal(1, "GFA05", "0.025", nozzle_id="HH00-0.4", slot=5),
            _cal(0, "GFG99", "0.040"),
        )
    )

    (preview,) = await filler.fill(LINKED, [type_id], actor=ACTOR, refresh=True)
    assert [(r.extruder, r.k_value, r.nozzle_type) for r in preview.k_values] == [
        (0, Decimal("0.020"), "HS00-0.4"),
        (1, Decimal("0.025"), "HH00-0.4"),
    ]
    assert types.k_values(type_id) == []
    assert route.calls.last.request.url.params["nozzle_diameter"] == "0.4"

    (done,) = await filler.fill(LINKED, [type_id], actor=ACTOR)
    assert done.k_reason is None
    saved = types.k_values(type_id)
    assert [
        (r.printer_id, r.extruder, r.nozzle_diameter, r.nozzle_type, r.k_value, r.cali_idx)
        for r in saved
    ] == [
        (printer, 0, "0.4", "HS00-0.4", Decimal("0.02"), 4),
        (printer, 1, "0.4", "HH00-0.4", Decimal("0.025"), 5),
    ]
    assert (saved[0].name, saved[0].setting_id) == ("cal GFA05 0", "SGFA05")
    assert len(done.k_values) == 2
    assert done.written


@respx.mock
async def test_a_printer_calibration_never_overwrites_a_k_value_a_person_set(
    filler: TypeFiller, types: SpoolTypeStore, pg_pool: PgPool
) -> None:
    """R-6714-13: a key the type has a row for keeps it, however the nozzle is spelled;
    the calibration fills only the keys with none."""
    printer = _h2c(pg_pool)
    type_id = _type(types, pg_pool)
    _presets(types, type_id, ("H2C", "0.4", "GFA05"))
    own = TypeKValueCreate(
        printer_id=printer, extruder=0, nozzle_diameter="0.40", k_value=Decimal("0.05")
    )
    types.set_k_values(type_id, _rows(type_id, [own]), actor="ann")
    respx.get(f"{API}/printers/1/kprofiles/").mock(
        return_value=_k_table(_cal(0, "GFA05", "0.020"), _cal(1, "GFA05", "0.030"))
    )

    (done,) = await filler.fill(LINKED, [type_id], actor=ACTOR)

    assert [(r.extruder, r.k_value) for r in done.k_values] == [(1, Decimal("0.030"))]
    saved = types.k_values(type_id)
    assert [(r.extruder, r.nozzle_diameter, r.k_value) for r in saved] == [
        (0, "0.40", Decimal("0.05")),
        (1, "0.4", Decimal("0.03")),
    ]

    # Filled once, the next fill has nothing to add.
    (again,) = await filler.fill(LINKED, [type_id], actor=ACTOR)
    assert (again.k_values, again.written) == ([], False)
    assert [r.source for r in types.k_values(type_id)] == ["person", "printer_calibration"]


@respx.mock
async def test_a_newer_calibration_replaces_an_imported_one_never_a_person_row(
    filler: TypeFiller, types: SpoolTypeStore, pg_pool: PgPool
) -> None:
    """#6856 (R-6714-19): each K row says who set it. A printer calibration replaces a
    ``printer_calibration`` row of its key when the printer's value changed; a row a
    person set, or edited, stands. A person's save keeps the source of a row it leaves as
    it was."""
    _h2c(pg_pool)
    type_id = _type(types, pg_pool)
    _presets(types, type_id, ("H2C", "0.4", "GFA05"))
    table = respx.get(f"{API}/printers/1/kprofiles/")
    table.mock(return_value=_k_table(_cal(0, "GFA05", "0.020"), _cal(1, "GFA05", "0.030")))
    await filler.fill(LINKED, [type_id], actor=ACTOR)
    first = types.k_values(type_id)
    assert [(r.extruder, r.k_value, r.source) for r in first] == [
        (0, Decimal("0.020"), "printer_calibration"),
        (1, Decimal("0.030"), "printer_calibration"),
    ]

    # A person edits extruder 1 and leaves extruder 0 as it was.
    rows = [
        TypeKValueCreate.model_validate(r.model_dump(exclude={"type_id", "source"})) for r in first
    ]
    rows[1] = rows[1].model_copy(update={"k_value": Decimal("0.05")})
    types.set_k_values(type_id, _rows(type_id, rows), actor="ann")
    assert [r.source for r in types.k_values(type_id)] == ["printer_calibration", "person"]

    # The printer recalibrated both: the imported row moves, the person's stands.
    table.mock(return_value=_k_table(_cal(0, "GFA05", "0.022"), _cal(1, "GFA05", "0.033")))
    (preview,) = await filler.fill(LINKED, [type_id], actor=ACTOR, refresh=True)
    assert [(r.extruder, r.k_value) for r in preview.k_values] == [(0, Decimal("0.022"))]
    (done,) = await filler.fill(LINKED, [type_id], actor=ACTOR)
    assert [(r.extruder, r.k_value) for r in done.k_values] == [(0, Decimal("0.022"))]
    assert [(r.extruder, r.k_value, r.source) for r in types.k_values(type_id)] == [
        (0, Decimal("0.022"), "printer_calibration"),
        (1, Decimal("0.05"), "person"),
    ]


def test_a_profile_fetch_merge_tags_its_rows(types: SpoolTypeStore, pg_pool: PgPool) -> None:
    """#6856 (R-6714-19): the 3DFP fetch's rows say ``profile_fetch``; it still replaces
    the rows of its keys, whoever set them (#6857 decides that)."""
    printer = _h2c(pg_pool)
    type_id = _type(types, pg_pool)
    own = TypeKValueCreate(
        printer_id=printer, extruder=0, nozzle_diameter="0.4", k_value=Decimal("0.05")
    )
    types.set_k_values(type_id, _rows(type_id, [own]), actor="ann")
    fetched = _rows(type_id, [own.model_copy(update={"k_value": Decimal("0.02")})])
    with pg_pool.connection() as conn:
        SpoolTypeStore.merge_k_values(conn, type_id, fetched)
    assert [(r.k_value, r.source) for r in types.k_values(type_id)] == [
        (Decimal("0.02"), "profile_fetch")
    ]


def test_add_k_values_skips_every_key_the_type_has(types: SpoolTypeStore, pg_pool: PgPool) -> None:
    """R-6714-13, in the store: the check is under the type's row lock, and ``0.40`` is
    ``0.4``."""
    printer = _h2c(pg_pool)
    type_id = _type(types, pg_pool)
    own = TypeKValueCreate(
        printer_id=printer, extruder=0, nozzle_diameter="0.40", k_value=Decimal("0.05")
    )
    types.set_k_values(type_id, _rows(type_id, [own]), actor="ann")
    new = own.model_copy(update={"nozzle_diameter": "0.4", "k_value": Decimal("0.01")})
    other = own.model_copy(update={"extruder": 1, "k_value": Decimal("0.02")})

    added = types.add_k_values(type_id, _rows(type_id, [new, other]), actor=ACTOR)

    assert added is not None
    assert [(r.extruder, r.k_value) for r in added] == [(1, Decimal("0.02"))]
    assert [(r.extruder, r.k_value) for r in types.k_values(type_id)] == [
        (0, Decimal("0.05")),
        (1, Decimal("0.02")),
    ]
    assert types.add_k_values(UUID(int=1), [], actor=ACTOR) is None


@respx.mock
async def test_calibrations_that_disagree_for_one_extruder_are_skipped_with_a_reason(
    filler: TypeFiller, types: SpoolTypeStore, pg_pool: PgPool
) -> None:
    """R-6714-14: the table holds one row per printer, extruder and nozzle; two calibrations
    of the filament there that differ (in K or nozzle type) are not guessed between."""
    _h2c(pg_pool)
    type_id = _type(types, pg_pool)
    _presets(types, type_id, ("H2C", "0.4", "GFA05"))
    respx.get(f"{API}/printers/1/kprofiles/").mock(
        return_value=_k_table(
            _cal(0, "GFA05", "0.020", slot=1),
            _cal(0, "GFA05", "0.030", slot=2),
            _cal(1, "GFA05", "0.025", slot=3),
            _cal(1, "GFA05", "0.0250", slot=4),
        )
    )

    (done,) = await filler.fill(LINKED, [type_id], actor=ACTOR)

    # Extruder 1's two rows agree, so one is taken.
    assert [(r.extruder, r.k_value) for r in done.k_values] == [(1, Decimal("0.025"))]
    assert "extruder 0" in (done.k_reason or "")
    assert "2 calibrations" in (done.k_reason or "")


@respx.mock
async def test_a_k_table_read_that_fails_is_a_k_reason_and_the_fields_still_fill(
    filler: TypeFiller, types: SpoolTypeStore, pg_pool: PgPool
) -> None:
    _h2c(pg_pool)
    type_id = _type(types, pg_pool)
    _presets(types, type_id, ("H2C", "0.4", "GFA05"))
    respx.get(f"{API}/printers/1/kprofiles/").mock(return_value=httpx.Response(503))

    (done,) = await filler.fill(LINKED, [type_id], actor=ACTOR)

    assert done.filled["nozzle_temp_min"] == 190
    assert done.k_values == []
    assert (done.k_reason or "").startswith("H2C did not give its 0.4 mm K table")


@respx.mock
async def test_a_k_row_of_another_shape_is_skipped_and_counted_the_rest_import(
    filler: TypeFiller, types: SpoolTypeStore, pg_pool: PgPool
) -> None:
    """#6856: the K table is read row by row, as Bambuddy's spools are; a row that does not
    validate is left out, and ``k_reason`` says how many were."""
    _h2c(pg_pool)
    type_id = _type(types, pg_pool)
    _presets(types, type_id, ("H2C", "0.4", "GFA05"))
    bad = _cal(1, "GFA05", "0.030") | {"extruder_id": "left"}
    respx.get(f"{API}/printers/1/kprofiles/").mock(
        return_value=_k_table(_cal(0, "GFA05", "0.020"), bad, {"slot_id": "x"})
    )

    (done,) = await filler.fill(LINKED, [type_id], actor=ACTOR)

    assert [(r.extruder, r.k_value) for r in done.k_values] == [(0, Decimal("0.020"))]
    assert "2 K calibrations" in (done.k_reason or "")


@respx.mock
async def test_only_printers_of_the_preset_model_are_asked_once_per_nozzle(
    filler: TypeFiller, types: SpoolTypeStore, pg_pool: PgPool
) -> None:
    _h2c(pg_pool)
    with pg_pool.connection() as conn:
        conn.execute(
            "INSERT INTO printers (id, serial, name, model, bambuddy_printer_id)"
            " VALUES (gen_random_uuid(), 'TEST0000000X1C1', 'X1C', 'X1C', 2)"
        )
    one, two = _type(types, pg_pool, code="ONE"), _type(types, pg_pool, code="TWO")
    _presets(types, one, ("H2C", "0.4", "GFA05"))
    _presets(types, two, ("H2C", "0.40", "GFA05"))
    h2c = respx.get(f"{API}/printers/1/kprofiles/").mock(
        return_value=_k_table(_cal(0, "GFA05", "0.020"))
    )
    x1c = respx.get(f"{API}/printers/2/kprofiles/").mock(return_value=_k_table())

    done = await filler.fill(LINKED, [one, two], actor=ACTOR, refresh=True)

    assert (h2c.call_count, x1c.call_count) == (1, 0)
    assert [[r.k_value for r in d.k_values] for d in done] == [[Decimal("0.020")]] * 2


@respx.mock
async def test_a_spool_k_value_wins_over_a_calibration_of_the_same_key(
    filler: TypeFiller, types: SpoolTypeStore, pg_pool: PgPool
) -> None:
    """R-6714-15: a type with none takes its linked spools' K values first (R-6714-5); the
    printer's calibrations fill only the keys those leave."""
    _h2c(pg_pool)
    type_id = _type(types, pg_pool)
    _presets(types, type_id, ("H2C", "0.4", "GFA05"))
    spool = FilamentSpoolStore(pg_pool).create(SpoolCreate(type_id=type_id), actor="ann").id
    with pg_pool.connection() as conn:
        conn.execute(
            "INSERT INTO filament_bb_links (spool_id, bb_spool_id, linked_at)"
            " VALUES (%s, 40, now())",
            (spool,),
        )
    k = {"printer_id": 1, "extruder": 0, "nozzle_diameter": "0.4", "k_value": 0.03}
    respx.get(f"{API}/inventory/spools").mock(
        return_value=httpx.Response(200, json=[{"id": 40, "material": "PLA", "k_profiles": [k]}])
    )
    respx.get(f"{API}/printers/1/kprofiles/").mock(
        return_value=_k_table(_cal(0, "GFA05", "0.020"), _cal(1, "GFA05", "0.025"))
    )

    (preview,) = await filler.fill(LINKED, [type_id], actor=ACTOR, refresh=True)
    assert [(r.extruder, r.k_value) for r in preview.k_values] == [
        (0, Decimal("0.03")),
        (1, Decimal("0.025")),
    ]

    shown = [
        TypeKValueCreate.model_validate(r.model_dump(exclude={"type_id", "source"}))
        for r in preview.k_values
    ]
    done = await filler.apply(LINKED, type_id, {}, actor=ACTOR, k_values=shown)
    assert done is not None
    assert done.written
    assert [(r.extruder, r.k_value, r.source) for r in types.k_values(type_id)] == [
        (0, Decimal("0.03"), "profile"),
        (1, Decimal("0.025"), "printer_calibration"),
    ]

    # The calibration changed since the refresh: refused, as a spool's is (R-6714-g).
    respx.get(f"{API}/printers/1/kprofiles/").mock(return_value=_k_table(_cal(1, "GFA05", "0.026")))
    types.set_k_values(type_id, [], actor="ann")
    with pytest.raises(ProfileChangedError, match="K values"):
        await filler.apply(LINKED, type_id, {}, actor=ACTOR, k_values=shown)
