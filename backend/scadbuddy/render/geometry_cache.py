"""Geometry first, colours second: a mesh model's meshes, keyed apart from its colours.

Only a mesh model's geometry is shared. Its ``model.scad`` is compiled from
``scene.json`` (`meshmodel/compile.py`), every colour is a literal ``color("#…")`` of a
part, a peg or an add, and nothing in the scene reads a colour back, so the scene says
exactly which geometry each colour covers. A ``.scad`` template's colour can flow into
anything the program computes, and only rendering it tells, so it keeps only the
piece cache.

`geometry_key` hashes the scene's objects with each colour replaced by its *class*,
the place it first appears in the scene (`scene_classes`), and the ``model.scad`` they
compile to, classed the same way (so a change to `compile_scene` is another key), with
the meshes' and paint codes' contents, the template file, the libraries and the
parameters. Recolouring a
part is a scene edit, a new revision and a new piece key, but the same geometry key
while the colours that are equal stay equal: the piece is then the first one's meshes
with each class's colour swapped (`recolour_piece`), followed by the ordinary finish
stage (the covers and the 3MF) in its own colours. Only the files a piece's colours are
read from are rewritten: `layout.json` (which `PlateLayout.load` takes every part's
colour from) and `preview.glb`. The copied `render.3mf` and `solid_*.3mf` keep the
colours they were rendered in, and nothing reads a colour off them once the layout is
written; so only a piece that was rendered is recorded as a geometry's source
(`RenderPiece` finishes a recoloured one without its key).

A model whose ``model.scad`` is not what its scene compiled (ejected, or edited by
hand) has no key (`mesh_scene`). A recoloured piece is published with its source's
`RenderMainResult`: its log tail, diagnostics and notes are the source render's.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from scadbuddy.core.paths import SOURCE_NAME
from scadbuddy.library.libraries import ModelLibrary
from scadbuddy.meshmodel.compile import compile_scene
from scadbuddy.meshmodel.scene import (
    SCENE_NAME,
    Add,
    Joint,
    Op,
    Scene,
    SceneObject,
    load_scene,
    mesh_files,
    text_sha256,
)
from scadbuddy.render.glb import write_glb
from scadbuddy.render.jobs import UNCOLOURED_MATERIAL_INDEX
from scadbuddy.render.schema import ParamValue
from scadbuddy.render.split import ColourPart, Paint, split_by_material

#: Part of every key: bump it when what a geometry key promises changes, or when what a
#: row holds does (`RenderMainResult`, a piece's files), so no older row is read under
#: the new meaning. A change to what `compile_scene` writes needs no bump: the compiled
#: source is in the key (#3448).
GEOMETRY_VERSION = 5
_HEX = re.compile(r"#[0-9A-Fa-f]{6}\b")
#: A colour as `compile_scene` writes it, around each piece. A custom op's own
#: ``color("#…")`` that equals a class is classed too: harmless, since the op's text is
#: in the key's objects as it is.
_COLOUR_CALL = re.compile(r'color\("(#[0-9A-Fa-f]{6})"\)')


def mesh_scene(root: Path, file: str) -> Scene | None:
    """The scene ``root``'s ``file`` is compiled from, or None when it is not a mesh
    model's ``model.scad`` exactly as its scene last compiled it."""
    scene_path = root / SCENE_NAME
    if file != SOURCE_NAME or not scene_path.is_file():
        return None
    try:
        scene = load_scene(scene_path.read_text(encoding="utf-8"))
    except ValidationError:
        return None  # Rendered from model.scad as it is; only its geometry is unshared.
    source = (root / SOURCE_NAME).read_text(encoding="utf-8")
    return scene if text_sha256(source) == scene.generated_sha256 else None


def _default_colour(obj: SceneObject) -> str:
    """What an op without a colour of its own prints in (`compile._apply`)."""
    return next((p.colour for p in obj.parts if not p.negative), obj.parts[0].colour)


def _printed(op: Op) -> bool:
    """Whether ``op`` adds a colour's volume: a peg or an add (a socket is cut)."""
    return isinstance(op, Add) or (isinstance(op, Joint) and op.role == "peg")


def scene_classes(scene: Scene) -> list[str]:
    """Each distinct colour the scene prints, first seen first: every printed part's,
    then every peg's and add's (its own, or its object's default)."""
    classes: list[str] = []
    for obj in scene.objects:
        default = _default_colour(obj)
        found = [p.colour for p in obj.parts if not p.negative]
        found += [
            op.colour or default for op in obj.ops if isinstance(op, Joint | Add) and _printed(op)
        ]
        for colour in found:
            if colour not in classes:
                classes.append(colour)
    return classes


def _classed(scene: Scene, classes: Sequence[str]) -> list[dict[str, Any]]:
    """The scene's objects, dumped, with each printed colour its class's index and
    every colour that prints nothing (a negative part's, a socket's) dropped."""
    objects: list[dict[str, Any]] = scene.model_dump(mode="json")["objects"]
    for obj, dumped in zip(scene.objects, objects, strict=True):
        default = _default_colour(obj)
        for part, entry in zip(obj.parts, dumped["parts"], strict=True):
            entry["colour"] = None if part.negative else {"class": classes.index(part.colour)}
        for op, entry in zip(obj.ops, dumped["ops"], strict=True):
            if isinstance(op, Joint | Add):
                entry["colour"] = (
                    {"class": classes.index(op.colour or default)} if _printed(op) else None
                )
    return objects


