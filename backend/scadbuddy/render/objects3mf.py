"""A library 3MF's objects, as Arrange lays them out again (#1863).

A file ScadBuddy did not render has no manifest, so its objects are read from the 3MF
itself: one object per build item, the items that place one object the same way up
counted together. Each object is its closed meshes, one per filament colour, in the
build item's orientation (where it sits does not matter: Arrange places it).

Colours come from where the slicer that wrote the file keeps them: a Bambu Studio
project (ScadBuddy's own files and a wrapped STL among them) names each part's extruder
in ``Metadata/model_settings.config``, and its colour is that filament's in
``project_settings.config``; any other 3MF colours triangles by its ``basematerials``.

A part painted in several colours in Bambu Studio (``paint_color``, #1965) is one part
of its own, its painting carried as it is (:class:`~scadbuddy.render.split.Paint`):
where each colour falls on it is the slicer's to work out again, as it was for the file.

What cannot be read faithfully is refused (:class:`UnreadableObjectsError`) rather than
guessed: a sliced file, PrusaSlicer's painting, painting in a file with no Bambu Studio
filaments for it to name, a painted part the file mirrors, a negative part. Modifiers
and support blockers or enforcers print nothing and are dropped, with a note.

The file is untrusted: the archive is bounded as the print path bounds it
(:data:`~scadbuddy.render.bambu3mf.MAX_UNCOMPRESSED_BYTES`), every entry is read
capped, and at most :data:`MAX_OBJECTS` distinct objects are read. Components may
name one object many times, so what the file expands to is bounded too: at most
:data:`MAX_VISITS` objects visited and :data:`MAX_TRIANGLES` triangles produced, however
few bytes asked for them.
"""

from __future__ import annotations

import io
import json
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Callable, Iterator
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
    MeshArrays,
    PlateParts,
    _read_capped,
    parse_model,
)
from scadbuddy.render.geometry import NoSuchPlateError
from scadbuddy.render.glb import BoundingBox, bounding_box
from scadbuddy.render.jobs import LAYOUT_NAME, PartSource, PlateLayout
from scadbuddy.render.paint import PaintCodeError, states
from scadbuddy.render.split import PAINT_ATTRIBUTE, ColourPart, Paint, normalise_colour

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
#: The most objects one read visits, components included: a few KB of components that
#: each name the same object many times would otherwise expand without bound.
MAX_VISITS = 20_000
#: The most triangles one read produces, every instance of a mesh counted: about what a
#: model file at the archive cap can hold, so a real file never reaches it.
MAX_TRIANGLES = 5_000_000
#: The most ``paint_color`` digits one read carries, every instance of a painted mesh
#: counted, as `MAX_TRIANGLES` counts its faces (#1965): each placed copy of a painted
#: mesh carries its codes into its piece and the output. Four digits a face at the
#: triangle cap; a real project averages about two (library file 688: 26,884 digits over
#: 13,873 faces).
MAX_PAINT_DIGITS = 20_000_000


class UnreadableObjectsError(ValueError):
    """The file's objects cannot be read faithfully; the message says why."""


#: One mesh as read: its colour, vertices, faces, and its painting when painted.
type _Piece = tuple[str, np.ndarray, np.ndarray, Paint | None]


def _check_faces(faces: np.ndarray, points: np.ndarray) -> None:
    if faces.size and (faces.min() < 0 or faces.max() >= len(points)):
        raise UnreadableObjectsError("a triangle names a vertex the mesh lacks")


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
        #: Every parsed model's meshes, by ``<mesh>`` element.
        self.meshes: dict[ET.Element, MeshArrays] = {}

    def model(self, name: str) -> tuple[ET.Element, dict[str, ET.Element], float]:
        if name not in self.models:
            if name not in self.names:
                raise UnreadableObjectsError(f"the 3MF names a model file it lacks ({name})")
            try:
                parsed = parse_model(self.archive, name)
            except ET.ParseError:
                raise UnreadableObjectsError(f"{name} is not readable XML") from None
            root = parsed.root
            self.meshes.update(parsed.meshes)
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


def _model_settings(archive: _Archive) -> ET.Element | None:
    """Bambu Studio's ``model_settings.config``; None when the file has none (not a
    Bambu project)."""
    if MODEL_SETTINGS_NAME not in archive.names:
        return None
    return _parse(
        _read_capped(archive.archive, MODEL_SETTINGS_NAME, MAX_SETTINGS_BYTES * 16),
        MODEL_SETTINGS_NAME,
    )


