"""Printability measurements of a rendered model, in pure numpy (#284).

This is the groundwork the print analyzers stand on: the numbers a check such as
"open or non-manifold mesh" or "tall and narrow on a small footprint" is decided
from. It measures; it does not judge. Severity, suppression and fixes belong to
the analyzer layer.

What it reads
    The *closed per-colour solids* ScadBuddy writes into the output's 3MF
    (``3D/Objects/object_<extruder>.model``), never the preview split. Splitting
    OpenSCAD's single 3MF object by per-triangle material leaves an open seam
    wherever two colours touch, by design (CLAUDE.md, "Verified OpenSCAD facts"),
    so edge checks on it would be false positives. A colour whose solid render
    failed falls back to its split mesh in the 3MF too; the render records that as
    a warning, :func:`split_colours` reads it back, and such a part is reported
    with ``source="split"`` and no edge check (``edges_checked=False``).

Frame
    Everything is in the model's own OpenSCAD coordinates (millimetres, Z up), the
    same frame as the preview GLB before its Y-up turn, so a location can be
    highlighted there directly. The build plate is taken to be the plane of the
    lowest vertex of the whole model (``bed_z``): the 3MF's build item only
    translates the model, it never rotates it, so this is the face it prints on.

Methods and their limits
    *Edges.* Vertices are merged by exact coordinate first, so two triangles that
    meet at the same point without sharing a vertex index still count as
    connected. An edge used by one triangle is *open*; by three or more it is
    *non-manifold* (typically two solids touching along a line or a
    zero-thickness wall). Winding consistency is not checked.

    *Overhangs.* A face's overhang angle is how far its outward normal tips below
    horizontal: 0 deg for a vertical wall, 90 deg for a flat ceiling. Buckets are
    cumulative ("45 deg or more" includes the 60 and 75 deg faces). Faces lying on
    the bed are bed contact, not overhang. Nothing here knows whether an overhang
    is supported from below (a bridge between two pillars looks like any other
    90 deg ceiling), so the areas are an upper bound on what needs support.

    *Bed contact.* Downward-facing faces (within 1 deg of straight down) whose
    three vertices all lie within :data:`BED_TOLERANCE_MM` of ``bed_z``. The
    *footprint* is the XY box of those faces, and ``height_to_base_ratio`` is the
    model's height over the footprint's shorter side: a crude tip-over signal that
    ignores the contact's actual shape (an L-shaped foot counts as its box).

    *Thinnest wall.* A ray cast from the centroid of each sampled face straight
    inwards (against its normal) to the first other triangle of the same part;
    the shortest such distance is the estimate. At most :data:`WALL_SAMPLES`
    faces per part are sampled, fewer on a part so large that the rays times its
    triangles would pass :data:`WALL_PAIR_BUDGET` (never fewer than
    :data:`WALL_MIN_SAMPLES`), so the cost is bounded by the budget rather than
    growing with the square of the mesh. Samples are spread evenly by index,
    which favours finely tessellated regions -- curves and text, where thin walls
    usually are. It measures
    thickness *perpendicular to a face*, so it can miss a thin region no sampled
    face sits on, and it over-reads where a ray escapes through an open mesh
    (split parts). Treat it as an estimate, not a guarantee.

    *Smallest feature.* Each part is broken into connected pieces (islands); the
    smallest is the one whose bounding box has the shortest side. That catches a
    separate dot or a disconnected letter, not a thin fin on a larger body (the
    wall estimate covers that). It is measured on the mesh as rendered and knows
    nothing of the chosen nozzle or layer height.

Not covered yet (see #284): bridges, text height at the chosen detail, and any
mapping of a location back to ``.scad`` source lines.
"""

from __future__ import annotations

import json
import math
import xml.etree.ElementTree as ET
import zipfile
import zlib
from collections.abc import Collection, Sequence
from pathlib import Path
from typing import Literal

import numpy as np
import numpy.typing as npt
import trimesh
from pydantic import BaseModel, Field

from scadbuddy.render.bambu3mf import CORE_NS, MODEL_SETTINGS_NAME, PROJECT_SETTINGS_NAME
from scadbuddy.render.glb import BoundingBox
from scadbuddy.render.jobs import UNCOLOURED_WARNING
from scadbuddy.render.solids import SPLIT_FALLBACK
from scadbuddy.render.split import ColourPart, normalise_colour

