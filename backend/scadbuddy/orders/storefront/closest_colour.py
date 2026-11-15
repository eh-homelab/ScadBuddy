"""The closest colour (#6258): the colour a part moves to when the material it prints in
does not offer its colour. The page has a twin of this function; the shared vectors in
``test-fixtures/storefront/closest-colour.json`` pin both. Change them together.

The rule, in order:

1. The first candidate (in candidate order) with the same colour name (casefolded, never
   a missing name) or the same colour (`rgba`: case aside, ``#RRGGBB`` is
   ``#RRGGBBFF``).
2. Else the candidate at the least distance. The distance is
   ``sqrt(dE00**2 + (ALPHA_WEIGHT * dA)**2)``: dE00 is CIEDE2000 between the two
   colours in CIELAB (sRGB, D65), and dA is the difference of their alphas, each 0 to 1.
   So a clear filament (alpha 0) of the same RGB as an opaque white is 50 from it, as
   far as a mid grey is from white, and an opaque light grey is nearer.
3. A tie goes to the first candidate. A candidate hex that is not a colour is never
   chosen by distance.

A swatch's alpha is its ``alpha`` field when it is set (a filament's ``Filament.alpha``,
0 to 255, #6258), else the hex's ``AA`` byte, else opaque (`rgba`).
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

#: dE00 per unit of alpha difference: fully clear against fully opaque counts as 50,
#: half the distance from black to white.
ALPHA_WEIGHT: Final = 50.0

_HEX: Final = re.compile(r"#?([0-9a-fA-F]{6})([0-9a-fA-F]{2})?")
#: The D65 white point (CIE 1931 2°), X and Z for Y = 1.
_WHITE: Final = (0.95047, 1.0, 1.08883)


@dataclass(frozen=True)
class Swatch:
    """A colour to compare: its hex (``#RRGGBB`` or ``#RRGGBBAA``), its name, if any, and
    its alpha (0 to 255), if it has one apart from the hex (#6258: a filament's
    ``alpha``)."""

    hex: str
    name: str | None
    alpha: int | None = None


def parse_hex(text: str) -> tuple[int, int, int, int] | None:
    """``text`` as red, green, blue and alpha (0 to 255), or None when it is not a hex.
    No alpha is opaque (255)."""
    match = _HEX.fullmatch(text.strip())
    if match is None:
        return None
    rgb, alpha = match.groups()
    return (
        int(rgb[0:2], 16),
        int(rgb[2:4], 16),
        int(rgb[4:6], 16),
        int(alpha, 16) if alpha is not None else 255,
    )


def rgba(swatch: Swatch) -> tuple[int, int, int, int] | None:
    """``swatch`` as red, green, blue and alpha, or None when its hex is not one. The
    alpha is the swatch's ``alpha`` when it is set, else the hex's (#6258)."""
    parsed = parse_hex(swatch.hex)
    if parsed is None or swatch.alpha is None:
        return parsed
    return (*parsed[:3], swatch.alpha)


def _linear(channel: int) -> float:
    value = channel / 255
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def lab(red: int, green: int, blue: int) -> tuple[float, float, float]:
    """An sRGB colour (0 to 255 each) in CIELAB, under D65."""
    r, g, b = _linear(red), _linear(green), _linear(blue)
    x = 0.4124564 * r + 0.3575761 * g + 0.1804375 * b
    y = 0.2126729 * r + 0.7151522 * g + 0.0721750 * b
    z = 0.0193339 * r + 0.1191920 * g + 0.9503041 * b

    def f(t: float) -> float:
        return t ** (1 / 3) if t > (6 / 29) ** 3 else t / (3 * (6 / 29) ** 2) + 4 / 29

    fx, fy, fz = f(x / _WHITE[0]), f(y / _WHITE[1]), f(z / _WHITE[2])
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def ciede2000(first: tuple[float, float, float], second: tuple[float, float, float]) -> float:
    """CIEDE2000 between two CIELAB colours, kL = kC = kH = 1 (Sharma, Wu and Dalal,
    2005)."""
    l1, a1, b1 = first
    l2, a2, b2 = second
    c_bar = (math.hypot(a1, b1) + math.hypot(a2, b2)) / 2
    g = 0.5 * (1 - math.sqrt(c_bar**7 / (c_bar**7 + 25.0**7)))
    a1p, a2p = (1 + g) * a1, (1 + g) * a2
    c1p, c2p = math.hypot(a1p, b1), math.hypot(a2p, b2)
    h1p = math.degrees(math.atan2(b1, a1p)) % 360 if c1p else 0.0
    h2p = math.degrees(math.atan2(b2, a2p)) % 360 if c2p else 0.0
    dlp = l2 - l1
    dcp = c2p - c1p
    if c1p * c2p == 0:
        dhp = 0.0
    elif abs(h2p - h1p) <= 180:
        dhp = h2p - h1p
    elif h2p - h1p > 180:
        dhp = h2p - h1p - 360
    else:
        dhp = h2p - h1p + 360
    d_hp = 2 * math.sqrt(c1p * c2p) * math.sin(math.radians(dhp / 2))
    lp_bar = (l1 + l2) / 2
    cp_bar = (c1p + c2p) / 2
    if c1p * c2p == 0:
        hp_bar = h1p + h2p
    elif abs(h1p - h2p) <= 180:
        hp_bar = (h1p + h2p) / 2
    elif h1p + h2p < 360:
        hp_bar = (h1p + h2p + 360) / 2
    else:
        hp_bar = (h1p + h2p - 360) / 2
    t = (
        1
        - 0.17 * math.cos(math.radians(hp_bar - 30))
        + 0.24 * math.cos(math.radians(2 * hp_bar))
        + 0.32 * math.cos(math.radians(3 * hp_bar + 6))
        - 0.20 * math.cos(math.radians(4 * hp_bar - 63))
    )
    d_theta = 30 * math.exp(-(((hp_bar - 275) / 25) ** 2))
    r_c = 2 * math.sqrt(cp_bar**7 / (cp_bar**7 + 25.0**7))
    s_l = 1 + 0.015 * (lp_bar - 50) ** 2 / math.sqrt(20 + (lp_bar - 50) ** 2)
    s_c = 1 + 0.045 * cp_bar
    s_h = 1 + 0.015 * cp_bar * t
    r_t = -math.sin(math.radians(2 * d_theta)) * r_c
    return math.sqrt(
        (dlp / s_l) ** 2 + (dcp / s_c) ** 2 + (d_hp / s_h) ** 2 + r_t * (dcp / s_c) * (d_hp / s_h)
    )


def _same(target: Swatch, candidate: Swatch) -> bool:
    if (
        target.name is not None
        and candidate.name is not None
        and target.name.casefold() == candidate.name.casefold()
    ):
        return True
    parsed = rgba(target)
    return parsed is not None and parsed == rgba(candidate)


def distance(target: Swatch, candidate: Swatch) -> float:
    """The distance of the module's rule, step 2; infinite when either hex is not one."""
    first, second = rgba(target), rgba(candidate)
    if first is None or second is None:
        return math.inf
    d_e = ciede2000(lab(*first[:3]), lab(*second[:3]))
    d_a = ALPHA_WEIGHT * abs(first[3] - second[3]) / 255
    return math.hypot(d_e, d_a)


def closest_colour(target: Swatch, candidates: Sequence[Swatch]) -> int | None:
    """The index in ``candidates`` of the colour closest to ``target`` (the module's
    rule), or None when there are none. With a target hex that is not a colour and no
    name match, the first candidate."""
    if not candidates:
        return None
    for index, candidate in enumerate(candidates):
        if _same(target, candidate):
            return index
    best, best_distance = 0, math.inf
    for index, candidate in enumerate(candidates):
        each = distance(target, candidate)
        if each < best_distance:
            best, best_distance = index, each
    return best