def _object_settings(config: ET.Element | None) -> dict[str, _ObjectSettings] | None:
    """Bambu Studio's per-object and per-part settings, by object id; None when the
    file has none."""
    if config is None:
        return None
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
    visits: int = 0
    triangles: int = 0
    paint_digits: int = 0
    #: Each painted mesh's extruders, decoded once however often it is placed: by the
    #: codes tuple's id, the tuple kept so the id stays its own.
    painted_states: dict[int, tuple[tuple[str, ...], set[int]]] = field(default_factory=dict)

    def _spend(self, visits: int = 0, triangles: int = 0, paint_digits: int = 0) -> None:
        self.visits += visits
        self.triangles += triangles
        self.paint_digits += paint_digits
        if self.visits > MAX_VISITS or self.triangles > MAX_TRIANGLES:
            raise UnreadableObjectsError("the 3MF expands to too many objects or triangles to read")
        if self.paint_digits > MAX_PAINT_DIGITS:
            raise UnreadableObjectsError("the 3MF expands to too much painting to read")

    def colour_of(self, extruder: int) -> str:
        return self.filaments[extruder - 1] if extruder <= len(self.filaments) else STL_COLOUR

    def meshes(
        self,
        model: str,
        object_id: str,
        matrix: np.ndarray,
        extruder: int | None,
        depth: int = 0,
    ) -> Iterator[_Piece]:
        """(colour, vertices, faces, painting) of every mesh the object is made of,
        transformed by ``matrix``. ``extruder`` is the Bambu part's, which colours the
        whole mesh but where it is painted."""
        if depth > _MAX_DEPTH:
            raise UnreadableObjectsError("the 3MF's components nest too deep")
        self._spend(visits=1)
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
    ) -> Iterator[_Piece]:
        arrays = self.archive.meshes[mesh]
        if (
            mesh.find(f"{_CORE}vertices") is None
            or mesh.find(f"{_CORE}triangles") is None
            or not len(arrays.faces)
        ):
            return
        # The budget counts what the arrays already hold: it bounds the work below, not
        # the parse. The archive's size cap (`MAX_UNCOMPRESSED_BYTES`) bounds that.
        self._spend(triangles=len(arrays.faces))
        points = arrays.vertices
        if not np.isfinite(points).all():
            raise UnreadableObjectsError("a vertex of the 3MF is not a finite number")
        points = (np.c_[points, np.ones(len(points))] @ matrix.T)[:, :3]
        name = obj.get("name") or obj.get("id")
        if arrays.prusa_painted:
            raise UnreadableObjectsError(
                f"object {name} is painted in PrusaSlicer, which Arrange cannot read;"
                " paint it in Bambu Studio, or split it into a part per colour"
            )
        mirrored = np.linalg.det(matrix[:3, :3]) < 0
        if arrays.paint is not None:
            yield self._painted(name, points, arrays.faces, arrays.paint, extruder, mirrored)
            return
        buckets: dict[str, np.ndarray] = {}
        if extruder is not None:
            buckets[self.colour_of(extruder)] = arrays.faces
        else:
            materials = _materials(root)
            default_pid, default_index = obj.get("pid", ""), int(obj.get("pindex", 0) or 0)
            pids = (*arrays.pids, default_pid)
            # Each triangle's (pid, p1), a missing one the object's; colours in the order
            # their first triangle comes.
            keys = np.stack(
                [
                    np.where(arrays.pid < 0, len(arrays.pids), arrays.pid),
                    np.where(arrays.p1 < 0, default_index, arrays.p1),
                ],
                axis=1,
            )
            unique, first, inverse = np.unique(keys, axis=0, return_index=True, return_inverse=True)
            by_key = [materials.get((pids[pid], int(p1)), STL_COLOUR) for pid, p1 in unique]
            # A dict, not list.index: a hostile file controls both counts.
            colours = {
                colour: index
                for index, colour in enumerate(
                    dict.fromkeys(by_key[key] for key in np.argsort(first))
                )
            }
            per_triangle = np.array([colours[c] for c in by_key])[inverse.reshape(-1)]
            order = np.argsort(per_triangle, kind="stable")
            ends = np.cumsum(np.bincount(per_triangle, minlength=len(colours)))
            for colour, index in colours.items():
                start = ends[index - 1] if index else 0
                buckets[colour] = arrays.faces[order[start : ends[index]]]
        for colour, array in buckets.items():
            _check_faces(array, points)
            yield colour, points, array[:, ::-1] if mirrored else array, None

    def _painted(
        self,
        name: str | None,
        points: np.ndarray,
        faces: np.ndarray,
        codes: tuple[str, ...],
        extruder: int | None,
        mirrored: bool,
    ) -> _Piece:
        """A painted mesh, whole: its own colour the part's extruder's, its painting
        naming the file's filaments by extruder number (#1965)."""
        if extruder is None or not self.filaments:
            raise UnreadableObjectsError(
                f"object {name} is painted, but the file names no Bambu Studio filaments"
                " for its paint"
            )
        if mirrored:
            # Reversing each face to keep it facing out would move its painting: the
            # code places colour by the face's own corners, in their order.
            raise UnreadableObjectsError(
                f"object {name} is painted and mirrored, which Arrange cannot carry over"
            )
        _check_faces(faces, points)
        # Charged on every placement, as its faces are: each copy carries its codes on.
        self._spend(paint_digits=sum(map(len, codes)))
        if id(codes) not in self.painted_states:
            try:
                found = {state for code in codes if code for state in states(code)}
            except PaintCodeError as error:
                raise UnreadableObjectsError(
                    f"object {name}'s painting cannot be read: {error}"
                ) from None
            self.painted_states[id(codes)] = (codes, found)
        used = self.painted_states[id(codes)][1]
        if used and max(used) > len(self.filaments):
            raise UnreadableObjectsError(
                f"object {name} is painted with extruder {max(used)}, and the file has"
                f" {len(self.filaments)} filaments"
            )
        return self.colour_of(extruder), points, faces, Paint(codes, tuple(self.filaments))

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
        # A painted mesh stays a part of its own: its faces are its painting's (#1965).
        painted: list[tuple[str, trimesh.Trimesh, Paint]] = []
        notes: list[str] = []

        def add(pieces: Iterator[_Piece]) -> None:
            for colour, points, faces, paint in pieces:
                part = trimesh.Trimesh(vertices=points, faces=faces, process=False)
                part.remove_unreferenced_vertices()
                if paint is not None:
                    painted.append((colour, part, paint))
                else:
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
        for colour, mesh, paint in painted:
            index = len(parts) + 1
            parts.append(ColourPart(index, f"Color {index}", colour, mesh, paint))
        return parts, notes


