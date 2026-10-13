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
#: Point and triangle pairs `nearest_triangles` measures at once (memory, not time).
_CHUNK_PAIRS = 1 << 22
#: Most distinct source points one cell may hold, and most cells one triangle may be
#: registered in (a larger one is checked against every point instead).
CELL_CAP = 64


class NearPairsExceeded(ValueError):  # noqa: N818 (named by the plan)
    """`nearest_triangles` would measure more point and triangle pairs than its budget."""

    def __init__(self, pairs: int, cap: int) -> None:
        super().__init__(f"{pairs} candidate pairs to measure; the limit is {cap}")
        self.pairs = pairs
        self.cap = cap


def match_points(source: FloatArray, query: FloatArray, tol: float) -> IntArray:
    """For each ``query`` point, the index of the nearest ``source`` point at most
    ``tol`` away, or -1. Points are binned into cells no smaller than ``tol``, so a
    match is in the point's own cell or one of its 26 neighbours. Identical source
    points are one point; a cell that still holds more than ``CELL_CAP`` distinct
    points raises ValueError (the work per query point is bounded by it)."""
    found = np.full(len(query), -1, dtype=np.int64)
    if len(source) == 0 or len(query) == 0:
        return found
    source, first = np.unique(source, axis=0, return_index=True)
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
    if most > CELL_CAP:
        raise ValueError(f"{most} distinct points in one cell; the limit is {CELL_CAP}")
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
            found[rows[better]] = first[candidates[better]]
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


def _commit(
    index: IntArray,
    distance: FloatArray,
    points: FloatArray,
    triangles: FloatArray,
    rows: IntArray,
    cols: IntArray,
    tol: float,
) -> None:
    """Measure the pairs (``points[rows[i]]``, ``triangles[cols[i]]``), at most
    ``_CHUNK_PAIRS`` at a time, and keep each point's nearest within ``tol``."""
    for start in range(0, len(rows), _CHUNK_PAIRS):
        r = rows[start : start + _CHUNK_PAIRS]
        c = cols[start : start + _CHUNK_PAIRS]
        measured = point_triangle_distance(points[r], triangles[c])
        keep = measured <= tol
        r, c, measured = r[keep], c[keep], measured[keep]
        if len(r) == 0:
            continue
        order = np.lexsort((measured, r))
        r, c, measured = r[order], c[order], measured[order]
        first = np.r_[True, r[1:] != r[:-1]]
        r, c, measured = r[first], c[first], measured[first]
        better = measured < distance[r]
        index[r[better]] = c[better]
        distance[r[better]] = measured[better]


def _expand(counts: IntArray) -> tuple[IntArray, IntArray]:
    """For ``counts`` [2, 0, 3]: owners [0, 0, 2, 2, 2] and offsets [0, 1, 0, 1, 2]."""
    owners = np.repeat(np.arange(len(counts)), counts)
    starts = np.cumsum(counts) - counts
    return owners, np.arange(int(counts.sum())) - starts[owners]


def nearest_triangles(
    points: FloatArray, triangles: FloatArray, tol: float, *, max_pairs: int = 20_000_000
) -> tuple[IntArray, FloatArray]:
    """For each point, the index of the nearest of ``triangles`` (shape (m, 3, 3)) at
    most ``tol`` away, or -1, and that distance (inf for -1). Triangles are binned in a
    uniform grid (cells of the median box extent, at least ``tol``), each in every cell
    its box grown by ``tol`` covers, so a point's candidates are its own cell's. A
    triangle covering more than ``CELL_CAP`` cells is "large": it is a candidate of
    every point whose grown box holds it. The work is bounded by a pair budget: the
    candidate pairs (each point's cell's triangles, plus the large ones for every
    point) are counted before any is measured, and more than ``max_pairs`` raises
    ``NearPairsExceeded`` (carrying ``pairs`` and ``cap``) instead."""
    index = np.full(len(points), -1, dtype=np.int64)
    distance = np.full(len(points), np.inf)
    if len(points) == 0 or len(triangles) == 0:
        return index, distance
    low = triangles.min(axis=1) - tol
    high = triangles.max(axis=1) + tol
    origin = low.min(axis=0)
    span = float((high.max(axis=0) - origin).max())
    cell = max(float(np.median((high - low).max(axis=1))), tol, span / (_CELLS - 2))
    first = np.floor((low - origin) / cell).astype(np.int64)
    dims = np.floor((high - origin) / cell).astype(np.int64) - first + 1
    counts = dims.prod(axis=1)
    large = np.flatnonzero(counts > CELL_CAP)
    small = np.flatnonzero(counts <= CELL_CAP)

    def key(c: IntArray) -> IntArray:
        return (c[:, 0] << (2 * _BITS)) | (c[:, 1] << _BITS) | c[:, 2]

    owners, k = _expand(counts[small])
    tri = small[owners]
    d = dims[tri]
    step = np.stack([k // (d[:, 1] * d[:, 2]), (k // d[:, 2]) % d[:, 1], k % d[:, 2]], axis=1)
    keys = key(first[tri] + step)
    order = np.argsort(keys, kind="stable")
    keys, tri = keys[order], tri[order]

    home = np.floor((points - origin) / cell).astype(np.int64)
    inside = ((home >= 0) & (home < _CELLS)).all(axis=1)
    wanted = key(np.clip(home, 0, _CELLS - 1))
    begin = np.searchsorted(keys, wanted, side="left")
    end = np.where(inside, np.searchsorted(keys, wanted, side="right"), begin)
    pairs = int((end - begin).sum()) + len(points) * len(large)
    if pairs > max_pairs:
        raise NearPairsExceeded(pairs, max_pairs)
    # Points in groups whose candidate pairs fit one chunk (a lone point may exceed it).
    ends = np.cumsum(end - begin)
    row = 0
    while row < len(points):
        base = ends[row - 1] if row else 0
        stop = max(row + 1, int(np.searchsorted(ends, base + _CHUNK_PAIRS, side="right")))
        stop = min(stop, len(points))
        who, within = _expand((end - begin)[row:stop])
        _commit(
            index, distance, points, triangles, row + who, tri[begin[row:stop][who] + within], tol
        )
        row = stop
    if len(large):
        block = max(1, _CHUNK_PAIRS // len(large))
        for start in range(0, len(points), block):
            chunk = points[start : start + block]
            near = (
                (chunk[:, None, :] >= low[large][None]) & (chunk[:, None, :] <= high[large][None])
            ).all(axis=2)
            rows, cols = np.nonzero(near)
            _commit(index, distance, points, triangles, start + rows, large[cols], tol)
    return index, distance
