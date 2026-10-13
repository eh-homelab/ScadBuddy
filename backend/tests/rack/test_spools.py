"""What a rack pick records of its group's spools, and how a hotend's current filament
is named (#2170). Pure: no database."""

from __future__ import annotations

from scadbuddy.bambuddy.filament_ids import filament_material, filament_name
from scadbuddy.bambuddy.filaments import SpoolOption
from scadbuddy.bambuddy.models import NozzleRackSlot
from scadbuddy.rack.usage import PickedSpool, group_spools
from tests.rack.helpers import requirement


def test_a_group_records_each_used_slot_and_its_spool() -> None:
    filaments = [
        requirement(1, color="#3F8E43").model_copy(update={"used_grams": 12.5}),
        requirement(2, filament_type="PETG", color="#ffffff"),
        requirement(3, group_id=1),
        requirement(4, used=False),
    ]
    spools = {
        1: SpoolOption(
            spool_id=7,
            material="PLA",
            subtype="Basic",
            brand="Bambu",
            color_name="Green",
            colour="#3F8E43",
        )
    }

    assert group_spools(filaments, spools, 0) == [
        PickedSpool(
            slot_id=1,
            spool_id=7,
            label="Bambu PLA Basic Green",
            material="PLA",
            colour="#3F8E43",
            grams=12.5,
        ),
        # No inventory spool, and not sliced: the plate's own type and colour.
        PickedSpool(slot_id=2, label="PETG", material="PETG", colour="#FFFFFF"),
    ]


def test_a_filament_id_names_its_profile_and_material() -> None:
    assert filament_name("GFA00") == "Bambu PLA Basic"
    assert filament_material("GFA00") == "PLA"
    assert filament_material("gfl99") == "PLA"
    assert filament_material("GFG02") == "PETG"
    assert filament_material("GFZ42") is None
    assert filament_material("") is None
    assert filament_name(None) is None


def test_an_empty_mount_has_no_serial() -> None:
    """The firmware says ``N/A``; every empty mount would otherwise share one history."""
    empty = NozzleRackSlot.model_validate(
        {"id": 1, "serial_number": "N/A", "filament_id": None, "max_temp": 0}
    )
    assert empty.serial_number == ""
    assert empty.filament_id == ""
