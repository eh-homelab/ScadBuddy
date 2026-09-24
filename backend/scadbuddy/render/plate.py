"""Where an object and its prime tower go on a given printer's plate.

Bambu Studio decides two things from geometry ScadBuddy has to get right before
it hands a 3MF over: whether every extruder can reach the object, and whether
every extruder can reach the prime tower. On the H2 series those are not the
same rectangle — ``extruder_printable_area`` gives extruder 2 a 25 mm dead zone
on the left — and a path in an extruder's dead zone is what
``GCodeProcessor::check_gcode_paths`` flags as ``error_code = 1``, which the CLI
reports as *"Found G-code in unprintable area of multi-extruder printers after
slicing."* (see #105).

Every number here comes from Bambu Studio's own profiles:

* the plate polygons are generated into :mod:`scadbuddy.render.plate_profiles`
  from ``resources/profiles/BBL/machine/*.json``;
* :data:`PRIME_TOWER_SIDE` is the largest ``prime_tower_width`` in the BBL
  process profiles (60 mm, in 90 of the 91 of them; ``PrintConfig.cpp``'s own
  default is 35). The tower's *depth* is computed by the slicer from the flush
  volumes, so the reservation is square at the widest width rather than guessed;
* :data:`PRIME_TOWER_BRIM` is ``prime_tower_brim_width``'s 3 mm default, which
  sits outside the tower footprint.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from scadbuddy.render.plate_profiles import EXTRA_ALIASES, PLATE_PROFILES

if TYPE_CHECKING:
    import numpy as np

#: Widest ``prime_tower_width`` across BBL process profiles, and the side of the
#: square this module reserves for the tower.
PRIME_TOWER_SIDE = 60.0
#: ``prime_tower_brim_width``'s default, extending outside the tower footprint.
PRIME_TOWER_BRIM = 3.0
#: Gap left between the object and the tower.
TOWER_CLEARANCE = 5.0
#: Gap left between the tower (with its brim) and the edge of the reachable area.
EDGE_MARGIN = 2.0

#: A printer preset names its nozzle: "Bambu Lab H2C 0.4 nozzle".
_NOZZLE_SUFFIX = re.compile(r"\s+\d+(?:\.\d+)?\s+nozzle$", re.IGNORECASE)
_LAB_PREFIX = "bambu lab "


class PlateFitError(ValueError):
    """The object, or the object plus its prime tower, does not fit the plate."""


@dataclass(frozen=True)
class Rect:
    min_x: float
    min_y: float
    max_x: float
    max_y: float

    @property
    def width(self) -> float:
        return self.max_x - self.min_x

    @property
    def depth(self) -> float:
        return self.max_y - self.min_y

    @property
    def centre(self) -> tuple[float, float]:
        return ((self.min_x + self.max_x) / 2, (self.min_y + self.max_y) / 2)

    def overlaps(self, other: Rect) -> bool:
        return (
            self.min_x < other.max_x
            and self.max_x > other.min_x
            and self.min_y < other.max_y
            and self.max_y > other.min_y
        )


@dataclass(frozen=True)
class PlateGeometry:
    """One printer model's plate, as the 3MF writer needs it."""

    #: The machine profile's ``printer_model``; ``None`` for the fallback plate.
    model: str | None
    #: The bed extent, which is what the build item is laid out against.
    size: tuple[float, float]
    #: Where *every* extruder can reach — the intersection of the per-extruder
    #: areas, or the whole bed on a single-extruder machine.
    usable: Rect
    #: Cutouts nothing may be printed in (the X1/P1 filament cutter corner).
    exclusions: tuple[Rect, ...]
    extruders: int


@dataclass(frozen=True)
class Placement:
    """What the 3MF writer emits: a build-item translation and a tower corner."""

    #: Translation applied to the assembly, in plate coordinates.
    offset: tuple[float, float, float]
    #: ``wipe_tower_x`` / ``wipe_tower_y`` — the tower's *front-left corner*, the
    #: way ``PrintConfig.cpp`` documents them. ``None`` when no tower is needed.
    tower: tuple[float, float] | None


def _bounding_rect(points: tuple[tuple[float, float], ...]) -> Rect:
    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    return Rect(min(xs), min(ys), max(xs), max(ys))


