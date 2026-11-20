"""A spool type's values from its slicer filament profile (#6714,
`filament/profile_fill.py`): the preset it reads, the read, and the mapping."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
import respx

from scadbuddy.bambuddy.client import BambuddyClient, BambuddyConfig
from scadbuddy.filament import models as m
from scadbuddy.filament.profile_fill import (
    Unread,
    canonical_nozzle,
    choose_preset,
    cost_hint,
    profile_values,
    read_profile,
)
from scadbuddy.orders.density import DensityTable
from scadbuddy.slicing.profiles import Profiles

#: A small BBL tree: `GFA05` with an `@base`, an H2C 0.6/0.8 preset and an H2C 0.4 one,
#: and `Generic PETG @BBL H2C 0.4 nozzle`, which the local preset fixture inherits.
TREE = Path(__file__).parent / "type_profiles"
BASE = "http://bambuddy.test"
TYPE = UUID(int=7)


def local_preset() -> dict[str, Any]:
    """`GET /local-presets/2` as Bambuddy answered it on 2026-11-20, trimmed to the keys
    the fill reads."""
    found: dict[str, Any] = json.loads((TREE / "local_preset_2.json").read_text())
    return found


@pytest.fixture
def table() -> DensityTable:
    return DensityTable.build(Profiles(TREE))


def preset(model: str, nozzle: str, filament: str = "GFA05") -> m.TypePreset:
    return m.TypePreset(
        type_id=TYPE,
        printer_model=model,
        nozzle_diameter=nozzle,
        slicer_filament=filament,
        slicer_filament_name=None,
    )


def test_the_h2c_04_preset_comes_first_then_any_04_then_the_first() -> None:
    h2c_02, x1c_04, h2c_04 = preset("H2C", "0.2"), preset("X1C", "0.4"), preset("H2C", "0.4")
    assert choose_preset([h2c_02, x1c_04, h2c_04]) == h2c_04
    assert choose_preset([h2c_02, x1c_04]) == x1c_04
    assert choose_preset([h2c_02, preset("X1C", "0.6")]) == h2c_02
    assert choose_preset([]) is None


def test_the_mapping_takes_first_values_and_the_h2c_plates() -> None:
    flat = {
        "nozzle_temperature_range_low": ["220", "225"],
        "nozzle_temperature_range_high": "240",
        "filament_density": ["1.275"],
        "filament_diameter": ["1.75"],
        # The H2C's plates: SuperTack, Engineering and Textured PEI. Cool Plate and the
        # High Temp plate are not the H2C's, and 0 is a plate the filament is not for.
        "supertack_plate_temp": ["0"],
        "eng_plate_temp": ["70"],
        "textured_plate_temp": ["80"],
        "cool_plate_temp": ["35"],
        "hot_plate_temp": ["90"],
        "filament_cost": ["30"],
    }
    assert profile_values(flat) == {
        "nozzle_temp_min": 220,
        "nozzle_temp_max": 240,
        "bed_temp_min": 70,
        "bed_temp_max": 80,
        "density_g_cm3": Decimal("1.275"),
        "diameter_mm": Decimal("1.75"),
    }
    assert cost_hint(flat) == "30"


@pytest.mark.parametrize("bad", ["", "nil", [], ["nil"], ["x"], ["0"], {"a": 1}, None])
def test_an_empty_nil_or_unreadable_value_is_skipped(bad: Any) -> None:
    flat = {"nozzle_temperature_range_low": bad, "filament_density": bad, "eng_plate_temp": bad}
    found = profile_values(flat)
    assert "density_g_cm3" not in found
    assert "bed_temp_min" not in found
    assert "nozzle_temp_min" not in found
    assert cost_hint({"filament_cost": bad}) is None


async def test_a_system_id_reads_the_h2c_04_preset_through_inherits(table: DensityTable) -> None:
    read = await read_profile(preset("H2C", "0.4"), table, None)
    assert not isinstance(read, Unread)
    assert read.source.name == "Bambu PLA Silk @BBL H2C 0.4 nozzle"
    assert (read.source.origin, read.source.preset_id) == ("system", "GFA05")
    values = profile_values(read.values)
    # Its own eng plate 60, the base's textured 55, the parent's temperatures.
    assert values == {
        "nozzle_temp_min": 190,
        "nozzle_temp_max": 240,
        "bed_temp_min": 55,
        "bed_temp_max": 60,
        "density_g_cm3": Decimal("1.32"),
        "diameter_mm": Decimal("1.75"),
    }


async def test_another_printer_reads_the_base_and_a_setting_id_its_preset(
    table: DensityTable,
) -> None:
    read = await read_profile(preset("X1C", "0.4"), table, None)
    assert not isinstance(read, Unread)
    assert read.source.name == "Bambu PLA Silk @base"
    read = await read_profile(preset("H2C", "0.6", "GFSA05_23"), table, None)
    assert not isinstance(read, Unread)
    assert read.source.name == "Bambu PLA Silk @BBL H2C"
    assert profile_values(read.values)["nozzle_temp_min"] == 210


async def test_an_unknown_id_or_no_tree_is_a_reason(table: DensityTable) -> None:
    read = await read_profile(preset("H2C", "0.4", "OFDSrzZ8"), table, None)
    assert isinstance(read, Unread)
    assert "OFDSrzZ8" in read.reason
    read = await read_profile(preset("H2C", "0.4"), None, None)
    assert isinstance(read, Unread)
    assert "profile" in read.reason


@respx.mock
async def test_a_local_preset_is_read_from_bambuddy_over_its_parent(table: DensityTable) -> None:
    respx.get(f"{BASE}/api/v1/local-presets/2").mock(
        return_value=httpx.Response(200, json=local_preset())
    )
    async with BambuddyClient(BambuddyConfig(base_url=BASE, api_key="k")) as client:
        read = await read_profile(preset("H2C", "0.4", "2"), table, client)
    assert not isinstance(read, Unread)
    assert read.source.origin == "bambuddy_local"
    assert read.source.name == "Cookiecad PETG Magic Dark Magic (3DFP 7JdoWkaDB) @H2C"
    assert profile_values(read.values) == {
        "nozzle_temp_min": 220,
        "nozzle_temp_max": 240,
        "bed_temp_min": 70,
        "bed_temp_max": 80,
        "density_g_cm3": Decimal("1.275"),
        # Not in the preset's own setting: its parent's, through the tree.
        "diameter_mm": Decimal("1.75"),
    }
    assert cost_hint(read.values) == "30"


@respx.mock
async def test_a_local_preset_bambuddy_refuses_or_cannot_reach_is_a_reason(
    table: DensityTable,
) -> None:
    respx.get(f"{BASE}/api/v1/local-presets/2").mock(return_value=httpx.Response(404))
    async with BambuddyClient(BambuddyConfig(base_url=BASE, api_key="k")) as client:
        read = await read_profile(preset("H2C", "0.4", "2"), table, client)
    assert isinstance(read, Unread)
    assert "local preset 2" in read.reason
    read = await read_profile(preset("H2C", "0.4", "2"), table, None)
    assert isinstance(read, Unread)
    assert "no Bambuddy" in read.reason


@pytest.mark.parametrize(
    ("stored", "canonical"),
    [("0.40", "0.4"), ("0.4", "0.4"), (" 0.6 ", "0.6"), ("1.0", "1"), ("wide", "wide")],
)
def test_a_nozzle_is_spelled_as_the_presets_spell_it(stored: str, canonical: str) -> None:
    assert canonical_nozzle(stored) == canonical


async def test_a_nozzle_stored_as_040_reads_the_h2c_04_preset(table: DensityTable) -> None:
    read = await read_profile(preset("H2C", "0.40"), table, None)
    assert not isinstance(read, Unread)
    assert read.source.name == "Bambu PLA Silk @BBL H2C 0.4 nozzle"
