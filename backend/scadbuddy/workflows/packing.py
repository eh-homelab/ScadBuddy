"""Plates from parts, by footprint (spec 2026-09-27 §5.2 `pack`, goal
``fewest_plates``). Pure: runs in an activity and in `plate_of` in the workflow.
Phase 5's Arrange replaces `shelf_pack`."""

from __future__ import annotations

from collections.abc import Sequence

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from scadbuddy.template import Part
    from scadbuddy.workflows.models import Layout, LayoutPlate, PackItem, Placed, PlateSize

#: Space between neighbouring parts, mm.
GAP_MM = 5.0


class PackError(ValueError):
    pass


def shelf_pack(items: Sequence[PackItem], plate: PlateSize) -> Layout:
    if len(items) == 1 and items[0].count == 1:
        return Layout(own=items[0].part.piece_key)
    for item in items:
        if item.part.plates > 1:
            raise PackError(
                f"{item.part.file} lays out its own {item.part.plates} plates; pack it on its own"
            )
    copies = sorted(
        (item.part for item in items for _ in range(item.count)),
        key=lambda p: (-p.bbox.size[1], -p.bbox.size[0], p.piece_key),
    )
    plates: list[LayoutPlate] = []
    current: list[Placed] = []
    x = y = row = 0.0
    for part in copies:
        w, d = part.bbox.size[0], part.bbox.size[1]
        if w > plate.width or d > plate.depth:
            raise PackError(
                f"{part.file} ({w:.0f} x {d:.0f} mm) is larger than the plate "
                f"({plate.width:.0f} x {plate.depth:.0f} mm)"
            )
        if x + w > plate.width:
            x, y, row = 0.0, y + row + GAP_MM, 0.0
        if y + d > plate.depth:
            plates.append(LayoutPlate(items=current))
            current, x, y, row = [], 0.0, 0.0, 0.0
        current.append(Placed(piece_key=part.piece_key, x=x, y=y))
        x += w + GAP_MM
        row = max(row, d)
    if current:
        plates.append(LayoutPlate(items=current))
    return Layout(plates=plates)


def explicit_plate(parts: Sequence[Part], at: Sequence[tuple[float, float, float]]) -> LayoutPlate:
    """`plate_of(items, at=…)`: the pipeline's own placement. Rotation arrives with
    Arrange (phase 5)."""
    if len(parts) != len(at):
        raise PackError(f"{len(parts)} parts but {len(at)} positions")
    if any(rot for _, _, rot in at):
        raise PackError("rotation in plate_of arrives with Arrange (phase 5); pass 0")
    return LayoutPlate(
        items=[
            Placed(piece_key=p.piece_key, x=x, y=y) for p, (x, y, _) in zip(parts, at, strict=True)
        ]
    )