def _intersect(rects: list[Rect]) -> Rect:
    return Rect(
        max(rect.min_x for rect in rects),
        max(rect.min_y for rect in rects),
        min(rect.max_x for rect in rects),
        min(rect.max_y for rect in rects),
    )


def _geometry(model: str) -> PlateGeometry:
    printable, per_extruder, exclude, _height = PLATE_PROFILES[model]
    bed = _bounding_rect(printable)
    areas = [_bounding_rect(polygon) for polygon in per_extruder]
    return PlateGeometry(
        model=model,
        size=(bed.max_x, bed.max_y),
        usable=_intersect(areas) if areas else bed,
        exclusions=(_bounding_rect(exclude),) if exclude else (),
        extruders=max(len(per_extruder), 1),
    )


DEFAULT_PLATE_SIZE = (256.0, 256.0)
#: Used when the target printer is unknown — the size every 3MF was laid out on
#: before plate geometry followed the printer.
DEFAULT_PLATE = PlateGeometry(
    model=None,
    size=DEFAULT_PLATE_SIZE,
    usable=Rect(0.0, 0.0, *DEFAULT_PLATE_SIZE),
    exclusions=(),
    extruders=1,
)

_BY_MODEL: dict[str, PlateGeometry] = {model: _geometry(model) for model in PLATE_PROFILES}
_BY_ALIAS: dict[str, PlateGeometry] = {}
for _model, _plate in _BY_MODEL.items():
    _BY_ALIAS[_model.lower()] = _plate
    if _model.lower().startswith(_LAB_PREFIX):
        _BY_ALIAS[_model[len(_LAB_PREFIX) :].lower()] = _plate
for _code, _model in EXTRA_ALIASES.items():
    _BY_ALIAS[_code.lower()] = _BY_MODEL[_model]


def plate_for(model: str | None) -> PlateGeometry:
    """The plate for a Bambuddy printer ``model`` or a printer preset name.

    Accepts what either source of truth hands over: ``"H2C"`` (Bambuddy's
    ``Printer.model``), ``"Bambu Lab H2C"`` (the profile's ``printer_model``) or
    ``"Bambu Lab H2C 0.4 nozzle"`` (a printer preset). An unrecognised name is
    not an error — it falls back to :data:`DEFAULT_PLATE`, which is what a
    deployment with no Bambuddy connection gets.
    """
    if not model:
        return DEFAULT_PLATE
    key = _NOZZLE_SUFFIX.sub("", model.strip()).lower()
    return _BY_ALIAS.get(key, DEFAULT_PLATE)


def _tower_sides(area: Rect, tower: float) -> list[tuple[str, Rect, Rect]]:
    """``(name, strip, remainder)`` for each edge the tower could sit against."""
    band = tower + TOWER_CLEARANCE
    return [
        (
            "front",
            Rect(area.min_x, area.min_y, area.max_x, area.min_y + tower),
            Rect(area.min_x, area.min_y + band, area.max_x, area.max_y),
        ),
        (
            "back",
            Rect(area.min_x, area.max_y - tower, area.max_x, area.max_y),
            Rect(area.min_x, area.min_y, area.max_x, area.max_y - band),
        ),
        (
            "left",
            Rect(area.min_x, area.min_y, area.min_x + tower, area.max_y),
            Rect(area.min_x + band, area.min_y, area.max_x, area.max_y),
        ),
        (
            "right",
            Rect(area.max_x - tower, area.min_y, area.max_x, area.max_y),
            Rect(area.min_x, area.min_y, area.max_x - band, area.max_y),
        ),
    ]


def _tower_corner(
    side: str, strip: Rect, object_rect: Rect, plate: PlateGeometry, tower: float
) -> tuple[float, float] | None:
    """Centre the tower in ``strip`` on the object's cross axis, then check it."""
    if side in ("front", "back"):
        x = object_rect.centre[0] - tower / 2
        x = min(max(x, strip.min_x), strip.max_x - tower)
        y = strip.min_y
    else:
        x = strip.min_x
        y = object_rect.centre[1] - tower / 2
        y = min(max(y, strip.min_y), strip.max_y - tower)
    footprint = Rect(x, y, x + tower, y + tower)
    if footprint.overlaps(object_rect):
        return None
    if any(footprint.overlaps(cutout) for cutout in plate.exclusions):
        return None
    # ``wipe_tower_x``/``_y`` name the tower itself; the brim sits outside it.
    return (x + PRIME_TOWER_BRIM, y + PRIME_TOWER_BRIM)


