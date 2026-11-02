from collections.abc import Collection
from pathlib import Path
from typing import Any

import pytest

from scadbuddy.bambuddy.dispatch import SlicePlan
from scadbuddy.bambuddy.models import LocalPresetDetail, PresetRef
from scadbuddy.slicing.held_presets import HeldRow, held_row
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


class MemoryHeld:
    """`HeldPresets` in memory: Bambuddy's local presets as a refresh kept them (#4645)."""

    def __init__(self, *details: LocalPresetDetail) -> None:
        self.docs = {detail.id: detail for detail in details}
        #: The ids each `documents` call asked for.
        self.asked: list[list[int]] = []

    async def rows(self) -> list[HeldRow]:
        return [held_row(detail) for detail in self.docs.values()]

    async def documents(self, ids: Collection[int]) -> dict[int, LocalPresetDetail]:
        self.asked.append(sorted(ids))
        return {i: self.docs[i] for i in ids if i in self.docs}


def local_preset(
    preset_id: int, name: str, slot: str, setting: dict[str, Any], **row: Any
) -> LocalPresetDetail:
    """A row of Bambuddy's ``GET /local-presets/{id}`` (#4233), ``setting`` its document."""
    return LocalPresetDetail.model_validate(
        {
            "id": preset_id,
            "name": name,
            "preset_type": slot,
            "source": "orcaslicer",
            "inherits": setting.get("inherits"),
            "setting": setting,
            **row,
        }
    )
