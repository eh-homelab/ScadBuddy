"""A library 3MF's objects, as Arrange lays them out again (#1863).

A file ScadBuddy did not render has no manifest, so its objects are read from the 3MF
itself: one object per build item, the items that place one object the same way up
counted together. Each object is its closed meshes, one per filament colour, in the
build item's orientation (where it sits does not matter: Arrange places it).

Colours come from where the slicer that wrote the file keeps them: a Bambu Studio
project (ScadBuddy's own files and a wrapped STL among them) names each part's extruder
in ``Metadata/model_settings.config``, and its colour is that filament's in
``project_settings.config``; any other 3MF colours triangles by its ``basematerials``.

What cannot be read faithfully is refused (:class:`UnreadableObjectsError`) rather than
guessed: a sliced file, a part painted in several colours, a negative part. Modifiers
and support blockers or enforcers print nothing and are dropped, with a note.

The file is untrusted: the archive is bounded as the print path bounds it
(:data:`~scadbuddy.render.bambu3mf.MAX_UNCOMPRESSED_BYTES`), every entry is read
capped, and at most :data:`MAX_OBJECTS` distinct objects are read.
"""

from __future__ import annotations

import io
import json
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from xml.sax.saxutils import quoteattr

import numpy as np
import trimesh

from scadbuddy.render.bambu3mf import (
    _GCODE,
    CORE_NS,
    MAX_SETTINGS_BYTES,
    MAX_UNCOMPRESSED_BYTES,
    MODEL_RELATIONSHIP,
    MODEL_SETTINGS_NAME,
    PRODUCTION_NS,
    PROJECT_SETTINGS_NAME,
    ROOT_MODEL_NAME,
    STL_COLOUR,
    PlateParts,
    _read_capped,
)
from scadbuddy.render.glb import BoundingBox, bounding_box
from scadbuddy.render.jobs import LAYOUT_NAME, PartSource, PlateLayout
from scadbuddy.render.split import ColourPart, normalise_colour

#: The most distinct objects one file is read as: Arrange takes 200 objects a request.
MAX_OBJECTS = 200
#: The one mesh file a library piece keeps, every colour a material of it: the shape of
#: a render's split 3MF, which `PlateLayout.load` reads.
PIECE_MESH_NAME = "object.3mf"

_CORE = f"{{{CORE_NS}}}"
_PATH = f"{{{PRODUCTION_NS}}}path"
_RELS = "_rels/.rels"
_UNITS = {
    "micron": 0.001,
    "millimeter": 1.0,
    "centimeter": 10.0,
    "inch": 25.4,
    "foot": 304.8,
    "meter": 1000.0,
}
#: Parts that print nothing: settings regions and support hints (Bambu Studio's subtypes).
_DROPPED = {"modifier_part", "support_blocker", "support_enforcer"}
#: A component chain deeper than this is not a file any slicer writes.
_MAX_DEPTH = 8


class UnreadableObjectsError(ValueError):
    """The file's objects cannot be read faithfully; the message says why."""


@dataclass(frozen=True)
class ReadObject:
    """One object of the file: its closed meshes per colour, and how many build items
    place it this way up."""

    name: str
    count: int
    parts: list[ColourPart]
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _PartSettings:
    extruder: int | None
    subtype: str


@dataclass(frozen=True)
class _ObjectSettings:
    name: str | None
    extruder: int | None
    parts: dict[str, _PartSettings]


class _Archive:
    """The archive's model files, each parsed once and read within the archive's cap."""

    def __init__(self, archive: zipfile.ZipFile) -> None:
        self.archive = archive
        self.names = set(archive.namelist())
        self.models: dict[str, tuple[ET.Element, dict[str, ET.Element], float]] = {}

    def model(self, name: str) -> tuple[ET.Element, dict[str, ET.Element], float]:
        if name not in self.models:
            if name not in self.names:
                raise UnreadableObjectsError(f"the 3MF names a model file it lacks ({name})")
            root = _parse(_read_capped(self.archive, name, MAX_UNCOMPRESSED_BYTES), name)
            resources = root.find(f"{_CORE}resources")
            objects = {
                obj.get("id", ""): obj
                for obj in (resources if resources is not None else [])
                if obj.tag == f"{_CORE}object"
            }
            scale = _UNITS.get(root.get("unit", "millimeter"), 1.0)
            self.models[name] = (root, objects, scale)
        return self.models[name]


def _parse(data: bytes, name: str) -> ET.Element:
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        raise UnreadableObjectsError(f"{name} is not readable XML") from None


