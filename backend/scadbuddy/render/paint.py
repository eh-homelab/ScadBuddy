"""Bambu Studio's per-triangle colour painting (``paint_color``), read and renumbered
(#1965).

A painted triangle carries a hex string: the tree its painting split it into, each leaf
the extruder that prints that piece (0: the part's own). The encoding is Bambu Studio's
`TriangleSelector::serialize`
(https://github.com/bambulab/BambuStudio/blob/v02.08.04.61/src/libslic3r/TriangleSelector.cpp,
``serialize`` and ``deserialize``), PrusaSlicer's
(https://github.com/prusa3d/PrusaSlicer/blob/version_2.8.1/src/libslic3r/TriangleSelector.cpp)
extended past state 17: a stream of bits, pre-order, each node two bits of how many
sides it splits, then two of its special side and its children (last first) when split,
or two bits of its state when a leaf. A state of 3 or more is ``11`` and then four-bit
groups of the state less 3: ``1111`` for each 15 of it, then the rest. Every four bits
are one hex digit, least significant bit first, and the string holds the digits last
first.

`remap` and `states` read or rewrite only the leaves' states; the split, and so where on
the triangle each colour is, passes through untouched. `pieces` follows the split too,
for a preview that shows where each colour prints (#3956). A file is untrusted, so a code that
does not parse is :class:`PaintCodeError`, never a guess.
"""

from __future__ import annotations

from collections.abc import Mapping

import numpy as np

#: The longest code read, in hex digits. A triangle's code grows with how finely its
#: painting split it: the longest in a real two-colour Bambu Studio project (Bambuddy
#: library file 688, 13,873 painted faces) is 84. Bounded because the archive's size cap
#: bounds a code's bytes, not what decoding it costs: each digit becomes four bits here.
MAX_CODE_DIGITS = 4096
#: The highest extruder a code is read or written with: far past any printer's filament
#: count, and it bounds how long a hostile leaf's run of ``1111`` groups may be.
MAX_STATE = 255


class PaintCodeError(ValueError):
    """A ``paint_color`` that is not a painting this module can read."""


def _bits(code: str) -> list[int]:
    if not code:
        raise PaintCodeError("an empty paint code")
    if len(code) > MAX_CODE_DIGITS:
        raise PaintCodeError(f"a paint code is longer than {MAX_CODE_DIGITS} digits")
    bits: list[int] = []
    for digit in reversed(code):
        try:
            nibble = int(digit, 16)
        except ValueError:
            raise PaintCodeError(f"paint code {code!r} is not hex") from None
        bits += [(nibble >> bit) & 1 for bit in range(4)]
    return bits


def _walk(code: str, mapping: Mapping[int, int] | None) -> tuple[set[int], list[int]]:
    """Every leaf state ``code`` holds, 0 (the part's own) included, and its bits with
    each painted leaf's state renumbered by ``mapping`` (unrenumbered when None)."""
    bits = _bits(code)
    out: list[int] = []
    found: set[int] = set()
    at = 0

    def take(count: int) -> int:
        nonlocal at
        if at + count > len(bits):
            raise PaintCodeError(f"paint code {code!r} ends inside a triangle")
        value = sum(bits[at + bit] << bit for bit in range(count))
        at += count
        return value

    def put(value: int, count: int) -> None:
        out.extend((value >> bit) & 1 for bit in range(count))

    # Nodes still to read: a split node is replaced by its children, so it adds their
    # number less itself. No recursion, so a deep tree costs no stack.
    pending = 1
    while pending:
        sides = take(2)
        put(sides, 2)
        if sides:
            put(take(2), 2)
            pending += sides
            continue
        state = take(2)
        if state == 3:
            while (group := take(4)) == 0b1111:
                state += 15
                if state > MAX_STATE:
                    raise PaintCodeError(f"paint code {code!r} names an extruder past {MAX_STATE}")
            state += group
        found.add(state)
        if state and mapping is not None:
            state = mapping.get(state, state)
        if not 0 <= state <= MAX_STATE:
            raise PaintCodeError(f"extruder {state} is past what a paint code can name")
        if state >= 3:
            put(3, 2)
            rest = state - 3
            while rest >= 15:
                put(0b1111, 4)
                rest -= 15
            put(rest, 4)
        else:
            put(state, 2)
        pending -= 1
    if any(bits[at:]):
        raise PaintCodeError(f"paint code {code!r} runs on past its triangle")
    return found, out


