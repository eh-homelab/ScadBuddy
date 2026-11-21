"""A type's profile fetches (#6625): the run store, the live gaps, and the merge of fetched
presets and K values over the type's own rows. The import over Bambuddy and the worker's
routes are `tests/api/test_crud_profile_fetch.py`."""

from __future__ import annotations

import json
from decimal import Decimal
from uuid import UUID

import pytest

from scadbuddy.core.problems import ApiError
from scadbuddy.filament.models import TypeKValue, TypePreset
from scadbuddy.filament.profile_fetch import (
    FetchRefusedError,
    ProfileFetches,
    _Ending,
    _merge_k_values,
    clean_preset,
)
from scadbuddy.filament.types import SpoolTypeStore
from scadbuddy.printers.store import PrinterStore
from scadbuddy.profile_fetch.wire import ExportTarget, MissingKey
from tests.conftest import PgPool
from tests.filament.test_types import _body

pytestmark = pytest.mark.requires_postgres

NOZZLES = ["0.2", "0.4"]


def _fetches(pool: PgPool, nozzles: list[str] = NOZZLES) -> ProfileFetches:
    return ProfileFetches(pool, lambda: nozzles)


def _type(pool: PgPool, tdfp_id: int | None = 30173) -> UUID:
    return SpoolTypeStore(pool).create(_body(pool, tdfp_id=tdfp_id)).id


def _printer(pool: PgPool, model: str | None, bambuddy_id: int) -> str:
    row = PrinterStore(pool, None).create_linked(
        serial=f"SERIAL{bambuddy_id}",
        name=f"P{bambuddy_id}",
        model=model,
        bambuddy_printer_id=bambuddy_id,
    )
    return row.id


def test_a_run_targets_each_printer_model_with_each_nozzle(pg_pool: PgPool) -> None:
    _printer(pg_pool, "H2C", 1)
    _printer(pg_pool, "X1C", 2)
    _printer(pg_pool, "H2C", 3)
    _printer(pg_pool, None, 4)
    type_id = _type(pg_pool)
    run, joined = _fetches(pg_pool).open(type_id, actor="person:elan")
    assert not joined
    assert (run.status, run.tdfp_id, run.type_id) == ("running", 30173, type_id)
    assert run.targets == [
        ExportTarget(printer_model="H2C", nozzles=NOZZLES),
        ExportTarget(printer_model="X1C", nozzles=NOZZLES),
    ]


def test_a_second_open_joins_the_running_one(pg_pool: PgPool) -> None:
    _printer(pg_pool, "H2C", 1)
    type_id = _type(pg_pool)
    first, _ = _fetches(pg_pool).open(type_id, actor="a")
    again, joined = _fetches(pg_pool).open(type_id, actor="b")
    assert joined
    assert again.id == first.id
    assert _fetches(pg_pool).latest(type_id) == first


@pytest.mark.parametrize(
    ("tdfp_id", "printer", "code"),
    [(None, True, "no_tdfp_id"), (30173, False, "no_printer_models")],
)
def test_a_run_needs_a_tdfp_id_and_a_target(
    pg_pool: PgPool, tdfp_id: int | None, printer: bool, code: str
) -> None:
    if printer:
        _printer(pg_pool, "H2C", 1)
    type_id = _type(pg_pool, tdfp_id)
    with pytest.raises(FetchRefusedError) as refused:
        _fetches(pg_pool).open(type_id, actor="a")
    assert refused.value.code == code


def test_no_such_type_is_refused(pg_pool: PgPool) -> None:
    with pytest.raises(FetchRefusedError) as refused:
        _fetches(pg_pool).open(UUID(int=7), actor="a")
    assert refused.value.code == "type_not_found"
    assert _fetches(pg_pool).latest(UUID(int=7)) is None


def test_gaps_are_the_targets_the_type_has_no_preset_for(pg_pool: PgPool) -> None:
    _printer(pg_pool, "H2C", 1)
    type_id = _type(pg_pool)
    types = SpoolTypeStore(pg_pool)
    types.set_presets(
        type_id,
        [
            TypePreset(
                type_id=type_id,
                printer_model="H2C",
                nozzle_diameter="0.4",
                slicer_filament="12",
                slicer_filament_name="n",
            )
        ],
        actor="ann",
    )
    gaps = _fetches(pg_pool).gaps(type_id)
    assert [(g.printer_model, g.nozzle_diameter) for g in gaps] == [("H2C", "0.2")]
    # Computed live: a nozzle added to the setting is a gap at once.
    later = _fetches(pg_pool, ["0.2", "0.4", "0.6"]).gaps(type_id)
    assert [g.nozzle_diameter for g in later] == ["0.2", "0.6"]


