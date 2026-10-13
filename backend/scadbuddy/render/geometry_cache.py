"""Geometry first, colours second: a piece's meshes, keyed apart from its colours.

A render depends on its colour parameters only through which of them share a value:
`split_by_material` makes one part per distinct material colour, `render_solids`
renders one closed solid per part, and `extruder_order` ranks the parts by the first
colour parameter naming each. So `geometry_key` replaces every colour parameter's value
with its *class*, the place its colour first appears among the colour parameters in
declaration order (`colour_classes`). Two pieces that differ only in which colours
those classes are share a geometry key, and the second is the first one's meshes with
each class's colour swapped (`recolour_piece`), followed by the ordinary finish stage
(the covers and the 3MF) in its own colours. Only the files a piece's colours are read
from are rewritten: `layout.json` (which `PlateLayout.load` takes every part's colour
from) and `preview.glb`. The copied `render.3mf` and `solid_*.3mf` keep the colours they
were rendered in, and nothing reads a colour off them once the layout is written.

What a key cannot see, and so what reuse refuses or assumes:

- A colour no colour parameter names: a literal, one a library draws, or one computed
  from a parameter. A layout holding one is never reused (`reusable`): a derived colour
  would keep its old value. A template whose own source writes a literal colour gets no
  key at all (`has_literal_colour`), because a parameter that happened to equal it was
  merged with it into one part, which a recolour would then colour as a whole. A
  library's literal is not scanned for; it is refused by `reusable` unless it happens
  to equal a colour parameter's value at the first render.
- A colour parameter read as anything but a colour (``if (c == "#000000")``) is assumed
  not to change the geometry. Equality between two colour parameters is safe: the
  classes keep it. A value that is not a colour (`colour_hex` is None) stays in the key
  as it is.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from scadbuddy.render.colours import colour_hex
from scadbuddy.render.glb import write_glb
from scadbuddy.render.schema import CustomizerSchema, ParamValue
from scadbuddy.render.solids import WRAPPER_PREFIX
from scadbuddy.render.split import ColourPart, split_by_material

#: Part of every key: bump it when what a geometry key promises changes, so no older
#: row is read under the new meaning.
GEOMETRY_VERSION = 1
#: Material 0 is OpenSCAD's "Default": geometry no `color()` call reached.
UNCOLOURED_MATERIAL_INDEX = 0
#: A `color()` call whose first argument is a string or vector literal.
_LITERAL_COLOUR = re.compile(r"\bcolor\s*\(\s*(?:c\s*=\s*)?[\"\[]")
_HEX = re.compile(r"#[0-9A-Fa-f]{6}\b")


def colour_classes(schema: CustomizerSchema, params: Mapping[str, ParamValue]) -> list[str]:
    """Each distinct colour the colour parameters name, in declaration order of first
    use: `extruder_order`'s ranking, which is why a class keeps its extruder."""
    classes: list[str] = []
    for parameter in schema.parameters:
        if parameter.type != "color":
            continue
        colour = colour_hex(params.get(parameter.name, parameter.initial))
        if colour is not None and colour not in classes:
            classes.append(colour)
    return classes


def geometry_key(
    slug: str,
    revision: str,
    file: str,
    schema: CustomizerSchema,
    params: Mapping[str, ParamValue],
) -> str | None:
    """The key of the piece's geometry, or None for a template with no colour
    parameter (its piece key already says everything)."""
    colours = [p for p in schema.parameters if p.type == "color"]
    if not colours:
        return None
    classes = colour_classes(schema, params)
    tokens: list[Any] = []
    for parameter in colours:
        value = params.get(parameter.name, parameter.initial)
        colour = colour_hex(value)
        tokens.append({"class": classes.index(colour)} if colour is not None else {"value": value})
    names = {p.name for p in colours}
    shape = {name: value for name, value in params.items() if name not in names}
    raw = json.dumps(
        [GEOMETRY_VERSION, slug, revision, file, shape, tokens],
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def has_literal_colour(root: Path) -> bool:
    """Whether a `.scad` of the template at ``root`` calls `color()` with a literal."""
    for path in root.rglob("*.scad"):
        if path.name.startswith(WRAPPER_PREFIX) or not path.is_file():
            continue
        if _LITERAL_COLOUR.search(path.read_text(encoding="utf-8", errors="replace")):
            return True
    return False


def _layout_colours(layout: Mapping[str, Any]) -> set[str]:
    found = {colour.upper() for colour in layout["colours"]}
    for plate in layout["plates"]:
        for part in plate["parts"]:
            if part["material_index"] != UNCOLOURED_MATERIAL_INDEX:
                found.add(part["colour"].upper())
    return found


def reusable(layout: Mapping[str, Any], classes: Sequence[str]) -> bool:
    """Whether every colour the layout holds is one of its colour classes."""
    return _layout_colours(layout) <= {colour.upper() for colour in classes}


def _swap(colour: str, mapping: Mapping[str, str]) -> str:
    return mapping.get(colour.upper(), colour)


def _swap_text(text: str, mapping: Mapping[str, str]) -> str:
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


def class_order(parts: Sequence[ColourPart], classes: Sequence[str]) -> list[ColourPart]:
    """``parts`` in extruder order (`render.jobs.extruder_order`): each part ranked by
    its colour's class, then every part no class names, uncoloured geometry with them,
    in OpenSCAD's material order."""
    ranks = {colour.upper(): rank for rank, colour in enumerate(classes)}

    def rank(part: ColourPart) -> int:
        if part.material_index == UNCOLOURED_MATERIAL_INDEX:
            return len(ranks)
        return ranks.get(part.colour.upper(), len(ranks))

    return sorted(parts, key=rank)


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
    ``classes``: every file but the dot-named ones and ``skip`` (what the finish stage
    writes, and the two rewritten here) copied as it is, then the recoloured layout and
    a preview written from the raw render's split. ``work`` is emptied first: it may
    hold an earlier attempt's files, and every file in it is published."""
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
    parts = class_order(split_by_material(source / raw_name), classes)
    write_glb(
        [
            ColourPart(
                part.material_index,
                part.name,
                _swap(part.colour, mapping),
                part.mesh,
                part.paint,
                negative=part.negative,
            )
            for part in parts
        ],
        work / preview_name,
    )
