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
from itertools import pairwise
from typing import Literal

import numpy as np
import trimesh

from scadbuddy.meshmodel.frames import unit

CutSpec = tuple[Sequence[float], Sequence[float], Literal["above", "below"]]


#: Twice an object's 64-part cap: more open chains than this is no closed mesh.
MAX_OPEN_CHAINS = 128


class OpenSectionError(ValueError):
    """A section whose boundary does not close: the mesh has a hole in it."""


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


def _encloses(a: np.ndarray, b: np.ndarray, c: np.ndarray) -> bool:
    """Whether ``c`` is inside the 2D segments ``a``->``b``: a ray in +x crosses an odd
    number of them."""
    ab = b - a
    straddles = (a[:, 1] > c[1]) != (b[:, 1] > c[1])
    with np.errstate(divide="ignore", invalid="ignore"):
        x = a[:, 0] + (c[1] - a[:, 1]) * ab[:, 0] / ab[:, 1]
    return bool(np.count_nonzero(straddles & (x > c[0])) % 2)


def _clip(
    a: np.ndarray,
    b: np.ndarray,
    origin: np.ndarray,
    u: np.ndarray,
    v: np.ndarray,
    cut: CutSpec,
) -> tuple[np.ndarray, np.ndarray]:
    """The 2D boundary ``a``->``b`` (interior on the left) cut to ``cut``'s kept side:
    segments outside are dropped, crossing ones trimmed at the cut line, and the line
    closed between each exit and the next entry along it, so the boundary still
    encloses the kept region."""
    cut_point, cut_normal, keep = cut
    n = unit(cut_normal)
    sign = 1.0 if keep == "above" else -1.0
    gu, gv = sign * float(u @ n), sign * float(v @ n)
    base = sign * float((origin - np.asarray(cut_point, dtype=float)) @ n)
    if np.hypot(gu, gv) < 1e-9:  # the cut plane is parallel to the section's
        return (a, b) if base >= -1e-9 else (a[:0], b[:0])
    fa, fb = base + a @ (gu, gv), base + b @ (gu, gv)
    in_a, in_b = fa >= -1e-9, fb >= -1e-9
    start, end = [a[in_a & in_b]], [b[in_a & in_b]]
    along = np.array([gv, -gu]) / np.hypot(gu, gv)
    events: list[tuple[float, int, np.ndarray]] = []  # (position, 0 exit / 1 enter, point)
    for i in np.flatnonzero(in_a != in_b):
        t = fa[i] / (fa[i] - fb[i])
        at = a[i] + t * (b[i] - a[i])
        if in_a[i]:
            start.append(a[i : i + 1])
            end.append(at[None, :])
            events.append((float(at @ along), 0, at))
        else:
            start.append(at[None, :])
            end.append(b[i : i + 1])
            events.append((float(at @ along), 1, at))
    # Close the line over each stretch between neighbouring crossings, as many times as
    # exits outnumber entries so far (parts that touch or overlap cross at the same
    # places, and a tie in either order then adds nothing). Along `along`, the kept
    # region is on the left.
    events.sort(key=lambda e: e[0])
    open_chains = 0
    for first, second in pairwise(events):
        open_chains += 1 if first[1] == 0 else -1
        if abs(open_chains) > MAX_OPEN_CHAINS:
            raise OpenSectionError("the section is not closed")
        for _ in range(abs(open_chains)):
            from_, to = (first[2], second[2]) if open_chains > 0 else (second[2], first[2])
            start.append(from_[None, :])
            end.append(to[None, :])
    return np.concatenate(start), np.concatenate(end)


def measure(
    mesh: trimesh.Trimesh,
    point: Sequence[float],
    normal: Sequence[float],
    holes: Sequence[trimesh.Trimesh] = (),
    cuts: Sequence[CutSpec] = (),
) -> Section | None:
    """``mesh``'s section at the plane, less each of ``holes`` (an object's negative
    parts). A hole's boundary goes in reversed, so its area and moments subtract, its
    edge is a wall the room is measured to, and a centre inside it is outside the
    section (a keyring hole makes a ring). A hole reaching past the mesh's own section
    subtracts that part too, which only makes the section smaller: a stricter fit. A
    stretch of a hole lying outside the mesh's own section still counts toward
    ``inside`` parity (it crosses one boundary), which only matters if a negative part
    pokes past the surface along the plane.

    ``cuts`` (an object's earlier cuts, in the mesh's frame) clip the section to what
    they keep: the cut line becomes a wall, and a section they remove entirely is
    ``None``."""
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
    for limit in cuts:
        a, b = _clip(a, b, origin, u, v, limit)
    if len(a) == 0:
        return None
    cross = a[:, 0] * b[:, 1] - b[:, 0] * a[:, 1]
    area = float(cross.sum() / 2)
    if area <= 1e-9:
        return None
    c = ((a + b) * cross[:, None]).sum(axis=0) / (6 * area)
    ab = b - a
    t = np.clip(((c - a) * ab).sum(axis=1) / np.maximum((ab * ab).sum(axis=1), 1e-18), 0.0, 1.0)
    room = float(np.linalg.norm(c - (a + t[:, None] * ab), axis=1).min())
    return Section(area, origin + c[0] * u + c[1] * v, room, _encloses(a, b, c))


