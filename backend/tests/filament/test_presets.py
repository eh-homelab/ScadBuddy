"""A spool's own presets and K values over its type's (#6262, spec §5.3)."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from scadbuddy.filament.catalog import CatalogRefError, CatalogStore
from scadbuddy.filament.models import (
    Material,
    SpoolCreate,
    SpoolKValueCreate,
    SpoolPatch,
    SpoolPresetCreate,
    SpoolTypeCreate,
    TypeKValue,
    TypePreset,
)
from scadbuddy.filament.presets import effective_k_values, effective_presets, preset_for
from scadbuddy.filament.spools import FilamentSpoolStore
from scadbuddy.filament.types import SpoolTypeStore, TypeRowsDuplicateError
from tests.conftest import PgPool
from tests.support.printers import bambuddy_printer

pytestmark = pytest.mark.requires_postgres


def _type_with_spool(pool: PgPool) -> tuple[SpoolTypeStore, FilamentSpoolStore, UUID, UUID]:
    material = next(
        r.id
        for r in CatalogStore(pool).list("materials")
        if isinstance(r, Material) and r.name == "PLA"
    )
    types = SpoolTypeStore(pool)
    kind = types.create(
        SpoolTypeCreate(code="PLA-X", material_id=material, label_weight_g=1000, rgba="#FFFFFFFF")
    )
    spools = FilamentSpoolStore(pool)
    spool = spools.create(SpoolCreate(type_id=kind.id), actor="ann")
    return types, spools, kind.id, spool.id


def _type_preset(type_id: UUID, nozzle: str, filament: str) -> TypePreset:
    return TypePreset(
        type_id=type_id,
        printer_model="H2C",
        nozzle_diameter=nozzle,
        slicer_filament=filament,
        slicer_filament_name=None,
    )


def _own(nozzle: str, filament: str) -> SpoolPresetCreate:
    return SpoolPresetCreate(printer_model="H2C", nozzle_diameter=nozzle, slicer_filament=filament)


def test_a_spool_preset_overrides_its_types_for_that_key(pg_pool: PgPool) -> None:
    types, spools, type_id, spool_id = _type_with_spool(pg_pool)
    types.set_presets(
        type_id, [_type_preset(type_id, "0.4", "GFA00"), _type_preset(type_id, "0.6", "GFA01")]
    )
    before = spools.presets(spool_id)
    assert before is not None
    assert [(p.nozzle_diameter, p.slicer_filament, p.overridden) for p in before] == [
        ("0.4", "GFA00", False),
        ("0.6", "GFA01", False),
    ]
    saved = spools.set_presets(spool_id, [_own("0.4", "P1")], actor="ann")
    assert saved is not None
    assert [(p.nozzle_diameter, p.slicer_filament, p.overridden) for p in saved] == [
        ("0.4", "P1", True),
        ("0.6", "GFA01", False),
    ]
    assert spools.presets(spool_id) == saved


def test_a_spool_preset_for_a_key_the_type_lacks_is_kept(pg_pool: PgPool) -> None:
    types, spools, type_id, spool_id = _type_with_spool(pg_pool)
    types.set_presets(type_id, [_type_preset(type_id, "0.4", "GFA00")])
    saved = spools.set_presets(spool_id, [_own("0.2", "P2")], actor="ann")
    assert saved is not None
    assert [(p.nozzle_diameter, p.overridden) for p in saved] == [("0.2", True), ("0.4", False)]


def test_setting_an_empty_list_resets_to_the_type(pg_pool: PgPool) -> None:
    types, spools, type_id, spool_id = _type_with_spool(pg_pool)
    types.set_presets(type_id, [_type_preset(type_id, "0.4", "GFA00")])
    spools.set_presets(spool_id, [_own("0.4", "P1")], actor="ann")
    reset = spools.set_presets(spool_id, [], actor="ann")
    assert reset is not None
    assert [(p.slicer_filament, p.overridden) for p in reset] == [("GFA00", False)]
    events = [e for e in spools.events(spool_id) if e.kind == "edited"]
    assert len(events) == 2
    changes = events[1].data["changes"]["presets"]
    assert [row["slicer_filament"] for row in changes[0]] == ["P1"]
    assert changes[1] == []


def test_a_repeated_key_is_refused_and_no_spool_is_none(pg_pool: PgPool) -> None:
    _, spools, _, spool_id = _type_with_spool(pg_pool)
    with pytest.raises(TypeRowsDuplicateError):
        spools.set_presets(spool_id, [_own("0.4", "A"), _own("0.4", "B")], actor="ann")
    missing = UUID(int=9)
    assert spools.presets(missing) is None
    assert spools.set_presets(missing, [], actor="ann") is None
    assert spools.k_values(missing) is None
    assert spools.set_k_values(missing, [], actor="ann") is None


def test_preset_for_is_exact(pg_pool: PgPool) -> None:
    types, _, type_id, spool_id = _type_with_spool(pg_pool)
    types.set_presets(type_id, [_type_preset(type_id, "0.4", "GFA00")])
    with pg_pool.connection() as conn:
        rows = effective_presets(conn, spool_id)
    assert preset_for(rows, "H2C", "0.2") is None
    assert preset_for(rows, "H2D", "0.4") is None
    found = preset_for(rows, "H2C", "0.4")
    assert found is not None
    assert found.slicer_filament == "GFA00"


def test_a_spool_k_value_overrides_its_types_and_resets(pg_pool: PgPool, pg_conninfo: str) -> None:
    types, spools, type_id, spool_id = _type_with_spool(pg_pool)
    printer = UUID(bambuddy_printer(pg_conninfo, 1))
    types.set_k_values(
        type_id,
        [
            TypeKValue(
                type_id=type_id,
                printer_id=printer,
                extruder=ext,
                nozzle_diameter="0.4",
                nozzle_type=None,
                k_value=Decimal("0.02"),
                name=None,
                cali_idx=None,
                setting_id=None,
            )
            for ext in (0, 1)
        ],
    )
    own = SpoolKValueCreate(
        printer_id=printer, extruder=1, nozzle_diameter="0.4", k_value=Decimal("0.05"), name="mine"
    )
    saved = spools.set_k_values(spool_id, [own], actor="ann")
    assert saved is not None
    assert [(k.extruder, k.k_value, k.overridden) for k in saved] == [
        (0, Decimal("0.0200"), False),
        (1, Decimal("0.0500"), True),
    ]
    assert spools.k_values(spool_id) == saved
    reset = spools.set_k_values(spool_id, [], actor="ann")
    assert reset is not None
    assert [k.overridden for k in reset] == [False, False]
    assert [e.kind for e in spools.events(spool_id)].count("edited") == 2
    with pytest.raises(CatalogRefError):
        spools.set_k_values(
            spool_id,
            [own.model_copy(update={"printer_id": UUID(int=9)})],
            actor="ann",
        )


def test_a_spool_only_k_key_shows_and_a_repeated_key_is_refused(
    pg_pool: PgPool, pg_conninfo: str
) -> None:
    _, spools, _, spool_id = _type_with_spool(pg_pool)
    printer = UUID(bambuddy_printer(pg_conninfo, 1))
    own = SpoolKValueCreate(
        printer_id=printer, extruder=0, nozzle_diameter="0.4", k_value=Decimal("0.03")
    )
    saved = spools.set_k_values(spool_id, [own], actor="ann")
    assert saved is not None
    assert [(k.printer_id, k.k_value, k.overridden) for k in saved] == [
        (printer, Decimal("0.0300"), True)
    ]
    with pg_pool.connection() as conn:
        assert effective_k_values(conn, spool_id) == saved
    with pytest.raises(TypeRowsDuplicateError):
        spools.set_k_values(spool_id, [own, own], actor="ann")


def _migrated(pool: PgPool, spool_id: UUID, filament: str) -> None:
    """The rows the presets-per-nozzle migration makes of a native ``*`` preset."""
    with pool.connection() as conn:
        for nozzle in ("0.2", "0.4", "0.6", "0.8"):
            conn.execute(
                "INSERT INTO filament_spool_presets (spool_id, printer_model, nozzle_diameter,"
                " slicer_filament, migrated) VALUES (%s, 'H2C', %s, %s, true)",
                (spool_id, nozzle, filament),
            )


def _rows(pool: PgPool, spool_id: UUID) -> list[tuple[str, str, str, bool]]:
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT printer_model, nozzle_diameter, slicer_filament, migrated"
            " FROM filament_spool_presets WHERE spool_id = %s"
            " ORDER BY printer_model, nozzle_diameter",
            (spool_id,),
        ).fetchall()
    return [tuple(r.values()) for r in rows]


def test_a_first_type_drops_the_migrated_presets(pg_pool: PgPool) -> None:
    types, spools, type_id, _ = _type_with_spool(pg_pool)
    types.set_presets(type_id, [_type_preset(type_id, "0.6", "T6")])
    kind = types.get(type_id)
    assert kind is not None
    needs = spools.create(SpoolCreate(material_id=kind.material_id), actor="ann")
    _migrated(pg_pool, needs.id, "P1")
    # A row the person set themselves is theirs: a type never drops it.
    with pg_pool.connection() as conn:
        conn.execute(
            "UPDATE filament_spool_presets SET slicer_filament = 'MINE', migrated = false"
            " WHERE spool_id = %s AND nozzle_diameter = '0.2'",
            (needs.id,),
        )
    spools.update(needs.id, SpoolPatch(type_id=type_id), actor="ann")
    presets = spools.presets(needs.id)
    assert presets is not None
    assert [(p.nozzle_diameter, p.slicer_filament, p.overridden) for p in presets] == [
        ("0.2", "MINE", True),
        ("0.6", "T6", False),
    ]
    (edited,) = [e for e in spools.events(needs.id) if e.kind == "edited"]
    assert edited.data["changes"]["type_id"] == [None, str(type_id)]
    before, after = edited.data["changes"]["presets"]
    assert [r["slicer_filament"] for r in before] == ["MINE", "P1", "P1", "P1"]
    assert [r["slicer_filament"] for r in after] == ["MINE"]


def test_the_effective_presets_say_which_own_rows_are_migrated(pg_pool: PgPool) -> None:
    types, spools, type_id, _ = _type_with_spool(pg_pool)
    types.set_presets(type_id, [_type_preset(type_id, "0.6", "T6")])
    kind = types.get(type_id)
    assert kind is not None
    needs = spools.create(SpoolCreate(material_id=kind.material_id), actor="ann")
    _migrated(pg_pool, needs.id, "P1")
    spools.set_presets(needs.id, [_own("0.4", "P1"), _own("0.2", "MINE")], actor="ann")
    with pg_pool.connection() as conn:
        rows = effective_presets(conn, needs.id)
    # The unchanged row stays migrated, the person's own is not; the 0.6 one is the type's
    # (none here: the spool has no type yet), and a migrated row is always an own one.
    assert [(p.nozzle_diameter, p.overridden, p.migrated) for p in rows] == [
        ("0.2", True, False),
        ("0.4", True, True),
    ]


def test_a_whole_list_save_keeps_the_migrated_rows(pg_pool: PgPool) -> None:
    types, spools, type_id, _ = _type_with_spool(pg_pool)
    types.set_presets(type_id, [_type_preset(type_id, "0.6", "T6")])
    kind = types.get(type_id)
    assert kind is not None
    needs = spools.create(SpoolCreate(material_id=kind.material_id), actor="ann")
    _migrated(pg_pool, needs.id, "P1")
    # What the page sends back: the four rows it showed, the 0.6 one changed, and 0.2's
    # name filled in; the unchanged 0.4 and 0.8 rows stay migrated.
    sent = [_own(n, "P1") for n in ("0.4", "0.8")]
    sent.append(_own("0.6", "MINE"))
    sent.append(_own("0.2", "P1").model_copy(update={"slicer_filament_name": "named"}))
    spools.set_presets(needs.id, sent, actor="ann")
    assert [(r[1], r[3]) for r in _rows(pg_pool, needs.id)] == [
        ("0.2", False),
        ("0.4", True),
        ("0.6", False),
        ("0.8", True),
    ]
    spools.update(needs.id, SpoolPatch(type_id=type_id), actor="ann")
    presets = spools.presets(needs.id)
    assert presets is not None
    assert [(p.nozzle_diameter, p.slicer_filament, p.overridden) for p in presets] == [
        ("0.2", "P1", True),
        ("0.6", "MINE", True),
    ]


def test_a_later_type_keeps_the_presets(pg_pool: PgPool) -> None:
    types, spools, type_id, spool_id = _type_with_spool(pg_pool)
    _migrated(pg_pool, spool_id, "P1")
    kind = types.get(type_id)
    assert kind is not None
    other = types.create(
        SpoolTypeCreate(
            code="PLA-Y", material_id=kind.material_id, label_weight_g=1000, rgba="#000000FF"
        )
    )
    spools.update(spool_id, SpoolPatch(type_id=other.id), actor="ann")
    assert [r[3] for r in _rows(pg_pool, spool_id)] == [True] * 4
    (edited,) = [e for e in spools.events(spool_id) if e.kind == "edited"]
    assert "presets" not in edited.data["changes"]


def test_a_preset_key_refuses_the_wildcard() -> None:
    for model, nozzle in (("*", "0.4"), ("H2C", " * ")):
        with pytest.raises(ValidationError):
            SpoolPresetCreate(printer_model=model, nozzle_diameter=nozzle, slicer_filament="X")
