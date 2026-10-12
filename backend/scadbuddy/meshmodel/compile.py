"""`compile_scene`: ``scene.json`` to ``model.scad``, pure and deterministic (spec
2026-10-11 §3.3). Exposed numbers become top-level assignments before any module, so
OpenSCAD's customizer lists them. Each part is ``color(c) import(mesh)``; the mesh file
carries no colour (§3.3), so ``render/solids.py``'s per-colour re-render still yields
one closed solid per colour. Geometry an op adds (a peg, an ``add``) is in the object's
first part's colour."""

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


def _wrap(obj: SceneObject, index: int, op: Op, inner: list[str]) -> list[str]:
    nested = _indent(inner)
    colour = obj.parts[0].colour
    if isinstance(op, Cut):
        floor = "0" if op.keep == "above" else _f(-2 * HALF)
        tool = (
            f"translate({_vec(op.point)}) multmatrix({_rot(frame(op.normal))}) "
            f"translate([{_f(-HALF)}, {_f(-HALF)}, {floor}]) cube({_f(2 * HALF)});"
        )
        return ["intersection() {", *nested, f"  {tool}", "}"]
    if isinstance(op, Joint):
        place = f"translate({_vec(op.at)}) multmatrix({_rot(frame(op.axis))})"
        d, h, c = _n(op.diameter), _n(op.depth), _n(op.clearance)
        if op.role == "peg":
            return [
                "union() {",
                *nested,
                f'  color("{op.colour or colour}") {place} sb_key({d}, {h}, "{op.key}");',
                "}",
            ]
        return [
            "difference() {",
            *nested,
            f'  {place} translate([0, 0, -0.01]) sb_key({d}, {h} + {c} + 0.01, "{op.key}", {c});',
            "}",
        ]
    if isinstance(op, Add):
        shape = f"{_placed(op.transform)} {_primitive(op)}"
        if op.colour is None:
            return ["union() {", *nested, f'  color("{colour}") {shape}', "}"]
        # Carved out of the rest first, so no two colours ever claim the same volume.
        return [
            "union() {",
            "  difference() {",
            *_indent(nested),
            f"    {shape}",
            "  }",
            f'  color("{op.colour}") {shape}',
            "}",
        ]
    if isinstance(op, Subtract):
        return ["difference() {", *nested, f"  {_placed(op.transform)} {_primitive(op)}", "}"]
    assert isinstance(op, Custom)
    return [f"{obj.id}_op{index}() {{", *nested, "}"]


def _modules(obj: SceneObject) -> list[str]:
    solids = [f'color("{p.colour}") import("{p.mesh}");' for p in obj.parts if not p.negative]
    cuts = [f'import("{p.mesh}");' for p in obj.parts if p.negative]
    out = [f"module {obj.id}_parts() {{"]
    if cuts:
        # The negative parts cut every printed part, as Bambu Studio cuts them (#2337).
        out += ["  difference() {", "    union() {", *_indent(_indent(_indent(solids))), "    }"]
        out += [*_indent(_indent(cuts)), "  }"]
    else:
        out += _indent(solids)
    out.append("}")
    for index, op in enumerate(obj.ops):
        if isinstance(op, Custom):
            # Unindented: indenting would put spaces inside a multi-line string in the snippet.
            out += [f"module {obj.id}_op{index}() {{", *op.scad.splitlines(), "}"]
    body = [f"{obj.id}_parts();"]
    for index, op in enumerate(obj.ops):
        body = _wrap(obj, index, op, body)
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