def test_merge_replaces_only_the_given_keys(pg_pool: PgPool) -> None:
    printer = UUID(_printer(pg_pool, "H2C", 1))
    type_id = _type(pg_pool)
    types = SpoolTypeStore(pg_pool)

    def preset(model: str, nozzle: str, filament: str) -> TypePreset:
        return TypePreset(
            type_id=type_id,
            printer_model=model,
            nozzle_diameter=nozzle,
            slicer_filament=filament,
            slicer_filament_name=None,
        )

    def k(extruder: int, nozzle: str, value: str) -> TypeKValue:
        return TypeKValue(
            type_id=type_id,
            printer_id=printer,
            extruder=extruder,
            nozzle_diameter=nozzle,
            nozzle_type=None,
            k_value=Decimal(value),
            name=None,
            cali_idx=None,
            setting_id=None,
        )

    types.set_presets(type_id, [preset("X1C", "0.4", "1"), preset("H2C", "0.4", "2")], actor="ann")
    with pg_pool.connection() as conn, conn.transaction():
        types.merge_k_values(conn, type_id, [k(0, "0.4", "0.02"), k(1, "0.4", "0.03")])
    with pg_pool.connection() as conn, conn.transaction():
        types.merge_presets(conn, type_id, [preset("H2C", "0.4", "9"), preset("H2C", "0.2", "8")])
        types.merge_k_values(conn, type_id, [k(0, "0.4", "0.05")])
    assert [
        (p.printer_model, p.nozzle_diameter, p.slicer_filament) for p in types.presets(type_id)
    ] == [
        ("H2C", "0.2", "8"),
        ("H2C", "0.4", "9"),
        ("X1C", "0.4", "1"),
    ]
    assert [(v.extruder, v.k_value) for v in types.k_values(type_id)] == [
        (0, Decimal("0.0500")),
        (1, Decimal("0.0300")),
    ]


def test_missing_reason_of_the_last_run_names_the_gap(pg_pool: PgPool) -> None:
    _printer(pg_pool, "H2C", 1)
    type_id = _type(pg_pool)
    fetches = _fetches(pg_pool)
    run, _ = fetches.open(type_id, actor="a")
    with pg_pool.connection() as conn:
        conn.execute(
            "UPDATE filament_profile_fetches SET status = 'partial', missing = %s::jsonb"
            " WHERE id = %s",
            ('[{"printer_model": "H2C", "nozzle_diameter": "0.2", "reason": "no 0.2"}]', run.id),
        )
    gaps = fetches.gaps(type_id)
    assert MissingKey(printer_model="H2C", nozzle_diameter="0.2", reason="no 0.2") in gaps


PRESET = {"name": "Inland PLA+ @BBL H2C", "inherits": "Generic PLA @BBL H2C", "from": "User"}


def test_clean_preset_strips_gcode_and_post_process() -> None:
    doc = {
        **PRESET,
        "type": "filament",
        "filament_start_gcode": ["G28"],
        "filament_end_gcode": ["M84"],
        "Machine_Start_GCODE": "M84",
        "post_process": ["/bin/sh"],
        "gcode_flavor_note": "kept: the name does not end in gcode",
    }
    name, body = clean_preset(json.dumps(doc).encode())
    assert name == PRESET["name"]
    assert json.loads(body) == {
        **PRESET,
        "type": "filament",
        "gcode_flavor_note": "kept: the name does not end in gcode",
    }
    # The same bytes again: cleaning is stable, so the import's second pass changes nothing.
    assert clean_preset(body) == (name, body)


@pytest.mark.parametrize(
    "over",
    [
        {"type": "machine"},
        {"type": "process"},
        {"printer_settings_id": "Bambu Lab H2C 0.4 nozzle"},
        {"print_settings_id": "0.20mm Standard @BBL H2C"},
        {"inherits": ""},
        {"inherits": ["Generic PLA"]},
        {"inherits": None},
    ],
)
def test_clean_preset_refuses_what_is_not_a_filament_preset(over: dict[str, object]) -> None:
    doc = {**PRESET, **over}
    if over.get("inherits", "x") is None:
        del doc["inherits"]
    with pytest.raises(ApiError) as caught:
        clean_preset(json.dumps(doc).encode())
    assert caught.value.status == 422


def test_a_kept_k_value_of_the_same_number_leaves_no_note(pg_pool: PgPool) -> None:
    """#6866: ``0.0200`` stored and ``0.02`` fetched agree, so only a real disagreement
    is noted."""
    printer = UUID(_printer(pg_pool, "H2C", 1))
    type_id = _type(pg_pool)

    def k(value: str) -> TypeKValue:
        return TypeKValue(
            type_id=type_id,
            printer_id=printer,
            extruder=0,
            nozzle_diameter="0.4",
            nozzle_type=None,
            k_value=Decimal(value),
            name=None,
            cali_idx=None,
            setting_id=None,
        )

    types = SpoolTypeStore(pg_pool)
    with pg_pool.connection() as conn, conn.transaction():
        types.merge_k_values(conn, type_id, [k("0.0200")])
        conn.execute(
            "UPDATE filament_spool_type_k_values SET source = 'person' WHERE type_id = %s",
            (type_id,),
        )
    for fetched, notes in (("0.02", 0), ("0.03", 1)):
        ending = _Ending(status="imported", missing=[], k_values=[k(fetched)])
        with pg_pool.connection() as conn, conn.transaction():
            _merge_k_values(conn, type_id, ending)
        assert len(ending.notes) == notes
