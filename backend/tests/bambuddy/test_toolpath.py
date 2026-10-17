"""#2072 — the sliced plate's toolpath, read from its G-code."""

from __future__ import annotations

import json
import struct
from array import array

import pytest

from scadbuddy.bambuddy import toolpath
from scadbuddy.bambuddy.toolpath import OTHER, TRAVEL, Toolpath, parse

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


#: The end of a Bambu Studio file as `GCode.cpp` writes it (L3383-L3428 at
#: bambulab/BambuStudio@79df7ae3): the custom role, the end-code marker, the filament and
#: machine end code, the M73 placeholder, then the executable block's end.
END_OF_FILE = b"""\
;FEATURE: Custom
; MACHINE_END_GCODE_START
; filament end gcode
G1 Z1.0 F900 ; lift
G1 X65 Y245 F12000 ; park
G1 Y265 F3000
M73 P100 R0
; EXECUTABLE_BLOCK_END
"""


def test_the_end_code_is_not_drawn() -> None:
    """#2885: Bambu Studio puts its machine end code (lift, park) before
    ``; EXECUTABLE_BLOCK_END``, so the print ends at ``; MACHINE_END_GCODE_START``."""
    body = GCODE.split(b"; EXECUTABLE_BLOCK_END")[0]
    path = parse(body + END_OF_FILE)

    assert segments(path) == segments(parse(GCODE))
    assert all(max(s[1], s[4]) < 245 for s in segments(path)), "the park move is not drawn"


def test_the_speed_scale_is_of_the_moves_drawn() -> None:
    """#2885: a cut layer's fastest move does not set the Speed view's scale."""
    # The cut layer's fast move is drawn first, then the bound is met on the next one.
    fast_last_layer = GCODE.replace(
        b"G3 X100 Y120 I0 J5 E1 F3000", b"G1 X100 Y130 E1 F60000\nG1 X90 Y130 E1 F3000"
    )
    assert parse(fast_last_layer).max_speed == 1000

    path = parse(fast_last_layer, max_segments=7)

    assert path.truncated
    assert path.max_speed == 200


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


def test_layers_and_features_are_bounded_whatever_the_file_says(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file of nothing but markers must not grow the header without bound."""
    monkeypatch.setattr(toolpath, "MAX_LAYERS", 3)
    monkeypatch.setattr(toolpath, "MAX_FEATURES", 2)
    names = b"".join(b"; FEATURE: f%d\nG1 X%d E1 F60\n" % (i, i + 1) for i in range(5))
    path = parse(b"; CHANGE_LAYER\n" * 10 + names + b"; FEATURE: " + b"x" * 500 + b"\n")

    assert path.truncated
    assert len(path.layers) == 3
    assert path.features == ["f0", "f1", OTHER]


def test_a_number_past_float_range_is_not_drawn() -> None:
    huge = b"9" * 400
    path = parse(
        b"; CHANGE_LAYER\n; Z_HEIGHT: " + huge + b"\n; FEATURE: w\n"
        b"G1 X1 E1 F" + huge + b"\nG1 X2 E1 F60\n"
    )

    assert path.layers == [(0.0, 0)]
    assert path.count == 1
    data = path.encode()
    (size,) = struct.unpack_from("<I", data)
    assert json.loads(data[4 : 4 + size])["max_speed"] == 1


def test_the_header_names_the_wire_format_version() -> None:
    """#3360: the frontend's `TOOLPATH_VERSION` refuses any other, so the header says it."""
    # The literal is the frontend's `TOOLPATH_VERSION`: the two move together, so a bump
    # here edits this test and `frontend/src/lib/toolpath.ts` in the same change.
    assert Toolpath().header()["version"] == toolpath.VERSION == 1
