"""`compile_scene` (spec 2026-10-11 §3.3)."""
# ruff: noqa: E501 (the expected OpenSCAD lines are single strings)

from __future__ import annotations

from collections.abc import Sequence

from scadbuddy.meshmodel.compile import compile_scene
from scadbuddy.meshmodel.scene import (
    Add,
    Custom,
    Cut,
    Exposed,
    Joint,
    Op,
    Part,
    Scene,
    SceneObject,
    SceneSource,
    Subtract,
    Transform,
)

SOURCE = SceneSource(kind="upload", name="carrot.3mf", sha256="ab" * 32)


def scene(*objects: SceneObject) -> Scene:
    return Scene(source=SOURCE, objects=list(objects))


def carrot(oid: str = "o1", ops: Sequence[Op] = (), **update: object) -> SceneObject:
    return SceneObject(
        id=oid,
        name="Carrot",
        parts=[
            Part(id="p1", mesh=f"meshes/{oid}-p1.3mf", colour="#F08A24"),
            Part(id="p2", mesh=f"meshes/{oid}-p2.3mf", colour="#3A7D2C"),
        ],
        ops=list(ops),
    ).model_copy(update=update)


def test_a_plain_object_compiles_to_imports_in_colour() -> None:
    plain = SceneObject(
        id="o1", name="Box", parts=[Part(id="p1", mesh="meshes/o1-p1.3mf", colour="#F08A24")]
    )
    assert compile_scene(scene(plain)) == (
        "// Generated from scene.json by ScadBuddy; edit the scene, or eject (scene_eject) to edit this file\n"
        "\n"
        "module o1() {\n"
        '  color("#F08A24") import("meshes/o1-p1.3mf");\n'
        "}\n"
        "\n"
        "translate([0, 0, 0]) rotate([0, 0, 0]) scale([1, 1, 1]) o1();\n"
    )


def test_compiling_is_deterministic() -> None:
    s = scene(carrot(ops=[Cut(point=(0, 0, 30), normal=(0, 0, 1), keep="below")]))
    assert compile_scene(s) == compile_scene(s.model_copy(deep=True))


def test_a_cut_intersects_with_a_half_space_on_the_kept_side() -> None:
    below = compile_scene(
        scene(carrot(ops=[Cut(point=(0, 0, 30), normal=(0, 0, 1), keep="below")]))
    )
    above = compile_scene(
        scene(carrot(ops=[Cut(point=(0, 0, 30), normal=(0, 0, 1), keep="above")]))
    )
    assert "intersection() {" in below
    assert (
        "translate([0, 0, 30]) multmatrix([[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]])"
        in below
    )
    assert "translate([-2000, -2000, -4000]) cube(4000);" in below
    assert "translate([-2000, -2000, 0]) cube(4000);" in above


def test_exposed_numbers_become_customizer_parameters_before_any_module() -> None:
    text = compile_scene(
        scene(
            carrot(
                ops=[
                    Cut(
                        point=(0, 0, Exposed(expose="cut_height", value=32.5)),
                        normal=(0, 0, 1),
                        keep="below",
                    )
                ]
            )
        )
    )
    lines = text.splitlines()
    assert lines[1] == "cut_height = 32.5;"
    assert lines.index("cut_height = 32.5;") < next(
        i for i, line in enumerate(lines) if line.startswith("module")
    )
    assert "translate([0, 0, cut_height])" in text


def test_a_joint_adds_a_peg_in_colour_and_cuts_a_grown_socket() -> None:
    peg = Joint(
        at=(0, 0, 30), axis=(0, 0, -1), diameter=4, depth=6, clearance=0.2, key="flat", role="peg"
    )
    socket = peg.model_copy(update={"role": "socket"})
    text = compile_scene(scene(carrot("o1", ops=[socket]), carrot("o2", ops=[peg])))
    assert "module sb_key(d, h, key, grow = 0) {" in text
    assert (
        'color("#F08A24") translate([0, 0, 30]) multmatrix([[1, 0, 0, 0], [0, -1, 0, 0], [0, 0, -1, 0], [0, 0, 0, 1]]) sb_key(4, 6, "flat");'
        in text
    )
    assert 'translate([0, 0, -0.01]) sb_key(4, 6 + 0.2 + 0.01, "flat", 0.2);' in text


def test_no_key_module_without_a_joint() -> None:
    assert "sb_key" not in compile_scene(scene(carrot()))


def test_add_and_subtract_place_centred_primitives() -> None:
    add = Add(primitive="cylinder", size=(3, 3, 8), transform=Transform(translate=(0, 0, 30)))
    sub = Subtract(primitive="cube", size=(2, 2, 2))
    text = compile_scene(scene(carrot(ops=[add, sub])))
    # The added cylinder is a piece of its own, which the later subtract cuts too.
    assert (
        '  color("#F08A24") difference() {\n'
        "    translate([0, 0, 30]) rotate([0, 0, 0]) scale([1, 1, 1]) cylinder(h = 8, d = 3, center = true, $fn = 64);\n"
        "    translate([0, 0, 0]) rotate([0, 0, 0]) scale([1, 1, 1]) cube([2, 2, 2], center = true);\n"
        "  }\n"
    ) in text


