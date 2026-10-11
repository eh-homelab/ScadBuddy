"""#2166 — ScadBuddy plans which nozzle each filament prints from, by material, and
ignores where a spool rests when the Filament Track Switch is fitted."""

from __future__ import annotations

import io
import json
import zipfile
from typing import Any

import pytest

from scadbuddy.bambuddy.extruders import LEFT, RIGHT
from scadbuddy.bambuddy.filaments import FilamentPlan
from scadbuddy.bambuddy.models import NozzleChoice, PrinterStatus, Spool, SpoolAssignment
from scadbuddy.bambuddy.nozzle_plan import (
    PlanInput,
    plan_inputs,
    plan_nozzles,
    spool_name,
)
from scadbuddy.bambuddy.resolver import PrintChoices
from scadbuddy.bambuddy.trays import family
from scadbuddy.render.bambu3mf import FilamentMap, state_nozzles
from tests.bambuddy.test_extruders import fts_status, mapped_status

HIGH_FLOW_04 = {"nozzle_type": "HH01", "nozzle_diameter": "0.4"}
HF = [NozzleChoice(size="0.4", flow="high_flow")]


def h2c(*, switch: bool = True, **changes: Any) -> PrinterStatus:
    """The maintainer's H2C on 2026-10-10: a 0.4 High Flow on each side."""
    status = fts_status() if switch else mapped_status()
    return status.model_copy(
        update={
            "nozzles": [
                status.nozzles[0].model_copy(update=HIGH_FLOW_04),
                status.nozzles[1].model_copy(update=HIGH_FLOW_04),
            ],
            **changes,
        }
    )


GREEN = PlanInput(1, "PLA", "#3F8E43", "Mistletoe Green", rests=RIGHT)
BLACK = PlanInput(2, "PLA", "#27272C", "Inland Black", rests=RIGHT)


def test_two_colours_of_one_material_go_one_to_each_nozzle() -> None:
    """Queue item 268's file, both spools resting on the right: with the switch the
    rest is ignored, and green and black each get a nozzle of their own."""
    plan = plan_nozzles(h2c(), HF, [GREEN, BLACK])

    assert plan is not None
    assert [(slot.slot_id, slot.side) for slot in plan.slots] == [(1, "R"), (2, "L")]
    assert plan.summary == "Mistletoe Green → right · Inland Black → left · 0.4 High Flow"
    assert plan.track_switch
    assert plan.nozzle_stats(HF) == ["High Flow#1", "High Flow#1"]
    assert plan.filament_map(2) == ["2", "1"]
    assert plan.volume_map(2) == ["1", "1"]


def test_without_the_switch_a_spool_prints_on_the_side_it_feeds() -> None:
    plan = plan_nozzles(h2c(switch=False), HF, [GREEN, BLACK])

    assert plan is not None
    assert {slot.side for slot in plan.slots} == {"R"}
    assert plan.nozzle_stats(HF) == ["High Flow#0", "High Flow#1"]
    assert plan.summary.endswith("· 0.4 High Flow")


def test_materials_that_cannot_share_a_hotend_are_kept_apart() -> None:
    """Three PLA and one PETG: the PLA share a side, and the PETG has the other."""
    filaments = [
        PlanInput(1, "PLA Basic"),
        PlanInput(2, "PETG HF"),
        PlanInput(3, "PLA-Matte"),
        PlanInput(4, "PLA"),
    ]
    plan = plan_nozzles(h2c(), HF, filaments)

    assert plan is not None
    sides = {slot.slot_id: slot.side for slot in plan.slots}
    assert sides[1] == sides[3] == sides[4] != sides[2]


def test_a_side_chosen_by_hand_wins() -> None:
    plan = plan_nozzles(h2c(), HF, [GREEN, BLACK], {1: "R", 2: "R"})

    assert plan is not None
    assert [slot.side for slot in plan.slots] == ["R", "R"]
    assert all(slot.by_hand for slot in plan.slots)
    assert plan.nozzle_stats(HF) == ["High Flow#0", "High Flow#1"]


def test_only_the_side_with_the_size_is_planned() -> None:
    """Queue item 159's printer: the right 0.2, the left 0.4. A 0.2 print goes right."""
    plan = plan_nozzles(fts_status(), [NozzleChoice(size="0.2")], [GREEN, BLACK])

    assert plan is not None
    assert {slot.side for slot in plan.slots} == {"R"}


@pytest.mark.parametrize(
    "status", [None, mapped_status(nozzles=[HIGH_FLOW_04], ams_extruder_map={})]
)
def test_nothing_is_planned_without_two_known_sides(status: PrinterStatus | None) -> None:
    assert plan_nozzles(status, HF, [GREEN, BLACK]) is None


def test_a_family_is_the_materials_first_word() -> None:
    assert family("PLA-CF") == family("pla basic") == "PLA"
    assert family(None) == ""


def test_a_spool_is_named_as_a_person_names_it() -> None:
    assert spool_name(
        Spool(id=1, material="PLA", brand="Bambu Lab", color_name="Mistletoe Green")
    ) == ("Mistletoe Green")
    assert (
        spool_name(Spool(id=2, material="PLA", brand="Inland", color_name="Black"))
        == "Inland Black"
    )
    assert spool_name(Spool(id=3, material="PETG", brand="Generic")) == "Generic PETG"


def test_inputs_come_from_the_inventory_and_where_spools_rest() -> None:
    spools = [Spool(id=1, material="PLA", color_name="Mistletoe Green", rgba="3F8E43FF")]
    assignments = [SpoolAssignment(id=1, spool_id=1, printer_id=1, ams_id=2, tray_id=0)]
    plan = FilamentPlan.model_validate({"slots": [{"slot_id": 1, "spool_id": 1}]})

    [found] = plan_inputs(plan, spools, assignments, fts_status(), printer_id=1)

    assert found == PlanInput(1, "PLA", "#3F8E43", "Mistletoe Green", rests=LEFT)


def _two_filaments() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("3D/3dmodel.model", "<model/>")
        settings = {"filament_colour": ["#3F8E43", "#27272C"]}
        archive.writestr("Metadata/project_settings.config", json.dumps(settings))
    return buffer.getvalue()


def test_the_map_is_written_in_manual_mode() -> None:
    """What the 3MF carries for the slicer (module docstring of ``nozzle_plan``)."""
    payload = state_nozzles(
        _two_filaments(),
        nozzle_stats=["High Flow#1", "High Flow#1"],
        filament_map=FilamentMap(("1", "2"), ("1", "1")),
    )
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        settings = json.loads(archive.read("Metadata/project_settings.config"))
    assert settings["filament_map"] == ["1", "2"]
    assert settings["filament_map_mode"] == "Manual"
    assert settings["filament_volume_map"] == ["1", "1"]


def test_a_map_of_another_length_is_not_written() -> None:
    payload = state_nozzles(_two_filaments(), filament_map=FilamentMap(("1",), ("1",)))
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        settings = json.loads(archive.read("Metadata/project_settings.config"))
    assert "filament_map_mode" not in settings


def test_the_choices_carry_sides_chosen_by_hand() -> None:
    choices = PrintChoices.model_validate({"nozzles": [{"size": "0.4"}], "sides": {"2": "L"}})
    assert choices.sides == {2: "L"}
