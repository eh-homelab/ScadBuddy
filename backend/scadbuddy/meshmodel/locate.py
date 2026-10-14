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
from scadbuddy.meshmodel.edits import MeshOf
from scadbuddy.meshmodel.frames import unit
from scadbuddy.meshmodel.meshes import mesh_loader
from scadbuddy.meshmodel.paint import placement
from scadbuddy.meshmodel.scene import SCENE_NAME, Cut, Scene, load_scene, text_sha256, value
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
    """How far ``world`` is outside the placed ``printed`` meshes: 0 inside (the side the
    nearest triangle's outward normal turns from), else the distance to the nearest
    triangle."""
    triangles = np.concatenate(
        [
            np.asarray(mesh.vertices, dtype=float)[np.asarray(mesh.faces)] @ m.T + t
            for mesh in printed
        ]
    )
    distance = point_triangle_distance(np.tile(world, (len(triangles), 1)), triangles)
    nearest = int(distance.argmin())
    a, b, c = triangles[nearest]
    # A mirrored placement winds its triangles the other way: the normal points inward.
    outward = np.cross(b - a, c - a) * np.sign(np.linalg.det(m))
    inside = float(np.dot(world - a, outward)) < 0.0
    return 0.0 if inside else float(distance[nearest])


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
