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
* the tower's size is :func:`tower_footprint`: Bambu Studio's own estimate of it
  (``Print::wipe_tower_data``), from the filaments on the plate, the prime volume and
  the plate's height (#6735). With the rib wall (on by default; no H2C process preset
  turns it off) the tower is a square whose side grows with those; without it the tower is
  ``prime_tower_width`` wide (:data:`PRIME_TOWER_SIDE`, the largest in the BBL
  process profiles) and its depth grows with them;
* with ``wipe_tower_no_sparse_layers`` the objects keep the slicer's keep-out from the
  tower (:func:`tower_footprint`, ``TowerFootprint.keep_out``);
* the brim reserved round the tower is :func:`tower_brim`: the larger of
  ``prime_tower_brim_width``'s 3 mm default (:data:`PRIME_TOWER_BRIM`) and the brim
  Bambu Studio draws when that option is ``-1`` ("Auto"), which grows with the
  tower's height to 8 mm (:data:`PRIME_TOWER_AUTO_BRIM`, #5545).

The tower's brim is what the slicer's out-of-bed check catches (#5545). Bambu Studio
02.08.02.61 places a rib-wall tower (``prime_tower_rib_wall``, on by default) so that
its first-layer wall starts at ``wipe_tower_x``/``wipe_tower_y`` (``WipeTower.cpp``
``generate_support_wall_new`` sets ``m_rib_offset`` from that wall's bounding box), and
draws the brim outside the wall, ``int((brim + s/2) / s)`` loops ``s`` apart, about
0.38 mm for a 0.4 nozzle. With ``prime_tower_brim_width`` at ``-1`` the brim is
``WipeTower::get_auto_brim_by_height``: ``height / 100 * 8`` below 100 mm, 8 mm from
there up. ``GCodeProcessor::check_multi_extruder_gcode_valid`` then fails a plate with
any extrusion outside ``printable_area`` grown by 2 mm (error bit 4), which the CLI
reports as -104, "Found G-code outside of the printable area". Measured on 2026-11-07
with the deployed slicer image (``scadbuddy-slicer:sha-d54bcd4``), a 133 mm, 5-colour
H2C plate: the tower's lowest extrusion is 8.2 mm in front of ``wipe_tower_y``, so y 6
fails (an extrusion at y -2.205) and y 6.5 passes. The 3 mm reserved before put the
tower at y 5. A 220 mm, 8-colour plate with the tower at y 10 (this rule's place) sliced, its
lowest extrusion at y 1.809.

Two kinds of area are kept clear, and not in the same way (#122):

* ``bed_exclude_area`` (the X1/P1 filament cutter) is unprintable, always, so
  neither the object nor the tower may touch it — :attr:`PlateGeometry.exclusions`.
* ``wrapping_exclude_area`` (the H2/P2S clumping-detection strip) is unprintable
  only while the process option ``enable_wrapping_detection`` is on:
  ``layered_print_cleareance_valid`` in ``src/libslic3r/Print.cpp`` then fails
  validation with "Prime Tower is too close to clumping detection area" (and the
  equivalent for an object), ``get_bed_shape_with_excluded_area`` in
  ``PrintConfig.cpp`` cuts it out of the bed, and ``Brim.cpp`` keeps brims out of
  it. With the option off it is inert, and off is the default
  (``fdm_process_common.json``). The option is in the process preset Bambuddy
  slices with, which ScadBuddy never sees, so it has to be assumed on — but only
  for the tower, where avoiding it is free: :func:`_tower_corner` rejects a back
  placement that touches it and :func:`_tower_sides` offers the other three
  edges. Applying it to the object would refuse placements that print under the
  default, e.g. a 295 x 315 mm single-colour model on an H2C, which cannot clear
  a strip at y 310 and still fit — :attr:`PlateGeometry.wrapping_exclusions`.
"""

from __future__ import annotations

import itertools
import math
import re
from dataclasses import dataclass
from typing import Literal

import numpy as np

from scadbuddy.render.plate_profiles import (
    BED_TYPE_LABELS,
    BED_TYPES,
    EXTRA_ALIASES,
    PLATE_PROFILES,
    TOWER_KEEP_OUTS,
)

#: Widest ``prime_tower_width`` across BBL process profiles (60 mm, in 90 of the 91 of
#: them; ``PrintConfig.cpp``'s own default is 35): the tower's width when the file
#: states no width and the rib wall off.
PRIME_TOWER_SIDE = 60.0

# What Bambu Studio estimates the tower from, before a slice
# (``Print::wipe_tower_data``, src/libslic3r/Print.cpp L3243-3311 at tag v02.08.02.61).
# The process and filament presets are not known where the tower is placed, so each
# value is the BBL presets' default or their largest (#6735).

#: ``filament_prime_volume``, mm³: ``PrintConfig.cpp`` L2922-2929 and
#: ``fdm_filament_common.json`` state 45. Every BBL ``@base`` and H2C filament preset
#: states 30 or none, so 45 is the largest.
PRIME_VOLUME = 45.0
#: ``filament_change_length``, mm: ``PrintConfig.cpp`` L2826-2832 states 10. The largest
#: BBL filament preset is 12 (``Bambu PETG HF @base.json``).
FILAMENT_CHANGE_LENGTH = 12.0
#: ``filament_diameter``, mm, of every BBL filament preset.
FILAMENT_DIAMETER = 1.75
#: ``layer_height``, mm, of ``fdm_process_common.json`` and the "0.20mm Standard"
#: presets. A finer process makes a larger tower (the side grows as 1/sqrt of it).
LAYER_HEIGHT = 0.2
#: ``prime_tower_infill_gap``, 150 % (``PrintConfig.cpp`` L6294-6300); the H2C process
#: presets state no other value.
PRIME_TOWER_INFILL_GAP = 1.5
#: ``prime_tower_rib_width``, mm (``PrintConfig.cpp`` L6262-6269).
PRIME_TOWER_RIB_WIDTH = 8.0
#: ``WipeTower::min_depth_per_height`` (src/libslic3r/GCode/WipeTower.cpp L1587-1589):
#: (tower height, least depth), mm, interpolated between the points.
MIN_DEPTH_PER_HEIGHT = ((5.0, 5.0), (100.0, 20.0), (250.0, 40.0), (350.0, 60.0))
#: ``MAX_OUTER_NOZZLE_RADIUS`` (src/libslic3r/Print.hpp L45), mm: the keep-out for an
#: object no taller than ``nozzle_height``.
NOZZLE_CONE_RADIUS = 4.0
#: ``GCodeWriter::slope_threshold`` (src/libslic3r/GCodeWriter.cpp L19), 3 degrees: it
#: sets the radius of the spiral lift round the tower.
_SLOPE_THRESHOLD = 3 * math.pi / 180
#: The widest nozzle in the BBL machine presets, mm. Bambu Studio pads the tower by
#: 2 x the nozzle per side before the "No sparse layers" check
#: (``compacted_tower_footprint_padding``, ``Print.cpp`` L3335-3348).
WIDEST_NOZZLE = 0.8
#: ``prime_tower_brim_width``'s default (``PrintConfig.cpp``, and BBL's
#: ``fdm_process_common.json``), extending outside the tower footprint: the least
#: brim reserved, for a preset that states a width.
PRIME_TOWER_BRIM = 3.0
#: The widest brim ``WipeTower::get_auto_brim_by_height`` draws ("Auto", ``-1``),
#: reached at :data:`AUTO_BRIM_FULL_HEIGHT`.
PRIME_TOWER_AUTO_BRIM = 8.0
#: The tower height from which the auto brim is :data:`PRIME_TOWER_AUTO_BRIM`; below
#: it the brim is that in proportion.
AUTO_BRIM_FULL_HEIGHT = 100.0
#: Gap left between the object and the tower.
TOWER_CLEARANCE = 5.0
#: Gap left between the tower (with its brim) and the edge of the reachable area.
#: It also covers the brim's last loop, which can end up to half a line past the
#: brim width (the loop count rounds), about 0.2 mm.
EDGE_MARGIN = 2.0

#: A printer preset names its nozzle: "Bambu Lab H2C 0.4 nozzle".
_NOZZLE_SUFFIX = re.compile(r"\s+(\d+(?:\.\d+)?)\s+nozzle$", re.IGNORECASE)
_LAB_PREFIX = "bambu lab "


class PlateFitError(ValueError):
    """The object, or the object plus its prime tower, does not fit the plate."""


def tower_brim(height: float | None) -> float:
    """The brim to reserve round a prime tower as tall as ``height`` (#5545).

    The tower is as tall as the tallest object. Bambu Studio's auto brim
    (``prime_tower_brim_width`` ``-1``, as in the H2C process preset #5545 sliced with) is
    ``height / 100 * 8`` mm up to 8 mm (``WipeTower::get_auto_brim_by_height``); a
    preset that states a width uses that, 3 mm by default. The process preset is not
    known where the tower is placed, so this is the larger of the two. ``None`` (the
    height is not known) is the widest auto brim."""
    if height is None:
        return PRIME_TOWER_AUTO_BRIM
    auto = PRIME_TOWER_AUTO_BRIM * min(max(height, 0.0), AUTO_BRIM_FULL_HEIGHT)
    return max(PRIME_TOWER_BRIM, auto / AUTO_BRIM_FULL_HEIGHT)


@dataclass(frozen=True)
class TowerSettings:
    """The settings the slice builds a plate's prime tower from (#6735)."""

    #: Filaments the plate prints with. The tower grows with them.
    filaments: int = 2
    #: ``wipe_tower_no_sparse_layers``: the objects then keep the slicer's keep-out.
    no_sparse_layers: bool = False
    #: ``prime_tower_rib_wall``, on by default (``PrintConfig.cpp`` L6281-6286); no H2C
    #: process preset turns it off. The tower is then a square.
    rib_wall: bool = True
    #: ``prime_tower_width``, which sets the width only with the rib wall off.
    width: float = PRIME_TOWER_SIDE


@dataclass(frozen=True)
class TowerFootprint:
    """A prime tower on the plate, as :func:`tower_footprint` estimates it (#6735)."""

    #: The tower's own size on X and Y, mm, without the brim.
    width: float
    depth: float
    #: The brim round the tower (:func:`tower_brim`).
    brim: float
    #: The least gap, mm, the slicer accepts between the tower with its brim and an
    #: object: 0 (no overlap) unless "No sparse layers" sets a keep-out.
    keep_out: float

    @property
    def size(self) -> tuple[float, float]:
        """The tower with its brim, mm, on X and Y."""
        return (self.width + 2 * self.brim, self.depth + 2 * self.brim)

    @property
    def clearance(self) -> float:
        """The gap ScadBuddy keeps between the tower with its brim and the objects."""
        return max(TOWER_CLEARANCE, self.keep_out)


def min_tower_depth(height: float) -> float:
    """``WipeTower::get_limit_depth_by_height`` (WipeTower.cpp L1591-1625): the least
    depth of a tower as tall as ``height``."""
    points = MIN_DEPTH_PER_HEIGHT
    if height <= points[0][0]:
        return points[0][1]
    for (h0, d0), (h1, d1) in itertools.pairwise(points):
        if height < h1:
            return d0 + (height - h0) / (h1 - h0) * (d1 - d0)
    return points[-1][1]


def _keep_out(plate: PlateGeometry, height: float | None) -> float:
    """The gap the "No sparse layers" check needs between the tower with its brim and
    objects as tall as ``height`` (``None``: tall), mm; 0 for a printer with no profile.

    ``Print::compacted_wipe_tower_clearance_valid`` (``Print.cpp`` L3542-3598) grows the
    tower by the spiral lift's reach, ``2 * z_hop / (2 pi atan(slope_threshold))``
    (``compacted_wipe_tower_zone``, L3358-3405), and pads it by its brim plus 2 x the
    nozzle (L3335-3348). It then needs ``extruder_clearance_max_radius`` - 0.2 mm between
    that outline and an object taller than ``nozzle_height``, else
    ``MAX_OUTER_NOZZLE_RADIUS`` - 0.2 mm (``compacted_wipe_tower_clearance``, L3432-3433;
    ``compacted_tower_half_clearance``, Print.hpp L1270, half on each side). Only the
    objects' box is known here, so the gap is kept as a box, never as a circle round
    the corners: a stricter test than the slicer's."""
    spec = TOWER_KEEP_OUTS.get(plate.model) if plate.model is not None else None
    if spec is None:
        return 0.0
    radius, nozzle_height, z_hop = spec
    reach = 2 * z_hop / (2 * math.pi * math.atan(_SLOPE_THRESHOLD))
    short = height is not None and height <= nozzle_height
    clearance = NOZZLE_CONE_RADIUS if short else radius
    return (clearance - 0.2) + reach + 2 * WIDEST_NOZZLE


def tower_footprint(
    plate: PlateGeometry, height: float | None, settings: TowerSettings | None = None
) -> TowerFootprint:
    """The prime tower the slice builds on ``plate`` for objects as tall as ``height``
    (``None``: the plate's printable height), with ``settings`` (#6735).

    Bambu Studio's own estimate before a slice (``Print::wipe_tower_data``,
    ``Print.cpp`` L3243-3311), which its "No sparse layers" check also places
    (``estimated_wipe_tower_footprint``, L3488-3535):

    * volume = prime volume x N, where N is the filaments on a two-nozzle printer and
      one less elsewhere (L3282-3285); a two-nozzle printer adds
      ``filament_change_length`` x the filament's section for every two filaments
      (L3261-3273, L3286);
    * rib wall: depth = max(least depth for the height, sqrt(volume / layer height x
      infill gap)) + rib width / sqrt(2) (L3288-3295), and the width is the depth
      (L3512; ``WipeTower::plan_tower_new`` squares the real tower, WipeTower.cpp
      L4528);
    * no rib wall: the width is ``prime_tower_width`` and depth = max(least depth,
      volume / (layer height x width) x infill gap) (L3297-3306).

    One filament still gets a tower of one prime volume, as a smooth timelapse makes one
    (L3284). The brim is :func:`tower_brim`'s."""
    settings = settings or TowerSettings()
    tall = plate.height if height is None else height
    two_nozzles = plate.extruders == 2
    # The clamp matters only for a smooth timelapse, which prints a tower for one
    # filament. Every caller that places objects passes `tower=False` for one filament.
    # Only the send's `towers_within_reach` judges such a tower, for that reason (#6757).
    filaments = max(settings.filaments, 1)
    counted = filaments if two_nozzles else max(filaments - 1, 1)
    volume = PRIME_VOLUME * counted
    if two_nozzles:
        section = math.pi * FILAMENT_DIAMETER**2 / 4
        volume += FILAMENT_CHANGE_LENGTH * section * (filaments // 2)
    least = min_tower_depth(tall)
    if settings.rib_wall:
        depth = max(least, math.sqrt(volume / LAYER_HEIGHT * PRIME_TOWER_INFILL_GAP))
        depth += PRIME_TOWER_RIB_WIDTH / math.sqrt(2)
        width = depth
    else:
        # A width of 0 or less (or not finite) is no width: the default, not a division.
        width = (
            settings.width
            if math.isfinite(settings.width) and settings.width > 0
            else PRIME_TOWER_SIDE
        )
        depth = max(least, volume / (LAYER_HEIGHT * width) * PRIME_TOWER_INFILL_GAP)
    keep_out = _keep_out(plate, height) if settings.no_sparse_layers else 0.0
    return TowerFootprint(width, depth, tower_brim(height), keep_out)


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
    #: ``wrapping_exclude_area`` — the clumping-detection strip. Screened against
    #: the prime tower only; see the module docstring for why not the object.
    wrapping_exclusions: tuple[Rect, ...] = ()

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
    printable, per_extruder, exclude, wrapping, height = PLATE_PROFILES[model]
    bed = _bounding_rect(printable)
    areas = [_bounding_rect(polygon) for polygon in per_extruder]
    return PlateGeometry(
        model=model,
        size=(bed.max_x, bed.max_y),
        usable=_intersect(areas) if areas else bed,
        exclusions=(_bounding_rect(exclude),) if exclude else (),
        extruders=max(len(per_extruder), 1),
        height=height,
        wrapping_exclusions=(_bounding_rect(wrapping),) if wrapping else (),
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


def bed_types_for(model: str | None) -> tuple[str, ...]:
    """The bed types a printer takes (#83), named as :func:`plate_for` names it.

    An unknown printer is offered every type rather than none: its profile is the
    only thing that could rule one out.
    """
    known = plate_for(model).model
    return BED_TYPES[known] if known is not None else tuple(BED_TYPE_LABELS)


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


def _tower_sides(inset: Rect, area: Rect, tower: TowerFootprint) -> list[tuple[str, Rect, Rect]]:
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

    Only the axis the tower eats into is narrowed, and by the tower's size on that
    axis plus the clearance the object has to keep from it (``TowerFootprint.clearance``).
    """
    tx, ty = tower.size
    gap = tower.clearance
    return [
        (
            "front",
            Rect(inset.min_x, inset.min_y, inset.max_x, inset.min_y + ty),
            Rect(area.min_x, inset.min_y + ty + gap, area.max_x, area.max_y),
        ),
        (
            "back",
            Rect(inset.min_x, inset.max_y - ty, inset.max_x, inset.max_y),
            Rect(area.min_x, area.min_y, area.max_x, inset.max_y - ty - gap),
        ),
        (
            "left",
            Rect(inset.min_x, inset.min_y, inset.min_x + tx, inset.max_y),
            Rect(inset.min_x + tx + gap, area.min_y, area.max_x, area.max_y),
        ),
        (
            "right",
            Rect(inset.max_x - tx, inset.min_y, inset.max_x, inset.max_y),
            Rect(area.min_x, area.min_y, inset.max_x - tx - gap, area.max_y),
        ),
    ]


def _tower_corner(
    side: str, strip: Rect, object_rect: Rect, plate: PlateGeometry, tower: TowerFootprint
) -> tuple[float, float] | None:
    """Centre the tower in ``strip`` on the object's cross axis, then check it.

    ``object_rect`` is grown by the tower's clearance (:data:`TOWER_CLEARANCE`, or the
    "No sparse layers" keep-out) before the overlap test, so the gap holds on this path
    too and not only where a strip was reserved. Without that a tower could end up
    flush against the object — 2 mm away for a 280x180 model on an H2C — while every
    assertion still passed.
    """
    tx, ty = tower.size
    if side in ("front", "back"):
        x = object_rect.centre[0] - tx / 2
        x = min(max(x, strip.min_x), strip.max_x - tx)
        y = strip.min_y
    else:
        x = strip.min_x
        y = object_rect.centre[1] - ty / 2
        y = min(max(y, strip.min_y), strip.max_y - ty)
    footprint = Rect(x, y, x + tx, y + ty)
    gap = tower.clearance
    keep_clear = Rect(
        object_rect.min_x - gap,
        object_rect.min_y - gap,
        object_rect.max_x + gap,
        object_rect.max_y + gap,
    )
    if footprint.overlaps(keep_clear):
        return None
    if any(footprint.overlaps(cutout) for cutout in plate.exclusions):
        return None
    if any(footprint.overlaps(zone) for zone in plate.wrapping_exclusions):
        return None
    # ``wipe_tower_x``/``_y`` name the tower itself; the brim sits outside it.
    return (x + tower.brim, y + tower.brim)


Axis = Literal["X", "Y", "Z"]


def overshoots(
    size: tuple[float, float, float], plate: PlateGeometry
) -> list[tuple[Axis, float, float]]:
    """``(axis, size, limit)`` for each axis ``size`` does not fit (#81).

    X and Y against the area every extruder reaches — the first thing
    :func:`place_on_plate` refuses — and Z against ``printable_height``.
    """
    area = plate.usable
    limits: tuple[tuple[Axis, float, float], ...] = (
        ("X", size[0], area.width),
        ("Y", size[1], area.depth),
        ("Z", size[2], plate.height),
    )
    return [(axis, want, have) for axis, want, have in limits if want > have]


def fit_problem(
    size: tuple[float, float, float],
    plate: PlateGeometry,
    *,
    tower: bool,
    settings: TowerSettings | None = None,
) -> str | None:
    """Why :func:`place_on_plate` would refuse a model of ``size``, or ``None``.

    The same call the send makes, so the customizer's warning covers what an axis
    comparison cannot: the prime tower's room and the filament-cutter cutout (#81).
    ``settings`` sizes the tower (:func:`tower_footprint`, #6735).
    """
    try:
        place_on_plate(
            np.array([[0.0, 0.0, 0.0], list(size)]), plate, tower=tower, settings=settings
        )
    except PlateFitError as error:
        return str(error)
    return None


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


def _tower_inset(plate: PlateGeometry) -> Rect:
    """The reachable area less :data:`EDGE_MARGIN`, where a tower and its brim go."""
    area = plate.usable
    return Rect(
        area.min_x + EDGE_MARGIN,
        area.min_y + EDGE_MARGIN,
        area.max_x - EDGE_MARGIN,
        area.max_y - EDGE_MARGIN,
    )


def tower_beside(
    object_rect: Rect,
    plate: PlateGeometry,
    height: float | None = None,
    settings: TowerSettings | None = None,
) -> tuple[float, float] | None:
    """The ``wipe_tower_x``/``_y`` of a tower against the first edge of the reachable
    area it fits on clear of ``object_rect`` (``TowerFootprint.clearance``) and of the
    plate's cutouts, as :func:`place_on_plate` puts one beside a centred object; None
    when no edge has room. ``height`` is the tallest object's, which sets the brim
    (:func:`tower_brim`) and, with ``settings``, the tower's size
    (:func:`tower_footprint`). A ``height`` of ``None`` reserves the largest brim and
    keep-out; ``settings`` of ``None`` sizes the tower for the default two filaments."""
    tower = tower_footprint(plate, height, settings)
    for side, strip, _remainder in _tower_sides(_tower_inset(plate), plate.usable, tower):
        corner = _tower_corner(side, strip, object_rect, plate, tower)
        if corner is not None:
            return corner
    return None


def tower_reaches(
    corner: tuple[float, float],
    plate: PlateGeometry,
    height: float | None = None,
    settings: TowerSettings | None = None,
) -> bool:
    """Whether the tower :func:`tower_footprint` gives for ``height`` and ``settings``,
    at ``wipe_tower_x``/``_y`` ``corner``, lies, brim included, where every extruder
    reaches, clear of the plate's cutouts. An A1 mini's own two-filament tower at
    (15, 145) fits the A1 mini."""
    tower = tower_footprint(plate, height, settings)
    x, y = corner[0] - tower.brim, corner[1] - tower.brim
    footprint = Rect(x, y, x + tower.size[0], y + tower.size[1])
    inset = _tower_inset(plate)
    if (
        footprint.min_x < inset.min_x
        or footprint.min_y < inset.min_y
        or footprint.max_x > inset.max_x
        or footprint.max_y > inset.max_y
    ):
        return False
    return not any(
        footprint.overlaps(zone) for zone in (*plate.exclusions, *plate.wrapping_exclusions)
    )


def place_on_plate(
    bounds: np.ndarray,
    plate: PlateGeometry,
    *,
    tower: bool = True,
    settings: TowerSettings | None = None,
) -> Placement:
    """Centre ``bounds`` on ``plate`` and pick a prime-tower corner clear of it.

    ``bounds`` is a ``(2, 3)`` min/max array in the mesh's own coordinates. The
    object is centred on the area **every extruder can reach** rather than on the
    bed rectangle: on an H2C those centres are 10 mm apart in X, and the bed's is
    the wrong one because half the plate is out of extruder 2's range.

    When the object cannot be centred and still leave the tower room, it is
    shifted off centre — a strip is reserved against one edge and the object is
    centred in what is left. Only when no edge works is this a
    :class:`PlateFitError`, raised before anything is written or uploaded.

    The tower is the one :func:`tower_footprint` gives for the object's height and
    ``settings`` (#6735).
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

    height = float(high[2] - low[2])
    footprint = tower_footprint(plate, height, settings)
    inset = _tower_inset(plate)

    # First choice: the object stays centred and the tower takes whichever edge
    # it fits against. Only if none does is the object moved.
    object_centre = _clear_of_exclusions(area.centre, width, depth, plate)
    if object_centre is not None:
        corner = tower_beside(_rect_at(object_centre), plate, height, settings)
        if corner is not None:
            return Placement(offset=_offset(object_centre), tower=corner)

    for side, strip, remainder in _tower_sides(inset, area, footprint):
        if width > remainder.width or depth > remainder.depth:
            continue
        moved_centre = _clear_of_exclusions(remainder.centre, width, depth, plate, region=remainder)
        if moved_centre is None:
            continue
        moved = _rect_at(moved_centre)
        corner = _tower_corner(side, strip, moved, plate, footprint)
        if corner is not None:
            return Placement(offset=_offset(moved_centre), tower=corner)

    reach = f"reachable by all {plate.extruders} extruders" if plate.extruders > 1 else "printable"
    keep_out = (
        f', {footprint.keep_out:.0f} mm from the objects as "No sparse layers" needs,'
        if footprint.keep_out > TOWER_CLEARANCE
        else ""
    )
    raise PlateFitError(
        f"the model is {width:.1f} x {depth:.1f} mm, which leaves no room for the "
        f"{footprint.width:.0f} x {footprint.depth:.0f} mm prime tower a multi-colour "
        f"print needs{keep_out} on {plate.model or 'the default plate'} "
        f"({area.width:.0f} x {area.depth:.0f} mm {reach})"
    )
