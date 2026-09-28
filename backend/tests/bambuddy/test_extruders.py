"""#469 — which side a chosen spool is on, and which prints the nozzles can't take.

The recorded printer has the Filament Track Switch, which reports where each AMS rests
through ``ams_switch_inlet`` and lets any AMS reach either nozzle; a printer without one
wires each AMS to one side and reports it through ``ams_extruder_map``. Both are
covered, against ``printer-status-fts.json`` (printer 1 live on 2026-09-28: right 0.2
HS00, left 0.4 HH01; AMS 0/1 on inlet B, AMS 2 and the HT on inlet A).
"""

from __future__ import annotations

from typing import Any

import pytest

from scadbuddy.bambuddy.extruders import (
    LEFT,
    RIGHT,
    SlotSide,
    extruder_of,
    plan_extruders,
    side_of,
    slot_sides,
    track_switch,
)
from scadbuddy.bambuddy.filaments import FilamentPlan
from scadbuddy.bambuddy.models import NozzleInfo, PrinterStatus, SlotChoice, SpoolAssignment
from tests.bambuddy.conftest import recording


def fts_status() -> PrinterStatus:
    return PrinterStatus.model_validate(recording("printer-status-fts.json"))


def mapped_status(**changes: Any) -> PrinterStatus:
    """The same printer without a switch: each AMS names its extruder outright, and the
    inlet map is empty, as Bambuddy reports it then."""
    body = recording("printer-status-fts.json")
    body.update(
        {
            "ams_extruder_map": {"0": 0, "1": 1, "2": 1, "128": 0},
            "ams_switch_inlet": {},
            "fila_switch": {"installed": False},
            **changes,
        }
    )
    return PrinterStatus.model_validate(body)


def assignment(spool_id: int, ams_id: int, tray_id: int, printer_id: int = 1) -> SpoolAssignment:
    return SpoolAssignment(
        id=spool_id, spool_id=spool_id, printer_id=printer_id, ams_id=ams_id, tray_id=tray_id
    )


def plan(*pairs: tuple[int, int]) -> FilamentPlan:
    return FilamentPlan(slots=[SlotChoice(slot_id=slot, spool_id=spool) for slot, spool in pairs])


def test_the_status_carries_the_switch_and_its_inlets() -> None:
    status = fts_status()
    assert status.fila_switch is not None and status.fila_switch.installed
    assert status.ams_extruder_map == {}
    assert status.ams_switch_inlet == {"0": "B", "1": "B", "128": "A", "2": "A"}


@pytest.mark.parametrize(
    ("ams_id", "extruder"),
    [(0, RIGHT), (1, RIGHT), (2, LEFT), (128, LEFT), (3, None)],
)
def test_with_the_switch_the_inlet_names_the_side(ams_id: int, extruder: int | None) -> None:
    """Inlet A feeds the left extruder and B the right (upstream ``fts_routing.py``).
    AMS 3 is not bound to an inlet, so its side is unknown, never "right"."""
    assert extruder_of(ams_id, 0, fts_status()) == extruder


@pytest.mark.parametrize(
    ("ams_id", "extruder"), [(0, RIGHT), (1, LEFT), (2, LEFT), (128, RIGHT), (3, None)]
)
def test_without_the_switch_the_extruder_map_names_the_side(
    ams_id: int, extruder: int | None
) -> None:
    assert extruder_of(ams_id, 2, mapped_status()) == extruder


def test_a_real_extruder_id_wins_over_the_inlet() -> None:
    body = recording("printer-status-fts.json")
    body["ams_extruder_map"] = {"0": 1}
    assert extruder_of(0, 0, PrinterStatus.model_validate(body)) == LEFT


@pytest.mark.parametrize(("tray_id", "extruder"), [(0, LEFT), (1, RIGHT), (2, None)])
def test_the_external_holder_names_its_side_by_tray(tray_id: int, extruder: int | None) -> None:
    """Assignment ``ams_id`` 255: tray 0 is Ext-L (vt_tray 254), tray 1 is Ext-R (255)."""
    assert extruder_of(255, tray_id, fts_status()) == extruder


