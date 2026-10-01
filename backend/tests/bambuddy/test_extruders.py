"""#469 — which side a loaded spool feeds, as the picker labels it. The run checks
nothing against it, nor against the mounted nozzles (#768).

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
    extruder_of,
    high_flow_warnings,
    side_of,
    slicer_nozzle_stats,
    with_sides,
)
from scadbuddy.bambuddy.filaments import FilamentOptions, LoadedAt, SpoolOption
from scadbuddy.bambuddy.models import NozzleChoice, PrinterStatus
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


#: A single-nozzle printer (X1C, P1S, A1): Bambuddy reports an empty second entry.
SINGLE = [
    {"nozzle_type": "HS00", "nozzle_diameter": "0.4"},
    {"nozzle_type": "", "nozzle_diameter": ""},
]


@pytest.mark.parametrize("tray_id", [0, 1])
def test_a_single_nozzle_printers_external_holder_feeds_its_one_nozzle(tray_id: int) -> None:
    status = mapped_status(nozzles=SINGLE, ams_extruder_map={"0": 0}, fila_switch=None)
    assert extruder_of(255, tray_id, status) == RIGHT


def spool(spool_id: int, ams_id: int, tray_id: int, printer_id: int = 1) -> SpoolOption:
    return SpoolOption(
        spool_id=spool_id,
        material="PLA",
        loaded=LoadedAt(printer_id=printer_id, ams_id=ams_id, tray_id=tray_id),
    )


@pytest.mark.parametrize(("status", "switch"), [(fts_status(), True), (mapped_status(), False)])
def test_with_sides_labels_each_spool_loaded_here(status: PrinterStatus, switch: bool) -> None:
    """A spool in another printer, or on the shelf, gets no side on this one."""
    options = FilamentOptions(
        library_file_id=1,
        printer_id=1,
        spools=[
            spool(1, 0, 0),
            spool(2, 2, 0),
            spool(3, 0, 0, printer_id=2),
            SpoolOption(spool_id=4, material="PLA"),
        ],
    )
    labelled = with_sides(options, status)
    assert labelled.track_switch is switch
    assert [(row.extruder, row.side) for row in labelled.spools] == [
        (RIGHT, "R"),
        (LEFT, "L"),
        (None, None),
        (None, None),
    ]


def _nozzles(*types: tuple[str, str]) -> PrinterStatus:
    return mapped_status(
        nozzles=[{"nozzle_type": kind, "nozzle_diameter": size} for kind, size in types]
    )


STANDARD_04 = [NozzleChoice(size="0.4")]


def test_a_mounted_high_flow_nozzle_of_the_size_is_warned_about() -> None:
    """#723, #797: queue item 149's printer, a standard right and a High Flow left, both
    0.4, and a print chosen for Standard flow."""
    [warning] = high_flow_warnings(_nozzles(("HS01", "0.4"), ("HH01", "0.4")), STANDARD_04)
    assert warning.kind == "hf-mounted"
    assert warning.message.startswith("The left nozzle is High Flow")


def test_a_mounted_high_flow_nozzle_of_the_size_is_warned_about_when_high_flow_is_chosen() -> None:
    """#797: the slice is always Standard flow (#484), so a High Flow choice still warns
    of a mounted High Flow nozzle of the chosen size."""
    status = _nozzles(("HS01", "0.4"), ("HH01", "0.4"))
    [warning] = high_flow_warnings(status, [NozzleChoice(size="0.4", flow="high_flow")])
    assert warning.kind == "hf-mounted"
    assert warning.message.startswith("The left nozzle is High Flow")


def test_index_0_is_the_right_nozzle() -> None:
    """``PrinterStatus.nozzles[0]`` is the right (main) extruder."""
    [warning] = high_flow_warnings(_nozzles(("HH01", "0.4"), ("HS01", "0.4")), STANDARD_04)
    assert warning.message.startswith("The right nozzle is High Flow")


def test_no_high_flow_warning_for_standard_nozzles_another_size_or_no_status() -> None:
    assert high_flow_warnings(_nozzles(("HS01", "0.4"), ("HS01", "0.4")), STANDARD_04) == []
    assert (
        high_flow_warnings(_nozzles(("HS00", "0.2"), ("HH01", "0.4")), [NozzleChoice(size="0.2")])
        == []
    )
    assert high_flow_warnings(None, STANDARD_04) == []


# --- #834: which extruders the slicer may put filament on ---------------------------------
#
# ``extruder_nozzle_stats`` is in the slicer's extruder order, which is not the printer's:
# the H2C preset's ``physical_extruder_map`` is ["1", "0"], so the slicer's first extruder
# is the left (physical 1) and its second the right (physical 0). Measured against the
# deployed slicer (bambu-studio-api bambuddy-1.2.5.6), 2026-09-30: ["Standard#0",
# "Standard#1"] puts every filament on the right (``extruder_id="2"``), ["Standard#1",
# "Standard#0"] on the left, and ["Standard#0", "Standard#0"] fails the slice.
ONLY_RIGHT = ["Standard#0", "Standard#1"]
ONLY_LEFT = ["Standard#1", "Standard#0"]


def test_only_the_side_with_the_size_is_offered_to_the_slicer() -> None:
    """Queue item 159's printer: the right 0.2 HS00, the left 0.4 HH01."""
    assert slicer_nozzle_stats(fts_status(), "0.2") == ONLY_RIGHT


