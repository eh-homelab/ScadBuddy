"""A spool type's values from its slicer filament profile (#6714,
`filament/profile_fill.py`): the preset it reads, the read, and the mapping."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

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


def test_drying_takes_the_ams_ht_idle_values() -> None:
    """R-6714-9: `filament_dev_ams_drying_temperature` and `_time` hold one value per AMS
    and state, `[AMS 2 Pro idle, AMS HT idle, AMS 2 Pro printing, AMS HT printing]`; the
    type takes the AMS HT's idle pair, which the lower AMS limit does not cap."""
    flat = {
        "filament_dev_ams_drying_temperature": ["65", "80", "65", "75"],
        "filament_dev_ams_drying_time": ["12", "8.0", "12", "8.0"],
        "filament_dev_chamber_drying_bed_temperature": ["90.0"],
        "filament_dev_chamber_drying_time": ["12.0"],
    }
    assert profile_values(flat) == {"drying_temp_c": 80, "drying_hours": Decimal("8")}
    # One value only: that one.
    one = {"filament_dev_ams_drying_temperature": ["55"], "filament_dev_ams_drying_time": "6.5"}
    assert profile_values(one) == {"drying_temp_c": 55, "drying_hours": Decimal("6.5")}
    # 0, as a custom root preset stores it, is none; so is a time past the column.
    none = {
        "filament_dev_ams_drying_temperature": ["0", "0"],
        "filament_dev_ams_drying_time": ["0", "1000"],
    }
    assert profile_values(none) == {}


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
        # The base's AMS HT drying values (R-6714-9).
        "drying_temp_c": 45,
        "drying_hours": Decimal("12"),
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


async def test_a_bambuddy_local_preset_is_not_read(table: DensityTable) -> None:
    """ScadBuddy reads no Bambuddy local preset (#7244): the reason says to import it."""
    read = await read_profile(preset("H2C", "0.4", "2"), table, None)
    assert isinstance(read, Unread)
    assert "Bambuddy local preset" in read.reason and "import Bambuddy's presets" in read.reason


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
