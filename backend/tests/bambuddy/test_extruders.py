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
    high_flow_warning,
    high_flow_warnings,
    rack_volume_type,
    side_of,
    slicer_nozzle_stats,
    slicer_volume_types,
    with_sides,
)
from scadbuddy.bambuddy.filaments import FilamentOptions, LoadedAt, SpoolOption
from scadbuddy.bambuddy.models import FlowType, NozzleChoice, PrinterStatus
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


def _choose(size: str, left: str = "standard", right: str | None = None) -> list[NozzleChoice]:
    """The dialog's nozzles: the left side first, then the right."""
    return [
        NozzleChoice.model_validate({"size": size, "flow": left}),
        NozzleChoice.model_validate({"size": size, "flow": right or left}),
    ]


# --- #484: the flow the slice is for, as Bambu Studio writes it -----------------------------


@pytest.mark.parametrize(
    ("nozzles", "expected"),
    [
        (STANDARD_04, ["Standard", "Standard"]),
        ([NozzleChoice(size="0.4", flow="high_flow")], ["High Flow", "High Flow"]),
        (_choose("0.4", "high_flow", "standard"), ["High Flow", "Standard"]),
        (_choose("0.4", "standard", "high_flow"), ["Standard", "High Flow"]),
    ],
)
def test_the_flow_is_stated_per_extruder_in_the_slicers_order(
    nozzles: list[NozzleChoice], expected: list[str]
) -> None:
    """``nozzle_volume_type`` in Bambu Studio's spelling, the slicer's first extruder (the
    left) first, as the dialog lists them; one choice is both sides'."""
    assert slicer_volume_types(nozzles) == expected


@pytest.mark.parametrize(
    ("nozzles", "expected"),
    [
        (STANDARD_04, "Standard"),
        ([NozzleChoice(size="0.4", flow="high_flow")], "High Flow"),
        (_choose("0.4", "high_flow", "standard"), "Standard"),
        (_choose("0.4", "standard", "high_flow"), "High Flow"),
    ],
)
def test_the_rack_side_is_sliced_for_the_rights_flow(
    nozzles: list[NozzleChoice], expected: str
) -> None:
    """The rack swaps onto the right (physical 0), the dialog's second side."""
    assert rack_volume_type(nozzles, laid_out=True) == expected


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


def _size(size: str) -> list[NozzleChoice]:
    return [NozzleChoice.model_validate({"size": size})]


def test_only_the_side_with_the_size_is_offered_to_the_slicer() -> None:
    """Queue item 159's printer: the right 0.2 HS00, the left 0.4 HH01."""
    assert slicer_nozzle_stats(fts_status(), _size("0.2")) == ONLY_RIGHT


def test_a_size_only_the_left_has_puts_everything_on_the_left() -> None:
    status = _nozzles(("HS01", "0.4"), ("HS00", "0.2"))
    assert slicer_nozzle_stats(status, _size("0.2")) == ONLY_LEFT


def test_the_rack_counts_for_the_right_side() -> None:
    """The rack swaps onto the right (it serves physical extruder 0), so a spare of the
    size there is the right side's even when another size is mounted."""
    status = _nozzles(("HS01", "0.4"), ("HS00", "0.2"))
    # Every recorded spare is a 0.4, so the right has a standard 0.4 and the left does
    # not: the left's 0.2 is not the size.
    assert slicer_nozzle_stats(status, _size("0.4")) == ONLY_RIGHT


def test_a_nozzle_of_the_chosen_flow_is_preferred() -> None:
    """Queue item 149 paused on a High Flow left sliced as Standard: with Standard chosen
    the standard side is offered, with High Flow chosen the High Flow side, so named."""
    status = _nozzles(("HH01", "0.2"), ("HS00", "0.2"))
    assert slicer_nozzle_stats(status, _size("0.2")) == ONLY_LEFT
    assert slicer_nozzle_stats(status, _choose("0.2", "high_flow")) == [
        "High Flow#0",
        "High Flow#1",
    ]