def read_objects(payload: bytes) -> list[ReadObject]:
    """Every object ``payload``'s build places, each with its count, in build order.
    Raises :class:`UnreadableObjectsError` for a file whose objects cannot be read."""
    return _opened(payload, _read)


@dataclass(frozen=True)
class PlateRead:
    """One plate of a file: its parts, one per colour, and how many plates it has."""

    parts: list[ColourPart]
    plates: int


def read_plate_parts(payload: bytes, plate: int = 1) -> PlateRead:
    """The parts of ``payload``'s plate ``plate``, one per colour in the order colours
    appear, placed where the file places them: what a library file's preview and mesh
    checks read, as an output's read its own 3MF (#1753). Plates are Bambu Studio's
    (``model_settings.config``); a file that lists none is one plate. Refused as
    :func:`read_objects` refuses; a plate the file lacks is
    :class:`~scadbuddy.render.geometry.NoSuchPlateError`."""
    return _opened(payload, lambda archive: _read_plate(archive, plate))


def _plates(config: ET.Element | None) -> dict[int, set[tuple[str, int]]]:
    """Each plate's ``(object id, instance)`` pairs: an object's instances are its
    build items, in build order. Empty when the file lists no plates."""
    plates: dict[int, set[tuple[str, int]]] = {}
    for node in config.iter("plate") if config is not None else []:
        index = _extruder(_metadata(node).get("plater_id"))
        if index is None:
            continue
        on = plates.setdefault(index, set())
        for instance in node.findall("model_instance"):
            meta = _metadata(instance)
            number = meta.get("instance_id") or "0"
            on.add((meta.get("object_id", ""), int(number) if number.isdecimal() else 0))
    return plates