def centre_on_plate(bounds: np.ndarray, plate: PlateGeometry) -> Placement:
    """Centre ``bounds`` on ``plate`` with no tower and no fit check.

    The provisional layout the render pipeline uses: no printer has been chosen
    yet, so a model that overflows the fallback plate may well fit the one it is
    eventually sent to, and refusing the render would be answering a question
    nobody asked. :func:`place_on_plate` does the real check at send time.
    """
    low, high = bounds[0], bounds[1]
    centre = plate.usable.centre
    return Placement(
        offset=(
            centre[0] - float(low[0] + high[0]) / 2,
            centre[1] - float(low[1] + high[1]) / 2,
            -float(low[2]),
        ),
        tower=None,
    )


def place_on_plate(bounds: np.ndarray, plate: PlateGeometry, *, tower: bool = True) -> Placement:
    """Centre ``bounds`` on ``plate`` and pick a prime-tower corner clear of it.

    ``bounds`` is a ``(2, 3)`` min/max array in the mesh's own coordinates. The
    object is centred on the area **every extruder can reach** rather than on the
    bed rectangle: on an H2C those centres are 10 mm apart in X, and the bed's is
    the wrong one because half the plate is out of extruder 2's range.

    When the object cannot be centred and still leave the tower room, it is
    shifted off centre — a strip is reserved against one edge and the object is
    centred in what is left. Only when no edge works is this a
    :class:`PlateFitError`, raised before anything is written or uploaded.
    """
    low, high = bounds[0], bounds[1]
    width, depth = float(high[0] - low[0]), float(high[1] - low[1])
    area = plate.usable

    if width > area.width or depth > area.depth:
        axis, want, have = (
            ("X", width, area.width) if width > area.width else ("Y", depth, area.depth)
        )
        raise PlateFitError(
            f"the model is {want:.1f} mm on {axis} but "
            f"{plate.model or 'the default plate'} only reaches {have:.1f} mm there"
        )

    def _offset(centre: tuple[float, float]) -> tuple[float, float, float]:
        return (
            centre[0] - float(low[0] + high[0]) / 2,
            centre[1] - float(low[1] + high[1]) / 2,
            -float(low[2]),
        )

    def _rect_at(centre: tuple[float, float]) -> Rect:
        return Rect(
            centre[0] - width / 2,
            centre[1] - depth / 2,
            centre[0] + width / 2,
            centre[1] + depth / 2,
        )

    if not tower:
        return Placement(offset=_offset(area.centre), tower=None)

    reserved = PRIME_TOWER_SIDE + 2 * PRIME_TOWER_BRIM
    inset = Rect(
        area.min_x + EDGE_MARGIN,
        area.min_y + EDGE_MARGIN,
        area.max_x - EDGE_MARGIN,
        area.max_y - EDGE_MARGIN,
    )

    # First choice: the object stays centred and the tower takes whichever edge
    # it fits against. Only if none does is the object moved.
    centred = _rect_at(area.centre)
    for side, strip, _remainder in _tower_sides(inset, reserved):
        corner = _tower_corner(side, strip, centred, plate, reserved)
        if corner is not None:
            return Placement(offset=_offset(area.centre), tower=corner)

    for side, strip, remainder in _tower_sides(inset, reserved):
        if width > remainder.width or depth > remainder.depth:
            continue
        moved = _rect_at(remainder.centre)
        corner = _tower_corner(side, strip, moved, plate, reserved)
        if corner is not None:
            return Placement(offset=_offset(remainder.centre), tower=corner)

    raise PlateFitError(
        f"the model is {width:.1f} x {depth:.1f} mm, which leaves no room for the "
        f"{PRIME_TOWER_SIDE:.0f} mm prime tower a multi-colour print needs on "
        f"{plate.model or 'the default plate'} "
        f"({area.width:.0f} x {area.depth:.0f} mm reachable by every extruder)"
    )
