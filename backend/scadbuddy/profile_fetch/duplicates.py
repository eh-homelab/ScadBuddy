"""What the 3DFP create counts as a likely duplicate (#7025, R-7025-5).

The add form shows a list of filaments under "Avoid creating duplicates" before Save
Filament (measured by the script on #7025, 2026-11-22); a person read it before each save.
The browser service judges it with `likely_duplicates` and saves nothing when one is
likely. A filament of that list is a likely duplicate when it is filed under the same
brand, material and type (a field the card does not show does not clear it), and its name
or one of its colours is close to the ask's:

- names are close when, case, the site's weight suffix ("(250g)") and every character
  but letters and digits left out, one holds the other, or `difflib`'s ratio is at least
  `NAME_RATIO`;
- colours are close within CIEDE2000 `CLOSE_DELTA_E`, the storefront's colour distance
  (`orders/storefront/closest_colour.py`, a module of pure functions).

A list the browser read no card from is judged by its text: it stops when the text holds
the ask's colour name. An id in the ask's ``not_duplicates`` (a person judged it another
filament) is never a likely duplicate.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Final

from scadbuddy.orders.storefront.closest_colour import ciede2000, lab
from scadbuddy.profile_fetch.wire import _WEIGHT, Candidate, CreateAsk

#: Two colours this close (CIEDE2000) are close: a step a person barely tells apart.
CLOSE_DELTA_E: Final = 5.0
NAME_RATIO: Final = 0.85
#: The shortest name that counts as held inside another.
MIN_HELD: Final = 4


def _name(text: str) -> str:
    return re.sub(r"[^0-9a-z]", "", _WEIGHT.sub("", text).casefold())


def _agrees(asked: str, shown: str | None) -> bool:
    return shown is None or shown.strip().casefold() == asked.strip().casefold()


def _names_close(left: str, right: str) -> bool:
    first, second = _name(left), _name(right)
    if not first or not second:
        return False
    if first == second:
        return True
    shorter, longer = sorted((first, second), key=len)
    if len(shorter) >= MIN_HELD and shorter in longer:
        return True
    return SequenceMatcher(None, first, second).ratio() >= NAME_RATIO


def _lab(hex_colour: str) -> tuple[float, float, float]:
    value = hex_colour.lstrip("#")
    red, green, blue = (int(value[i : i + 2], 16) for i in (0, 2, 4))
    return lab(red, green, blue)


def _close_hex(asked: list[str], shown: list[str]) -> str | None:
    for mine in asked:
        for theirs in shown:
            if ciede2000(_lab(mine), _lab(theirs)) <= CLOSE_DELTA_E:
                return mine
    return None


def likely_duplicates(
    ask: CreateAsk, candidates: list[Candidate], text: str
) -> list[tuple[Candidate | None, str]]:
    """Each likely duplicate (the module docstring) with why; empty when none is."""
    found: list[tuple[Candidate | None, str]] = []
    for candidate in candidates:
        if candidate.tdfp_id in ask.not_duplicates:
            continue
        if not (
            _agrees(ask.brand, candidate.brand)
            and _agrees(ask.material, candidate.material)
            and _agrees(ask.material_type, candidate.material_type)
        ):
            continue
        if _names_close(ask.colour, candidate.name):
            found.append((candidate, f"a name close to {ask.colour!r}"))
            continue
        hex_colour = _close_hex(ask.hexes, candidate.hexes)
        if hex_colour is not None:
            found.append((candidate, f"a colour within ΔE {CLOSE_DELTA_E:g} of {hex_colour}"))
    if not candidates and _name(ask.colour) and _name(ask.colour) in _name(text):
        found.append((None, f"the duplicate list names {ask.colour!r}"))
    return found
