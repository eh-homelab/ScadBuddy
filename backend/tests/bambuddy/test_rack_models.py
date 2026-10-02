"""Bambuddy's rack and filament-group wire shapes (#836, spec 2026-10-01 §2, §6)."""

from __future__ import annotations

from typing import get_args

from scadbuddy.bambuddy.filaments import WarningKind
from scadbuddy.bambuddy.models import (
    FilamentRequirements,
    PrinterStatus,
    QueueItemCreate,
    RackAlgorithm,
)
from tests.bambuddy.conftest import recording


def test_a_rack_slot_carries_its_serial_and_hides_it_from_repr() -> None:
    body = recording("printer-status-rack.json")
    body["nozzle_rack"][2]["serial_number"] = "TEST-HOTEND-17"
    slot = PrinterStatus.model_validate(body).nozzle_rack[2]
    assert slot.id == 17
    assert slot.serial_number == "TEST-HOTEND-17"
    assert "TEST-HOTEND-17" not in repr(slot)


def test_a_sliced_files_requirements_carry_the_group() -> None:
    """As measured on library file 228 (spec §8 unknown 2)."""
    parsed = FilamentRequirements.model_validate(
        {
            "filaments": [
                {
                    "slot_id": 1,
                    "type": "PLA",
                    "color": "#00B1B7",
                    "group_id": 0,
                    "group": {
                        "on_rack": True,
                        "nozzle_diameter": "0.20",
                        "volume_type": "Standard",
                        "filament_color": "#00B1B7",
                    },
                }
            ]
        }
    )
    [filament] = parsed.filaments
    assert filament.group_id == 0
    assert filament.group is not None
    assert (filament.group.on_rack, filament.group.nozzle_diameter) == (True, "0.20")


def test_an_unsliced_upload_has_no_group() -> None:
    parsed = FilamentRequirements.model_validate(
        {"filaments": [{"slot_id": 1, "type": "", "group_id": None, "group": None}]}
    )
    assert (parsed.filaments[0].group_id, parsed.filaments[0].group) == (None, None)


def test_a_numeric_group_diameter_is_read_as_text() -> None:
    parsed = FilamentRequirements.model_validate(
        {"filaments": [{"slot_id": 1, "group_id": 0, "group": {"nozzle_diameter": 0.4}}]}
    )
    assert parsed.filaments[0].group is not None
    assert parsed.filaments[0].group.nozzle_diameter == "0.4"


def test_the_rack_choice_is_keyed_by_group_id_on_the_wire() -> None:
    item = QueueItemCreate(printer_id=1, library_file_id=77, nozzle_rack_choice={"0": 4})
    assert item.model_dump(mode="json", exclude_none=True)["nozzle_rack_choice"] == {"0": 4}


def test_the_rack_warning_kinds_and_algorithms_exist() -> None:
    kinds = set(get_args(WarningKind))
    assert {"rack-unsafe-material", "rack-left-to-bambuddy", "rack-manual-partial"} <= kinds
    assert get_args(RackAlgorithm) == ("least_used", "oldest_first", "newest_first", "bambuddy")


def test_a_null_serial_from_the_firmware_reads_as_empty() -> None:
    """Pre-flight F11: a null must not fail parsing the whole printer status."""
    body = recording("printer-status-rack.json")
    body["nozzle_rack"][2]["serial_number"] = None
    slot = PrinterStatus.model_validate(body).nozzle_rack[2]
    assert slot.serial_number == ""


def test_a_numeric_serial_from_the_firmware_reads_as_text() -> None:
    """Final review Important 1: a non-string serial must not fail the whole status parse
    (and so cannot leak through a ValidationError's ``input_value``)."""
    body = recording("printer-status-rack.json")
    body["nozzle_rack"][2]["serial_number"] = 917  # invented
    slot = PrinterStatus.model_validate(body).nozzle_rack[2]
    assert slot.serial_number == "917"
