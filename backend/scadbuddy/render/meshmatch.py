"""Matching a rendered mesh back to the meshes it came from (#2146, spec 2026-10-11
§7, §9): points to points within a tolerance, and points to the nearest triangle.

numpy only: trimesh's proximity queries need rtree, which is not installed (measured
2026-10-13 in backend/.venv, trimesh 5.1.1: `trimesh.proximity.closest_point` raises
``ModuleNotFoundError: rtree``)."""

from __future__ import annotations

import itertools

import numpy as np
import numpy.typing as npt

FloatArray = npt.NDArray[np.float64]
IntArray = npt.NDArray[np.int64]

#: Bits of a cell's key per axis: three fit one int64.
_BITS = 21
_CELLS = 1 << _BITS
#: The smallest cell, mm.
MIN_CELL = 1e-3
#: Point and triangle pairs `nearest_triangles` compares at once (memory, not time).
_CHUNK_PAIRS = 1 << 22


def match_points(source: FloatArray, query: FloatArray, tol: float) -> IntArray:
    """For each ``query`` point, the index of the nearest ``source`` point at most
    ``tol`` away, or -1. Points are binned into cells no smaller than ``tol``, so a
    match is in the point's own cell or one of its 26 neighbours."""
    found = np.full(len(query), -1, dtype=np.int64)
    if len(source) == 0 or len(query) == 0:
        return found
    low = np.minimum(source.min(axis=0), query.min(axis=0)) - 2 * tol
    span = float((np.maximum(source.max(axis=0), query.max(axis=0)) - low).max()) + 2 * tol
    cell = max(MIN_CELL, tol, span / (_CELLS - 2))

    def cells(points: FloatArray) -> IntArray:
        return np.floor((points - low) / cell).astype(np.int64)

    def key(c: IntArray) -> IntArray:
        return (c[:, 0] << (2 * _BITS)) | (c[:, 1] << _BITS) | c[:, 2]

    keys = key(cells(source))
    order = np.argsort(keys, kind="stable")
    ordered = keys[order]
    most = int(np.unique(ordered, return_counts=True)[1].max())
    best = np.full(len(query), np.inf)
    home = cells(query)
    for offset in itertools.product((-1, 0, 1), repeat=3):
        shifted = home + np.asarray(offset, dtype=np.int64)
        valid = ((shifted >= 0) & (shifted < _CELLS)).all(axis=1)
        wanted = key(np.clip(shifted, 0, _CELLS - 1))
        start = np.searchsorted(ordered, wanted)
        for step in range(most):
            at = start + step
            ok = valid & (at < len(ordered))
            ok[ok] = ordered[at[ok]] == wanted[ok]
            rows = np.flatnonzero(ok)
            if len(rows) == 0:
                break
            candidates = order[at[rows]]
            distance = np.linalg.norm(source[candidates] - query[rows], axis=1)
            better = (distance <= tol) & (distance < best[rows])
            best[rows[better]] = distance[better]
            found[rows[better]] = candidates[better]
    return found


def point_triangle_distance(points: FloatArray, triangles: FloatArray) -> FloatArray:
    """Row by row, the distance from ``points[i]`` to the triangle ``triangles[i]``
    (shape (n, 3, 3)): Ericson, *Real-Time Collision Detection* §5.1.5
    (ClosestPtPointTriangle), by Voronoi region, vectorised. A degenerate triangle is
    measured to its nearest corner."""
    a, b, c = triangles[:, 0], triangles[:, 1], triangles[:, 2]
    ab, ac = b - a, c - a

    def dot(x: FloatArray, y: FloatArray) -> FloatArray:
        return np.asarray(np.einsum("ij,ij->i", x, y), dtype=np.float64)

    d1, d2 = dot(ab, points - a), dot(ac, points - a)
    d3, d4 = dot(ab, points - b), dot(ac, points - b)
    d5, d6 = dot(ab, points - c), dot(ac, points - c)
    va, vb, vc = d3 * d6 - d5 * d4, d5 * d2 - d1 * d6, d1 * d4 - d3 * d2
    closest = np.empty_like(points)
    done = np.zeros(len(points), dtype=bool)

    def take(mask: npt.NDArray[np.bool_], value: FloatArray) -> None:
        pick = mask & ~done
        closest[pick] = value[pick]
        done[pick] = True

    with np.errstate(divide="ignore", invalid="ignore"):
        take((d1 <= 0) & (d2 <= 0), a)
        take((d3 >= 0) & (d4 <= d3), b)
        take((d6 >= 0) & (d5 <= d6), c)
        take((vc <= 0) & (d1 >= 0) & (d3 <= 0), a + (d1 / (d1 - d3))[:, None] * ab)
        take((vb <= 0) & (d2 >= 0) & (d6 <= 0), a + (d2 / (d2 - d6))[:, None] * ac)
        edge = (d4 - d3) / ((d4 - d3) + (d5 - d6))
        take((va <= 0) & (d4 - d3 >= 0) & (d5 - d6 >= 0), b + edge[:, None] * (c - b))
        total = va + vb + vc
        inside = a + (vb / total)[:, None] * ab + (vc / total)[:, None] * ac
        take(np.ones(len(points), dtype=bool), inside)
        distance = np.linalg.norm(closest - points, axis=1)
    bad = ~np.isfinite(distance)
    if bad.any():
        corners = np.linalg.norm(triangles[bad] - points[bad][:, None, :], axis=2)
        distance[bad] = corners.min(axis=1)
    return np.asarray(distance, dtype=np.float64)


def nearest_triangles(
    points: FloatArray, triangles: FloatArray, tol: float
) -> tuple[IntArray, FloatArray]:
    """For each point, the index of the nearest of ``triangles`` (shape (m, 3, 3)) at
    most ``tol`` away, or -1, and that distance (inf for -1). Only triangles whose box,
    grown by ``tol``, holds the point are measured."""
    index = np.full(len(points), -1, dtype=np.int64)
    distance = np.full(len(points), np.inf)
    if len(points) == 0 or len(triangles) == 0:
        return index, distance
    low = triangles.min(axis=1) - tol
    high = triangles.max(axis=1) + tol
    chunk = max(1, _CHUNK_PAIRS // len(triangles))
    for start in range(0, len(points), chunk):
        block = points[start : start + chunk]
        near = ((block[:, None, :] >= low[None]) & (block[:, None, :] <= high[None])).all(axis=2)
        rows, cols = np.nonzero(near)
        if len(rows) == 0:
            continue
        measured = point_triangle_distance(block[rows], triangles[cols])
        keep = measured <= tol
        rows, cols, measured = rows[keep], cols[keep], measured[keep]
        if len(rows) == 0:
            continue
        order = np.lexsort((measured, rows))
        rows, cols, measured = rows[order], cols[order], measured[order]
        first = np.r_[True, rows[1:] != rows[:-1]]
        index[start + rows[first]] = cols[first]
        distance[start + rows[first]] = measured[first]
    return index, distance