def _matrix(text: str | None) -> np.ndarray:
    """A 3MF transform (row vectors: ``p' = p M + t``) as a 4x4 for column vectors."""
    matrix = np.eye(4)
    if not text:
        return matrix
    try:
        values = [float(v) for v in text.split()]
    except ValueError:
        raise UnreadableObjectsError(f"transform {text!r} is not 12 numbers") from None
    if len(values) != 12:
        raise UnreadableObjectsError(f"transform {text!r} is not 12 numbers")
    matrix[:3, :3] = np.array(values[:9]).reshape(3, 3).T
    matrix[:3, 3] = values[9:]
    return matrix


def _metadata(node: ET.Element) -> dict[str, str]:
    return {m.get("key", ""): m.get("value", "") for m in node.findall("metadata")}


def _extruder(value: str | None) -> int | None:
    try:
        number = int(value) if value else None
    except ValueError:
        return None
    return number if number is not None and number >= 1 else None


def _object_settings(archive: _Archive) -> dict[str, _ObjectSettings] | None:
    """Bambu Studio's per-object and per-part settings, by object id; None when the
    file has none (not a Bambu project)."""
    if MODEL_SETTINGS_NAME not in archive.names:
        return None
    config = _parse(
        _read_capped(archive.archive, MODEL_SETTINGS_NAME, MAX_SETTINGS_BYTES * 16),
        MODEL_SETTINGS_NAME,
    )
    settings: dict[str, _ObjectSettings] = {}
    for obj in config.findall("object"):
        meta = _metadata(obj)
        parts = {
            part.get("id", ""): _PartSettings(
                _extruder(_metadata(part).get("extruder")), part.get("subtype") or "normal_part"
            )
            for part in obj.findall("part")
        }
        settings[obj.get("id", "")] = _ObjectSettings(
            meta.get("name") or None, _extruder(meta.get("extruder")), parts
        )
    return settings


def _filament_colours(archive: _Archive) -> list[str]:
    if PROJECT_SETTINGS_NAME not in archive.names:
        return []
    try:
        settings = json.loads(
            _read_capped(archive.archive, PROJECT_SETTINGS_NAME, MAX_SETTINGS_BYTES)
        )
    except ValueError:
        return []
    colours = settings.get("filament_colour") if isinstance(settings, dict) else None
    if not isinstance(colours, list):
        return []
    return [normalise_colour(c) if isinstance(c, str) and c else STL_COLOUR for c in colours]


def _root_model_name(archive: _Archive) -> str:
    if ROOT_MODEL_NAME in archive.names or _RELS not in archive.names:
        return ROOT_MODEL_NAME
    rels = _parse(_read_capped(archive.archive, _RELS, MAX_SETTINGS_BYTES), _RELS)
    for rel in rels:
        if rel.get("Type") == MODEL_RELATIONSHIP and rel.get("Target"):
            return str(rel.get("Target")).lstrip("/")
    return ROOT_MODEL_NAME


def _materials(root: ET.Element) -> dict[tuple[str, int], str]:
    resources = root.find(f"{_CORE}resources")
    found: dict[tuple[str, int], str] = {}
    for group in resources.iter(f"{_CORE}basematerials") if resources is not None else []:
        for index, base in enumerate(group.findall(f"{_CORE}base")):
            found[(group.get("id", ""), index)] = normalise_colour(base.get("displaycolor"))
    return found


