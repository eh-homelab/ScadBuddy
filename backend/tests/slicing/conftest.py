from pathlib import Path

import pytest

from scadbuddy.bambuddy.dispatch import SlicePlan
from scadbuddy.bambuddy.models import PresetRef
from scadbuddy.slicing.profiles import Profiles

#: A vendor tree laid out as Bambu Studio's `resources/profiles/` is: `BBL.json` beside
#: `BBL/`, and the image's `VERSION`.
PROFILES = Path(__file__).parent / "profiles"


@pytest.fixture
def profiles() -> Profiles:
    return Profiles(PROFILES)


@pytest.fixture
def plan() -> SlicePlan:
    """Standard refs name a preset, a cloud ref is its `setting_id`, as Bambuddy's are."""
    return SlicePlan(
        printer_preset=PresetRef(source="standard", id="Test Printer 0.4 nozzle"),
        process_preset=PresetRef(source="standard", id="leaf"),
        filament_presets=[
            PresetRef(source="cloud", id="GF_PLA"),
            PresetRef(source="standard", id="Test PETG"),
        ],
        filament_colours=["#FF0000", "#00FF00"],
        bed_type="Textured PEI Plate",
    )
