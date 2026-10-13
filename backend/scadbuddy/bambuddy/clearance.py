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
    ParsedModel,
    _read_capped,
    parse_model,
    plate_settings,
)
from scadbuddy.render.plate import plate_for
from scadbuddy.render.plate_profiles import CLEARANCES


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


def _points(
    archive: zipfile.ZipFile,
    models: dict[str, ParsedModel],
    path: str,
    object_id: str,
    matrix: np.ndarray,
    depth: int = 0,
) -> list[np.ndarray]:
    """The vertices of object ``object_id`` of model file ``path``, placed by ``matrix``,
    its components followed (a few levels deep, as a slicer writes them)."""
    if depth > 4:
        return []
    if path not in models:
        models[path] = parse_model(archive, path)
    model = models[path]
    found = next(
        (obj for obj in model.root.iter(f"{{{CORE_NS}}}object") if obj.get("id") == object_id),
        None,
    )
    if found is None:
        return []
    points: list[np.ndarray] = []
    mesh = found.find(f"{{{CORE_NS}}}mesh")
    if mesh is not None and mesh in model.meshes:
        vertices = np.asarray(model.meshes[mesh].vertices, dtype=float)
        if vertices.size:
            homogeneous = np.hstack([vertices, np.ones((len(vertices), 1))])
            points.append((homogeneous @ matrix)[:, :3])
    for component in found.iter(f"{{{CORE_NS}}}component"):
        target = (component.get(f"{{{PRODUCTION_NS}}}path") or f"/{path}").lstrip("/")
        inner = _transform(component.get("transform")) @ matrix
        points += _points(archive, models, target, component.get("objectid", ""), inner, depth + 1)
    return points


def placed_objects(payload: bytes, plate: int) -> list[PlacedObject]:
    """Every build item on ``plate`` of a 3MF, each where it sits: the objects a
    by-object print finishes one at a time. A file with no plate list is one plate."""
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
        models: dict[str, ParsedModel] = {}
        root = parse_model(archive, ROOT_MODEL_NAME)
        models[ROOT_MODEL_NAME] = root
        placed: list[PlacedObject] = []
        for number, item in enumerate(root.root.iter(f"{{{CORE_NS}}}item"), start=1):
            object_id = item.get("objectid", "")
            if on_plate is not None and object_id not in on_plate:
                continue
            points = _points(
                archive, models, ROOT_MODEL_NAME, object_id, _transform(item.get("transform"))
            )
            if not points:
                continue
            every = np.vstack(points)
            low, high = every.min(axis=0), every.max(axis=0)
            placed.append(
                PlacedObject(
                    object_names.get(object_id) or f"object {number}",
                    (float(low[0]), float(low[1]), float(low[2])),
                    (float(high[0]), float(high[1]), float(high[2])),
                )
            )
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
