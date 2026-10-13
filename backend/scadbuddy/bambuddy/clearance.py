"""Whether a by-object plate's objects clear the toolhead and the gantry (#956).

Printing by object finishes each object before starting the next, so the toolhead
travels past the ones already printed. Bambu Studio refuses to slice such a plate when
two objects sit closer than the printer's ``extruder_clearance_radius``, when more than
one is taller than ``extruder_clearance_height_to_rod`` (only the last may pass under
the gantry), or when one is taller than ``extruder_clearance_height_to_lid``. This says
so before the print, as warnings, and never refuses: Bambuddy's slicer still has the
last word (``docs/superpowers/specs/2026-10-13-declared-objects-design.md`` §4).

An object's footprint here is its bounding box on the bed, which holds Bambu's convex
hull: the check may warn where Bambu Studio would not, never the other way round.
"""

from __future__ import annotations

import io
import math
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from scadbuddy.bambuddy.filaments import FilamentWarning
from scadbuddy.render.bambu3mf import (
    CORE_NS,
    MAX_SETTINGS_BYTES,
    MODEL_SETTINGS_NAME,
    PRODUCTION_NS,
    ROOT_MODEL_NAME,
    ArchiveTooLargeError,
    ParsedModel,
    TooManyTrianglesError,
    _read_capped,
    parse_model,
    plate_settings,
)
from scadbuddy.render.plate import plate_for
from scadbuddy.render.plate_profiles import CLEARANCES
from scadbuddy.render.read_budget import ReadBudget


@dataclass(frozen=True)
class Clearance:
    """A printer model's clearances, mm (``CLEARANCES``)."""

    model: str
    radius: float
    height_to_rod: float
    height_to_lid: float


def clearance_for(model: str | None) -> Clearance | None:
    """The clearances of a printer ``model``, named as :func:`plate_for` names it;
    ``None`` for a model with no profile, which is then not checked."""
    canonical = plate_for(model).model
    if canonical is None or canonical not in CLEARANCES:
        return None
    radius, rod, lid = CLEARANCES[canonical]
    return Clearance(canonical, radius, rod, lid)


@dataclass(frozen=True)
class PlacedObject:
    """One build item of a plate: its name and its box where it sits, mm."""

    name: str
    low: tuple[float, float, float]
    high: tuple[float, float, float]

    @property
    def height(self) -> float:
        return self.high[2] - self.low[2]


def _transform(text: str | None) -> np.ndarray:
    """A 3MF ``transform`` (12 numbers, row-major 4 by 3) as a 4 by 4 matrix for row vectors."""
    matrix = np.eye(4)
    if text:
        values = [float(value) for value in text.split()]
        if len(values) == 12:
            matrix[:, :3] = np.array(values).reshape(4, 3)
    return matrix


class _Bounds:
    """Each object's box in its own frame, read once (#956). An object a file names
    many times through components costs one read, not one per mention, and every
    component followed, triangle parsed and byte inflated counts against one
    :class:`ReadBudget`, as Arrange's reader of an untrusted file spends one."""

    def __init__(self, archive: zipfile.ZipFile, budget: ReadBudget) -> None:
        self.archive = archive
        self.models: dict[str, ParsedModel] = {}
        self.boxes: dict[tuple[str, str], np.ndarray | None] = {}
        self.visits = budget.max_visits
        self.triangles = budget.limit("max_triangles")
        self.bytes = budget.model_bytes()

    def _model(self, path: str) -> ParsedModel:
        if path not in self.models:
            model = parse_model(
                self.archive,
                path,
                self.bytes,
                max_triangles=self.triangles,
                max_vertices=None if self.triangles is None else 3 * self.triangles,
            )
            stored = sum(len(arrays.faces) for arrays in model.meshes.values())
            inflated = self.archive.getinfo(path).file_size
            if self.triangles is not None:
                self.triangles = max(0, self.triangles - stored)
            if self.bytes is not None:
                self.bytes = max(0, self.bytes - inflated)
            self.models[path] = model
        return self.models[path]

    def box(self, path: str, object_id: str, depth: int = 0) -> np.ndarray | None:
        """The 8 corners of object ``object_id`` of ``path``'s box, in its own frame;
        ``None`` when it holds no vertex or the budget ran out."""
        key = (path, object_id)
        if key in self.boxes:
            return self.boxes[key]
        self.boxes[key] = None  # a cycle reads as empty
        self.visits -= 1
        if depth > _MAX_DEPTH or self.visits < 0:
            raise _OverBudgetError
        model = self._model(path)
        found = next(
            (o for o in model.root.iter(f"{{{CORE_NS}}}object") if o.get("id") == object_id),
            None,
        )
        if found is None:
            return None
        points: list[np.ndarray] = []
        mesh = found.find(f"{{{CORE_NS}}}mesh")
        if mesh is not None and mesh in model.meshes:
            vertices = np.asarray(model.meshes[mesh].vertices, dtype=float)
            if vertices.size:
                points.append(vertices.reshape(-1, 3))
        for component in found.iter(f"{{{CORE_NS}}}component"):
            target = (component.get(f"{{{PRODUCTION_NS}}}path") or f"/{path}").lstrip("/")
            inner = self.box(target, component.get("objectid", ""), depth + 1)
            if inner is not None:
                points.append(_moved(inner, _transform(component.get("transform"))))
        if not points:
            return None
        every = np.vstack(points)
        low, high = every.min(axis=0), every.max(axis=0)
        corners = np.array(
            [
                [x, y, z]
                for x in (low[0], high[0])
                for y in (low[1], high[1])
                for z in (low[2], high[2])
            ]
        )
        self.boxes[key] = corners
        return corners