def test_a_coloured_add_carves_its_space_out_of_the_rest() -> None:
    add = Add(
        primitive="cylinder",
        size=(8, 8, 12),
        transform=Transform(translate=(0, 0, 30)),
        colour="#0ACC38",
    )
    text = compile_scene(scene(carrot(ops=[add])))
    shape = "translate([0, 0, 30]) rotate([0, 0, 0]) scale([1, 1, 1]) cylinder(h = 12, d = 8, center = true, $fn = 64);"
    assert (
        "module o1() {\n"
        '  color("#F08A24") difference() {\n'
        '    import("meshes/o1-p1.3mf");\n'
        f"    {shape}\n"
        "  }\n"
        '  color("#3A7D2C") difference() {\n'
        '    import("meshes/o1-p2.3mf");\n'
        f"    {shape}\n"
        "  }\n"
        f'  color("#0ACC38") {shape}\n'
        "}\n"
    ) in text


def test_a_negative_part_is_cut_out_of_the_printed_parts() -> None:
    obj = carrot()
    holed = obj.model_copy(
        update={"parts": [obj.parts[0], obj.parts[1].model_copy(update={"negative": True})]}
    )
    text = compile_scene(scene(holed))
    assert (
        "module o1() {\n"
        '  color("#F08A24") difference() {\n'
        '    import("meshes/o1-p1.3mf");\n'
        '    import("meshes/o1-p2.3mf");\n'
        "  }\n"
        "}\n"
    ) in text


def test_ops_add_in_the_first_printed_parts_colour() -> None:
    obj = carrot(ops=[Add(primitive="cube", size=(2, 2, 2))])
    holed = obj.model_copy(
        update={"parts": [obj.parts[0].model_copy(update={"negative": True}), obj.parts[1]]}
    )
    text = compile_scene(scene(holed))
    assert (
        'color("#3A7D2C") translate([0, 0, 0]) rotate([0, 0, 0]) scale([1, 1, 1]) cube([2, 2, 2], center = true);'
        in text
    )


def test_a_custom_op_is_a_module_given_the_geometry_as_children() -> None:
    text = compile_scene(
        scene(carrot(ops=[Custom(scad="minkowski() { children(); sphere(0.5); }")]))
    )
    assert "module o1_op0() {\nminkowski() { children(); sphere(0.5); }\n}" in text
    multi = compile_scene(scene(carrot(ops=[Custom(scad='echo("a\nb");\nchildren();')])))
    assert (
        'module o1_op0() {\necho("a\nb");\nchildren();\n}' in multi
    )  # a string literal keeps its text
    # Each colour's piece is given to it on its own.
    assert (
        "module o1() {\n"
        '  color("#F08A24") o1_op0() {\n'
        '    import("meshes/o1-p1.3mf");\n'
        "  }\n"
        '  color("#3A7D2C") o1_op0() {\n'
        '    import("meshes/o1-p2.3mf");\n'
        "  }\n"
        "}\n"
    ) in text


def test_the_object_is_placed_by_its_transform() -> None:
    text = compile_scene(
        scene(
            carrot(transform=Transform(translate=(5, -2.25, 0), rotate=(90, 0, 0), scale=(1, 1, 2)))
        )
    )
    assert text.endswith("translate([5, -2.25, 0]) rotate([90, 0, 0]) scale([1, 1, 2]) o1();\n")


def body(text: str, oid: str) -> list[str]:
    """The lines of ``module <oid>() {…}``, without its braces."""
    lines = text.splitlines()
    start = lines.index(f"module {oid}() {{")
    return lines[start + 1 : lines.index("}", start)]


def test_every_cutter_is_inside_its_pieces_colour() -> None:
    # A cutter outside every color() makes faces of no colour (the Default material) or
    # of a third one, in OpenSCAD 2026.10.05's Manifold 3MF.
    cut = Cut(point=(0, 0, 30), normal=(0, 0, 1), keep="below")
    socket = Joint(
        at=(0, 0, 30), axis=(0, 0, -1), diameter=4, depth=6, clearance=0.2, role="socket"
    )
    hole = Subtract(primitive="sphere", size=(3, 3, 3))
    text = compile_scene(scene(carrot(ops=[cut, socket, hole])))
    statements = body(text, "o1")
    assert [line for line in statements if not line.startswith("    ")] == [
        '  color("#F08A24") difference() {',
        "  }",
        '  color("#3A7D2C") difference() {',
        "  }",
    ]
    for colour in ("#F08A24", "#3A7D2C"):
        start = statements.index(f'  color("{colour}") difference() {{')
        piece = "\n".join(statements[start : statements.index("  }", start)])
        assert piece.count("cube(4000);") == 1
        assert piece.count('"flat", 0.2);') == 1
        assert piece.count("sphere(d = 3, $fn = 64);") == 1
