# backend/scadbuddy/meshmodel/paint.py
"""Paint transfer (#2146, spec 2026-10-11 §7). OpenSCAD drops painting: its output
triangles carry only material indices. A painted part's codes (``<mesh stem>.paint.json``,
`ingest.py`) are carried here onto the rendered part of its colour, after the render
(`render/jobs.py` `paint_layout`, the render's ``paint`` stage):

1. An output triangle whose three vertices are a source triangle's, each within
   :data:`VERBATIM_MM`, takes that triangle's code verbatim, so painting finer than the
   triangle survives. Its vertices are turned to start where the source's do, since a
   code splits its triangle by vertex position; a match wound the other way (a mirrored
   object) is not verbatim.
2. Any other triangle (a cut face, a retriangulated face, an added shape's) takes the
   colour of the nearest source triangle within :data:`NEAR_MM` when that triangle is
   one colour (`render/paint.py` `uniform_state`), and otherwise the part's own (``""``).

Measured 2026-10-13 with openscad/openscad:dev.2026-10-12 (``--backend=Manifold``) on
Bambuddy library file 688 (``test-fixtures/makerworld/688-carrot-garden.3mf``), its four
objects op-free: all 69,150 output triangles matched a source triangle within 1e-4 mm, so
all 13,873 painted ones were carried; only 87 % fell in the same 1e-4 mm grid cell
(OpenSCAD's 3MF import holds coordinates as 32-bit floats and its export writes 6
decimals), and about two thirds started at another vertex. With the green anchor of
`tests/test_mesh_models_fixtures.py` on the small carrot, every painted triangle clear of
the anchor was carried verbatim (5,631 of 6,319 by the test's mask, 2026-10-14).
`tests/test_mesh_models_paint.py` re-measures both in the image."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
import trimesh

from scadbuddy.core.paths import SOURCE_NAME
from scadbuddy.meshmodel.meshes import mesh_loader
from scadbuddy.meshmodel.scene import (
    SCENE_NAME,
    Part,
    Scene,
    SceneObject,
    load_scene,
    text_sha256,
)
from scadbuddy.meshmodel.shared import MeshOf, transform_matrix
from scadbuddy.render.meshmatch import NearPairsExceeded, match_points, nearest_triangles
from scadbuddy.render.paint import single_code, uniform_state
from scadbuddy.render.schema import ParamValue
from scadbuddy.render.split import ColourPart, Paint

FloatArray = npt.NDArray[np.float64]
IntArray = npt.NDArray[np.int64]

#: An output triangle is its source triangle when every vertex is within this, mm (§7).
VERBATIM_MM = 1e-4
#: How near a source triangle another triangle must be to take its colour, mm (§7).
NEAR_MM = 0.01
#: Most candidate pairs a part's non-verbatim faces may measure; past it they take the
#: part's own colour (`nearest_triangles`' ``max_pairs``).
MAX_NEAR_PAIRS = 20_000_000
PAINT_SUFFIX = ".paint.json"
HAND_EDITED = (
    "model.scad was edited by hand since the scene compiled it, so no part's painting is "
    "carried: each part prints in its own colour"
)
#: What phases 1-2 noted on a painted object (`ingest.py`); dropped now paint is carried.
OLD_PAINT_NOTE = "the paint is not carried into renders or prints until phase 3"


def paint_file(part: Part) -> str:
    """Where a painted part's codes are kept beside its mesh (`ingest.py`)."""
    return part.mesh.removesuffix(".3mf") + PAINT_SUFFIX


def placement(obj: SceneObject) -> tuple[FloatArray, FloatArray]:
    """The object's linear part and translation, mesh frame to world (`frames.py`)."""
    return transform_matrix(obj.transform)


def mirrored(obj: SceneObject) -> bool:
    return float(np.linalg.det(placement(obj)[0])) < 0


def with_params(scene: Scene, params: Mapping[str, ParamValue]) -> Scene:
    """The scene with each exposed number at the render's value for it (a number only;
    a parameter the render does not set keeps the scene's), so a part is looked for
    where it rendered."""

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            if set(node) == {"expose", "value"}:
                given = params.get(node["expose"])
                if isinstance(given, int | float) and not isinstance(given, bool):
                    return {"expose": node["expose"], "value": float(given)}
                return node
            return {key: walk(item) for key, item in node.items()}
        if isinstance(node, list):
            return [walk(item) for item in node]
        return node

    return Scene.model_validate(walk(scene.model_dump(mode="json")))


def paint_warnings(scene: Scene) -> list[str]:
    """Where an object's painting cannot be carried whole: a mirrored object's triangles
    render wound the other way, so none is its source's verbatim (rule 1)."""
    return [
        f"{obj.id} ({obj.name}) is mirrored, so its painting is carried only where a whole "
        "triangle is one colour; finer painting prints in the part's own colour"
        for obj in scene.objects
        if mirrored(obj) and any(p.painted and not p.negative for p in obj.parts)
    ]


