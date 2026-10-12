"""`compile_scene`: ``scene.json`` to ``model.scad``, pure and deterministic (spec
2026-10-11 §3.3). Exposed numbers become top-level assignments before any module, so
OpenSCAD's customizer lists them. Each part is ``color(c) import(mesh)``; the mesh file
carries no colour (§3.3), so ``render/solids.py``'s per-colour re-render still yields
one closed solid per colour.

An object compiles per piece: one ``color(c) <statement>;`` per colour piece, starting
with one per printed part. Every op that removes geometry (a cut's half-space, a socket,
a ``subtract``, the negative parts) wraps every piece, and a ``custom`` op is given each
piece on its own as its ``children()``; an op that adds geometry (a peg, an ``add``)
appends a piece in its colour, or the object's first printed part's. So every cutter
sits inside its piece's ``color()``: in OpenSCAD 2026.10.05's Manifold 3MF a face takes
the colour of the solid that made it, and a cutter outside every ``color()`` made the
cut faces the Default material and a socket's wall a colour of its own (#2146; measured
in the image, ``tests/test_mesh_models_openscad.py``). The geometry is the same as
wrapping their union, since (A | B) - C = (A - C) | (B - C) and
(A | B) & H = (A & H) | (B & H); a custom op that does not distribute over a union sees
each colour apart."""

from __future__ import annotations

import numpy as np

from scadbuddy.meshmodel.frames import frame
from scadbuddy.meshmodel.scene import (
    Add,
    Custom,
    Cut,
    Exposed,
    Joint,
    Num,
    Op,
    Primitive,
    Scene,
    SceneObject,
    Subtract,
    Transform,
    exposed_values,
)

HEADER = (
    "// Generated from scene.json by ScadBuddy; edit the scene, or eject (scene_eject) "
    "to edit this file"
)
#: Half the side of the cube a cut intersects with, mm: past any printer's bed.
HALF = 2000.0
KEY_MODULE = [
    "module sb_key(d, h, key, grow = 0) {",
    "  r = d / 2 + grow;",
    '  if (key == "hex") cylinder(h = h, r = r / cos(30), $fn = 6);',
    '  else if (key == "flat") intersection() {',
    "    cylinder(h = h, r = r, $fn = 48);",
    "    translate([-r, -r, 0]) cube([2 * r, r + 0.35 * d + grow, h]);",
    "  }",
    "  else cylinder(h = h, r = r, $fn = 48);",
    "}",
]


def _f(x: float) -> str:
    text = f"{x:.6f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _n(v: Num) -> str:
    return v.expose if isinstance(v, Exposed) else _f(v)


def _vec(v: tuple[Num, Num, Num]) -> str:
    return "[" + ", ".join(_n(x) for x in v) + "]"


def _rot(r: np.ndarray) -> str:
    rows = [[*map(float, r[i]), 0.0] for i in range(3)] + [[0.0, 0.0, 0.0, 1.0]]
    return "[" + ", ".join("[" + ", ".join(_f(x) for x in row) + "]" for row in rows) + "]"


def _placed(t: Transform) -> str:
    return f"translate({_vec(t.translate)}) rotate({_vec(t.rotate)}) scale({_vec(t.scale)})"


def _primitive(p: Primitive) -> str:
    if p.primitive == "cube":
        return f"cube({_vec(p.size)}, center = true);"
    if p.primitive == "cylinder":
        return f"cylinder(h = {_n(p.size[2])}, d = {_n(p.size[0])}, center = true, $fn = 64);"
    return f"sphere(d = {_n(p.size[0])}, $fn = 64);"


def _indent(lines: list[str]) -> list[str]:
    return ["  " + line for line in lines]


#: One colour's share of an object: its colour and the OpenSCAD statement for it.
Piece = tuple[str, list[str]]


def _wrapped(head: str, piece: list[str], *tools: str) -> list[str]:
    return [f"{head} {{", *_indent(piece), *_indent(list(tools)), "}"]


def _cut_all(pieces: list[Piece], head: str, *tools: str) -> list[Piece]:
    return [(colour, _wrapped(head, piece, *tools)) for colour, piece in pieces]


def _apply(obj: SceneObject, index: int, op: Op, pieces: list[Piece]) -> list[Piece]:
    colour = next((p.colour for p in obj.parts if not p.negative), obj.parts[0].colour)
    if isinstance(op, Cut):
        floor = "0" if op.keep == "above" else _f(-2 * HALF)
        tool = (
            f"translate({_vec(op.point)}) multmatrix({_rot(frame(op.normal))}) "
            f"translate([{_f(-HALF)}, {_f(-HALF)}, {floor}]) cube({_f(2 * HALF)});"
        )
        return _cut_all(pieces, "intersection()", tool)
    if isinstance(op, Joint):
        place = f"translate({_vec(op.at)}) multmatrix({_rot(frame(op.axis))})"
        d, h, c = _n(op.diameter), _n(op.depth), _n(op.clearance)
        if op.role == "peg":
            return [*pieces, (op.colour or colour, [f'{place} sb_key({d}, {h}, "{op.key}");'])]
        socket = f'{place} translate([0, 0, -0.01]) sb_key({d}, {h} + {c} + 0.01, "{op.key}", {c});'
        return _cut_all(pieces, "difference()", socket)
    if isinstance(op, Add):
        shape = f"{_placed(op.transform)} {_primitive(op)}"
        if op.colour is None:
            return [*pieces, (colour, [shape])]
        # Carved out of the rest first, so no two colours ever claim the same volume.
        return [*_cut_all(pieces, "difference()", shape), (op.colour, [shape])]
    if isinstance(op, Subtract):
        return _cut_all(pieces, "difference()", f"{_placed(op.transform)} {_primitive(op)}")
    assert isinstance(op, Custom)
    return [(c, _wrapped(f"{obj.id}_op{index}()", piece)) for c, piece in pieces]


def _modules(obj: SceneObject) -> list[str]:
    out = []
    for index, op in enumerate(obj.ops):
        if isinstance(op, Custom):
            # Unindented: indenting would put spaces inside a multi-line string in the snippet.
            out += [f"module {obj.id}_op{index}() {{", *op.scad.splitlines(), "}"]
    # The negative parts cut every printed part, as Bambu Studio cuts them (#2337).
    cuts = [f'import("{p.mesh}");' for p in obj.parts if p.negative]
    pieces: list[Piece] = [
        (
            p.colour,
            _wrapped("difference()", [f'import("{p.mesh}");'], *cuts)
            if cuts
            else [f'import("{p.mesh}");'],
        )
        for p in obj.parts
        if not p.negative
    ]
    for index, op in enumerate(obj.ops):
        pieces = _apply(obj, index, op, pieces)
    body = []
    for colour, (first, *rest) in pieces:
        body += [f'color("{colour}") {first}', *rest]
    return [*out, f"module {obj.id}() {{", *_indent(body), "}"]


def compile_scene(scene: Scene) -> str:
    lines = [HEADER]
    lines += [f"{name} = {_f(v)};" for name, v in exposed_values(scene).items()]
    lines.append("")
    if any(isinstance(op, Joint) for obj in scene.objects for op in obj.ops):
        lines += [*KEY_MODULE, ""]
    for obj in scene.objects:
        lines += [*_modules(obj), ""]
    lines += [f"{_placed(obj.transform)} {obj.id}();" for obj in scene.objects]
    return "\n".join(lines) + "\n"