#: How deep components may nest; a slicer writes two levels.
_MAX_DEPTH = 8


class _OverBudgetError(Exception):
    """The file asks for more than the read budget allows: it is not checked."""


def _moved(points: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    homogeneous = np.hstack([points, np.ones((len(points), 1))])
    moved: np.ndarray = (homogeneous @ matrix)[:, :3]
    return moved


def placed_objects(
    payload: bytes, plate: int, budget: ReadBudget | None = None
) -> list[PlacedObject]:
    """Every build item on ``plate`` of a 3MF, each where it sits: the objects a
    by-object print finishes one at a time. A file with no plate list is one plate.

    Each box is the box of its components' boxes, so a rotated component can make it
    larger than the object, never smaller (the module's one-way rule). Raises
    `ValueError` for a file past ``budget`` (default: the default :class:`ReadBudget`).
    """
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = set(archive.namelist())
        object_names: dict[str, str] = {}
        on_plate: set[str] | None = None
        if MODEL_SETTINGS_NAME in names:
            # Capped as every settings read of an untrusted file is.
            config = ET.fromstring(_read_capped(archive, MODEL_SETTINGS_NAME, MAX_SETTINGS_BYTES))
            for obj in config.iter("object"):
                for entry in obj.findall("metadata"):
                    if entry.get("key") == "name" and obj.get("id"):
                        object_names[obj.get("id", "")] = entry.get("value") or ""
            listed = plate_settings(config)
            if listed:
                on_plate = {
                    object_id
                    for each in listed
                    if each.index == plate
                    for object_id in each.object_ids
                }
        bounds = _Bounds(archive, budget or ReadBudget())
        placed: list[PlacedObject] = []
        try:
            root = bounds._model(ROOT_MODEL_NAME)
            for number, item in enumerate(root.root.iter(f"{{{CORE_NS}}}item"), start=1):
                object_id = item.get("objectid", "")
                if on_plate is not None and object_id not in on_plate:
                    continue
                corners = bounds.box(ROOT_MODEL_NAME, object_id)
                if corners is None:
                    continue
                where = _moved(corners, _transform(item.get("transform")))
                low, high = where.min(axis=0), where.max(axis=0)
                placed.append(
                    PlacedObject(
                        object_names.get(object_id) or f"object {number}",
                        (float(low[0]), float(low[1]), float(low[2])),
                        (float(high[0]), float(high[1]), float(high[2])),
                    )
                )
        except (_OverBudgetError, TooManyTrianglesError, ArchiveTooLargeError) as error:
            raise ValueError(f"the 3MF is past the read budget: {error}") from error
    return placed


def _gap(a: PlacedObject, b: PlacedObject) -> float:
    """The distance between two footprints on the bed, 0 when they overlap."""
    dx = max(0.0, b.low[0] - a.high[0], a.low[0] - b.high[0])
    dy = max(0.0, b.low[1] - a.high[1], a.low[1] - b.high[1])
    return math.hypot(dx, dy)


def clearance_warnings(
    objects: Sequence[PlacedObject], clearance: Clearance, *, plate: int | None = None
) -> list[FilamentWarning]:
    """What a by-object print of ``objects`` would hit, as warnings (never refusals).
    One object, or none, clears everything."""
    if len(objects) < 2:
        return []
    where = f"Plate {plate}: " if plate is not None else ""
    warnings: list[FilamentWarning] = []
    close = [
        (a, b, gap)
        for index, a in enumerate(objects)
        for b in objects[index + 1 :]
        if (gap := _gap(a, b)) < clearance.radius
    ]
    if close:
        a, b, gap = min(close, key=lambda pair: pair[2])
        more = f" (and {len(close) - 1} more pairs)" if len(close) > 1 else ""
        warnings.append(
            FilamentWarning(
                kind="sequence-clearance",
                message=(
                    f"{where}printing by object, {a.name} and {b.name} are {gap:.0f} mm "
                    f"apart{more}, and the toolhead needs {clearance.radius:g} mm around "
                    f"each object on a {clearance.model}. Move them apart, or print by layer."
                ),
            )
        )
    tall = [obj for obj in objects if obj.height > clearance.height_to_rod]
    if len(tall) > 1:
        warnings.append(
            FilamentWarning(
                kind="sequence-clearance",
                message=(
                    f"{where}printing by object, only one object may be taller than "
                    f"{clearance.height_to_rod:g} mm (the gantry), and {len(tall)} are: "
                    f"{', '.join(obj.name for obj in tall)}. Print by layer, or keep one "
                    "tall object."
                ),
            )
        )
    for obj in objects:
        if obj.height > clearance.height_to_lid:
            warnings.append(
                FilamentWarning(
                    kind="sequence-clearance",
                    message=(
                        f"{where}printing by object, {obj.name} is {obj.height:.0f} mm "
                        f"tall, past the {clearance.height_to_lid:g} mm the toolhead "
                        "clears under the lid. Print by layer."
                    ),
                )
            )
    return warnings