@pytest.mark.parametrize("mapped", [2, 14, -1])
def test_an_extruder_id_other_than_0_or_1_is_unknown(mapped: int) -> None:
    """Review #4: upstream stores whatever the AMS info bits hold, bar 0xE."""
    assert extruder_of(0, 0, mapped_status(ams_extruder_map={"0": mapped})) is None


def test_an_unreadable_status_knows_no_side() -> None:
    assert extruder_of(0, 0, None) is None


def test_sides_are_labelled_as_the_printer_labels_them() -> None:
    assert (side_of(RIGHT), side_of(LEFT), side_of(None)) == ("R", "L", None)


def test_each_slot_takes_the_side_of_its_spools_tray_on_this_printer() -> None:
    sides = slot_sides(
        plan((1, 9), (2, 10), (3, 5), (4, 7)),
        [
            assignment(9, 0, 1),
            assignment(10, 2, 0),
            # Spool 7 is in another printer: none of this printer's wiring applies.
            assignment(7, 2, 0, printer_id=2),
        ],
        fts_status(),
        printer_id=1,
    )
    assert [(side.slot_id, side.extruder) for side in sides] == [
        (1, RIGHT),
        (2, LEFT),
        (3, None),
        (4, None),
    ]
    assert (sides[1].ams_id, sides[2].ams_id) == (2, None)


def test_the_last_assignment_of_a_spool_wins() -> None:
    """Bambuddy keeps history rows; a spool moved from AMS 0 to AMS 2 is on the left."""
    [side] = slot_sides(
        plan((1, 9)), [assignment(9, 0, 1), assignment(9, 2, 0)], fts_status(), printer_id=1
    )
    assert side.extruder == LEFT


def test_each_unknown_side_says_why() -> None:
    sides = slot_sides(plan((1, 5), (2, 9)), [assignment(9, 3, 0)], fts_status(), printer_id=1)
    assert [side.unknown for side in sides] == ["not-loaded", "no-side"]
    [side] = slot_sides(plan((1, 9)), [assignment(9, 0, 1)], None, printer_id=1)
    assert side.unknown == "no-status"


# --- the plan: with the Filament Track Switch, sides constrain nothing ------------------

RIGHT_02, LEFT_02 = SlotSide(1, 9, 0, 1, RIGHT), SlotSide(2, 10, 2, 0, LEFT)
SHELF = SlotSide(2, 5, None, None, None, "not-loaded")
NO_SIDE = SlotSide(2, 11, 3, 0, None, "no-side")


def both_02(**changes: Any) -> dict[str, Any]:
    return {"nozzles": [{"nozzle_type": "HS00", "nozzle_diameter": "0.2"}] * 2, **changes}


def fts(**changes: Any) -> PrinterStatus:
    body = recording("printer-status-fts.json")
    body.update(changes)
    return PrinterStatus.model_validate(body)


def test_the_recorded_printer_has_the_switch() -> None:
    assert track_switch(fts_status())
    assert not track_switch(mapped_status())
    assert not track_switch(None)


def test_with_the_switch_a_left_resting_spool_prints_in_one_color_on_the_right() -> None:
    """User ruling: any AMS reaches either nozzle, so AMS 2 (inlet A) is not refused."""
    result = plan_extruders([LEFT_02], fts_status(), size="0.2", used_slots={1})
    assert result.errors == []
    [warning] = result.warnings
    assert warning.kind == "side-unknown"
    assert warning.message == (
        "Only the right nozzle is 0.2 mm, and the slicer picks the extruder. If it picks "
        "the left, the printer pauses at the first layer."
    )


