"""Where a plane cuts a mesh (spec 2026-10-11 §5): the section's area, centroid, and
``room`` (distance from the centroid to the nearest edge), for sizing a peg; and the
points an object's cuts keep, for `lay_flat`'s drop to the bed.

numpy only. trimesh's own section path needs shapely and its slicing needs scipy,
neither installed (measured 2026-10-11, plan 2026-10-11-mesh-models-phase-1-2). The
section's segments come from ``mesh_plane``, each oriented with the interior on its
left by its face's outward normal, so Green's theorem gives area and centroid without
assembling loops."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

import numpy as np
import trimesh

from scadbuddy.meshmodel.frames import unit

CutSpec = tuple[Sequence[float], Sequence[float], Literal["above", "below"]]


@dataclass(frozen=True)
class Section:
    area: float
    centroid: np.ndarray
    room: float
    inside: bool


def _basis(n: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    helper = np.array([1.0, 0.0, 0.0]) if abs(n[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
    u = unit(np.cross(n, helper))
    return u, np.cross(n, u)


def _segments(
    mesh: trimesh.Trimesh, point: np.ndarray, n: np.ndarray
) -> tuple[np.ndarray, np.ndarray] | None:
    lines, faces = trimesh.intersections.mesh_plane(mesh, n, point, return_faces=True)
    if len(lines) == 0:
        return None
    start, end = lines[:, 0], lines[:, 1]
    left = np.cross(n, end - start)
    flip = np.einsum("ij,ij->i", left, mesh.face_normals[faces]) > 0
    return np.where(flip[:, None], end, start), np.where(flip[:, None], start, end)


def measure(
    mesh: trimesh.Trimesh,
    point: Sequence[float],
    normal: Sequence[float],
    holes: Sequence[trimesh.Trimesh] = (),
) -> Section | None:
    """``mesh``'s section at the plane, less each of ``holes`` (an object's negative
    parts). A hole's boundary goes in reversed, so its area and moments subtract, its
    edge is a wall the room is measured to, and a centre inside it is outside the
    section (a keyring hole makes a ring). A hole reaching past the mesh's own section
    subtracts that part too, which only makes the section smaller: a stricter fit. A
    stretch of a hole lying outside the mesh's own section still counts toward
    ``inside`` parity (it crosses one boundary), which only matters if a negative part
    pokes past the surface along the plane."""
    n = unit(normal)
    origin = np.asarray(point, dtype=float)
    found = _segments(mesh, origin, n)
    if found is None:
        return None
    start, end = found
    for hole in holes:
        cut = _segments(hole, origin, n)
        if cut is not None:
            start, end = np.concatenate([start, cut[1]]), np.concatenate([end, cut[0]])
    u, v = _basis(n)
    a = np.stack([(start - origin) @ u, (start - origin) @ v], axis=1)
    b = np.stack([(end - origin) @ u, (end - origin) @ v], axis=1)
    cross = a[:, 0] * b[:, 1] - b[:, 0] * a[:, 1]
    area = float(cross.sum() / 2)
    if area <= 1e-9:
        return None
    c = ((a + b) * cross[:, None]).sum(axis=0) / (6 * area)
    ab = b - a
    t = np.clip(((c - a) * ab).sum(axis=1) / np.maximum((ab * ab).sum(axis=1), 1e-18), 0.0, 1.0)
    room = float(np.linalg.norm(c - (a + t[:, None] * ab), axis=1).min())
    straddles = (a[:, 1] > c[1]) != (b[:, 1] > c[1])
    with np.errstate(divide="ignore", invalid="ignore"):
        x = a[:, 0] + (c[1] - a[:, 1]) * ab[:, 0] / ab[:, 1]
    inside = bool(np.count_nonzero(straddles & (x > c[0])) % 2)
    return Section(area, origin + c[0] * u + c[1] * v, room, inside)


def kept(points: np.ndarray, cuts: Sequence[CutSpec]) -> np.ndarray:
    mask = np.ones(len(points), dtype=bool)
    for point, normal, keep in cuts:
        side = (points - np.asarray(point, dtype=float)) @ unit(normal)
        mask &= side >= -1e-9 if keep == "above" else side <= 1e-9
    return points[mask]


def cut_points(mesh: trimesh.Trimesh, cuts: Sequence[CutSpec]) -> np.ndarray:
    points = [kept(np.asarray(mesh.vertices, dtype=float), cuts)]
    for index, (point, normal, _) in enumerate(cuts):
        found = _segments(mesh, np.asarray(point, dtype=float), unit(normal))
        if found is not None:
            others = [c for i, c in enumerate(cuts) if i != index]
            points.append(kept(np.concatenate(found), others))
    return np.concatenate(points)