def test_each_side_is_named_for_its_own_chosen_flow() -> None:
    """Bambu Studio names each extruder's nozzles by flow (``High Flow#1``); a side is
    named for the flow chosen for it, whether or not it is the one offered."""
    status = _nozzles(("HH01", "0.2"), ("HS01", "0.6"))
    assert slicer_nozzle_stats(status, _choose("0.2", "standard", "high_flow")) == [
        "Standard#0",
        "High Flow#1",
    ]


@pytest.mark.parametrize(
    ("status", "nozzles", "expected"),
    [
        # A High Flow left and a standard right, each chosen as it is mounted.
        (
            _nozzles(("HS00", "0.2"), ("HH01", "0.2")),
            _choose("0.2", "high_flow", "standard"),
            ["High Flow#1", "Standard#0"],
        ),
        # The same the other way round: a High Flow right and a standard left.
        (
            _nozzles(("HH01", "0.2"), ("HS00", "0.2")),
            _choose("0.2", "standard", "high_flow"),
            ["Standard#0", "High Flow#1"],
        ),
    ],
)
def test_two_sides_of_their_chosen_flows_offer_the_high_flow_one(
    status: PrinterStatus, nozzles: list[NozzleChoice], expected: list[str]
) -> None:
    """Both sides can print as chosen, but left to choose the slicer put the print on
    the Standard right every time (live slices, 2026-10-06), so a High Flow choice
    changed nothing. The High Flow side is offered alone, as Studio's own stats steer."""
    assert slicer_nozzle_stats(status, nozzles) == expected


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (_nozzles(("HH01", "0.2"), ("HS01", "0.6")), ONLY_RIGHT),
        (_nozzles(("HS01", "0.6"), ("HH01", "0.2")), ONLY_LEFT),
    ],
)
def test_a_nozzle_of_the_other_flow_still_beats_another_size(
    status: PrinterStatus, expected: list[str]
) -> None:
    """Nothing is refused on the mounted nozzles (#768): the one side with the size is
    offered, named for the flow chosen, here Standard."""
    assert slicer_nozzle_stats(status, _size("0.2")) == expected


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
    assert slicer_nozzle_stats(status, _size(size)) is None


# --- #723, #797: a side the slice may use with the other flow mounted --------------------
#
# The slice states each side's flow (#484) and is offered a side with a nozzle of that
# flow when one side alone has it (#834), so a mounted nozzle of the size in the other
# flow pauses the print only where the slice may still use it.

HF_LEFT_SLICED_STANDARD = (
    "The left nozzle is High Flow and this print is sliced for a Standard nozzle there, so "
    'if it prints on the left, the printer pauses at the first layer ("the left nozzle is '
    'not matched with slicing file"). Fit a Standard nozzle there before it starts.'
)


@pytest.mark.parametrize(
    ("status", "warned"),
    [
        (_nozzles(("HH01", "0.2"), ("HS01", "0.6")), RIGHT),
        (_nozzles(("HS01", "0.6"), ("HH01", "0.2")), LEFT),
    ],
)
def test_the_one_side_offered_in_the_other_flow_is_warned_about(
    status: PrinterStatus, warned: int
) -> None:
    """Standard chosen, and the only nozzle of the size is High Flow: the slice is offered
    that side as Standard (above), queue item 149's pause. Warned, never refused."""
    assert high_flow_warnings(status, _size("0.2")) == [high_flow_warning(warned, "standard")]


def test_the_warning_says_which_side_and_both_flows() -> None:
    [warning] = high_flow_warnings(_nozzles(("HS01", "0.6"), ("HH01", "0.2")), _size("0.2"))
    assert warning.kind == "hf-mounted"
    assert warning.message == HF_LEFT_SLICED_STANDARD