@pytest.mark.parametrize(
    ("size", "only", "other"), [("0.2", "right", "0.4"), ("0.4", "left", "0.2")]
)
def test_differing_nozzles_refuse_a_multi_color_print(size: str, only: str, other: str) -> None:
    """Queue item 108: the slicer spread two colors across both nozzles, and a pin in the
    3MF is ignored, so nothing but the refusal keeps that print off the printer."""
    result = plan_extruders([RIGHT_02, LEFT_02], fts_status(), size=size, used_slots={1, 2})
    assert result.errors == [
        "This printer has a 0.2 mm nozzle on the right and 0.4 mm on the left. The slicer "
        f"spreads a multi-color print across both, and ScadBuddy can't keep it on the {only}, "
        f"so the {other} mm side would pause it at the first layer. Fit a {size} mm nozzle "
        "on both sides, or print in one color."
    ]


def test_a_second_color_with_no_spool_still_counts() -> None:
    """The filaments the plate uses decide, not how many slots have a spool: the slicer
    maps every filament the plate's parts are assigned."""
    result = plan_extruders([RIGHT_02], fts_status(), size="0.2", used_slots={1, 2})
    assert len(result.errors) == 1


@pytest.mark.parametrize("status", [fts(**both_02()), mapped_status(**both_02())])
def test_both_nozzles_matching_print_any_colors(status: PrinterStatus) -> None:
    result = plan_extruders([RIGHT_02, LEFT_02], status, size="0.2", used_slots={1, 2})
    assert result.errors == []
    assert result.warnings == []


@pytest.mark.parametrize("status", [fts_status(), mapped_status()])
def test_neither_side_fitted_with_the_size_is_refused(status: PrinterStatus) -> None:
    result = plan_extruders([RIGHT_02], status, size="0.6", used_slots={1})
    assert result.errors == [
        "Neither nozzle is 0.6 mm: the right has 0.2 mm and the left 0.4 mm. Choose 0.2 or "
        "0.4, or fit a 0.6 mm nozzle."
    ]


def test_unreported_nozzles_are_warned_about() -> None:
    result = plan_extruders([RIGHT_02], None, size="0.2", used_slots={1, 2})
    assert result.errors == []
    [warning] = result.warnings
    assert warning.kind == "side-unknown"
    assert warning.slot_id is None
    assert "couldn't read which nozzles" in warning.message


#: A single-nozzle printer (X1C, P1S, A1): Bambuddy reports an empty second entry.
SINGLE = [
    {"nozzle_type": "HS00", "nozzle_diameter": "0.4"},
    {"nozzle_type": "", "nozzle_diameter": ""},
]


@pytest.mark.parametrize("nozzles", [SINGLE, SINGLE[:1]])
def test_a_single_nozzle_printer_prints_many_colors_through_its_one_nozzle(
    nozzles: list[dict[str, str]],
) -> None:
    """Review of #538: an X1C or P1S with an AMS has no left side to pause on."""
    status = mapped_status(nozzles=nozzles, ams_extruder_map={"0": 0}, fila_switch=None)
    result = plan_extruders([RIGHT_02, SHELF], status, size="0.4", used_slots={1, 2})
    assert result == type(result)()


@pytest.mark.parametrize("nozzles", [SINGLE, SINGLE[:1]])
def test_a_single_nozzle_printer_with_the_wrong_size_is_refused(
    nozzles: list[dict[str, str]],
) -> None:
    """Review of #538: the "no side matches" branch must refuse a single-nozzle
    printer's known, wrong-size nozzle the way the "one side matches" branch already
    does for the same printer shape — not warn and let a doomed print through."""
    status = mapped_status(nozzles=nozzles, ams_extruder_map={"0": 0}, fila_switch=None)
    result = plan_extruders([RIGHT_02, SHELF], status, size="0.2", used_slots={1, 2})
    assert result.errors == [
        "The nozzle is 0.4 mm, not 0.2 mm. Choose 0.4, or fit a 0.2 mm nozzle."
    ]
    assert result.warnings == []


