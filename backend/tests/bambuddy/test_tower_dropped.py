"""#3962: a Bambuddy slice that came back with no prime tower that was asked for, on a
plate of two or more filaments, is said on the run, never refused."""

from __future__ import annotations

import io
import json
import zipfile
from collections.abc import AsyncGenerator
from typing import Any, cast

import pytest

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.dispatch import SlicePlan
from scadbuddy.bambuddy.models import FilamentRequirement, FilamentRequirements, PresetRef
from scadbuddy.bambuddy.options import PrintOptions
from scadbuddy.bambuddy.prime_tower import tower_dropped
from scadbuddy.bambuddy.print_run import PlannedRun, PlatePlan, queue_plate
from scadbuddy.core.problems import ApiError

#: Preview slice 140's own (issue #3962): the CLI turned the tower off, so it differs
#: from the process preset's.
DROPPED = {"enable_prime_tower": "0", "different_settings_to_system": ["enable_prime_tower", ""]}


def sliced_3mf(settings: dict[str, Any], *slots: int, plate: int = 1) -> bytes:
    filaments = "".join(
        f'<filament id="{slot}" type="PLA" color="#FF0000" used_g="3.5"/>' for slot in slots
    )
    info = f'<config><plate><metadata key="index" value="{plate}"/>{filaments}</plate></config>'
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zipped:
        zipped.writestr("Metadata/project_settings.config", json.dumps(settings))
        zipped.writestr("Metadata/slice_info.config", info)
        zipped.writestr(f"Metadata/plate_{plate}.gcode", "; sliced")
    return buffer.getvalue()


class Library:
    """Bambuddy holding one sliced file."""

    def __init__(self, payload: bytes | Exception) -> None:
        self.payload = payload
        self.downloads: list[int] = []

    async def download_library_file(self, file_id: int) -> AsyncGenerator[bytes]:
        self.downloads.append(file_id)
        if isinstance(self.payload, Exception):
            raise self.payload
        yield self.payload[:100]
        yield self.payload[100:]


async def warned(library: Library, overrides: dict[str, Any], plate_id: int = 1) -> list[str]:
    found = await tower_dropped(library, sliced=9, plate_id=plate_id, process_overrides=overrides)
    assert all(warning.kind == "prime-tower-not-applied" for warning in found)
    return [warning.message for warning in found]


async def test_a_tower_asked_for_and_sliced_off_is_warned() -> None:
    library = Library(sliced_3mf({"enable_prime_tower": "0"}, 1, 2, 3))

    [message] = await warned(library, {"enable_prime_tower": "1"})

    assert message.startswith(
        "Bambuddy's slicer dropped the prime tower from plate 1, which prints 3 filaments"
    )
    assert library.downloads == [9]


async def test_the_presets_own_tower_turned_off_by_the_slicer_is_warned() -> None:
    """No choice was made, so the preset's default (on) stood, and the CLI changed it."""
    assert await warned(Library(sliced_3mf(DROPPED, 1, 3)), {})


async def test_a_tower_the_slice_kept_says_nothing() -> None:
    kept = {"enable_prime_tower": "1", "different_settings_to_system": [""]}
    assert await warned(Library(sliced_3mf(kept, 1, 2)), {"enable_prime_tower": "1"}) == []


async def test_a_plate_of_one_filament_says_nothing() -> None:
    """Bambu Studio turns the tower off for it by design."""
    assert await warned(Library(sliced_3mf(DROPPED, 2)), {"enable_prime_tower": "1"}) == []


async def test_a_preset_whose_own_tower_is_off_says_nothing() -> None:
    off = {"enable_prime_tower": "0", "different_settings_to_system": ["sparse_infill_density"]}
    assert await warned(Library(sliced_3mf(off, 1, 2)), {}) == []


async def test_a_tower_asked_off_is_not_read() -> None:
    library = Library(sliced_3mf(DROPPED, 1, 2))
    assert await warned(library, {"enable_prime_tower": "0"}) == []
    assert library.downloads == []


@pytest.mark.parametrize(
    "payload", [b"not a zip", ApiError(502, "Bambuddy is down")], ids=["unreadable", "unanswered"]
)
async def test_a_file_that_cannot_be_read_says_nothing(payload: bytes | Exception) -> None:
    assert await warned(Library(payload), {"enable_prime_tower": "1"}) == []


async def test_a_whole_file_slice_reads_its_only_plate() -> None:
    library = Library(sliced_3mf({"enable_prime_tower": "0"}, 1, 2, plate=1))
    assert await warned(library, {"enable_prime_tower": "1"}, plate_id=3)


class Bambuddy(Library):
    """Enough of Bambuddy to queue one plate."""

    async def filament_requirements(
        self, file_id: int, *, plate_id: int | None = None
    ) -> FilamentRequirements:
        return FilamentRequirements(
            file_id=file_id,
            plate_id=plate_id,
            filaments=[
                FilamentRequirement(slot_id=n, type="PLA", color=c, used_in_plate=True)
                for n, c in ((1, "#FF0000"), (2, "#00FF00"))
            ],
        )

    async def enqueue(self, item: object) -> Any:
        return type("Item", (), {"id": 31})()


def planned(slicer: str) -> PlannedRun:
    shared = PresetRef(source="standard", id="Bambu PLA Basic @BBL H2C")
    plan = SlicePlan(
        printer_preset=PresetRef(source="standard", id="Bambu Lab H2C 0.4 nozzle"),
        process_preset=PresetRef(source="standard", id="0.20mm Standard @BBL H2C"),
        filament_presets=[shared, shared],
        filament_colours=["#FF0000", "#00FF00"],
        bed_type="Textured PEI Plate",
    )
    return PlannedRun.model_validate(
        {
            "library_file_id": 5,
            "printer_id": 1,
            "nozzle_size": "0.4",
            "copies": 1,
            "options": PrintOptions(),
            "plates": [PlatePlan(plate_id=1, plan=plan)],
            "slicer": slicer,
        }
    )


async def test_a_queued_plate_carries_the_warning() -> None:
    bambuddy = Bambuddy(sliced_3mf(DROPPED, 1, 2))
    run = planned("bambuddy")

    queued = await queue_plate(
        cast(BambuddyClient, bambuddy), planned=run, plate=run.plates[0], sliced=9, rack=None
    )

    assert queued.item_id == 31
    assert [w.kind for w in queued.warnings] == ["prime-tower-not-applied"]


async def test_a_native_slice_is_not_read_for_it() -> None:
    """The native slicer keeps the tower with ``--filament-colour`` (#4422)."""
    bambuddy = Bambuddy(sliced_3mf(DROPPED, 1, 2))
    run = planned("native")

    queued = await queue_plate(
        cast(BambuddyClient, bambuddy), planned=run, plate=run.plates[0], sliced=9, rack=None
    )

    assert queued.warnings == []
    assert bambuddy.downloads == []
