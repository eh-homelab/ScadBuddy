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
    #: The profile's ``printable_height``. Required, not defaulted: the 3MF
    #: writer states it in ``project_settings.config``, and this whole module
    #: exists because a wrong value in that file is not rejected — it is either
    #: silently honoured or, in #110's case, a segfault. A default would let a
    #: future call site write ``"printable_height": "0"`` and find out later.
    height: float

    @property
    def key(self) -> str:
        """Stable identity for "was the sent file laid out for this plate?"."""
        return self.model or "default"


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
    printable, per_extruder, exclude, height = PLATE_PROFILES[model]
    bed = _bounding_rect(printable)
    areas = [_bounding_rect(polygon) for polygon in per_extruder]
    return PlateGeometry(
        model=model,
        size=(bed.max_x, bed.max_y),
        usable=_intersect(areas) if areas else bed,
        exclusions=(_bounding_rect(exclude),) if exclude else (),
        extruders=max(len(per_extruder), 1),
        height=height,
    )


DEFAULT_PLATE_SIZE = (256.0, 256.0)
#: Z for the fallback plate. Nothing reads it as a constraint — it is stated in
#: the 3MF because the loader requires the key to exist (#110).
DEFAULT_PLATE_HEIGHT = 250.0
#: Used when the target printer is unknown — the size every 3MF was laid out on
#: before plate geometry followed the printer.
DEFAULT_PLATE = PlateGeometry(
    model=None,
    size=DEFAULT_PLATE_SIZE,
    usable=Rect(0.0, 0.0, *DEFAULT_PLATE_SIZE),
    exclusions=(),
    extruders=1,
    height=DEFAULT_PLATE_HEIGHT,
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


def _clear_of_exclusions(
    centre: tuple[float, float],
    width: float,
    depth: float,
    plate: PlateGeometry,
    region: Rect | None = None,
) -> tuple[float, float] | None:
    """``centre`` moved just far enough that the object misses every cutout.

    The cutouts are the X1/P1 filament-cutter corner. They were screened against
    the *tower* from the start and never against the object itself, so a large
    single-colour model on an X1 sat across the cutter with nothing complaining —
    the same silently-unprintable outcome this module exists to prevent, one bed
    over. A cutout is always a bed corner, so clearing it means pushing away from
    that corner along whichever axis costs less; ``None`` means the object cannot
    clear it and still fit.

    ``region`` is where the object has to end up — the whole reachable area when
    it keeps the centre, or the strip left over once the tower has reserved one.
    Bounding the push by the reachable area in the latter case would let it walk
    into the tower's strip; ``_tower_corner`` then rejects that side and the
    search moves on, so the outcome is safe but needlessly conservative. No
    current profile has both a multi-extruder dead zone and a cutter cutout, so
    this is reachable only by a future one — which is exactly when a bound that
    quietly refuses a placement that fits would be hardest to spot.
    """
    area = region if region is not None else plate.usable
    for _ in range(len(plate.exclusions) + 1):
        rect = Rect(
            centre[0] - width / 2,
            centre[1] - depth / 2,
            centre[0] + width / 2,
            centre[1] + depth / 2,
        )
        hit = next((cut for cut in plate.exclusions if rect.overlaps(cut)), None)
        if hit is None:
            return centre
        # Push out of the cutout the cheap way, and only away from the bed edge
        # it hugs: a cutout touching min_x can only be escaped towards max_x.
        dx = (hit.max_x - rect.min_x) if hit.min_x <= area.min_x else -(rect.max_x - hit.min_x)
        dy = (hit.max_y - rect.min_y) if hit.min_y <= area.min_y else -(rect.max_y - hit.min_y)
        centre = (centre[0] + dx, centre[1]) if abs(dx) <= abs(dy) else (centre[0], centre[1] + dy)
        if (
            centre[0] - width / 2 < area.min_x
            or centre[0] + width / 2 > area.max_x
            or centre[1] - depth / 2 < area.min_y
            or centre[1] + depth / 2 > area.max_y
        ):
            return None
    return None


def _tower_sides(inset: Rect, area: Rect, tower: float) -> list[tuple[str, Rect, Rect]]:
    """``(name, strip, remainder)`` for each edge the tower could sit against.

    Two rectangles, deliberately not the same one. The ``strip`` the tower takes
    comes from ``inset`` — :data:`EDGE_MARGIN` keeps the tower and its brim off
    the edge of the reachable area. The ``remainder`` the object is moved into
    comes from ``area``, because that margin exists for the tower and there is no
    reason to charge the object for it: shrinking the object's bound on the axis
    the tower does not even compete for cost 2 x ``EDGE_MARGIN`` of usable size
    and refused models that fit. A 298 x 200 mm two-colour model on an H2C
    (300 mm of reachable width) was rejected with "no room for the prime tower"
    while a valid placement existed — object at (175, 195.5), tower at (145, 5).

    Only the axis the tower eats into is narrowed, and by ``band``: the tower
    plus the clearance the object has to keep from it.
    """
    band = tower + TOWER_CLEARANCE
    return [
        (
            "front",
            Rect(inset.min_x, inset.min_y, inset.max_x, inset.min_y + tower),
            Rect(area.min_x, inset.min_y + band, area.max_x, area.max_y),
        ),
        (
            "back",
            Rect(inset.min_x, inset.max_y - tower, inset.max_x, inset.max_y),
            Rect(area.min_x, area.min_y, area.max_x, inset.max_y - band),
        ),
        (
            "left",
            Rect(inset.min_x, inset.min_y, inset.min_x + tower, inset.max_y),
            Rect(inset.min_x + band, area.min_y, area.max_x, area.max_y),
        ),
        (
            "right",
            Rect(inset.max_x - tower, inset.min_y, inset.max_x, inset.max_y),
            Rect(area.min_x, area.min_y, inset.max_x - band, area.max_y),
        ),
    ]


def _tower_corner(
    side: str, strip: Rect, object_rect: Rect, plate: PlateGeometry, tower: float
) -> tuple[float, float] | None:
    """Centre the tower in ``strip`` on the object's cross axis, then check it.

    ``object_rect`` is grown by :data:`TOWER_CLEARANCE` before the overlap test, so
    the gap the constant promises holds on this path too and not only where a strip
    was reserved. Without that a tower could end up flush against the object — 2 mm
    away for a 280x180 model on an H2C — while every assertion still passed.
    """
    if side in ("front", "back"):
        x = object_rect.centre[0] - tower / 2
        x = min(max(x, strip.min_x), strip.max_x - tower)
        y = strip.min_y
    else:
        x = strip.min_x
        y = object_rect.centre[1] - tower / 2
        y = min(max(y, strip.min_y), strip.max_y - tower)
    footprint = Rect(x, y, x + tower, y + tower)
    keep_clear = Rect(
        object_rect.min_x - TOWER_CLEARANCE,
        object_rect.min_y - TOWER_CLEARANCE,
        object_rect.max_x + TOWER_CLEARANCE,
        object_rect.max_y + TOWER_CLEARANCE,
    )
    if footprint.overlaps(keep_clear):
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
        centre = _clear_of_exclusions(area.centre, width, depth, plate)
        if centre is None:
            raise PlateFitError(
                f"the model is {width:.1f} x {depth:.1f} mm, which cannot avoid the "
                f"area {plate.model or 'the default plate'} cannot print in "
                f"(the filament cutter) and still fit its "
                f"{area.width:.0f} x {area.depth:.0f} mm bed"
            )
        return Placement(offset=_offset(centre), tower=None)

    reserved = PRIME_TOWER_SIDE + 2 * PRIME_TOWER_BRIM
    inset = Rect(
        area.min_x + EDGE_MARGIN,
        area.min_y + EDGE_MARGIN,
        area.max_x - EDGE_MARGIN,
        area.max_y - EDGE_MARGIN,
    )

    # First choice: the object stays centred and the tower takes whichever edge
    # it fits against. Only if none does is the object moved.
    object_centre = _clear_of_exclusions(area.centre, width, depth, plate)
    if object_centre is not None:
        centred = _rect_at(object_centre)
        for side, strip, _remainder in _tower_sides(inset, area, reserved):
            corner = _tower_corner(side, strip, centred, plate, reserved)
            if corner is not None:
                return Placement(offset=_offset(object_centre), tower=corner)

    for side, strip, remainder in _tower_sides(inset, area, reserved):
        if width > remainder.width or depth > remainder.depth:
            continue
        moved_centre = _clear_of_exclusions(remainder.centre, width, depth, plate, region=remainder)
        if moved_centre is None:
            continue
        moved = _rect_at(moved_centre)
        corner = _tower_corner(side, strip, moved, plate, reserved)
        if corner is not None:
            return Placement(offset=_offset(moved_centre), tower=corner)

    reach = f"reachable by all {plate.extruders} extruders" if plate.extruders > 1 else "printable"
    raise PlateFitError(
        f"the model is {width:.1f} x {depth:.1f} mm, which leaves no room for the "
        f"{PRIME_TOWER_SIDE:.0f} mm prime tower a multi-colour print needs on "
        f"{plate.model or 'the default plate'} "
        f"({area.width:.0f} x {area.depth:.0f} mm {reach})"
    )