@dataclass
class _Reader:
    archive: _Archive
    #: Bambu Studio's settings, when the file is a Bambu project.
    settings: dict[str, _ObjectSettings] | None
    filaments: list[str]

    def colour_of(self, extruder: int) -> str:
        return self.filaments[extruder - 1] if extruder <= len(self.filaments) else STL_COLOUR

    def meshes(
        self,
        model: str,
        object_id: str,
        matrix: np.ndarray,
        extruder: int | None,
        depth: int = 0,
    ) -> Iterator[tuple[str, np.ndarray, np.ndarray]]:
        """(colour, vertices, faces) of every mesh the object is made of, transformed
        by ``matrix``. ``extruder`` is the Bambu part's, which colours the whole mesh."""
        if depth > _MAX_DEPTH:
            raise UnreadableObjectsError("the 3MF's components nest too deep")
        root, objects, scale = self.archive.model(model)
        obj = objects.get(object_id)
        if obj is None:
            raise UnreadableObjectsError(f"the 3MF names object {object_id} it lacks")
        mesh = obj.find(f"{_CORE}mesh")
        if mesh is not None:
            yield from self._mesh(
                root, obj, mesh, matrix @ np.diag([scale, scale, scale, 1.0]), extruder
            )
            return
        components = obj.find(f"{_CORE}components")
        for component in components if components is not None else []:
            path = (component.get(_PATH) or "").lstrip("/") or model
            yield from self.meshes(
                path,
                component.get("objectid", ""),
                matrix @ _matrix(component.get("transform")),
                extruder,
                depth + 1,
            )

    def _mesh(
        self,
        root: ET.Element,
        obj: ET.Element,
        mesh: ET.Element,
        matrix: np.ndarray,
        extruder: int | None,
    ) -> Iterator[tuple[str, np.ndarray, np.ndarray]]:
        node = mesh.find(f"{_CORE}vertices")
        triangles = mesh.find(f"{_CORE}triangles")
        if node is None or triangles is None or not len(triangles):
            return
        points = np.array(
            [[float(v.get("x", 0)), float(v.get("y", 0)), float(v.get("z", 0))] for v in node],
            dtype=np.float64,
        )
        points = (np.c_[points, np.ones(len(points))] @ matrix.T)[:, :3]
        materials = _materials(root) if extruder is None else {}
        default_pid, default_index = obj.get("pid", ""), int(obj.get("pindex", 0) or 0)
        buckets: dict[str, list[tuple[int, int, int]]] = {}
        for triangle in triangles:
            if triangle.get("paint_color"):
                raise UnreadableObjectsError(
                    f"object {obj.get('name') or obj.get('id')} is painted in several colours,"
                    " which Arrange cannot read"
                )
            if extruder is not None:
                colour = self.colour_of(extruder)
            else:
                key = (
                    triangle.get("pid", default_pid),
                    int(triangle.get("p1", default_index) or 0),
                )
                colour = materials.get(key, STL_COLOUR)
            buckets.setdefault(colour, []).append(
                (int(triangle.get("v1", 0)), int(triangle.get("v2", 0)), int(triangle.get("v3", 0)))
            )
        mirrored = np.linalg.det(matrix[:3, :3]) < 0
        for colour, faces in buckets.items():
            array = np.array(faces, dtype=np.int64)
            if array.size and (array.min() < 0 or array.max() >= len(points)):
                raise UnreadableObjectsError("a triangle names a vertex the mesh lacks")
            yield colour, points, array[:, ::-1] if mirrored else array

    def item(
        self, model: str, object_id: str, matrix: np.ndarray
    ) -> tuple[list[ColourPart], list[str]]:
        """The parts of one build item, joined by colour in the order colours appear,
        and a note for each kind of part left out."""
        _, objects, _ = self.archive.model(model)
        obj = objects.get(object_id)
        if obj is None:
            raise UnreadableObjectsError(
                f"the build places object {object_id}, which the 3MF lacks"
            )
        settings = self.settings.get(object_id) if self.settings is not None else None
        by_colour: dict[str, list[trimesh.Trimesh]] = {}
        notes: list[str] = []

        def add(pieces: Iterator[tuple[str, np.ndarray, np.ndarray]]) -> None:
            for colour, points, faces in pieces:
                part = trimesh.Trimesh(vertices=points, faces=faces, process=False)
                part.remove_unreferenced_vertices()
                by_colour.setdefault(colour, []).append(part)

        components = obj.find(f"{_CORE}components")
        if settings is None or components is None:
            fallback = (settings.extruder or 1) if settings is not None else None
            add(self.meshes(model, object_id, matrix, fallback))
        else:
            for component in components:
                cid = component.get("objectid", "")
                part = settings.parts.get(cid, _PartSettings(None, "normal_part"))
                if part.subtype == "negative_part":
                    raise UnreadableObjectsError(
                        f"object {settings.name or object_id} has a negative part, which"
                        " Arrange cannot cut out"
                    )
                if part.subtype in _DROPPED:
                    note = f"a {part.subtype.replace('_', ' ')} was left out"
                    if note not in notes:
                        notes.append(note)
                    continue
                path = (component.get(_PATH) or "").lstrip("/") or model
                add(
                    self.meshes(
                        path,
                        cid,
                        matrix @ _matrix(component.get("transform")),
                        part.extruder or settings.extruder or 1,
                    )
                )
        parts: list[ColourPart] = []
        for index, (colour, meshes) in enumerate(by_colour.items(), start=1):
            joined = meshes[0] if len(meshes) == 1 else trimesh.util.concatenate(meshes)
            if not isinstance(joined, trimesh.Trimesh) or joined.is_empty:
                continue
            parts.append(ColourPart(index, f"Color {index}", colour, joined))
        return parts, notes


def read_objects(payload: bytes) -> list[ReadObject]:
    """Every object ``payload``'s build places, each with its count, in build order.
    Raises :class:`UnreadableObjectsError` for a file whose objects cannot be read."""
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as zipped:
            return _read(_Archive(zipped))
    except zipfile.BadZipFile:
        raise UnreadableObjectsError("the file is not a 3MF archive") from None
    except (KeyError, ValueError) as error:
        if isinstance(error, UnreadableObjectsError):
            raise
        raise UnreadableObjectsError(
            f"the 3MF could not be read ({type(error).__name__})"
        ) from None