def _classed_source(scene: Scene, classes: Sequence[str]) -> str:
    """The ``model.scad`` the scene compiles to (`mesh_scene` checked that it is the one
    on disk) with each class's ``color("#…")`` its class's index: the compiler's output,
    whatever the colours."""

    def classed(match: re.Match[str]) -> str:
        colour = match.group(1)
        return f"color({classes.index(colour)})" if colour in classes else match.group(0)

    return _COLOUR_CALL.sub(classed, compile_scene(scene))


def _file_digests(root: Path, scene: Scene) -> dict[str, str]:
    return {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in sorted(mesh_files(scene))
    }


def geometry_key(
    slug: str,
    file: str,
    root: Path,
    scene: Scene,
    libraries: Sequence[ModelLibrary],
    params: Mapping[str, ParamValue],
) -> str:
    """The key of the mesh model's geometry: the same for two scenes that differ only
    in which colours their colour classes are."""
    classes = scene_classes(scene)
    objects = _classed(scene, classes)
    raw = json.dumps(
        [
            GEOMETRY_VERSION,
            slug,
            file,
            objects,
            _classed_source(scene, classes),
            _file_digests(root, scene),
            [library.model_dump(mode="json") for library in libraries],
            dict(params),
        ],
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def mesh_geometry(
    slug: str,
    file: str,
    root: Path,
    libraries: Sequence[ModelLibrary],
    params: Mapping[str, ParamValue],
) -> tuple[str, list[str]] | None:
    """The piece's geometry key and its colour classes' colours, or None when its
    geometry is never shared (`mesh_scene`). Reads files: run it off the loop."""
    scene = mesh_scene(root, file)
    if scene is None:
        return None
    return geometry_key(slug, file, root, scene, libraries, params), scene_classes(scene)


def _layout_colours(layout: Mapping[str, Any]) -> set[str]:
    found = {colour.upper() for colour in layout["colours"]}
    for plate in layout["plates"]:
        for part in plate["parts"]:
            if part["material_index"] != UNCOLOURED_MATERIAL_INDEX:
                found.add(part["colour"].upper())
    return found


def painted(layout: Mapping[str, Any]) -> bool:
    """Whether any part of the layout carries a painting (`PartSource.paint`)."""
    return any("paint" in part for plate in layout["plates"] for part in plate["parts"])


def reusable(layout: Mapping[str, Any], classes: Sequence[str]) -> bool:
    """Whether every colour the layout holds is one of its colour classes, and no part
    carries a painting: its ``paint-*.json`` keeps the palette it was painted in, which
    `recolour_layout` does not swap, so a painted colour that is also a class would no
    longer be a filament."""
    if painted(layout):
        return False
    return _layout_colours(layout) <= {colour.upper() for colour in classes}


def _swap(colour: str, mapping: Mapping[str, str]) -> str:
    return mapping.get(colour.upper(), colour)


def _swap_text(text: str, mapping: Mapping[str, str]) -> str:
    """``text`` with each class's colour swapped, a hex value that is no class left as it
    is. Every hex in a mesh model's warnings is a colour the render names (a solid's
    fallback, a colour on no plate): it has no colour, font or text parameter, the only
    other warnings that quote a value (#3448)."""
    return _HEX.sub(lambda match: _swap(match.group(0), mapping), text)


def recolour_layout(layout: Mapping[str, Any], mapping: Mapping[str, str]) -> dict[str, Any]:
    """``layout`` (as `PlateLayout.save` writes it) in the new colours: one pass, so
    a class swapped onto another's old colour is not swapped again."""
    return {
        **layout,
        "colours": [_swap(colour, mapping) for colour in layout["colours"]],
        "warnings": [_swap_text(warning, mapping) for warning in layout["warnings"]],
        "plates": [
            {
                **plate,
                "parts": [
                    {**part, "colour": _swap(part["colour"], mapping)} for part in plate["parts"]
                ],
            }
            for plate in layout["plates"]
        ],
    }


def recolour_piece(
    source: Path,
    work: Path,
    layout: Mapping[str, Any],
    raw_name: str,
    *,
    classes: Sequence[str],
    colours: Sequence[str],
    skip: frozenset[str],
    layout_name: str,
    preview_name: str,
) -> None:
    """Make ``work`` ``source``'s geometry in ``colours``, class for class with
    ``classes`` (the source's `scene_classes`): every file but the dot-named ones and
    ``skip`` (what the finish stage writes, and the two rewritten here) copied as it
    is, then the recoloured layout and a preview written from the raw render's split.
    ``work`` is emptied first: it may hold an earlier attempt's files, and every file
    in it is published."""
    mapping = {old.upper(): new for old, new in zip(classes, colours, strict=True)}
    for path in work.iterdir():
        if path.name.startswith("."):
            continue
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()

    def ignore(directory: str, names: list[str]) -> set[str]:
        top = Path(directory) == source
        return {n for n in names if n.startswith(".") or (top and n in skip)}

    shutil.copytree(source, work, ignore=ignore, dirs_exist_ok=True)
    (work / layout_name).write_text(json.dumps(recolour_layout(layout, mapping)), encoding="utf-8")
    # In the split's own order, as a mesh model's extruders are (`extruder_order`: it
    # has no colour parameter to rank by).
    parts = split_by_material(source / raw_name)
    write_glb(
        [
            ColourPart(
                part.material_index,
                part.name,
                _swap(part.colour, mapping),
                part.mesh,
                # Its painting's colours swapped too, now that a preview draws them
                # (#3956), as an arranged copy's are (`workflows/outputs.py`).
                None
                if part.paint is None
                else Paint(
                    part.paint.codes,
                    tuple(_swap(c, mapping) for c in part.paint.colours),
                    part.paint.reversed,
                ),
                negative=part.negative,
            )
            for part in parts
        ],
        work / preview_name,
    )
