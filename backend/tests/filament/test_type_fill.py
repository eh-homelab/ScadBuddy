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

from scadbuddy.filament.catalog import CatalogStore
from scadbuddy.filament.models import (
    Material,
    SpoolCreate,
    SpoolTypeCreate,
    SpoolTypePatch,
    TypeKValue,
    TypeKValueCreate,
    TypePreset,
)
from scadbuddy.filament.profile_fill import ProfileChangedError, TypeFiller
from scadbuddy.filament.spools import FilamentSpoolStore
from scadbuddy.filament.types import SpoolTypeStore
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.orders.density import DensityTable
from scadbuddy.slicing.profiles import Profiles
from tests.conftest import PgPool
from tests.filament.test_profile_fill import BASE, TREE, local_preset

pytestmark = pytest.mark.requires_postgres

LINKED = StoredSettings(bambuddy_url=BASE, bambuddy_api_key="k")
UNLINKED = StoredSettings()
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
    )


async def test_only_empty_fields_are_filled_from_the_h2c_04_preset(
    filler: TypeFiller, types: SpoolTypeStore, pg_pool: PgPool
) -> None:
    type_id = _type(types, pg_pool, nozzle_temp_max=245)
    _presets(types, type_id, ("H2C", "0.2", "GFG99"), ("H2C", "0.4", "GFA05"))

    (done,) = await filler.fill(UNLINKED, [type_id])

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

    again = await filler.fill(UNLINKED, [type_id])
    assert (again[0].filled, again[0].written) == ({}, False)


async def test_a_fill_that_would_leave_a_range_backwards_skips_that_pair(
    filler: TypeFiller, types: SpoolTypeStore, pg_pool: PgPool
) -> None:
    type_id = _type(types, pg_pool, nozzle_temp_max=180)
    _presets(types, type_id, ("H2C", "0.4", "GFA05"))

    (done,) = await filler.fill(UNLINKED, [type_id])

    assert "nozzle_temp_min" not in done.filled
    assert done.skipped["nozzle_temp_min"].startswith("nozzle_temp_min 190 would be above")
    row = types.get(type_id)
    assert row is not None
    assert (row.nozzle_temp_min, row.bed_temp_min) == (None, 55)


async def test_a_range_set_meanwhile_skips_that_type_and_the_rest_are_filled(
    filler: TypeFiller, types: SpoolTypeStore, pg_pool: PgPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R-6714-e: a person sets one end of a range between the read and the row lock; that
    type's `TypeTempError` is a skip, never a 500, and the types after it are filled."""
    raced = _type(types, pg_pool, "RACED")
    later = _type(types, pg_pool, "LATER")
    for type_id in (raced, later):
        _presets(types, type_id, ("H2C", "0.4", "GFA05"))
    fill_empty = types.fill_empty

    def meanwhile(type_id: UUID, values: Any) -> Any:
        if type_id == raced:
            types.update(raced, SpoolTypePatch(nozzle_temp_max=150))
        return fill_empty(type_id, values)

    monkeypatch.setattr(types, "fill_empty", meanwhile)

    first, second = await filler.fill(UNLINKED, [raced, later])

    assert (first.filled, first.written) == ({}, False)
    assert set(first.skipped) == {
        "nozzle_temp_min",
        "nozzle_temp_max",
        "bed_temp_min",
        "bed_temp_max",
        "density_g_cm3",
    }
    assert all("changed meanwhile" in why for why in first.skipped.values())
    row = types.get(raced)
    assert row is not None
    assert (row.nozzle_temp_min, row.nozzle_temp_max, row.density_g_cm3) == (None, 150, None)
    assert second.written
    assert second.filled["nozzle_temp_min"] == 190


async def test_a_type_without_presets_or_a_profile_is_left_alone_with_a_reason(
    filler: TypeFiller, types: SpoolTypeStore, pg_pool: PgPool
) -> None:
    bare = _type(types, pg_pool, code="BARE")
    unknown = _type(types, pg_pool, code="UNKNOWN")
    _presets(types, unknown, ("H2C", "0.4", "OFDSrzZ8"))
    local = _type(types, pg_pool, code="LOCAL")
    _presets(types, local, ("H2C", "0.4", "2"))

    found = {one.code: one for one in await filler.fill(UNLINKED, None)}

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

    done = await filler.fill(LINKED, [one, two])

    assert route.call_count == 1
    assert [d.source.origin if d.source else None for d in done] == ["bambuddy_local"] * 2
    assert done[0].filled["density_g_cm3"] == Decimal("1.275")
    assert done[0].cost_hint == "30"


async def test_a_refresh_answers_the_diff_and_writes_nothing(
    filler: TypeFiller, types: SpoolTypeStore, pg_pool: PgPool
) -> None:
    type_id = _type(types, pg_pool, density_g_cm3="1.3", nozzle_temp_min=190, diameter_mm="2.85")
    _presets(types, type_id, ("H2C", "0.4", "GFA05"))

    (diff,) = await filler.fill(UNLINKED, [type_id], refresh=True)

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

    done = await filler.apply(UNLINKED, type_id, {"density_g_cm3": Decimal("1.32")})

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
        await filler.apply(UNLINKED, type_id, {"nozzle_temp_min": 185})
    assert await filler.apply(UNLINKED, UUID(int=1), {}) is None


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

    (preview,) = await filler.fill(LINKED, [type_id], refresh=True)
    assert [(r.extruder, r.k_value) for r in preview.k_values] == [
        (0, Decimal("0.02")),
        (1, Decimal("0.025")),
    ]
    assert types.k_values(type_id) == []

    # K values that differ from what Bambuddy now gives are refused, as a field is
    # (R-6714-g), and nothing is written.
    shown = [
        TypeKValueCreate.model_validate(r.model_dump(exclude={"type_id"})) for r in preview.k_values
    ]
    changed = [shown[0].model_copy(update={"k_value": Decimal("0.021")}), shown[1]]
    with pytest.raises(ProfileChangedError, match="K values"):
        await filler.apply(LINKED, type_id, {}, k_values=changed)
    assert types.k_values(type_id) == []

    # A person confirms what the refresh showed; no field, so no profile is needed.
    done = await filler.apply(LINKED, type_id, {}, k_values=shown)
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
    (again,) = await filler.fill(LINKED, [type_id])
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

    (done,) = await filler.fill(LINKED, [type_id])

    assert (done.k_values, done.written) == ([], False)
    assert (done.k_reason or "").startswith("Bambuddy did not list its spools")


async def test_a_fill_never_writes_over_a_value_set_meanwhile(
    types: SpoolTypeStore, pg_pool: PgPool
) -> None:
    type_id = _type(types, pg_pool)
    types.update(type_id, SpoolTypePatch(density_g_cm3=Decimal("1.2")))
    assert types.fill_empty(type_id, {"density_g_cm3": Decimal("1.32"), "bed_temp_min": 55}) == {
        "bed_temp_min": 55
    }
    row = types.get(type_id)
    assert row is not None
    assert row.density_g_cm3 == Decimal("1.2")
    assert types.fill_k_values(UUID(int=1), []) is None
    with pytest.raises(ValueError, match="not a profile field"):
        types.fill_empty(type_id, {"cost_per_kg": 1})


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
        thread = threading.Thread(target=lambda: answers.append(types.fill_k_values(type_id, [])))
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