def _read(archive: _Archive) -> list[ReadObject]:
    infos = archive.archive.infolist()
    if any(_GCODE.fullmatch(info.filename) for info in infos):
        raise UnreadableObjectsError("the file is sliced already; print it from Bambuddy")
    if sum(info.file_size for info in infos) > MAX_UNCOMPRESSED_BYTES:
        raise UnreadableObjectsError("the 3MF is too large to read")
    model = _root_model_name(archive)
    root, _, _ = archive.model(model)
    build = root.find(f"{_CORE}build")
    items = [
        item
        for item in (build if build is not None else [])
        if item.tag == f"{_CORE}item" and item.get("printable", "1") != "0"
    ]
    reader = _Reader(archive, _object_settings(archive), _filament_colours(archive))
    groups: dict[tuple[str, str, tuple[float, ...]], tuple[np.ndarray, int]] = {}
    for item in items:
        matrix = _matrix(item.get("transform"))
        path = (item.get(_PATH) or "").lstrip("/") or model
        key = (path, item.get("objectid", ""), tuple(np.round(matrix[:3, :3], 6).ravel()))
        if key in groups:
            groups[key] = (groups[key][0], groups[key][1] + 1)
            continue
        if len(groups) == MAX_OBJECTS:
            raise UnreadableObjectsError(f"the 3MF places more than {MAX_OBJECTS} distinct objects")
        groups[key] = (matrix, 1)
    objects: list[ReadObject] = []
    for (path, object_id, _), (matrix, count) in groups.items():
        parts, notes = reader.item(path, object_id, matrix)
        if not parts:
            continue
        _, found, _ = archive.model(path)
        settings = reader.settings.get(object_id) if reader.settings is not None else None
        name = (
            (settings.name if settings is not None else None)
            or found[object_id].get("name")
            or f"Object {object_id}"
        )
        objects.append(ReadObject(name=name, count=count, parts=parts, notes=notes))
    if not objects:
        raise UnreadableObjectsError("the 3MF places no object with any geometry")
    return objects


def _hex(colour: str) -> str:
    return colour.upper() + "FF" if len(colour) == 7 else colour.upper()


def piece_mesh(parts: list[ColourPart]) -> bytes:
    """The object's colours as one core 3MF, a material each: the shape of a render's
    split 3MF, so `split_by_material` reads it back part by part."""
    bases = "".join(
        f"<base name={quoteattr(part.name)} displaycolor={quoteattr(_hex(part.colour))}/>"
        for part in parts
    )
    vertices: list[str] = []
    triangles: list[str] = []
    offset = 0
    for index, part in enumerate(parts):
        vertices += [
            f'<vertex x="{v[0]!r}" y="{v[1]!r}" z="{v[2]!r}"/>' for v in part.mesh.vertices.tolist()
        ]
        triangles += [
            f'<triangle v1="{a}" v2="{b}" v3="{c}" pid="1" p1="{index}"/>'
            for a, b, c in (part.mesh.faces + offset).tolist()
        ]
        offset += len(part.mesh.vertices)
    model = (
        f'<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xmlns="{CORE_NS}">'
        f'<resources><basematerials id="1">{bases}</basematerials>'
        f'<object id="2" type="model" pid="1" pindex="0"><mesh><vertices>{"".join(vertices)}'
        f"</vertices><triangles>{''.join(triangles)}</triangles></mesh></object></resources>"
        '<build><item objectid="2"/></build></model>'
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(ROOT_MODEL_NAME, model)
    return buffer.getvalue()


def write_piece(directory: Path, obj: ReadObject) -> BoundingBox:
    """Write ``obj`` into ``directory`` as a piece `write_output` places: the mesh file,
    then its ``layout.json`` (last, so a piece with a layout is whole). Returns its box."""
    parts = [
        ColourPart(index, part.name, part.colour, part.mesh) for index, part in enumerate(obj.parts)
    ]
    (directory / PIECE_MESH_NAME).write_bytes(piece_mesh(parts))
    box = bounding_box(parts)
    layout = PlateLayout(
        plates=[PlateParts(tuple(parts), tuple(range(1, len(parts) + 1)))],
        colours=[part.colour for part in parts],
        warnings=[],
        bbox=box,
        sources=[tuple(PartSource(str(PurePosixPath(PIECE_MESH_NAME))) for _ in parts)],
    )
    layout.save(directory / LAYOUT_NAME)
    return box
