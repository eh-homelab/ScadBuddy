"""#2072 — the sliced plate's toolpath, read from its G-code."""

from __future__ import annotations

import json
import struct
from array import array

import pytest

from scadbuddy.bambuddy.toolpath import TRAVEL, Toolpath, parse

#: Bambu Studio's shape: machine start code (homing, a purge line), two layers, the end.
GCODE = b"""\
; HEADER_BLOCK_START
; total layer number: 2
; HEADER_BLOCK_END
G28
M83
G1 X18 Y1 Z0.8 F18000
G1 X200 Y1 E30 F1200 ; the purge line: not the print's
G1 X18 Y1 F18000
; CHANGE_LAYER
; Z_HEIGHT: 0.2
; LAYER_HEIGHT: 0.2
G1 Z0.2 F600
G0 X100 Y100 F30000
; FEATURE: Outer wall
G1 X110 Y100 E0.5 F3000
G1 X120 Y100 E0.5
G1 X120 Y110 E0.5
G1 E-0.8 F2400 ; a retraction draws nothing
; FEATURE: Sparse infill
G1 X100 Y110 E1 F12000
; CHANGE_LAYER
; Z_HEIGHT: 0.4
; LAYER_HEIGHT: 0.2
G1 Z0.4
; FEATURE: Outer wall
G3 X100 Y120 I0 J5 E1 F3000
; EXECUTABLE_BLOCK_END
G1 X0 Y250 F18000 ; parking: not the print's
"""


def segments(path: Toolpath) -> list[tuple[float, ...]]:
    raw = path.segments
    return [tuple(round(v, 3) for v in raw[i : i + 8]) for i in range(0, len(raw), 8)]


def test_only_the_print_is_read_by_layer_feature_and_speed() -> None:
    path = parse(GCODE)

    assert path.features == ["Outer wall", "Sparse infill"]
    assert [z for z, _ in path.layers] == [0.2, 0.4]
    drawn = segments(path)
    # The Z move to the first layer is a travel; so is the move to the wall.
    assert drawn[0][:6] == (18, 1, 0.8, 18, 1, 0.2)
    assert drawn[0][7] == TRAVEL
    assert drawn[1] == (18, 1, 0.2, 100, 100, 0.2, 500, TRAVEL)
    # Two collinear wall moves at one speed are one segment; the turn starts another.
    assert drawn[2] == (100, 100, 0.2, 120, 100, 0.2, 50, 1)
    assert drawn[3] == (120, 100, 0.2, 120, 110, 0.2, 50, 1)
    assert drawn[4] == (120, 110, 0.2, 100, 110, 0.2, 200, 2)
    # The purge line (x 200) and the parking move (y 250) are not the print's.
    assert all(max(s[0], s[3]) < 200 and max(s[1], s[4]) < 250 for s in drawn)
    assert path.max_speed == 200
    assert path.layers[1][1] == 5


def test_an_arc_is_drawn_as_chords_on_its_circle() -> None:
    path = parse(GCODE)
    arc = [s for s in segments(path)[path.layers[1][1] :] if s[7] == 1]

    # A half circle about (100, 115) from (100, 110) to (100, 120), counter-clockwise.
    assert len(arc) >= 12
    assert arc[0][:2] == (100, 110) and arc[-1][3:5] == (100, 120)
    for s in arc:
        assert abs(((s[3] - 100) ** 2 + (s[4] - 115) ** 2) ** 0.5 - 5) < 1e-3
    # Counter-clockwise from the bottom goes through x > 100.
    assert max(s[3] for s in arc) == pytest.approx(105, abs=0.01)


def test_past_the_bound_only_whole_layers_are_kept() -> None:
    path = parse(GCODE, max_segments=6)

    assert path.truncated
    assert [z for z, _ in path.layers] == [0.2]
    assert path.count == 5, "the second layer is left out whole"


def test_absolute_extrusion_draws_only_what_extrudes() -> None:
    path = parse(
        b"M82\n; CHANGE_LAYER\n; FEATURE: Inner wall\n"
        b"G1 X10 E1 F600\nG1 X20 E0.5\nG92 E0\nG1 X30 E0.2\n"
    )

    assert [s[7] for s in segments(path)] == [1, TRAVEL, 1]


def test_the_encoding_is_a_header_then_float32_records() -> None:
    path = parse(GCODE)
    data = path.encode()

    (size,) = struct.unpack_from("<I", data)
    assert size % 4 == 0
    header = json.loads(data[4 : 4 + size])
    assert header["segment_count"] == path.count
    assert header["features"] == ["Outer wall", "Sparse infill"]
    assert header["layers"][1] == {"z": 0.4, "first_segment": 5}
    records = array("f")
    records.frombytes(data[4 + size :])
    assert len(records) == path.count * 8
    assert records[16:24].tolist() == pytest.approx([100, 100, 0.2, 120, 100, 0.2, 50, 1])
