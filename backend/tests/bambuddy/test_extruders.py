"""#469 — which extruder a chosen spool feeds, and what that means for the slice.

On a dual-nozzle H2C each AMS is wired to one side, so a spool's extruder is fixed by
where it is loaded. The recorded printer has the Filament Track Switch, which reports
the side through ``ams_switch_inlet``; a printer without one reports it through
``ams_extruder_map``. Both are covered, against ``printer-status-fts.json`` (printer 1
live on 2026-09-28: right 0.2 HS00, left 0.4 HH01; AMS 0/1 on inlet B, AMS 2 and the
HT on inlet A).
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
from scadbuddy.bambuddy.models import PrinterStatus, SlotChoice, SpoolAssignment
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


def test_with_the_switch_a_left_resting_spool_prints_on_the_right_at_0_2() -> None:
    """User ruling: any AMS reaches either nozzle, so AMS 2 (inlet A) is not refused;
    every filament goes to the right, the only side with a 0.2."""
    result = plan_extruders([RIGHT_02, LEFT_02], fts_status(), size="0.2", filament_count=2)
    assert result.errors == []
    assert result.extruders == (RIGHT, RIGHT)
    assert result.warnings == []


def test_with_the_switch_every_filament_goes_to_the_only_matching_side() -> None:
    result = plan_extruders([RIGHT_02, LEFT_02], fts_status(), size="0.4", filament_count=2)
    assert result.extruders == (LEFT, LEFT)


def test_with_the_switch_an_unknown_side_is_still_pinned_without_a_warning() -> None:
    """Review #2: never Auto when the nozzles differ."""
    result = plan_extruders([RIGHT_02, SHELF], fts_status(), size="0.2", filament_count=2)
    assert result.extruders == (RIGHT, RIGHT)
    assert result.warnings == []


def test_with_the_switch_and_both_sides_matching_the_default_is_auto() -> None:
    result = plan_extruders([RIGHT_02, LEFT_02], fts(**both_02()), size="0.2", filament_count=2)
    assert result == type(result)()


def test_with_the_switch_and_both_sides_matching_a_picked_side_is_pinned() -> None:
    result = plan_extruders(
        [RIGHT_02, LEFT_02], fts(**both_02()), size="0.2", filament_count=2, chosen={1: LEFT}
    )
    # Slot 2 was left on Auto beside a pick, so it rests where its inlet does.
    assert result.extruders == (LEFT, LEFT)
    result = plan_extruders(
        [RIGHT_02, SHELF], fts(**both_02()), size="0.2", filament_count=2, chosen={2: LEFT}
    )
    assert result.extruders == (RIGHT, LEFT)


def test_a_picked_side_without_the_chosen_size_is_refused() -> None:
    result = plan_extruders(
        [RIGHT_02], fts_status(), size="0.2", filament_count=1, chosen={1: LEFT}
    )
    assert result.errors == [
        "Slot 1 can't print on the left: its nozzle is 0.4 mm and this print is sliced for 0.2 mm."
    ]


def test_a_picked_side_whose_nozzle_is_not_reported_is_refused() -> None:
    status = fts(nozzles=[{"nozzle_type": "HS00", "nozzle_diameter": "0.2"}])
    result = plan_extruders([RIGHT_02], status, size="0.2", filament_count=1, chosen={1: LEFT})
    assert result.errors == [
        "Slot 1 can't print on the left: the printer doesn't report the nozzle fitted there."
    ]


def test_a_pick_for_a_slot_the_model_has_not_is_ignored() -> None:
    result = plan_extruders(
        [RIGHT_02], fts_status(), size="0.2", filament_count=1, chosen={2: LEFT}
    )
    assert result.errors == []
    assert result.extruders == (RIGHT,)


@pytest.mark.parametrize("status", [fts_status(), mapped_status()])
def test_neither_side_fitted_with_the_size_is_refused(status: PrinterStatus) -> None:
    result = plan_extruders([RIGHT_02], status, size="0.6", filament_count=1)
    assert result.errors == [
        "Neither nozzle is 0.6 mm: the right has 0.2 mm and the left 0.4 mm. Choose 0.2 or "
        "0.4, or fit a 0.6 mm nozzle."
    ]
    assert result.extruders is None