@pytest.mark.parametrize("status", [fts_status(), mapped_status()])
def test_a_two_nozzle_printer_with_one_side_unreported_is_warned_not_refused(
    status: PrinterStatus,
) -> None:
    """Partial MQTT state: the left is there (its type, its AMS) but its size isn't."""
    nozzles = [
        {"nozzle_type": "HS00", "nozzle_diameter": "0.2"},
        {"nozzle_type": "HH01", "nozzle_diameter": ""},
    ]
    status = status.model_copy(update={"nozzles": [NozzleInfo.model_validate(n) for n in nozzles]})
    result = plan_extruders([RIGHT_02, SHELF], status, size="0.2", used_slots={1, 2})
    assert result.errors == []
    [warning] = result.warnings
    assert warning.kind == "side-unknown"
    assert warning.message == (
        "The right nozzle is 0.2 mm, but the printer didn't report the left one, so "
        "nothing checks that the slicer's extruders match them."
    )


@pytest.mark.parametrize("status", [fts_status(), mapped_status()])
def test_a_known_mismatched_side_with_the_other_unreported_is_warned_not_refused(
    status: PrinterStatus,
) -> None:
    """Review of #538: the right is known and wrong for the requested size, and nothing
    names the left at all — say what's known, not that nothing was read."""
    nozzles = [
        {"nozzle_type": "HS00", "nozzle_diameter": "0.4"},
        {"nozzle_type": "", "nozzle_diameter": ""},
    ]
    status = status.model_copy(update={"nozzles": [NozzleInfo.model_validate(n) for n in nozzles]})
    result = plan_extruders([RIGHT_02, SHELF], status, size="0.2", used_slots={1, 2})
    assert result.errors == []
    [warning] = result.warnings
    assert warning.kind == "side-unknown"
    assert warning.message == (
        "The right nozzle is 0.4 mm, not 0.2 mm, and the printer didn't report the left "
        "one, so nothing checks that the slicer's extruders match them."
    )


def test_an_unreported_left_on_a_printer_wired_to_it_is_warned_about() -> None:
    """Nothing but ``ams_extruder_map`` says the left exists."""
    status = mapped_status(nozzles=SINGLE, fila_switch=None)
    result = plan_extruders([RIGHT_02, SHELF], status, size="0.4", used_slots={1, 2})
    assert result.errors == []
    assert [warning.kind for warning in result.warnings] == ["side-unknown"]


def test_a_plate_using_one_filament_of_two_is_not_refused() -> None:
    """Only the plate's filaments count: slot 2's spool isn't printed from."""
    result = plan_extruders([RIGHT_02, LEFT_02], fts_status(), size="0.2", used_slots={1})
    assert result.errors == []


# --- without the switch, each AMS is wired to one side ----------------------------------


def test_without_the_switch_a_spool_on_the_other_nozzle_is_refused_with_the_way_out() -> None:
    result = plan_extruders([RIGHT_02, LEFT_02], mapped_status(), size="0.2", used_slots={1, 2})
    assert result.errors == [
        "Slot 2's spool (AMS 2, left) is on the 0.4 mm nozzle; this print is sliced for "
        "0.2 mm. Pick a spool on the right, or choose 0.4."
    ]


def test_without_the_switch_the_ht_and_external_holder_are_named() -> None:
    external, ht = SlotSide(1, 3, 255, 0, LEFT), SlotSide(1, 4, 128, 0, RIGHT)
    [error] = plan_extruders([external], mapped_status(), size="0.2", used_slots={1}).errors
    assert error.startswith("Slot 1's spool (the external spool holder, left)")
    [error] = plan_extruders([ht], mapped_status(), size="0.4", used_slots={1}).errors
    assert error.startswith("Slot 1's spool (AMS HT, right)")


def test_without_the_switch_a_spool_of_unknown_side_is_not_refused() -> None:
    status = mapped_status(**both_02())
    result = plan_extruders([RIGHT_02, SHELF], status, size="0.2", used_slots={1, 2})
    assert result == type(result)()
    result = plan_extruders([RIGHT_02, NO_SIDE], status, size="0.2", used_slots={1, 2})
    assert result == type(result)()
