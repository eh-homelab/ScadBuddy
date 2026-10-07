"""Arrange's packer (spec 2026-09-27 §7): objects onto plates, for a goal.

Heuristic, not a solver. Copies are grouped by filament signature (their spools under a
plan, else their colours), placed first-fit-decreasing into MaxRects free space with a
quarter turn when that fits better, and a plate only takes a copy if `render/plate.py`'s
`fit_problem` still passes for the plate's block with the prime tower that plate needs
(more than one colour). That is the same check `write_plates_3mf` makes, so what is
packed is always writable. Plates are then ordered so each needs the fewest filaments
the previous did not. A goal is two functions: which plates a copy may join, and
whether plates are reordered.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from scadbuddy.render.job_models import ManifestObject
from scadbuddy.render.plate import PlateGeometry, fit_problem, plate_for
from scadbuddy.template import Part
from scadbuddy.workflows.models import Layout, LayoutPlate, PackItem, Placed, PlateSize, SlotPlan
from scadbuddy.workflows.packing import GAP_MM, PackError

GOALS: tuple[str, ...] = ("fewest_plates", "fewest_swaps", "by_colour", "keep_together")
_EPS = 1e-6


def geometry_of(size: PlateSize) -> PlateGeometry:
    return plate_for(None if size.key == "default" else size.key)


def signature_of(part: Part, plan: SlotPlan | None, colours: Sequence[str]) -> frozenset[str]:
    """The filaments ``part`` needs: its spools under ``plan``, else its colours."""
    order = [c.upper() for c in colours]
    needs: set[str] = set()
    for colour in part.colours:
        slot = order.index(colour.upper()) + 1 if colour.upper() in order else None
        spool = plan.slots.get(slot) if plan is not None and slot is not None else None
        needs.add(f"spool:{spool}" if spool is not None else f"colour:{colour.upper()}")
    return frozenset(needs)


def part_of(obj: ManifestObject) -> Part:
    return Part(
        piece_key=obj.part,
        file=obj.file,
        bbox=obj.bbox,
        colours=list(obj.colours),
        notes=list(obj.notes),
        plates=obj.plates,
    )


@dataclass(frozen=True)
class _Copy:
    part: Part
    group: str | None
    signature: frozenset[str]
    order: int

    @property
    def size(self) -> tuple[float, float]:
        return self.part.bbox.size[0], self.part.bbox.size[1]


@dataclass(frozen=True)
class _Free:
    x: float
    y: float
    w: float
    d: float

    def contains(self, other: _Free) -> bool:
        return (
            self.x <= other.x + _EPS
            and self.y <= other.y + _EPS
            and self.x + self.w >= other.x + other.w - _EPS
            and self.y + self.d >= other.y + other.d - _EPS
        )


@dataclass
class _Sheet:
    width: float
    depth: float
    group: str | None
    signature: frozenset[str]
    free: list[_Free] = field(default_factory=list)
    #: copy, x, y, rot
    placed: list[tuple[_Copy, float, float, float]] = field(default_factory=list)
    colours: set[str] = field(default_factory=set)
    height: float = 0.0
    extent: tuple[float, float] = (0.0, 0.0)
    #: footprints (size, colours, height) this sheet refused; a sheet only fills up, so a
    #: refusal stands, and a full plate need not re-check every later copy.
    refused: set[tuple[tuple[float, float], frozenset[str], float]] = field(default_factory=set)

    def __post_init__(self) -> None:
        # The gap trails every part, so the sheet is one gap larger than the plate.
        self.free = [_Free(0.0, 0.0, self.width + GAP_MM, self.depth + GAP_MM)]

    def spots(self, w: float, d: float) -> list[_Free]:
        """Every free rectangle a ``w`` x ``d`` footprint (and its trailing gap) fits in."""
        return [
            free
            for free in self.free
            if w + GAP_MM <= free.w + _EPS and d + GAP_MM <= free.d + _EPS
        ]

    def occupy(self, used: _Free) -> None:
        pieces: list[_Free] = []
        for free in self.free:
            if (
                used.x >= free.x + free.w - _EPS
                or used.x + used.w <= free.x + _EPS
                or used.y >= free.y + free.d - _EPS
                or used.y + used.d <= free.y + _EPS
            ):
                pieces.append(free)
                continue
            if used.x > free.x + _EPS:
                pieces.append(_Free(free.x, free.y, used.x - free.x, free.d))
            if used.x + used.w < free.x + free.w - _EPS:
                right = used.x + used.w
                pieces.append(_Free(right, free.y, free.x + free.w - right, free.d))
            if used.y > free.y + _EPS:
                pieces.append(_Free(free.x, free.y, free.w, used.y - free.y))
            if used.y + used.d < free.y + free.d - _EPS:
                top = used.y + used.d
                pieces.append(_Free(free.x, top, free.w, free.y + free.d - top))
        self.free = [
            p
            for i, p in enumerate(pieces)
            if not any(j != i and q.contains(p) and (q != p or j < i) for j, q in enumerate(pieces))
        ]


def _try(sheet: _Sheet, copy: _Copy, geometry: PlateGeometry) -> bool:
    """Place ``copy`` on ``sheet`` if a turn of it fits and the plate stays writable."""
    w, d = copy.size
    key = ((w, d), frozenset(c.upper() for c in copy.part.colours), copy.part.bbox.size[2])
    if key in sheet.refused:
        return False
    turns = [(w, d, 0.0)] + ([(d, w, 90.0)] if abs(w - d) > _EPS else [])
    # Every spot, not one per turn: the plate check (a prime tower's corner, the X1C's
    # cutter) can refuse the first while another keeps the block writable. Least extent
    # first grows the block as a rectangle rather than an "L" across the whole plate,
    # then best-short-side-fit, then the lower-left spot, so the result is deterministic.
    options = [(spot, tw, td, rot) for tw, td, rot in turns for spot in sheet.spots(tw, td)]
    options.sort(
        key=lambda o: (
            max(sheet.extent[0], o[0].x + o[1]) * max(sheet.extent[1], o[0].y + o[2]),
            min(o[0].w - o[1], o[0].d - o[2]),
            o[3],
            o[0].y,
            o[0].x,
        )
    )
    colours = sheet.colours | {c.upper() for c in copy.part.colours}
    height = max(sheet.height, copy.part.bbox.size[2])
    for spot, tw, td, rot in options:
        extent = (max(sheet.extent[0], spot.x + tw), max(sheet.extent[1], spot.y + td))
        if fit_problem((extent[0], extent[1], height), geometry, tower=len(colours) > 1):
            continue
        sheet.occupy(_Free(spot.x, spot.y, tw + GAP_MM, td + GAP_MM))
        sheet.placed.append((copy, spot.x, spot.y, rot))
        sheet.colours, sheet.height, sheet.extent = colours, height, extent
        return True
    sheet.refused.add(key)
    return False


def _joins(goal: str) -> Callable[[_Sheet, _Copy], bool]:
    if goal == "fewest_plates":
        return lambda sheet, copy: True
    if goal == "by_colour":
        return lambda sheet, copy: sheet.signature == copy.signature
    if goal == "fewest_swaps":
        return lambda sheet, copy: copy.signature <= sheet.signature
    return lambda sheet, copy: sheet.group == copy.group


def order_plates(signatures: Sequence[frozenset[str]]) -> list[int]:
    """Greedy: start with the plate needing the most filaments, then always the plate
    that adds the fewest the previous one did not have (ties: the earlier plate)."""
    left = list(range(len(signatures)))
    if not left:
        return []
    current = min(left, key=lambda i: (-len(signatures[i]), i))
    ordered = [current]
    left.remove(current)
    while left:
        prev = signatures[current]
        current = min(
            left, key=lambda i: (len(signatures[i] - prev), -len(signatures[i] & prev), i)
        )
        ordered.append(current)
        left.remove(current)
    return ordered


def arrange(
    items: Sequence[PackItem],
    plate: PlateSize,
    *,
    goal: str = "fewest_plates",
    plan: SlotPlan | None = None,
    colours: Sequence[str] = (),
    allow_own: bool = True,
) -> Layout:
    if goal not in GOALS:
        raise PackError(f"pack goal {goal!r} is not one of {', '.join(GOALS)}")
    if not items:
        raise PackError("nothing to pack: pass at least one part")
    lone = len(items) == 1 and items[0].count == 1
    if allow_own and lone and (goal == "fewest_plates" or items[0].part.plates > 1):
        return Layout(own=items[0].part.piece_key)
    for item in items:
        if item.part.plates > 1:
            raise PackError(
                f"{item.part.file} lays out its own {item.part.plates} plates; pack it on its own"
            )
    geometry = geometry_of(plate)
    order = list(colours) or list(dict.fromkeys(c for item in items for c in item.part.colours))
    copies = [
        _Copy(
            item.part,
            item.group if goal == "keep_together" else None,
            signature_of(item.part, plan, order),
            n,
        )
        for n, item in enumerate(i for i in items for _ in range(i.count))
    ]
    copies.sort(
        key=lambda c: (
            -len(c.signature) if goal == "fewest_swaps" else 0,
            -(c.size[0] * c.size[1]),
            -max(c.size),
            c.order,
        )
    )
    joins = _joins(goal)
    sheets: list[_Sheet] = []
    for copy in copies:
        if any(joins(s, copy) and _try(s, copy, geometry) for s in sheets):
            continue
        sheet = _Sheet(geometry.usable.width, geometry.usable.depth, copy.group, copy.signature)
        if not _try(sheet, copy, geometry):
            w, d = copy.size
            why = (
                fit_problem(
                    (w, d, copy.part.bbox.size[2]), geometry, tower=len(copy.part.colours) > 1
                )
                or "no turn of it fits"
            )
            raise PackError(
                f"{copy.part.file} ({w:.0f} x {d:.0f} mm) does not fit the plate "
                f"({plate.width:.0f} x {plate.depth:.0f} mm): {why}"
            )
        sheets.append(sheet)
    if goal == "keep_together":
        seen: dict[str | None, int] = {}
        for sheet in sheets:
            seen[sheet.group] = seen.get(sheet.group, 0) + 1
        for group, count in seen.items():
            if group is not None and count > 1:
                raise PackError(f"group {group!r} does not fit on one plate")
    indices = (
        order_plates([s.signature for s in sheets])
        if goal in ("fewest_swaps", "by_colour")
        else list(range(len(sheets)))
    )
    return Layout(
        plates=[
            LayoutPlate(
                items=[
                    Placed(piece_key=c.part.piece_key, x=round(x, 3), y=round(y, 3), rot=rot)
                    for c, x, y, rot in sorted(sheets[i].placed, key=lambda p: p[0].order)
                ]
            )
            for i in indices
        ]
    )