def kept(points: np.ndarray, cuts: Sequence[CutSpec]) -> np.ndarray:
    mask = np.ones(len(points), dtype=bool)
    for point, normal, keep in cuts:
        side = (points - np.asarray(point, dtype=float)) @ unit(normal)
        mask &= side >= -1e-9 if keep == "above" else side <= 1e-9
    return points[mask]


def cut_points(mesh: trimesh.Trimesh, cuts: Sequence[CutSpec]) -> np.ndarray:
    """The vertices the cut mesh would have: the mesh's own that every cut keeps, each
    cut's section segment ends, each section segment's crossing of another cut's
    plane, and the point three cuts' planes share when it is inside the first's
    section; each kept by every other cut."""
    spots = [(np.asarray(p, dtype=float), unit(n)) for p, n, _ in cuts]

    def others(*skip: int) -> list[CutSpec]:
        return [c for i, c in enumerate(cuts) if i not in skip]

    points = [kept(np.asarray(mesh.vertices, dtype=float), cuts)]
    for i, (origin, n) in enumerate(spots):
        found = _segments(mesh, origin, n)
        if found is None:
            continue
        start, end = found
        points.append(kept(np.concatenate(found), others(i)))
        plane = np.stack(_basis(n), axis=1)
        flat_start, flat_end = (start - origin) @ plane, (end - origin) @ plane
        for j, (other, m) in enumerate(spots):
            if j == i or np.linalg.norm(np.cross(n, m)) < 1e-9:
                continue
            s0, s1 = (start - other) @ m, (end - other) @ m
            crossing = s0 * s1 < 0
            t = s0[crossing] / (s0[crossing] - s1[crossing])
            at = start[crossing] + t[:, None] * (end[crossing] - start[crossing])
            points.append(kept(at, others(i, j)))
            for k in range(j + 1, len(spots)):
                if k == i:
                    continue
                normals = np.stack([n, m, spots[k][1]])
                if abs(np.linalg.det(normals)) < 1e-9:
                    continue
                d = [n @ origin, m @ other, spots[k][1] @ spots[k][0]]
                common = np.linalg.solve(normals, d)
                if _encloses(flat_start, flat_end, (common - origin) @ plane):
                    points.append(kept(common[None, :], others(i, j, k)))
    return np.concatenate(points)


def layer_areas(vertices: np.ndarray, faces: np.ndarray, zs: np.ndarray) -> np.ndarray:
    """The level section's area at each of the ascending heights ``zs`` (weak joints,
    spec 2026-10-11 §9): :func:`measure`'s Green's-theorem sum over the segments each face
    leaves on the plane, oriented the same way, but every plane at once, each face
    visited only for the planes it crosses (``lo <= z < hi``, the side `measure` takes at a
    vertex's own height; a vertex on the plane counts as below it). A section of 1e-9 mm²
    or less is 0, as `measure`'s None is. Measured 2026-10-13: within
    5e-13 mm² of `measure` on the test carrot, and the Carrot Garden's whole profile in
    milliseconds where `measure` per layer took seconds."""
    if np.any(np.diff(zs) < 0):
        raise ValueError("zs must be ascending")
    areas = np.zeros(len(zs))
    if len(faces) == 0 or len(zs) == 0:
        return areas
    corners = vertices[faces]
    heights = corners[:, :, 2]
    first = np.searchsorted(zs, heights.min(axis=1), side="left")
    last = np.searchsorted(zs, heights.max(axis=1), side="left")
    count = np.maximum(last - first, 0)
    total = int(count.sum())
    if total == 0:
        return areas
    face = np.repeat(np.arange(len(faces)), count)
    plane = np.repeat(first, count) + np.arange(total) - np.repeat(np.cumsum(count) - count, count)
    tri = corners[face]
    side = tri[:, :, 2] - zs[plane][:, None]
    starts, ends = np.array([0, 1, 2]), np.array([1, 2, 0])
    crossing = (side[:, starts] <= 0) != (side[:, ends] <= 0)
    with np.errstate(divide="ignore", invalid="ignore"):
        t = side[:, starts] / (side[:, starts] - side[:, ends])
        points = tri[:, starts] + t[:, :, None] * (tri[:, ends] - tri[:, starts])
    # The two edges the plane crosses, in edge order.
    pick = np.argsort(~crossing, axis=1, kind="stable")[:, :2]
    ends_xy = np.take_along_axis(points, pick[:, :, None], axis=1)[:, :, :2]
    a, b = ends_xy[:, 0], ends_xy[:, 1]
    # As `_segments`: interior on the left by the face's outward normal.
    normal = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    d = b - a
    flip = (-d[:, 1] * normal[:, 0] + d[:, 0] * normal[:, 1]) > 0
    a, b = np.where(flip[:, None], b, a), np.where(flip[:, None], a, b)
    np.add.at(areas, plane, (a[:, 0] * b[:, 1] - b[:, 0] * a[:, 1]) / 2)
    areas[areas <= 1e-9] = 0.0
    return areas
