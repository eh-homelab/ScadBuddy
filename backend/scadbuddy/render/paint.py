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

Only the leaves' states are read or rewritten here; the split, and so where on the
triangle each colour is, passes through untouched. A file is untrusted, so a code that
does not parse is :class:`PaintCodeError`, never a guess.
"""

from __future__ import annotations

from collections.abc import Mapping

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
    """Every leaf state ``code`` holds but 0, and its bits with each leaf's state
    renumbered by ``mapping`` (unrenumbered when None)."""
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
        if state:
            found.add(state)
            if mapping is not None:
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
    return _walk(code, None)[0]


def remap(code: str, mapping: Mapping[int, int]) -> str:
    """``code`` with each painted extruder ``n`` printed by ``mapping[n]`` instead (one
    it does not name kept), the split unchanged."""
    _, bits = _walk(code, mapping)
    digits = [
        "0123456789ABCDEF"[sum(bits[at + bit] << bit for bit in range(4))]
        for at in range(0, len(bits), 4)
    ]
    return "".join(reversed(digits))