def test_unreported_nozzles_leave_the_slicer_to_choose_and_say_so() -> None:
    result = plan_extruders([RIGHT_02], None, size="0.2", filament_count=1)
    assert result.errors == []
    assert result.extruders is None
    [warning] = result.warnings
    assert warning.kind == "side-unknown"
    assert warning.slot_id is None
    assert "couldn't read which nozzles" in warning.message


# --- without the switch, each AMS is wired to one side ----------------------------------


def test_without_the_switch_a_spool_on_the_other_nozzle_is_refused_with_the_way_out() -> None:
    """Queue item 108, on a printer whose AMS 2 really is wired to the left."""
    result = plan_extruders([RIGHT_02, LEFT_02], mapped_status(), size="0.2", filament_count=2)
    assert result.errors == [
        "Slot 2's spool (AMS 2, left) is on the 0.4 mm nozzle; this print is sliced for "
        "0.2 mm. Pick a spool on the right, or choose 0.4."
    ]
    assert result.extruders is None


def test_without_the_switch_the_ht_and_external_holder_are_named() -> None:
    external, ht = SlotSide(1, 3, 255, 0, LEFT), SlotSide(1, 4, 128, 0, RIGHT)
    [error] = plan_extruders([external], mapped_status(), size="0.2", filament_count=1).errors
    assert error.startswith("Slot 1's spool (the external spool holder, left)")
    [error] = plan_extruders([ht], mapped_status(), size="0.4", filament_count=1).errors
    assert error.startswith("Slot 1's spool (AMS HT, right)")


def test_without_the_switch_an_unknown_side_is_pinned_to_the_only_match_and_warned() -> None:
    """Review #2 and #5: pinned, never Auto, and the warning names the actual cause."""
    result = plan_extruders([RIGHT_02, SHELF], mapped_status(), size="0.2", filament_count=2)
    assert result.extruders == (RIGHT, RIGHT)
    [warning] = result.warnings
    assert (warning.kind, warning.slot_id) == ("side-unknown", 2)
    assert warning.message == (
        "Slot 2's spool isn't loaded in this printer, so it's sliced for the right extruder, "
        "the one with the 0.2 mm nozzle. Load it where it feeds the right."
    )
    [warning] = plan_extruders(
        [RIGHT_02, NO_SIDE], mapped_status(), size="0.2", filament_count=2
    ).warnings
    assert warning.message.startswith("The printer doesn't say which extruder AMS 3 feeds (slot 2)")


def test_without_the_switch_and_both_matching_each_spool_keeps_its_side() -> None:
    result = plan_extruders(
        [RIGHT_02, LEFT_02], mapped_status(**both_02()), size="0.2", filament_count=2
    )
    assert result.extruders == (RIGHT, LEFT)


def test_without_the_switch_and_both_matching_an_unknown_side_is_auto_and_says_why() -> None:
    result = plan_extruders(
        [RIGHT_02, SHELF], mapped_status(**both_02()), size="0.2", filament_count=2
    )
    assert result.extruders is None
    [warning] = result.warnings
    assert warning.message == (
        "Slot 2's spool isn't loaded in this printer. Both nozzles are 0.2 mm, so the slicer "
        "chooses the extruder for every color."
    )


def test_without_the_switch_a_pick_against_the_spools_wiring_is_refused() -> None:
    result = plan_extruders(
        [RIGHT_02], mapped_status(**both_02()), size="0.2", filament_count=1, chosen={1: LEFT}
    )
    assert result.errors == [
        "Slot 1's spool (AMS 0) feeds the right extruder, so it can't print on the left."
    ]


def test_a_filament_with_no_spool_follows_the_first_pinned_one() -> None:
    result = plan_extruders([LEFT_02], mapped_status(), size="0.4", filament_count=3)
    assert result.extruders == (LEFT, LEFT, LEFT)