#: Bumped whenever a measurement changes meaning, so a cached analysis written by
#: an older version is computed again rather than served.
ANALYSIS_VERSION = 1

#: Overhang buckets, in degrees below horizontal.
OVERHANG_ANGLES = (45, 60, 75)
#: How far above the lowest vertex a face may sit and still count as on the bed.
BED_TOLERANCE_MM = 0.01
#: A face counts as facing straight down when its normal is within 1 degree of -Z.
_DOWN_COSINE = math.cos(math.radians(1.0))
#: Faces per part the wall estimate casts a ray from.
WALL_SAMPLES = 1024
#: Upper bound on ray x triangle pairs the wall estimate tests per part. A part with
#: more triangles than ``WALL_PAIR_BUDGET / WALL_SAMPLES`` casts fewer rays, down to
#: :data:`WALL_MIN_SAMPLES`, so the cost stops growing with the square of the mesh.
#: At the budget this is roughly two seconds of numpy on one core.
WALL_PAIR_BUDGET = 1 << 27
#: The fewest rays a part casts, however large it is.
WALL_MIN_SAMPLES = 64
#: Hits nearer than this are the ray's own face or a neighbour it touches.
_RAY_EPSILON = 1e-5
#: Upper bound on ray x triangle pairs the bounding-sphere pass holds at once.
_RAY_BATCH = 1 << 22
#: Located edges returned; the counts are always complete.
MAX_REPORTED_EDGES = 500

FloatArray = npt.NDArray[np.float64]
IntArray = npt.NDArray[np.int64]
Point = tuple[float, float, float]
PartSource = Literal["solid", "split"]
EdgeKind = Literal["open", "non_manifold"]


class MeshEdge(BaseModel):
    """One defective edge, located so the preview can draw it."""

    kind: EdgeKind
    #: 1-based extruder index of the part, as in ``OutputMeta.parts``.
    part: int
    colour: str
    start: Point
    end: Point
    #: How many triangles use the edge: 1 when open, 3 or more when non-manifold.
    faces: int


class PartGeometry(BaseModel):
    part: int
    name: str
    colour: str
    #: ``solid`` for a closed per-colour render; ``split`` when the render fell back
    #: to the preview's split mesh, whose seams are open by design.
    source: PartSource
    triangles: int
    #: ``None`` for a part with no triangles.
    bbox: BoundingBox | None
    edges_checked: bool
    open_edges: int | None = None
    non_manifold_edges: int | None = None
    #: Enclosed volume, only when the part is checked and closed.
    volume_mm3: float | None = None


class OverhangBucket(BaseModel):
    """Faces tipped at least ``min_angle_deg`` below horizontal (cumulative)."""

    min_angle_deg: int
    area_mm2: float
    faces: int
    #: Box around every face in the bucket, or ``None`` when it is empty.
    bbox: BoundingBox | None = None


class WallEstimate(BaseModel):
    thickness_mm: float
    part: int
    colour: str
    #: Centroid of the face the thinnest ray left from.
    at: Point
    #: Faces sampled across all parts.
    samples: int


class FeatureEstimate(BaseModel):
    #: Shortest side of the smallest island's bounding box.
    min_extent_mm: float
    part: int
    colour: str
    bbox: BoundingBox
    #: Islands found across all parts.
    islands: int


class GeometryAnalysis(BaseModel):
    """What :func:`analyze_geometry` measured. See the module docstring for methods."""

    version: int = ANALYSIS_VERSION
    parts: list[PartGeometry]
    bbox: BoundingBox
    #: Z of the build plate in model coordinates: the lowest vertex.
    bed_z: float
    height_mm: float
    bed_contact_area_mm2: float
    footprint: BoundingBox | None = None
    height_to_base_ratio: float | None = None
    overhangs: list[OverhangBucket]
    edges: list[MeshEdge] = Field(default_factory=list)
    #: True when there were more than :data:`MAX_REPORTED_EDGES` to locate.
    edges_truncated: bool = False
    thinnest_wall: WallEstimate | None = None
    smallest_feature: FeatureEstimate | None = None


def _box(low: FloatArray, high: FloatArray) -> BoundingBox:
    return BoundingBox(
        min=(float(low[0]), float(low[1]), float(low[2])),
        max=(float(high[0]), float(high[1]), float(high[2])),
        size=(float(high[0] - low[0]), float(high[1] - low[1]), float(high[2] - low[2])),
    )


