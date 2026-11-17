"""A filament's density from its spools' own, else their slicer preset's, else their
tray's reported preset's (#6136, #6262 Task 9 review ruling, `orders/density.py`)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from scadbuddy.orders.density import DensityTable, ProfileDensity, density_table, resolve
from scadbuddy.orders.integrations import LocalDensity, SpoolDensity, SpoolGroup
from scadbuddy.slicing.profiles import Profiles

#: A small BBL tree laid out as the image's: `@base` presets, per-printer children, an
#: `include`, a `Generic` family whose presets disagree, and `fdm_filament_common`'s 0.
PROFILES = Path(__file__).parent / "density_profiles"


@pytest.fixture
def table() -> DensityTable:
    return DensityTable.build(Profiles(PROFILES))


def group(**fields: Any) -> SpoolGroup:
    return SpoolGroup(material="PLA", subtype="Tri Color", brand="Bambu Lab", **fields)


def test_a_filament_id_gives_its_base_presets_density(table: DensityTable) -> None:
    # The `@base` preset's, not a printer's own override (H2C's 1.30).
    assert table.for_preset("GFA00") == ProfileDensity("1.26", "Bambu PLA Basic @base")
    # No `@base`: the one density its presets share, which here an `include` gives.
    assert table.for_preset("GFG50") == ProfileDensity("1.29", "Bambu PETG-CF @BBL X1C")


def test_a_setting_id_gives_that_presets_density(table: DensityTable) -> None:
    assert table.for_preset("GFSA00_99") == ProfileDensity("1.3", "Bambu PLA Basic @BBL H2C")
    assert table.for_preset("GFSA05") == ProfileDensity("1.32", "Bambu PLA Silk @BBL X1C")


def test_presets_that_disagree_or_an_unknown_id_give_none(table: DensityTable) -> None:
    assert table.for_preset("GFG99") is None
    assert table.for_preset("OFDSrzZ8") is None


def test_the_spools_preset_id_comes_first(table: DensityTable) -> None:
    """A Tri Color spool names Bambu PLA Silk: its name is never read."""
    assert resolve(group(slicer_filament="GFA05"), table) == ProfileDensity(
        "1.32", "Bambu PLA Silk @base (spool preset GFA05)"
    )


def test_a_local_preset_gives_its_own_density(table: DensityTable) -> None:
    local = LocalDensity(name="Cookiecad PETG Magic @H2C", density_g_cm3="1.275")
    assert resolve(group(slicer_filament="2", local_preset=local), table) == ProfileDensity(
        "1.275", "Bambuddy local preset Cookiecad PETG Magic @H2C (spool preset 2)"
    )


def test_the_spools_own_density_comes_first(table: DensityTable) -> None:
    own = SpoolDensity(number=41, density_g_cm3="1.2")
    assert resolve(
        group(slicer_filament="GFA00", tray_filament="GFA05", spool_density=own), table
    ) == ProfileDensity("1.2", "ScadBuddy spool #41")


def test_the_trays_preset_comes_after_the_spools(table: DensityTable) -> None:
    """A spool with no preset of its own, in a tray reporting ``GFA05``: Bambu PLA Silk."""
    assert resolve(group(tray_filament="GFA05"), table) == ProfileDensity(
        "1.32", "Bambu PLA Silk @base (tray preset GFA05)"
    )
    assert resolve(group(slicer_filament="GFA00", tray_filament="GFA05"), table) == (
        ProfileDensity("1.26", "Bambu PLA Basic @base (spool preset GFA00)")
    )
    # A spool preset the tree lacks falls through to the tray's.
    assert resolve(group(slicer_filament="GFG99", tray_filament="GFA05"), table) == (
        ProfileDensity("1.32", "Bambu PLA Silk @base (tray preset GFA05)")
    )
    local = LocalDensity(name="Cookiecad PETG Magic @H2C", density_g_cm3="1.275")
    assert resolve(group(tray_filament="2", tray_local_preset=local), table) == ProfileDensity(
        "1.275", "Bambuddy local preset Cookiecad PETG Magic @H2C (tray preset 2)"
    )


def test_no_source_gives_none(table: DensityTable) -> None:
    assert resolve(group(), table) is None
    # Without the image's tree only a local preset's or the spools' own densities count.
    assert resolve(group(slicer_filament="GFA05"), None) is None


def test_the_table_is_built_once_per_tree(tmp_path: Path) -> None:
    assert density_table(tmp_path) is None
    assert density_table(PROFILES) is density_table(PROFILES)
