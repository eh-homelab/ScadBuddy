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
    extruder_map,
    extruder_of,
    mismatch_errors,
    side_of,
    slot_sides,
    unknown_side_warnings,
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
        ams_extruder_map={"0": 0, "1": 1, "2": 1, "128": 0},
        ams_switch_inlet={},
        fila_switch={"installed": False},
        **changes,
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


def test_a_spool_on_the_side_whose_nozzle_differs_is_refused_with_the_way_out() -> None:
    """Queue item 108: a 0.2 slice with a spool on the left, where the 0.4 is fitted."""
    sides = [SlotSide(1, 9, 0, 1, RIGHT), SlotSide(2, 10, 2, 0, LEFT)]
    assert mismatch_errors(sides, "0.2", fts_status()) == [
        "Slot 2's spool (AMS 2, left) is on the 0.4 mm nozzle; this print is sliced for "
        "0.2 mm. Pick a spool on the right, or choose 0.4."
    ]


def test_with_no_side_fitted_for_the_size_the_way_out_is_the_nozzle() -> None:
    sides = [SlotSide(1, 9, 0, 1, RIGHT)]
    assert mismatch_errors(sides, "0.6", fts_status()) == [
        "Slot 1's spool (AMS 0, right) is on the 0.2 mm nozzle; this print is sliced for "
        "0.6 mm. Choose 0.2, or fit a 0.6 mm nozzle on the right."
    ]


def test_the_ht_and_the_external_holder_are_named() -> None:
    sides = [SlotSide(1, 3, 128, 0, LEFT), SlotSide(2, 4, 255, 0, LEFT)]
    messages = mismatch_errors(sides, "0.2", fts_status())
    assert messages[0].startswith("Slot 1's spool (AMS HT, left)")
    assert messages[1].startswith("Slot 2's spool (the external spool holder, left)")


def test_a_matching_side_or_an_unknown_one_is_not_refused() -> None:
    sides = [SlotSide(1, 9, 0, 1, RIGHT), SlotSide(2, 5, None, None, None)]
    assert mismatch_errors(sides, "0.2", fts_status()) == []


def test_a_side_whose_nozzle_is_not_reported_is_not_refused() -> None:
    status = mapped_status(nozzles=[{"nozzle_type": "HS00", "nozzle_diameter": "0.2"}])
    assert mismatch_errors([SlotSide(1, 10, 2, 0, LEFT)], "0.2", status) == []


def test_an_unknown_side_is_a_warning_that_the_slicer_chooses() -> None:
    [warning] = unknown_side_warnings(
        [SlotSide(1, 9, 0, 1, RIGHT), SlotSide(2, 5, None, None, None)]
    )
    assert warning.kind == "side-unknown"
    assert warning.slot_id == 2
    assert "slicer" in warning.message


def test_every_side_known_pins_each_filament_to_it() -> None:
    sides = [SlotSide(1, 9, 0, 1, RIGHT), SlotSide(2, 10, 2, 0, LEFT)]
    assert extruder_map(sides, filament_count=2) == (RIGHT, LEFT)


def test_a_filament_with_no_spool_follows_the_first_pinned_one() -> None:
    """A filament no plate uses still needs an entry; the slicer never reads it."""
    assert extruder_map([SlotSide(2, 10, 2, 0, LEFT)], filament_count=3) == (LEFT, LEFT, LEFT)


def test_any_unknown_side_leaves_the_whole_file_to_the_slicer() -> None:
    sides = [SlotSide(1, 9, 0, 1, RIGHT), SlotSide(2, 5, None, None, None)]
    assert extruder_map(sides, filament_count=2) is None
    assert extruder_map([], filament_count=2) is None