def scene_notes(scene: Scene) -> list[str]:
    """The notes `get_scene` shows: the scene's own, less phases 1-2's "not carried until
    phase 3", and the paint warnings."""
    return [note for note in scene.notes if OLD_PAINT_NOTE not in note] + paint_warnings(scene)


@dataclass(frozen=True)
class Source:
    """The triangles a rendered part may come from, in world coordinates, each with its
    code (``""`` unpainted), the codes numbered against ``palette``."""

    vertices: FloatArray
    faces: IntArray
    codes: tuple[str, ...]
    palette: tuple[str, ...]


@dataclass(frozen=True)
class CarriedPaint:
    """A rendered part's painting: its faces' codes and the colours they number, and
    each face's turn: its vertices ``np.roll(face, -turn)`` start where its source's do."""

    paint: Paint
    turns: IntArray


def transfer(
    output: trimesh.Trimesh,
    source: Source,
    problems: list[str],
    *,
    max_pairs: int = MAX_NEAR_PAIRS,
    label: str = "",
) -> tuple[list[str], IntArray, int]:
    """Each of ``output``'s faces' code and turn, by the module docstring's rules, and
    how many faces were carried verbatim. Work past a bound never fails the render: a
    source with too many distinct points in one cell carries nothing, and more than
    ``max_pairs`` candidate pairs leaves the non-verbatim faces in the part's own
    colour; each is appended to ``problems``, led by ``label`` (the scene parts) if given."""
    lead = f"{label}: " if label else ""
    vertices = np.asarray(output.vertices, dtype=np.float64)
    faces = np.asarray(output.faces, dtype=np.int64)
    codes = [""] * len(faces)
    turns = np.zeros(len(faces), dtype=np.int64)
    if len(faces) == 0 or len(source.faces) == 0:
        return codes, turns, 0
    try:
        ids = match_points(source.vertices, vertices, VERBATIM_MM)[faces]
    except ValueError as error:
        problems.append(
            f"{lead}its painting is not carried, so it prints in its own colour: {error}"
        )
        return codes, turns, 0
    whole = np.flatnonzero((ids >= 0).all(axis=1))
    found = np.full(len(faces), -1, dtype=np.int64)
    if len(whole):
        # The source triangle with the same three vertices, whatever their order.
        known = np.sort(source.faces, axis=1)
        _, group = np.unique(
            np.concatenate([known, np.sort(ids[whole], axis=1)]), axis=0, return_inverse=True
        )
        group = group.reshape(-1)
        # The lowest source face of each group, whatever the order of duplicates.
        first = np.full(int(group.max()) + 1, len(known), dtype=np.int64)
        np.minimum.at(first, group[: len(known)], np.arange(len(known)))
        hit = first[group[len(known) :]]
        found[whole] = np.where(hit < len(known), hit, -1)
    matched = np.flatnonzero(found >= 0)
    own = source.faces[found[matched]]
    seen = ids[matched]
    turn = (seen == own[:, :1]).argmax(axis=1)
    same = seen[np.arange(len(matched)), (turn + 1) % 3] == own[:, 1]
    verbatim = matched[same]
    turns[verbatim] = turn[same]
    for row, face in zip(verbatim.tolist(), found[verbatim].tolist(), strict=True):
        codes[row] = source.codes[face]
    rest = np.setdiff1d(np.arange(len(faces)), verbatim)
    if len(rest):
        one_colour: dict[str, str] = {}

        def whole_colour(code: str) -> str:
            if code not in one_colour:
                state = uniform_state(code)
                one_colour[code] = "" if state is None else single_code(state)
            return one_colour[code]

        centres = vertices[faces[rest]].mean(axis=1)
        try:
            nearest, _ = nearest_triangles(
                centres, source.vertices[source.faces], NEAR_MM, max_pairs=max_pairs
            )
        except NearPairsExceeded as error:
            problems.append(
                f"{lead}{len(rest)} faces are not its painting's own, and finding their colours "
                f"would take too long ({error}), so they print in the part's own colour"
            )
            return codes, turns, len(verbatim)
        for row, face in zip(rest.tolist(), nearest.tolist(), strict=True):
            if face >= 0:
                codes[row] = whole_colour(source.codes[face])
    return codes, turns, len(verbatim)