def test_either_side_the_slicer_may_choose_is_warned_about() -> None:
    """High Flow chosen for the left and Standard for the right, with the 0.2s mounted
    the other way round: neither side has its flow, so the slicer is left to choose, and
    each side is warned of in its own flow."""
    status = _nozzles(("HH01", "0.2"), ("HS00", "0.2"))
    nozzles = _choose("0.2", "high_flow", "standard")
    assert slicer_nozzle_stats(status, nozzles) is None
    assert high_flow_warnings(status, nozzles) == [
        high_flow_warning(RIGHT, "standard"),
        high_flow_warning(LEFT, "high_flow"),
    ]
    assert high_flow_warning(LEFT, "high_flow").message.startswith(
        "The left nozzle is Standard and this print is sliced for a High Flow nozzle there"
    )


@pytest.mark.parametrize(
    ("status", "nozzles"),
    [
        # Queue item 149's printer, a standard right and a High Flow left: the right.
        (_nozzles(("HS01", "0.4"), ("HH01", "0.4")), STANDARD_04),
        # The same the other way round, with no spare of the size: the left.
        (_nozzles(("HS00", "0.2"), ("HH01", "0.2")), _choose("0.2", "high_flow")),
    ],
)
def test_a_side_the_slice_is_not_offered_is_not_warned_about(
    status: PrinterStatus, nozzles: list[NozzleChoice]
) -> None:
    """The one side with the flow chosen is offered (#834), so the other flow mounted on
    the other side is never printed with."""
    assert high_flow_warnings(status, nozzles) == []


@pytest.mark.parametrize(
    ("mounted", "nozzles", "flow"),
    [
        # #1238: a High Flow 0.4 on both sides and standard spares, Standard chosen: the
        # right is offered for its spares.
        ((("HH01", "0.4"), ("HH01", "0.4")), STANDARD_04, "standard"),
        # Queue item 149's printer with High Flow chosen: the left has it and the right
        # only among its spares, so either may print.
        ((("HS01", "0.4"), ("HH01", "0.4")), _choose("0.4", "high_flow"), "high_flow"),
    ],
)
def test_the_rack_side_is_not_warned_about_once_the_rack_picks_it_a_hotend(
    mounted: tuple[tuple[str, str], tuple[str, str]], nozzles: list[NozzleChoice], flow: FlowType
) -> None:
    """The right's mounted nozzle is of the other flow until a rack pick swaps on a spare
    of the flow sliced there (#1238)."""
    status = _nozzles(*mounted)
    assert high_flow_warnings(status, nozzles) == [high_flow_warning(RIGHT, flow)]
    assert high_flow_warnings(status, nozzles, rack_picked=True) == []


def test_a_library_file_may_print_on_either_side() -> None:
    """#313: a library file prints as its author left it, so it is offered no side and
    queue item 149's High Flow left is warned of, as before #484."""
    status = _nozzles(("HS01", "0.4"), ("HH01", "0.4"))
    assert high_flow_warnings(status, STANDARD_04, laid_out=False) == [
        high_flow_warning(LEFT, "standard")
    ]


def test_a_library_file_is_judged_as_standard_whatever_flow_is_chosen() -> None:
    """A library file states no flow unless its author saved one, so it slices Standard
    (#313): High Flow chosen does not make the High Flow left match, and the rack side is
    judged Standard too."""
    status = _nozzles(("HS01", "0.4"), ("HH01", "0.4"))
    high_flow = _choose("0.4", "high_flow")
    assert high_flow_warnings(status, high_flow, laid_out=False) == [
        high_flow_warning(LEFT, "standard")
    ]
    assert rack_volume_type(high_flow, laid_out=False) == "Standard"


def test_no_warning_without_a_mounted_nozzle_of_the_size_or_a_status() -> None:
    assert high_flow_warnings(_nozzles(("HS01", "0.4"), ("HH01", "0.4")), _size("0.2")) == []
    assert high_flow_warnings(None, STANDARD_04) == []


def test_a_nozzle_with_no_type_code_is_no_flow_to_warn_of() -> None:
    """The offered right reports its size but no type, so nothing says it is Standard."""
    status = _nozzles(("", "0.2"), ("HS01", "0.6"))
    assert slicer_nozzle_stats(status, _choose("0.2", "high_flow")) == [
        "High Flow#0",
        "High Flow#1",
    ]
    assert high_flow_warnings(status, _choose("0.2", "high_flow")) == []
