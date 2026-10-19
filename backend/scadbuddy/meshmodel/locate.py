"""Which scene object a point of a mesh model's render lies in (#2146, spec 2026-10-11
§9), so a weak joint's suggested `split_at_plane` names the object `edit_scene` takes.
`analyze_geometry` reads a 3MF, which knows no scene; `GET /outputs/{id}/geometry`
attributes its weak joints here, with the scene at the output's revision."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path

import numpy as np
import trimesh

from scadbuddy.core.paths import SOURCE_NAME
from scadbuddy.meshmodel.frames import unit
from scadbuddy.meshmodel.meshes import mesh_loader
from scadbuddy.meshmodel.paint import placement
from scadbuddy.meshmodel.scene import SCENE_NAME, Cut, Scene, load_scene, text_sha256, value
from scadbuddy.meshmodel.shared import MeshOf
from scadbuddy.render.geometry import GeometryAnalysis
from scadbuddy.render.meshmatch import point_triangle_distance

logger = logging.getLogger(__name__)

#: How far outside an object's mesh box, or past one of its cuts, a point may lie and
#: still be in it, mm: a section's centroid is inside, but a cut face is a boundary.
SLACK_MM = 0.5


def _keeps(cut: Cut, local: np.ndarray) -> bool:
    side = float((local - np.asarray([value(x) for x in cut.point])) @ unit(cut.normal))
    return side >= -SLACK_MM if cut.keep == "above" else side <= SLACK_MM


def _nearness(
    world: np.ndarray, m: np.ndarray, t: np.ndarray, printed: Sequence[trimesh.Trimesh]
) -> float:
    """How far ``world`` is outside the placed ``printed`` meshes: 0 inside (their winding
    number about it is past one half), else the distance to the nearest triangle.

    Not the side of the nearest triangle: where the nearest feature is an edge or a
    corner, several triangles are as near, and on a thin wall their faces turn nearly
    opposite ways, so one of them says inside a point that is not (#3749)."""
    triangles = np.concatenate(
        [
            np.asarray(mesh.vertices, dtype=float)[np.asarray(mesh.faces)] @ m.T + t
            for mesh in printed
        ]
    )
    distance = point_triangle_distance(np.tile(world, (len(triangles), 1)), triangles)
    # A mirrored placement winds its triangles the other way: the number turns negative.
    # A point on the surface is about one half, either side of it by rounding, and about 0
    # from the nearest triangle: its nearness is about 0 whichever way it falls.
    inside = _winding_number(world, triangles) * np.sign(np.linalg.det(m)) > 0.5
    return 0.0 if inside else float(distance.min())


def _winding_number(point: np.ndarray, triangles: np.ndarray) -> float:
    """The triangles' generalised winding number about ``point``: each one's solid angle
    seen from it (Van Oosterom and Strackee's formula), summed over 4π. About 1 inside a
    closed mesh wound outward, 0 outside it (Jacobson, Kavan and Sorkine-Hornung,
    "Robust Inside-Outside Segmentation using Generalized Winding Numbers", 2013,
    https://igl.ethz.ch/projects/winding-number/)."""
    a, b, c = (triangles[:, i] - point for i in range(3))
    la, lb, lc = (np.linalg.norm(v, axis=1) for v in (a, b, c))
    numerator = np.einsum("ij,ij->i", a, np.cross(b, c))
    dots = np.einsum("ij,ij->i", a, b) * lc
    dots += np.einsum("ij,ij->i", a, c) * lb + np.einsum("ij,ij->i", b, c) * la
    angles = 2.0 * np.arctan2(numerator, la * lb * lc + dots)
    return float(angles.sum() / (4.0 * np.pi))


def object_at(scene: Scene, mesh_of: MeshOf, point: Sequence[float]) -> str | None:
    """The object whose printed parts' box, in its own frame and grown by
    :data:`SLACK_MM`, holds ``point`` (world) on the kept side of each of its cuts. When
    several do, the one whose own placed mesh holds the point, else is nearest it, the
    smaller box breaking a tie; None when none does."""
    world = np.asarray(point, dtype=float)
    found: list[tuple[float, str, np.ndarray, np.ndarray, list[trimesh.Trimesh]]] = []
    for obj in scene.objects:
        m, t = placement(obj)
        if abs(float(np.linalg.det(m))) < 1e-12:
            continue
        printed = [mesh_of(part.mesh) for part in obj.parts if not part.negative]
        if not printed:
            continue
        local = np.linalg.solve(m, world - t)
        low = np.min([mesh.bounds[0] for mesh in printed], axis=0) - SLACK_MM
        high = np.max([mesh.bounds[1] for mesh in printed], axis=0) + SLACK_MM
        if not ((local >= low) & (local <= high)).all():
            continue
        if not all(_keeps(op, local) for op in obj.ops if isinstance(op, Cut)):
            continue
        found.append((float(np.prod(high - low)), obj.id, m, t, printed))
    if not found:
        return None
    if len(found) == 1:
        return found[0][1]
    return min(found, key=lambda f: (_nearness(world, f[2], f[3], f[4]), f[0]))[1]


def attribute(analysis: GeometryAnalysis, directory: Path) -> GeometryAnalysis:
    """``analysis`` with each weak joint's suggestion naming the object its section's
    centroid is in, for a render of the mesh model in ``directory``. Unchanged where
    there is no scene, or model.scad was edited by hand (the scene no longer describes
    the geometry)."""
    if not analysis.weak_joints:
        return analysis
    try:
        scene = load_scene((directory / SCENE_NAME).read_text(encoding="utf-8"))
        source = (directory / SOURCE_NAME).read_text(encoding="utf-8")
        if text_sha256(source) != scene.generated_sha256:
            return analysis
        mesh_of = mesh_loader(directory)
        joints = [
            joint.model_copy(
                update={
                    "suggestion": joint.suggestion.model_copy(
                        update={"object": object_at(scene, mesh_of, joint.centroid)}
                    )
                }
            )
            for joint in analysis.weak_joints
        ]
    except FileNotFoundError:
        return analysis
    except (OSError, ValueError) as error:
        logger.warning("weak joints left unattributed: %s", type(error).__name__)
        return analysis
    return analysis.model_copy(update={"weak_joints": joints})