def _point(value: FloatArray) -> Point:
    return (float(value[0]), float(value[1]), float(value[2]))


def split_colours(warnings: Sequence[str], colours: Sequence[str]) -> set[str]:
    """The colours whose 3MF part is the open split mesh rather than a closed solid.

    Read back from the render's warnings: uncoloured geometry skips the solid
    renders altogether, and a colour whose solid render failed says so by name.
    """
    if UNCOLOURED_WARNING in warnings:
        return set(colours)
    return {
        colour
        for colour in colours
        if any(w.startswith(f"{colour}:") and w.endswith(SPLIT_FALLBACK) for w in warnings)
    }


class Unreadable3MFError(ValueError):
    """The 3MF cannot be read: not a zip, a corrupt entry, or malformed XML/JSON.

    One type for every way a file on disk can be damaged, so a caller maps them all
    the same way rather than letting ``ET.ParseError`` (a ``SyntaxError``) or a
    ``zlib.error`` escape as a server error.
    """


def parts_from_3mf(path: Path) -> list[ColourPart]:
    """The per-extruder parts of a ScadBuddy 3MF, in extruder order.

    Read from ``3D/Objects/object_<n>.model`` as `bambu3mf.write_bambu_3mf` wrote
    them -- the meshes in model coordinates, without the build item's placement.
    Raises :class:`Unreadable3MFError` when the file is damaged.
    """
    try:
        return _read_parts(path)
    except (zipfile.BadZipFile, ET.ParseError, zlib.error, EOFError, ValueError) as error:
        raise Unreadable3MFError(f"{type(error).__name__}: {error}") from error


def _read_parts(path: Path) -> list[ColourPart]:
    parts: list[ColourPart] = []
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        colours: list[str] = []
        if PROJECT_SETTINGS_NAME in names:
            settings = json.loads(archive.read(PROJECT_SETTINGS_NAME))
            colours = [str(value) for value in settings.get("filament_colour") or []]
        # Object N is extruder N in a one-plate file; a multi-plate one numbers its
        # objects across plates (spec §6.4), so each part's extruder is read from
        # the part list rather than assumed.
        extruders: dict[int, int] = {}
        if MODEL_SETTINGS_NAME in names:
            listing = ET.fromstring(archive.read(MODEL_SETTINGS_NAME))
            for part_node in listing.iter("part"):
                metadata = {m.get("key"): m.get("value") for m in part_node.findall("metadata")}
                part_id, extruder = part_node.get("id") or "", metadata.get("extruder") or ""
                if part_id.isdigit() and extruder.isdigit():
                    extruders[int(part_id)] = int(extruder)
        index = 1
        while (entry := f"3D/Objects/object_{index}.model") in names:
            root = ET.fromstring(archive.read(entry))
            obj = root.find(f".//{{{CORE_NS}}}object")
            vertices_node = root.find(f".//{{{CORE_NS}}}vertices")
            triangles_node = root.find(f".//{{{CORE_NS}}}triangles")
            vertices = np.array(
                [
                    [float(v.get("x", 0)), float(v.get("y", 0)), float(v.get("z", 0))]
                    for v in (vertices_node if vertices_node is not None else [])
                ],
                dtype=np.float64,
            ).reshape(-1, 3)
            faces = np.array(
                [
                    [int(t.get("v1", 0)), int(t.get("v2", 0)), int(t.get("v3", 0))]
                    for t in (triangles_node if triangles_node is not None else [])
                ],
                dtype=np.int64,
            ).reshape(-1, 3)
            name = (obj.get("name") if obj is not None else None) or f"Color {index}"
            number = extruders.get(index, index)
            colour = normalise_colour(colours[number - 1] if 1 <= number <= len(colours) else None)
            parts.append(
                ColourPart(
                    material_index=index,
                    name=name,
                    colour=colour,
                    mesh=trimesh.Trimesh(vertices=vertices, faces=faces, process=False),
                )
            )
            index += 1
    return parts


