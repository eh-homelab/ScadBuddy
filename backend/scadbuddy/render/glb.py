from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import numpy as np
import trimesh
from pydantic import BaseModel

from scadbuddy.render.split import ColourPart

# OpenSCAD is Z-up, glTF (and three.js) are Y-up.
Z_UP_TO_Y_UP = np.array(
    [
        [1.0, 0.0, 0.0, 0.0],
        [0.0, 0.0, 1.0, 0.0],
        [0.0, -1.0, 0.0, 0.0],
        [0.0, 0.0, 0.0, 1.0],
    ]
)


class BoundingBox(BaseModel):
    min: tuple[float, float, float]
    max: tuple[float, float, float]
    size: tuple[float, float, float]


def _rgba(colour: str) -> list[int]:
    value = colour.lstrip("#")
    return [int(value[i : i + 2], 16) for i in (0, 2, 4)] + [255]


def bounding_box(parts: Sequence[ColourPart]) -> BoundingBox:
    bounds = np.array([part.mesh.bounds for part in parts])
    low = bounds[:, 0, :].min(axis=0)
    high = bounds[:, 1, :].max(axis=0)
    return BoundingBox(
        min=(float(low[0]), float(low[1]), float(low[2])),
        max=(float(high[0]), float(high[1]), float(high[2])),
        size=(float(high[0] - low[0]), float(high[1] - low[1]), float(high[2] - low[2])),
    )


def write_glb(parts: Sequence[ColourPart], out_path: Path) -> BoundingBox:
    if not parts:
        raise ValueError("a GLB needs at least one colour part")
    scene = trimesh.Scene()
    for part in parts:
        mesh = part.mesh.copy()
        mesh.apply_transform(Z_UP_TO_Y_UP)
        mesh.visual = trimesh.visual.TextureVisuals(
            material=trimesh.visual.material.PBRMaterial(
                name=part.name,
                baseColorFactor=_rgba(part.colour),
                metallicFactor=0.0,
                roughnessFactor=0.8,
            )
        )
        scene.add_geometry(mesh, geom_name=part.name)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(scene.export(file_type="glb", include_normals=False))
    return bounding_box(parts)