def states(code: str) -> set[int]:
    """The extruders ``code`` paints with, 0 (the part's own) left out."""
    return _walk(code, None)[0] - {0}


def uniform_state(code: str) -> int | None:
    """The one extruder every piece of ``code``'s triangle prints with (0: the part's
    own, which ``""`` is), or None when its painting splits it between two (#2146,
    spec 2026-10-11 §7: a face that is not its source's verbatim takes only a whole
    colour)."""
    if not code:
        return 0
    found = _walk(code, None)[0]
    return next(iter(found)) if len(found) == 1 else None


def single_code(state: int) -> str:
    """The code of a triangle painted whole with extruder ``state``, unsplit: ``""`` for
    0, as Bambu Studio leaves an unpainted triangle. The bits are `_walk`'s for one
    leaf: two of no split, then the state's."""
    if not 0 <= state <= MAX_STATE:
        raise PaintCodeError(f"extruder {state} is past what a paint code can name")
    if state == 0:
        return ""
    bits = [0, 0]
    if state >= 3:
        bits += [1, 1]
        rest = state - 3
        while rest >= 15:
            bits += [1, 1, 1, 1]
            rest -= 15
        bits += [(rest >> bit) & 1 for bit in range(4)]
    else:
        bits += [(state >> bit) & 1 for bit in range(2)]
    bits += [0] * (-len(bits) % 4)
    digits = [
        "0123456789ABCDEF"[sum(bits[at + bit] << bit for bit in range(4))]
        for at in range(0, len(bits), 4)
    ]
    return "".join(reversed(digits))


def remap(code: str, mapping: Mapping[int, int]) -> str:
    """``code`` with each painted extruder ``n`` printed by ``mapping[n]`` instead (one
    it does not name kept), the split unchanged."""
    _, bits = _walk(code, mapping)
    digits = [
        "0123456789ABCDEF"[sum(bits[at + bit] << bit for bit in range(4))]
        for at in range(0, len(bits), 4)
    ]
    return "".join(reversed(digits))


def _children(corners: np.ndarray, sides: int, special: int) -> list[np.ndarray]:
    """The triangles Bambu Studio splits ``corners`` into (`TriangleSelector::perform_split`):
    its corners from the special side's on, each split side's midpoint inserted, in its
    children's order."""
    v0, v1, v2 = (corners[(special + offset) % 3] for offset in range(3))
    if sides == 1:
        m12 = (v1 + v2) / 2
        return [np.array([v0, v1, m12]), np.array([m12, v2, v0])]
    if sides == 2:
        m01, m02 = (v0 + v1) / 2, (v0 + v2) / 2
        return [np.array([v0, m01, m02]), np.array([m01, v1, m02]), np.array([v1, v2, m02])]
    m01, m12, m20 = (v0 + v1) / 2, (v1 + v2) / 2, (v2 + v0) / 2
    return [
        np.array([v0, m01, m20]),
        np.array([m01, v1, m12]),
        np.array([m12, v2, m20]),
        np.array([m01, m12, m20]),
    ]


def pieces(code: str, corners: np.ndarray) -> list[tuple[np.ndarray, int]]:
    """Each leaf of ``code``'s painting of the triangle ``corners`` (3x3) as its own
    triangle and the extruder that prints it (0: the part's own), where Bambu Studio puts
    it: a split node's children are its sides' midpoints' triangles, read last first."""
    if not code:
        return [(corners, 0)]
    bits = _bits(code)
    at = 0

    def take(count: int) -> int:
        nonlocal at
        if at + count > len(bits):
            raise PaintCodeError(f"paint code {code!r} ends inside a triangle")
        value = sum(bits[at + bit] << bit for bit in range(count))
        at += count
        return value

    found: list[tuple[np.ndarray, int]] = []
    # The triangles still to read, the next on top: a split one's children are pushed
    # first to last, so its last is read first, as the code holds them.
    pending = [corners]
    while pending:
        triangle = pending.pop()
        sides = take(2)
        if sides:
            pending.extend(_children(triangle, sides, take(2)))
            continue
        state = take(2)
        if state == 3:
            while (group := take(4)) == 0b1111:
                state += 15
                if state > MAX_STATE:
                    raise PaintCodeError(f"paint code {code!r} names an extruder past {MAX_STATE}")
            state += group
        found.append((triangle, state))
    if any(bits[at:]):
        raise PaintCodeError(f"paint code {code!r} runs on past its triangle")
    return found