def _read_plate(archive: _Archive, plate: int) -> PlateRead:
    model = _root_model_name(archive)
    items = _build_items(archive, model)
    config = _model_settings(archive)
    reader = _Reader(archive, _object_settings(config), _filament_colours(archive))
    plates = _plates(config)
    count = len(plates) or 1
    if (plates and plate not in plates) or (not plates and plate != 1):
        raise NoSuchPlateError(plate, count)
    instances: dict[str, int] = {}
    by_colour: dict[str, list[trimesh.Trimesh]] = {}
    for item in items:
        object_id = item.get("objectid", "")
        instance = instances.get(object_id, 0)
        instances[object_id] = instance + 1
        if plates and (object_id, instance) not in plates[plate]:
            continue
        path = (item.get(_PATH) or "").lstrip("/") or model
        parts, _ = reader.item(path, object_id, _matrix(item.get("transform")))
        for part in parts:
            by_colour.setdefault(part.colour, []).append(part.mesh)
    found: list[ColourPart] = []
    for index, (colour, meshes) in enumerate(by_colour.items(), start=1):
        joined = meshes[0] if len(meshes) == 1 else trimesh.util.concatenate(meshes)
        if isinstance(joined, trimesh.Trimesh):
            found.append(ColourPart(index, f"Color {index}", colour, joined))
    if not found:
        raise UnreadableObjectsError(f"plate {plate} of the 3MF places no object with any geometry")
    return PlateRead(found, count)


def _opened[T](payload: bytes, read: Callable[[_Archive], T]) -> T:
    """``read`` over ``payload``'s archive, every way it can fail an
    :class:`UnreadableObjectsError`."""
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as zipped:
            archive = _Archive(zipped)
            infos = zipped.infolist()
            if any(_GCODE.fullmatch(info.filename) for info in infos):
                raise UnreadableObjectsError("the file is sliced already; print it from Bambuddy")
            if sum(info.file_size for info in infos) > MAX_UNCOMPRESSED_BYTES:
                raise UnreadableObjectsError("the 3MF is too large to read")
            return read(archive)
    except zipfile.BadZipFile:
        raise UnreadableObjectsError("the file is not a 3MF archive") from None
    except (KeyError, ValueError) as error:
        if isinstance(error, UnreadableObjectsError):
            raise
        raise UnreadableObjectsError(
            f"the 3MF could not be read ({type(error).__name__})"
        ) from None


def _build_items(archive: _Archive, model: str) -> list[ET.Element]:
    root, _, _ = archive.model(model)
    build = root.find(f"{_CORE}build")
    return [
        item
        for item in (build if build is not None else [])
        if item.tag == f"{_CORE}item" and item.get("printable", "1") != "0"
    ]


def _read(archive: _Archive) -> list[ReadObject]:
    model = _root_model_name(archive)
    items = _build_items(archive, model)
    reader = _Reader(
        archive, _object_settings(_model_settings(archive)), _filament_colours(archive)
    )
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
    split 3MF, so `split_by_material` reads it back part by part. A painted part's
    triangles keep their ``paint_color``, renumbered to name the materials: each colour
    its painting uses that no part has is a material of no triangle, after the parts'."""
    palette = [part.colour for part in parts]
    for part in parts:
        if part.paint is not None:
            palette += [c for c in part.paint.used() if c.upper() not in map(str.upper, palette)]
    bases = "".join(
        f"<base name={quoteattr(parts[index].name if index < len(parts) else f'Paint {index + 1}')}"
        f" displaycolor={quoteattr(_hex(colour))}/>"
        for index, colour in enumerate(palette)
    )
    vertices: list[str] = []
    triangles: list[str] = []
    offset = 0
    for index, part in enumerate(parts):
        vertices += [
            f'<vertex x="{v[0]!r}" y="{v[1]!r}" z="{v[2]!r}"/>' for v in part.mesh.vertices.tolist()
        ]
        codes = (
            part.paint.renumbered(palette).codes
            if part.paint is not None
            else ("",) * len(part.mesh.faces)
        )
        triangles += [
            f'<triangle v1="{a}" v2="{b}" v3="{c}" pid="1" p1="{index}"'
            + (f' {PAINT_ATTRIBUTE}="{code}"' if code else "")
            + "/>"
            for (a, b, c), code in zip((part.mesh.faces + offset).tolist(), codes, strict=True)
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
        ColourPart(index, part.name, part.colour, part.mesh, part.paint)
        for index, part in enumerate(obj.parts)
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
