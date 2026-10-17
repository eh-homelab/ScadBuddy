from __future__ import annotations

import numpy as np
import pytest

from scadbuddy.render.paint import (
    PaintCodeError,
    pieces,
    remap,
    single_code,
    states,
    uniform_state,
)

# Codes for a whole triangle painted with extruders 1-4, as Bambu Studio's
# `TriangleSelector::serialize` (linked in the module) writes them.
WHOLE = {1: "4", 2: "8", 3: "0C", 4: "1C"}


def test_a_whole_triangle_reads_as_its_extruder() -> None:
    for extruder, code in WHOLE.items():
        assert states(code) == {extruder}


def test_a_split_triangle_reads_as_every_leaf() -> None:
    # One split side (bits 01), special side 0, then two leaves (children are written
    # last child first): extruder 2 then extruder 1. Nibbles in order 1, 8, 4.
    code = "481"
    assert states(code) == {1, 2}


def test_remap_rewrites_each_leaf_and_keeps_the_split() -> None:
    # The second leaf (extruder 1) becomes 3, which takes the 8-bit form "0C".
    assert remap("481", {1: 3, 2: 1}) == "0C41"
    assert states(remap("481", {1: 3, 2: 1})) == {1, 3}


def test_remap_round_trips_every_state() -> None:
    for state in range(1, 40):
        code = remap("4", {1: state})
        assert states(code) == {state}
        assert remap(code, {state: 1}) == "4"


def test_an_unpainted_leaf_stays_unpainted() -> None:
    assert remap("0", {1: 5}) == "0"
    assert states("0") == set()


@pytest.mark.parametrize("code", ["", "G", "1", "C"])
def test_a_malformed_code_is_refused(code: str) -> None:
    with pytest.raises(PaintCodeError):
        states(code)


def test_state_18_and_up_runs_on_in_groups_of_15() -> None:
    # 18 - 3 = 15: one ``1111`` group, then a group of 0 (Bambu Studio's form; a
    # PrusaSlicer-only reader would stop at the first group and read 18 as its last).
    assert remap("4", {1: 18}) == "0FC"
    assert states("0FC") == {18}
    assert states("1FC") == {19}


def test_a_state_past_the_cap_is_refused() -> None:
    with pytest.raises(PaintCodeError):
        remap("4", {1: 256})
    with pytest.raises(PaintCodeError):
        states("F" * 40 + "C")


def test_a_code_past_the_length_cap_is_refused_before_it_is_expanded() -> None:
    # One leaf state 1 ("4") padded with split nodes: a code a hostile file can make as
    # long as its archive allows. Refused by its length, not decoded into bits (#1965).
    from scadbuddy.render.paint import MAX_CODE_DIGITS

    long = "4" * (MAX_CODE_DIGITS + 1)
    with pytest.raises(PaintCodeError, match="longer"):
        states(long)
    with pytest.raises(PaintCodeError, match="longer"):
        remap(long, {1: 2})


# A real split code from Bambuddy library file 688: leaves of extruder 0 and 1.
MIXED = "00400644002044A3003003"
# Split in two (sides 01, special side 0), both halves extruder 2.
SPLIT_GREEN = "881"


def test_a_whole_triangle_has_one_state() -> None:
    for extruder, code in WHOLE.items():
        assert uniform_state(code) == extruder
    assert uniform_state("") == 0


def test_a_split_triangle_of_one_extruder_has_one_state() -> None:
    assert uniform_state(SPLIT_GREEN) == 2


def test_a_triangle_split_between_two_extruders_has_none() -> None:
    assert uniform_state("481") is None
    # The part's own colour (leaf 0) is a colour too: painted in part only.
    assert uniform_state(MIXED) is None
    assert states(MIXED) == {1}


def test_single_code_is_bambu_studios_whole_triangle() -> None:
    assert {s: single_code(s) for s in WHOLE} == WHOLE
    assert single_code(0) == ""


def test_single_code_round_trips_every_state() -> None:
    for state in range(0, 60):
        assert uniform_state(single_code(state)) == state
    assert remap(single_code(2), {2: 33}) == single_code(33)