def _welded(mesh: trimesh.Trimesh) -> tuple[FloatArray, IntArray]:
    """Vertices merged by exact coordinate, and the faces re-indexed onto them."""
    vertices = np.asarray(mesh.vertices, dtype=np.float64).reshape(-1, 3)
    faces = np.asarray(mesh.faces, dtype=np.int64).reshape(-1, 3)
    if len(vertices) == 0:
        return vertices, faces
    unique, inverse = np.unique(vertices, axis=0, return_inverse=True)
    return unique, inverse.reshape(-1)[faces]


def _edge_counts(faces: IntArray) -> tuple[IntArray, IntArray]:
    """Every undirected edge once, and how many triangles use it."""
    edges = np.concatenate([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]])
    edges.sort(axis=1)
    unique, counts = np.unique(edges, axis=0, return_counts=True)
    return unique.astype(np.int64), counts.astype(np.int64)


def _face_geometry(vertices: FloatArray, faces: IntArray) -> tuple[FloatArray, FloatArray]:
    """Unit normals (zero for degenerate faces) and areas."""
    corners = vertices[faces]
    cross = np.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0])
    length = np.linalg.norm(cross, axis=1)
    normals = np.zeros_like(cross)
    nonzero = length > 0
    normals[nonzero] = cross[nonzero] / length[nonzero, None]
    return normals, length / 2.0


def _signed_volume(vertices: FloatArray, faces: IntArray) -> float:
    corners = vertices[faces]
    return float(
        np.einsum("ij,ij->i", corners[:, 0], np.cross(corners[:, 1], corners[:, 2])).sum() / 6.0
    )


