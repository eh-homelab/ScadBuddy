"""#4013: a slice whose filaments are not the ones sent for it is never queued."""

from __future__ import annotations

from typing import cast

import pytest

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.dispatch import SlicePlan
from scadbuddy.bambuddy.models import FilamentRequirement, FilamentRequirements, PresetRef
from scadbuddy.bambuddy.options import PrintOptions
from scadbuddy.bambuddy.print_run import PlannedRun, PlatePlan, queue_plate, sliced_as_planned
from scadbuddy.core.problems import ApiError

GLOW, BLACK = "#D3CFB5", "#27272C"


class Sliced:
    """Bambuddy holding one sliced file, read back as its filament requirements."""

    def __init__(self, *filaments: FilamentRequirement) -> None:
        self.filaments = list(filaments)
        self.reads: list[tuple[int, int | None]] = []
        self.enqueued = 0

    async def filament_requirements(
        self, file_id: int, *, plate_id: int | None = None
    ) -> FilamentRequirements:
        self.reads.append((file_id, plate_id))
        return FilamentRequirements(file_id=file_id, plate_id=plate_id, filaments=self.filaments)

    async def enqueue(self, item: object) -> object:
        self.enqueued += 1
        raise AssertionError("a slice that is not the one planned was queued")


def slot(slot_id: int, color: str | None, *, used: bool = True) -> FilamentRequirement:
    return FilamentRequirement(slot_id=slot_id, type="PLA", color=color, used_in_plate=used)


async def test_a_slot_sliced_in_another_slots_colour_is_refused_naming_it() -> None:
    """Library file 4223: plate 2 of 2440 came back glow in both slots, and was queued."""
    sliced = Sliced(slot(1, GLOW), slot(2, GLOW))

    with pytest.raises(ApiError) as raised:
        await sliced_as_planned(sliced, sliced=4223, plate_id=2, colours=[GLOW, BLACK])

    assert raised.value.status == 502
    assert "plate 2 with filament 2 in #D3CFB5, the colour chosen for another filament," in (
        raised.value.detail
    )
    assert sliced.reads == [(4223, 2)]


async def test_the_filaments_sent_pass_whatever_their_spelling() -> None:
    sliced = Sliced(slot(1, "D3CFB5FF"), slot(2, "#27272c"))
    await sliced_as_planned(sliced, sliced=4962, plate_id=1, colours=[GLOW, BLACK])


async def test_a_slot_the_plate_leaves_unused_or_with_no_colour_is_not_judged() -> None:
    sliced = Sliced(slot(1, GLOW), slot(2, GLOW, used=False), slot(3, None), slot(4, GLOW))
    await sliced_as_planned(sliced, sliced=1, plate_id=1, colours=[GLOW, BLACK, BLACK, ""])


async def test_a_colour_that_is_no_other_slots_is_not_a_swap() -> None:
    """Only another slot's colour says Bambuddy gave the slot another's filament; one
    that is none of the chosen colours says nothing about which profile it sliced with."""
    sliced = Sliced(slot(1, "#0047BB"), slot(2, BLACK))
    await sliced_as_planned(sliced, sliced=77, plate_id=1, colours=["#688197", BLACK])


async def test_queueing_a_swapped_slice_stops_before_the_queue() -> None:
    sliced = Sliced(slot(1, GLOW), slot(2, GLOW))
    preset = PresetRef(source="cloud", id="GM041")
    plan = SlicePlan(
        printer_preset=preset,
        process_preset=preset,
        filament_presets=[PresetRef(source="local", id="68"), PresetRef(source="local", id="21")],
        filament_colours=[GLOW, BLACK],
        bed_type="Textured PEI Plate",
    )
    planned = PlannedRun(
        library_file_id=2440,
        printer_id=1,
        nozzle_size="0.4",
        copies=1,
        options=PrintOptions(),
        plates=[PlatePlan(plate_id=2, plan=plan)],
    )

    with pytest.raises(ApiError, match="filament 2"):
        await queue_plate(
            cast(BambuddyClient, sliced),
            planned=planned,
            plate=planned.plates[0],
            sliced=4223,
            rack=None,
        )

    assert sliced.enqueued == 0
