"""A filament's density from the slicer's profiles (#6136, `orders/density.py`)."""

from __future__ import annotations

from pathlib import Path

import pytest

from scadbuddy.orders.density import DensityTable, ProfileDensity, density_table
from scadbuddy.slicing.profiles import Profiles

#: A small BBL tree laid out as the image's: `@base` presets, per-printer children, an
#: `include`, a `Generic` family whose presets disagree, and `fdm_filament_common`'s 0.
PROFILES = Path(__file__).parent / "density_profiles"


@pytest.fixture
def table() -> DensityTable:
    return DensityTable.build(Profiles(PROFILES))


def test_the_vendors_family_of_the_material_and_subtype(table: DensityTable) -> None:
    # The `@base` preset's density, not a printer's own override (H2C's 1.30).
    assert table.lookup("PLA", "Basic", "Bambu Lab") == ProfileDensity(
        "1.26", "Bambu PLA Basic @base"
    )
    assert table.lookup("petg", "translucent", "bambu lab") == ProfileDensity(
        "1.25", "Bambu PETG Translucent @base"
    )


def test_a_family_with_no_subtype_and_a_value_from_an_include(table: DensityTable) -> None:
    # No `@base`: the one density its presets share, which here an `include` gives.
    assert table.lookup("PETG-CF", None, "Bambu Lab") == ProfileDensity(
        "1.29", "Bambu PETG-CF @BBL X1C"
    )


def test_a_subtype_with_no_preset_of_its_brand_gives_none(table: DensityTable) -> None:
    """Never another family's value, and never the Generic one for a brand the tree has."""
    assert table.lookup("PLA", "Galaxy", "Bambu Lab") is None


def test_bambus_dual_and_tri_color_are_its_silk(table: DensityTable) -> None:
    """Its "PLA Silk Multi-Color", sliced with Bambu PLA Silk (`SUBTYPE_FAMILIES`)."""
    for subtype in ("Dual Color", "tri color"):
        assert table.lookup("PLA", subtype, "Bambu Lab") == ProfileDensity(
            "1.32", "Bambu PLA Silk @base"
        )
    # Only for Bambu's own.
    assert table.lookup("PLA", "Tri Color", "Inland") == ProfileDensity("1.24", "Generic PLA @base")


def test_the_spools_slicer_filament_comes_first(table: DensityTable) -> None:
    """A spool whose preset is Bambu PLA Silk (`GFA05`) takes that density."""
    assert table.lookup("PLA", "Galaxy", "Bambu Lab", "GFA05") == ProfileDensity(
        "1.32", "Bambu PLA Silk @base"
    )
    # An id the tree lacks falls back to the names.
    assert table.lookup("PLA", "Basic", "Bambu Lab", "OFDSrzZ8") == ProfileDensity(
        "1.26", "Bambu PLA Basic @base"
    )


def test_another_makers_filament_takes_the_generic_family(table: DensityTable) -> None:
    assert table.lookup("PLA", "Silk", "Insignia") == ProfileDensity(
        "1.21", "Generic PLA Silk @base"
    )
    # A subtype the Generic presets do not name (Basic, Plus, Glow) is the material's.
    assert table.lookup("PLA", "Basic", "Inland") == ProfileDensity("1.24", "Generic PLA @base")
    assert table.lookup("PLA", "Glow", "Inland") == ProfileDensity("1.24", "Generic PLA @base")
    assert table.lookup("PLA", None, None) == ProfileDensity("1.24", "Generic PLA @base")


def test_a_family_whose_presets_disagree_gives_none(table: DensityTable) -> None:
    assert table.lookup("PETG", "Basic", "Insignia") is None


def test_a_material_the_tree_lacks_gives_none(table: DensityTable) -> None:
    assert table.lookup("PEEK", None, "Insignia") is None


def test_the_table_is_built_once_per_tree(tmp_path: Path) -> None:
    assert density_table(tmp_path) is None
    assert density_table(PROFILES) is density_table(PROFILES)
