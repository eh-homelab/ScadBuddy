"""A plate placed by the pipeline itself (spec 2026-09-27 §5.2 `plate_of`), and what
the packers share. Pure: `plate_of` runs it in the workflow. `pack` is Arrange's
(`workflows/arrange.py`, §7), in an activity."""

from __future__ import annotations

from collections.abc import Sequence

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from scadbuddy.template import Part
    from scadbuddy.workflows.models import LayoutPlate, Placed, PlateSize

#: Space between neighbouring parts, mm.
GAP_MM = 5.0


class PackError(ValueError):
    pass


def explicit_plate(
    parts: Sequence[Part],
    at: Sequence[tuple[float, float, float]],
    *,
    plate: PlateSize,
) -> LayoutPlate:
    """`plate_of(items, at=…)`: the pipeline's own placement, each part's box turned by
    a quarter turn about Z when asked, its min corner at (x, y), on ``plate``."""
    if len(parts) != len(at):
        raise PackError(f"{len(parts)} parts but {len(at)} positions")
    if any(rot % 90 for _, _, rot in at):
        raise PackError("plate_of rotates in quarter turns: 0, 90, 180 or 270")
    for part, (x, y, rot) in zip(parts, at, strict=True):
        w, d = part.bbox.size[0], part.bbox.size[1]
        if rot % 180:
            w, d = d, w
        if x < 0 or y < 0 or x + w > plate.width or y + d > plate.depth:
            raise PackError(
                f"{part.file} at ({x:.0f}, {y:.0f}) is off the plate "
                f"({plate.width:.0f} x {plate.depth:.0f} mm)"
            )
    return LayoutPlate(
        items=[
            Placed(piece_key=p.piece_key, x=x, y=y, rot=float(rot) % 360)
            for p, (x, y, rot) in zip(parts, at, strict=True)
        ]
    )
