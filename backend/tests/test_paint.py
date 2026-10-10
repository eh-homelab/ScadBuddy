from __future__ import annotations

import pytest

from scadbuddy.render.paint import PaintCodeError, remap, states

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