def test_a_size_only_the_left_has_puts_everything_on_the_left() -> None:
    status = _nozzles(("HS01", "0.4"), ("HS00", "0.2"))
    assert slicer_nozzle_stats(status, "0.2") == ONLY_LEFT


def test_the_rack_counts_for_the_right_side() -> None:
    """The rack swaps onto the right (it serves physical extruder 0), so a spare of the
    size there is the right side's even when another size is mounted."""
    status = _nozzles(("HS01", "0.4"), ("HS00", "0.2"))
    # Every recorded spare is a 0.4, so the right has a standard 0.4 and the left does
    # not: the left's 0.2 is not the size.
    assert slicer_nozzle_stats(status, "0.4") == ONLY_RIGHT


def test_a_standard_nozzle_is_preferred_to_a_high_flow_one_of_the_size() -> None:
    """ScadBuddy slices standard flow, and queue item 149 paused on a High Flow left."""
    status = _nozzles(("HH01", "0.2"), ("HS00", "0.2"))
    assert slicer_nozzle_stats(status, "0.2") == ONLY_LEFT


def test_a_high_flow_nozzle_of_the_size_still_beats_another_size() -> None:
    """Nothing is refused on the mounted nozzles (#768), and #723 warns of the flow."""
    status = _nozzles(("HH01", "0.2"), ("HS01", "0.6"))
    assert slicer_nozzle_stats(status, "0.2") == ONLY_RIGHT


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (_nozzles(("HH01", "0.2"), ("HS01", "0.6")), ONLY_RIGHT),
        (_nozzles(("HS01", "0.6"), ("HH01", "0.2")), ONLY_LEFT),
    ],
)
def test_a_high_flow_only_side_is_stated_as_standard(
    status: PrinterStatus, expected: list[str]
) -> None:
    """ScadBuddy slices a Standard process, and the deployed slicer (bambu-studio-api
    bambuddy-1.2.5.6, 2026-09-30) groups ["Standard#0", "High Flow#1"] and Studio's
    ["Standard#0|High Flow#0", "Standard#0|High Flow#1"] exactly as ["Standard#0",
    "Standard#1"], rewriting both to it. So the side is stated as Standard, never as
    High Flow: the label would change nothing but the cache key."""
    stats = slicer_nozzle_stats(status, "0.2")
    assert stats == expected
    assert not any("High Flow" in entry for entry in stats)


@pytest.mark.parametrize(
    ("status", "size"),
    [
        # Both sides have it (the owner's case (a)): either may print, so the slicer
        # keeps its own choice.
        (_nozzles(("HS00", "0.2"), ("HS00", "0.2")), "0.2"),
        # Neither has it: stating no nozzle anywhere fails the slice, and the printer
        # may swap one in (#768), so the file is left as it was.
        (fts_status(), "0.6"),
        # Unreadable, and a printer without a left extruder.
        (None, "0.2"),
        (
            mapped_status(
                nozzles=[{"nozzle_type": "HS00", "nozzle_diameter": "0.2"}],
                ams_extruder_map={"0": 0},
            ),
            "0.2",
        ),
    ],
)
def test_otherwise_the_slicer_is_left_to_choose(status: PrinterStatus | None, size: str) -> None:
    assert slicer_nozzle_stats(status, size) is None