def _source(
    directory: Path, members: Sequence[tuple[SceneObject, Part]], mesh_of: MeshOf
) -> tuple[Source, list[str]]:
    """The members' triangles placed in the world, their codes renumbered into one
    palette (the paintings' colours, first seen first), and what could not be read."""
    paints: dict[str, Paint] = {}
    unreadable: set[str] = set()
    for _, part in members:
        if part.painted and part.mesh not in paints and part.mesh not in unreadable:
            try:
                raw = json.loads((directory / paint_file(part)).read_text(encoding="utf-8"))
                paint = Paint(tuple(raw["codes"]), tuple(raw["colours"]))
                paint.used()  # a code naming an extruder past its colours is no painting
                paints[part.mesh] = paint
            except (OSError, ValueError, KeyError, TypeError):
                unreadable.add(part.mesh)
    palette = tuple(dict.fromkeys(c.upper() for p in paints.values() for c in p.colours))
    numbered = {mesh: paint.renumbered(palette) for mesh, paint in paints.items()}
    vertices: list[FloatArray] = []
    faces: list[IntArray] = []
    codes: list[str] = []
    problems: list[str] = []
    offset = 0
    for obj, part in members:
        mesh = mesh_of(part.mesh)
        m, t = placement(obj)
        vertices.append(np.asarray(mesh.vertices, dtype=np.float64) @ m.T + t)
        faces.append(np.asarray(mesh.faces, dtype=np.int64) + offset)
        offset += len(mesh.vertices)
        own = numbered.get(part.mesh)
        if part.painted and part.mesh in unreadable:
            problems.append(
                f"{obj.id} {part.id}: its painting ({paint_file(part)}) is missing or "
                "unreadable, so it prints in its own colour"
            )
        if own is not None and len(own.codes) != len(mesh.faces):
            problems.append(
                f"{obj.id} {part.id}: its painting is for another mesh, so it prints in its "
                "own colour"
            )
            own = None
        codes += list(own.codes) if own is not None else [""] * len(mesh.faces)
    # Members touching share points: one entry per distinct point, so a face's vertex
    # indices are the ones `match_points` answers with.
    points, inverse = np.unique(np.concatenate(vertices), axis=0, return_inverse=True)
    source = Source(points, inverse.reshape(-1)[np.concatenate(faces)], tuple(codes), palette)
    return source, problems


def carry_paint(
    directory: Path, params: Mapping[str, ParamValue], parts: Sequence[ColourPart]
) -> tuple[list[CarriedPaint | None], list[str]]:
    """For each of ``parts`` (one render of the model in ``directory``, a part per
    colour), the painting its colour's painted scene parts carry onto it, or None; and
    the warnings the render gives. A directory with no scene, or a scene with no painted
    part, gives nothing; one whose model.scad was edited by hand gives the warning."""
    carried: list[CarriedPaint | None] = [None] * len(parts)
    path = directory / SCENE_NAME
    if not path.is_file():
        return carried, []
    scene = load_scene(path.read_text(encoding="utf-8"))
    if not any(p.painted and not p.negative for o in scene.objects for p in o.parts):
        return carried, []
    if text_sha256((directory / SOURCE_NAME).read_text(encoding="utf-8")) != scene.generated_sha256:
        return carried, [HAND_EDITED]
    scene = with_params(scene, params)
    mesh_of = mesh_loader(directory)
    warnings = paint_warnings(scene)
    for index, part in enumerate(parts):
        if part.negative:
            continue
        colour = part.colour.upper()
        members = [
            (obj, p)
            for obj in scene.objects
            for p in obj.parts
            if not p.negative and p.colour == colour
        ]
        if not any(p.painted for _, p in members):
            continue
        source, problems = _source(directory, members, mesh_of)
        warnings += problems
        label = ", ".join(f"{obj.id} {p.id}" for obj, p in members if p.painted)
        codes, turns, _ = transfer(part.mesh, source, warnings, label=label)
        if any(codes):
            carried[index] = CarriedPaint(Paint(tuple(codes), source.palette), turns)
    return carried, warnings


def turned(mesh: trimesh.Trimesh, turns: IntArray) -> trimesh.Trimesh:
    """``mesh`` with each face's vertices turned by its turn: the same triangles, the
    same winding, each starting where its source triangle does."""
    faces = np.asarray(mesh.faces, dtype=np.int64)
    order = (np.arange(3)[None, :] + turns[:, None]) % 3
    return trimesh.Trimesh(
        vertices=mesh.vertices, faces=np.take_along_axis(faces, order, axis=1), process=False
    )


def write_carried(path: Path, carried: CarriedPaint) -> None:
    """The carried painting beside a render's layout (`render/jobs.py` `PartSource.paint`)."""
    document = {
        "codes": list(carried.paint.codes),
        "colours": list(carried.paint.colours),
        "turns": "".join(str(turn) for turn in carried.turns.tolist()),
    }
    path.write_text(json.dumps(document), encoding="utf-8")


def read_carried(path: Path, mesh: trimesh.Trimesh) -> tuple[trimesh.Trimesh, Paint]:
    """What :func:`write_carried` wrote, applied to the part's ``mesh`` as it was re-read
    from the render: its faces turned, and its painting."""
    document = json.loads(path.read_text(encoding="utf-8"))
    codes = tuple(str(code) for code in document["codes"])
    turns = np.array([int(turn) for turn in document["turns"]], dtype=np.int64)
    if len(codes) != len(mesh.faces) or len(turns) != len(mesh.faces):
        raise ValueError(f"{path.name} is the painting of another mesh")
    return turned(mesh, turns), Paint(codes, tuple(document["colours"]))