def test_single_code_refuses_a_state_no_code_names() -> None:
    with pytest.raises(PaintCodeError):
        single_code(256)
    with pytest.raises(PaintCodeError):
        single_code(-1)


# #3956: where each leaf lies, as Bambu Studio's `TriangleSelector::perform_split` puts it.
V0, V1, V2 = np.array([0.0, 0, 0]), np.array([4.0, 0, 0]), np.array([0.0, 4, 0])
CORNERS = np.array([V0, V1, V2])


def _code(*nodes: tuple[int, int]) -> str:
    """A code from its nodes in stream order, each (sides, special side or state)."""
    bits = [(value >> bit) & 1 for node in nodes for value in node for bit in range(2)]
    bits += [0] * (-len(bits) % 4)
    nibbles = [sum(bits[at + bit] << bit for bit in range(4)) for at in range(0, len(bits), 4)]
    return "".join("0123456789ABCDEF"[n] for n in reversed(nibbles))


def _found(code: str, corners: np.ndarray = CORNERS) -> dict[tuple[tuple[float, ...], ...], int]:
    return {
        tuple(map(tuple, triangle.tolist())): state for triangle, state in pieces(code, corners)
    }


def _tri(*points: np.ndarray) -> tuple[tuple[float, ...], ...]:
    return tuple(tuple(p.tolist()) for p in points)


def _area(triangle: np.ndarray) -> float:
    return float(np.linalg.norm(np.cross(triangle[1] - triangle[0], triangle[2] - triangle[0])) / 2)


def test_an_unsplit_triangle_is_one_piece() -> None:
    assert _found("") == {_tri(V0, V1, V2): 0}
    assert _found(WHOLE[3]) == {_tri(V0, V1, V2): 3}


def test_one_split_side_halves_the_side_opposite_the_special_corner() -> None:
    # "481": special side 0, children last first: extruder 2, then 1 (as above).
    assert _code((1, 0), (0, 2), (0, 1)) == "481"
    m12 = (V1 + V2) / 2
    assert _found("481") == {_tri(V0, V1, m12): 1, _tri(m12, V2, V0): 2}


def test_the_special_side_turns_the_split() -> None:
    m20 = (V2 + V0) / 2  # special side 1: the corners from V1 on, so V2-V0 is halved
    assert _found(_code((1, 1), (0, 2), (0, 1))) == {_tri(V1, V2, m20): 1, _tri(m20, V0, V1): 2}


def test_two_split_sides_meet_at_the_special_corner() -> None:
    m01, m02 = (V0 + V1) / 2, (V0 + V2) / 2
    code = _code((2, 0), (0, 1), (0, 2), (0, 1))  # children 2, 1, 0
    assert _found(code) == {_tri(V0, m01, m02): 1, _tri(m01, V1, m02): 2, _tri(V1, V2, m02): 1}


def test_three_split_sides_leave_the_middle_last() -> None:
    m01, m12, m20 = (V0 + V1) / 2, (V1 + V2) / 2, (V2 + V0) / 2
    code = _code((3, 0), (0, 2), (0, 1), (0, 1), (0, 1))  # the middle (child 3) first
    assert _found(code) == {
        _tri(V0, m01, m20): 1,
        _tri(m01, V1, m12): 1,
        _tri(m12, V2, m20): 1,
        _tri(m01, m12, m20): 2,
    }


def test_nested_splits_cover_the_triangle_exactly() -> None:
    # A child split again: areas add up to the whole, whatever the depth.
    code = _code((3, 0), (1, 2), (0, 2), (0, 1), (0, 1), (0, 1), (0, 0))
    got = pieces(code, CORNERS)
    assert len(got) == 5  # three leaves, and a child halved
    assert sum(_area(t) for t, _ in got) == pytest.approx(_area(CORNERS))


@pytest.mark.parametrize("code", ["4", "481"])
def test_pieces_refuses_what_states_refuses(code: str) -> None:
    with pytest.raises(PaintCodeError):
        pieces(code + "0F", CORNERS)