def _nearest_hits(
    origins: FloatArray, directions: FloatArray, triangles: FloatArray, skip: IntArray
) -> FloatArray:
    """Distance along each ray to the nearest triangle, ``inf`` if none.

    ``skip[i]`` is the triangle ray ``i`` starts on, which it must not hit.

    Two passes, so a dense mesh stays affordable without a spatial index: every
    ray is first tested against every triangle's bounding sphere, which is two
    matrix products, and only the few pairs that pass go through the exact
    Moller-Trumbore test.
    """
    count = len(origins)
    best = np.full(count, np.inf)
    if count == 0 or len(triangles) == 0:
        return best
    centres = triangles.mean(axis=1)
    radii = np.linalg.norm(triangles - centres[:, None, :], axis=2).max(axis=1) * 1.001 + 1e-6
    radii_sq = radii * radii
    centre_sq = np.einsum("tk,tk->t", centres, centres)
    per_batch = max(1, _RAY_BATCH // len(triangles))
    for start in range(0, count, per_batch):
        stop = min(count, start + per_batch)
        o = origins[start:stop]
        d = directions[start:stop]
        # Where along the ray each centre projects, and its squared distance off it,
        # computed in place: these are the only arrays the size of rays x triangles.
        along = d @ centres.T
        along -= np.einsum("rk,rk->r", o, d)[:, None]
        off_sq = o @ centres.T
        off_sq *= -2.0
        off_sq += centre_sq[None, :]
        off_sq += np.einsum("rk,rk->r", o, o)[:, None]
        near = along >= -radii[None, :]
        np.square(along, out=along)
        off_sq -= along
        near &= off_sq <= radii_sq[None, :]
        rows, cols = np.nonzero(near)
        keep = cols != skip[start + rows]
        rows, cols = rows[keep], cols[keep]
        if len(rows) == 0:
            continue
        ray_o, ray_d = o[rows], d[rows]
        v0 = triangles[cols, 0]
        e1 = triangles[cols, 1] - v0
        e2 = triangles[cols, 2] - v0
        p = np.cross(ray_d, e2)
        det = np.einsum("nk,nk->n", p, e1)
        with np.errstate(divide="ignore", invalid="ignore"):
            inv = 1.0 / det
            s = ray_o - v0
            u = np.einsum("nk,nk->n", s, p) * inv
            q = np.cross(s, e1)
            v = np.einsum("nk,nk->n", ray_d, q) * inv
            t = np.einsum("nk,nk->n", q, e2) * inv
            hit = (
                (np.abs(det) > 1e-12)
                & (u >= -1e-9)
                & (v >= -1e-9)
                & (u + v <= 1 + 1e-9)
                & (t > _RAY_EPSILON)
            )
        np.minimum.at(best, start + rows[hit], t[hit])
    return best


def wall_samples(triangles: int) -> int:
    """How many rays the wall estimate casts on a part of ``triangles`` faces."""
    if triangles <= 0:
        return 0
    return max(WALL_MIN_SAMPLES, min(WALL_SAMPLES, WALL_PAIR_BUDGET // triangles))


def _islands(vertex_count: int, faces: IntArray) -> IntArray:
    """A component label per vertex: vertices joined by an edge share one."""
    labels = np.arange(vertex_count, dtype=np.int64)
    if len(faces) == 0:
        return labels
    a = np.concatenate([faces[:, 0], faces[:, 1], faces[:, 2]])
    b = np.concatenate([faces[:, 1], faces[:, 2], faces[:, 0]])
    while True:
        low = np.minimum(labels[a], labels[b])
        updated = labels.copy()
        np.minimum.at(updated, a, low)
        np.minimum.at(updated, b, low)
        updated = updated[updated]
        if np.array_equal(updated, labels):
            return labels
        labels = updated


def analyze_geometry(
    parts: Sequence[ColourPart], *, split: Collection[str] = ()
) -> GeometryAnalysis:
    """Measure ``parts`` (one mesh per extruder, in extruder order).

    ``split`` names the colours whose mesh is the preview split rather than a closed
    solid; their edges are not checked. See the module docstring for each method.
    """
    if not parts:
        raise ValueError("there is no geometry to analyse")

    welded = [_welded(part.mesh) for part in parts]
    populated = [vertices for vertices, _ in welded if len(vertices)]
    if not populated:
        raise ValueError("there is no geometry to analyse")
    everything = np.concatenate(populated)
    low, high = everything.min(axis=0), everything.max(axis=0)
    bed_z = float(low[2])

    part_results: list[PartGeometry] = []
    edges: list[MeshEdge] = []
    total_edges = 0
    contact_area = 0.0
    contact_low = np.full(3, np.inf)
    contact_high = np.full(3, -np.inf)
    bucket_area = dict.fromkeys(OVERHANG_ANGLES, 0.0)
    bucket_faces = dict.fromkeys(OVERHANG_ANGLES, 0)
    bucket_low = {angle: np.full(3, np.inf) for angle in OVERHANG_ANGLES}
    bucket_high = {angle: np.full(3, -np.inf) for angle in OVERHANG_ANGLES}
    wall: WallEstimate | None = None
    samples = 0
    feature: FeatureEstimate | None = None
    islands = 0

    for number, (part, (vertices, faces)) in enumerate(zip(parts, welded, strict=True), start=1):
        source: PartSource = "split" if part.colour in split else "solid"
        if len(faces) == 0:
            # Still one entry per extruder, so `parts` lines up with the output's
            # colours; there is nothing to check or measure on it.
            part_results.append(
                PartGeometry(
                    part=number,
                    name=part.name,
                    colour=part.colour,
                    source=source,
                    triangles=0,
                    bbox=None,
                    edges_checked=False,
                )
            )
            continue
        normals, areas = _face_geometry(vertices, faces)
        corners = vertices[faces]

        # Edges.
        result = PartGeometry(
            part=number,
            name=part.name,
            colour=part.colour,
            source=source,
            triangles=len(faces),
            bbox=_box(vertices.min(axis=0), vertices.max(axis=0)),
            edges_checked=source == "solid",
        )
        if source == "solid":
            unique_edges, counts = _edge_counts(faces)
            open_mask = counts == 1
            bad_mask = counts > 2
            result.open_edges = int(open_mask.sum())
            result.non_manifold_edges = int(bad_mask.sum())
            if result.open_edges == 0 and result.non_manifold_edges == 0:
                result.volume_mm3 = abs(_signed_volume(vertices, faces))
            checks: tuple[tuple[npt.NDArray[np.bool_], EdgeKind], ...] = (
                (open_mask, "open"),
                (bad_mask, "non_manifold"),
            )
            for mask, kind in checks:
                located = unique_edges[mask]
                total_edges += len(located)
                room = MAX_REPORTED_EDGES - len(edges)
                for (first, second), uses in zip(
                    located[: max(room, 0)], counts[mask][: max(room, 0)], strict=True
                ):
                    edges.append(
                        MeshEdge(
                            kind=kind,
                            part=number,
                            colour=part.colour,
                            start=_point(vertices[first]),
                            end=_point(vertices[second]),
                            faces=int(uses),
                        )
                    )
        part_results.append(result)

        # Bed contact and overhangs.
        on_bed = (corners[:, :, 2] <= bed_z + BED_TOLERANCE_MM).all(axis=1)
        contact = on_bed & (normals[:, 2] <= -_DOWN_COSINE)
        if contact.any():
            contact_area += float(areas[contact].sum())
            points = corners[contact].reshape(-1, 3)
            contact_low = np.minimum(contact_low, points.min(axis=0))
            contact_high = np.maximum(contact_high, points.max(axis=0))
        angle = np.degrees(np.arcsin(np.clip(-normals[:, 2], -1.0, 1.0)))
        for threshold in OVERHANG_ANGLES:
            selected = (angle >= threshold - 1e-9) & ~on_bed & (areas > 0)
            if selected.any():
                bucket_area[threshold] += float(areas[selected].sum())
                bucket_faces[threshold] += int(selected.sum())
                points = corners[selected].reshape(-1, 3)
                bucket_low[threshold] = np.minimum(bucket_low[threshold], points.min(axis=0))
                bucket_high[threshold] = np.maximum(bucket_high[threshold], points.max(axis=0))

        # Thinnest wall.
        candidates = np.flatnonzero(areas > 0)
        rays = wall_samples(len(faces))
        if len(candidates) > rays:
            candidates = candidates[
                np.linspace(0, len(candidates) - 1, rays).round().astype(np.int64)
            ]
        if len(candidates):
            origins = corners[candidates].mean(axis=1)
            distances = _nearest_hits(origins, -normals[candidates], corners, candidates)
            samples += len(candidates)
            finite = np.isfinite(distances)
            if finite.any():
                best = int(np.argmin(np.where(finite, distances, np.inf)))
                thickness = float(distances[best])
                if wall is None or thickness < wall.thickness_mm:
                    wall = WallEstimate(
                        thickness_mm=thickness,
                        part=number,
                        colour=part.colour,
                        at=_point(origins[best]),
                        samples=0,
                    )

        # Smallest island.
        labels = _islands(len(vertices), faces)
        used = np.unique(faces)
        order = np.argsort(labels[used], kind="stable")
        sorted_labels = labels[used][order]
        points = vertices[used][order]
        starts = np.flatnonzero(np.r_[True, sorted_labels[1:] != sorted_labels[:-1]])
        island_lows = np.minimum.reduceat(points, starts, axis=0)
        island_highs = np.maximum.reduceat(points, starts, axis=0)
        extents = (island_highs - island_lows).min(axis=1)
        islands += len(starts)
        smallest = int(np.argmin(extents))
        if feature is None or float(extents[smallest]) < feature.min_extent_mm:
            feature = FeatureEstimate(
                min_extent_mm=float(extents[smallest]),
                part=number,
                colour=part.colour,
                bbox=_box(island_lows[smallest], island_highs[smallest]),
                islands=0,
            )

    footprint: BoundingBox | None = None
    ratio: float | None = None
    height = float(high[2] - low[2])
    if contact_area > 0:
        footprint = _box(contact_low, contact_high)
        base = min(footprint.size[0], footprint.size[1])
        ratio = height / base if base > 0 else None

    if wall is not None:
        wall.samples = samples
    if feature is not None:
        feature.islands = islands

    return GeometryAnalysis(
        parts=part_results,
        bbox=_box(low, high),
        bed_z=bed_z,
        height_mm=height,
        bed_contact_area_mm2=contact_area,
        footprint=footprint,
        height_to_base_ratio=ratio,
        overhangs=[
            OverhangBucket(
                min_angle_deg=threshold,
                area_mm2=bucket_area[threshold],
                faces=bucket_faces[threshold],
                bbox=(
                    _box(bucket_low[threshold], bucket_high[threshold])
                    if bucket_faces[threshold]
                    else None
                ),
            )
            for threshold in OVERHANG_ANGLES
        ],
        edges=edges,
        edges_truncated=total_edges > len(edges),
        thinnest_wall=wall,
        smallest_feature=feature,
    )


def analyze_3mf(path: Path, *, warnings: Sequence[str] = ()) -> GeometryAnalysis:
    """:func:`analyze_geometry` over a ScadBuddy 3MF, with the render's warnings
    saying which parts are split fallbacks."""
    parts = parts_from_3mf(path)
    return analyze_geometry(parts, split=split_colours(warnings, [part.colour for part in parts]))
