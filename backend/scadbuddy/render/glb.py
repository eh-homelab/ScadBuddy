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


#: The mesh extra holding a part's sRGB hex. ``baseColorFactor`` is linear, and
#: trimesh stores it in 8 bits, so the darks cannot be read back from it (#1319).
COLOUR_EXTRA = "scadbuddy_colour"


def _srgb_to_linear(channel: float) -> float:
    return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4


def _linear_rgba(colour: str) -> list[float]:
    """``colour``, an sRGB hex, as the linear factor glTF's ``baseColorFactor`` is."""
    value = colour.lstrip("#")
    return [_srgb_to_linear(int(value[i : i + 2], 16) / 255) for i in (0, 2, 4)] + [1.0]


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
        mesh.metadata[COLOUR_EXTRA] = part.colour
        mesh.visual = trimesh.visual.TextureVisuals(
            material=trimesh.visual.material.PBRMaterial(
                name=part.name,
                baseColorFactor=_linear_rgba(part.colour),
                metallicFactor=0.0,
                roughnessFactor=0.8,
            )
        )
        scene.add_geometry(mesh, geom_name=part.name)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(scene.export(file_type="glb", include_normals=False))
    return bounding_box(parts)


def _colour_of(mesh: trimesh.Trimesh) -> str:
    colour = mesh.metadata.get(COLOUR_EXTRA)
    if isinstance(colour, str):
        return colour
    # A preview from before #1319, which wrote the sRGB bytes as the factor.
    material = getattr(mesh.visual, "material", None)
    factor = getattr(material, "baseColorFactor", None)
    if factor is None:
        return "#FFFFFF"
    rgb = np.asarray(factor).ravel()[:3]
    if rgb.dtype.kind == "f" and float(rgb.max(initial=0.0)) <= 1.0:
        rgb = np.rint(rgb * 255)
    return "#" + "".join(f"{int(channel):02X}" for channel in rgb)


def read_glb(path: Path) -> list[ColourPart]:
    """The parts a preview written by :func:`write_glb` holds, back in OpenSCAD's
    Z-up millimetres: what a job or a saved output can be re-drawn from without
    another OpenSCAD run. Watertightness is not what the preview is for; the
    parts' meshes are only good for drawing."""
    scene = trimesh.load(path, file_type="glb", force="scene")
    if not isinstance(scene, trimesh.Scene):  # `force="scene"` promises one
        raise ValueError(f"{path.name} is not a glTF scene")
    y_up_to_z_up = np.linalg.inv(Z_UP_TO_Y_UP)
    parts: list[ColourPart] = []
    for index, node in enumerate(scene.graph.nodes_geometry, start=1):
        transform, geometry_name = scene.graph[node]
        geometry = scene.geometry[geometry_name]
        if not isinstance(geometry, trimesh.Trimesh) or len(geometry.faces) == 0:
            continue
        mesh = geometry.copy()
        mesh.apply_transform(y_up_to_z_up @ transform)
        parts.append(ColourPart(index, str(geometry_name), _colour_of(geometry), mesh))
    return parts
