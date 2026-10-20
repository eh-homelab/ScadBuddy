"""#1863 — a library 3MF's objects, as Arrange reads them."""

from __future__ import annotations

import io
import json
import tempfile
import zipfile
from pathlib import Path

import numpy as np
import pytest
import trimesh

from scadbuddy.bambuddy.library_view import plate_glb
from scadbuddy.render.bambu3mf import (
    IDENTITY,
    PlateParts,
    stl_3mf,
    write_bambu_3mf,
    write_plates_3mf,
)
from scadbuddy.render.geometry import NoSuchPlateError, analyze_geometry, parts_from_3mf
from scadbuddy.render.glb import bounding_box, read_glb
from scadbuddy.render.jobs import LAYOUT_NAME, PlateLayout
from scadbuddy.render.objects3mf import (
    OverBudgetError,
    UnreadableObjectsError,
    check_stl,
    read_objects,
    read_plate_parts,
    write_piece,
)
from scadbuddy.render.read_budget import ReadBudget
from scadbuddy.render.split import ColourPart, painted_pieces

CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
PROD = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"


def _box(x: float, y: float, z: float) -> trimesh.Trimesh:
    box: trimesh.Trimesh = trimesh.creation.box(extents=(x, y, z))
    box.apply_translation((x / 2, y / 2, z / 2))
    return box


def _mesh_xml(mesh: trimesh.Trimesh, object_id: int, *, triangle_extra: str = "") -> str:
    vertices = "".join(f'<vertex x="{v[0]}" y="{v[1]}" z="{v[2]}"/>' for v in mesh.vertices)
    triangles = "".join(
        f'<triangle v1="{f[0]}" v2="{f[1]}" v3="{f[2]}"{triangle_extra}/>' for f in mesh.faces
    )
    return (
        f'<object id="{object_id}" type="model"><mesh><vertices>{vertices}</vertices>'
        f"<triangles>{triangles}</triangles></mesh></object>"
    )


def _zip(entries: dict[str, str | bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def bambu_project(
    *,
    items: list[str],
    parts: dict[int, tuple[trimesh.Trimesh, str, int | None]],
    object_extruder: int = 1,
    component_transform: str = "1 0 0 0 1 0 0 0 1 0 0 0",
    triangle_extra: str = "",
    colours: tuple[str, ...] = ("#FF0000", "#00FF00"),
    types: tuple[str, ...] | None = None,
    sliced: bool = False,
) -> bytes:
    """A Bambu Studio project: object 10 of components in one object file, one build
    item per transform in ``items``; ``parts`` is component id → (mesh, subtype, extruder)."""
    objects = "".join(
        _mesh_xml(mesh, cid, triangle_extra=triangle_extra) for cid, (mesh, _, _) in parts.items()
    )
    components = "".join(
        f'<component p:path="/3D/Objects/object_1.model" objectid="{cid}"'
        f' transform="{component_transform}"/>'
        for cid in parts
    )
    root = (
        f'<model unit="millimeter" xmlns="{CORE}" xmlns:p="{PROD}"><resources>'
        f'<object id="10" name="Widget" type="model"><components>{components}</components>'
        "</object></resources><build>"
        + "".join(f'<item objectid="10" transform="{t}" printable="1"/>' for t in items)
        + "</build></model>"
    )
    settings = "".join(
        f'<part id="{cid}" subtype="{subtype}">'
        + (f'<metadata key="extruder" value="{extruder}"/>' if extruder is not None else "")
        + "</part>"
        for cid, (_, subtype, extruder) in parts.items()
    )
    model_settings = (
        f'<config><object id="10"><metadata key="name" value="Widget"/>'
        f'<metadata key="extruder" value="{object_extruder}"/>{settings}</object></config>'
    )
    entries: dict[str, str | bytes] = {
        "3D/3dmodel.model": root,
        "3D/Objects/object_1.model": (
            f'<model unit="millimeter" xmlns="{CORE}"><resources>{objects}</resources>'
            "<build/></model>"
        ),
        "Metadata/model_settings.config": model_settings,
        "Metadata/project_settings.config": json.dumps(
            {"filament_colour": list(colours)}
            | ({} if types is None else {"filament_type": list(types)})
        ),
    }
    if sliced:
        entries["Metadata/plate_1.gcode"] = "; sliced"
    return _zip(entries)


def at(x: float, y: float) -> str:
    return f"1 0 0 0 1 0 0 0 1 {x} {y} 0"


def test_a_bambu_project_is_its_objects_with_their_counts_and_colours() -> None:
    body = _box(20, 10, 5)
    cap = _box(20, 10, 2)
    cap.apply_translation((0, 0, 5))
    payload = bambu_project(
        items=[at(50, 50), at(100, 50)],
        parts={1: (body, "normal_part", None), 2: (cap, "normal_part", 2)},
    )
    [obj] = read_objects(payload)
    assert obj.name == "Widget" and obj.count == 2
    assert [p.colour for p in obj.parts] == ["#FF0000", "#00FF00"]
    low, high = (
        np.min([p.mesh.bounds[0] for p in obj.parts], axis=0),
        np.max([p.mesh.bounds[1] for p in obj.parts], axis=0),
    )
    assert tuple(np.round(high - low, 3)) == (20, 10, 7)


def test_an_object_keeps_the_material_the_project_states_for_each_of_its_colours() -> None:
    """A PLA keychain arranged must not reach the slicer as a slot of no material,
    which Bambuddy's slice dialog filled with Bambu ABS. A colour the object does not
    print in is not its."""
    payload = bambu_project(
        items=[at(50, 50)],
        parts={1: (_box(20, 10, 5), "normal_part", None)},
        colours=("#ff0000", "#00FF00", "#0000FF"),
        types=("PLA", "PETG", " "),
    )
    [obj] = read_objects(payload)
    assert obj.materials == {"#FF0000": "PLA"}


def test_one_colour_in_two_materials_is_of_unknown_material() -> None:
    payload = bambu_project(
        items=[at(50, 50)],
        parts={1: (_box(20, 10, 5), "normal_part", 1), 2: (_box(5, 5, 5), "normal_part", 3)},
        colours=("#FF0000", "#00FF00", "#FF0000"),
        types=("PLA", "PLA", "PETG"),
    )
    [obj] = read_objects(payload)
    assert obj.materials == {}


def test_a_project_stating_no_material_leaves_its_colours_without_one() -> None:
    payload = bambu_project(items=[at(50, 50)], parts={1: (_box(20, 10, 5), "normal_part", 2)})
    [obj] = read_objects(payload)
    assert obj.materials == {}


def test_an_item_turned_another_way_is_an_object_of_its_own() -> None:
    payload = bambu_project(
        items=[at(50, 50), "0 1 0 -1 0 0 0 0 1 100 50 0", at(10, 10)],
        parts={1: (_box(20, 10, 5), "normal_part", None)},
    )
    objects = read_objects(payload)
    assert [o.count for o in objects] == [2, 1]
    sizes = [tuple(np.round(o.parts[0].mesh.extents, 3)) for o in objects]
    assert sizes == [(20, 10, 5), (10, 20, 5)]


def test_a_component_transform_applies() -> None:
    payload = bambu_project(
        items=[at(0, 0)],
        parts={1: (_box(20, 10, 5), "normal_part", None)},
        component_transform="2 0 0 0 2 0 0 0 2 0 0 0",
    )
    [obj] = read_objects(payload)
    assert tuple(np.round(obj.parts[0].mesh.extents, 3)) == (40, 20, 10)


def test_a_modifier_is_dropped() -> None:
    payload = bambu_project(
        items=[at(0, 0)],
        parts={
            1: (_box(20, 10, 5), "normal_part", None),
            2: (_box(90, 90, 90), "modifier_part", 2),
        },
    )
    [obj] = read_objects(payload)
    assert [p.colour for p in obj.parts] == ["#FF0000"]
    assert obj.notes and "modifier" in obj.notes[0]


def _cut_project() -> bytes:
    """A 20x10x5 body with a 5 mm cube cut out of it, and the cube sticking out above."""
    cutter = _box(5, 5, 8)
    cutter.apply_translation((2, 2, 0))
    return bambu_project(
        items=[at(0, 0)],
        parts={1: (_box(20, 10, 5), "normal_part", None), 2: (cutter, "negative_part", 2)},
    )


def test_a_negative_part_is_carried_with_its_object_for_the_slicer() -> None:
    [obj] = read_objects(_cut_project())
    body, cut = obj.parts
    assert not body.negative and cut.negative
    assert tuple(np.round(cut.mesh.extents, 3)) == (5, 5, 8)
    # It prints nothing: the box is the body's, and it names no filament of its own.
    assert tuple(round(v, 3) for v in bounding_box(obj.parts).size) == (20, 10, 5)
    assert cut.colour == body.colour


def _two_cuts_project() -> bytes:
    """Library 2440's shape (#4061): a body with two negative parts that overlap."""
    hole = _box(5, 5, 8)
    hole.apply_translation((2, 2, 0))
    cleanup = _box(6, 3, 3)
    cleanup.apply_translation((4, 3, 1))
    return bambu_project(
        items=[at(0, 0)],
        parts={
            1: (_box(20, 10, 5), "normal_part", None),
            2: (hole, "negative_part", 1),
            3: (cleanup, "negative_part", 1),
        },
    )


def test_each_negative_part_stays_a_part_of_its_own() -> None:
    """#4061: joined into one mesh, an object's two overlapping negative parts failed
    Bambu Studio's toolpath stage (exit 156); the source, slicing, keeps them apart."""
    [obj] = read_objects(_two_cuts_project())
    body, hole, cleanup = obj.parts
    assert not body.negative and hole.negative and cleanup.negative
    assert (hole.name, cleanup.name) == ("Negative part 1", "Negative part 2")
    assert tuple(np.round(hole.mesh.extents, 3)) == (5, 5, 8)
    assert tuple(np.round(cleanup.mesh.extents, 3)) == (6, 3, 3)


def test_a_plate_read_leaves_a_negative_part_out() -> None:
    read = read_plate_parts(_cut_project(), 1)
    assert [p.negative for p in read.parts] == [False]
    assert tuple(np.round(read.parts[0].mesh.extents, 3)) == (20, 10, 5)


def test_a_piece_keeps_its_negative_part(tmp_path: Path) -> None:
    [obj] = read_objects(_cut_project())
    piece = tmp_path / "piece"
    piece.mkdir()
    box = write_piece(piece, obj)
    assert tuple(round(v, 3) for v in box.size) == (20, 10, 5)
    parts = PlateLayout.load(piece / LAYOUT_NAME).plates[0].parts
    assert [p.negative for p in parts] == [False, True]
    assert len(parts[1].mesh.faces) == len(obj.parts[1].mesh.faces)


def test_a_painted_object_is_one_part_carrying_its_painting() -> None:
    # Part 1 prints with extruder 1 (red), every face painted with extruder 2 (green);
    # part 2 is plain extruder 2 (#1965).
    body = _box(20, 10, 5)
    cap = _box(20, 10, 2)
    cap.apply_translation((0, 0, 5))
    payload = bambu_project(
        items=[at(50, 50)],
        parts={1: (body, "normal_part", 1)},
        triangle_extra=' paint_color="8"',
    )
    [obj] = read_objects(payload)
    [part] = obj.parts
    assert part.colour == "#FF0000"
    assert part.paint is not None
    assert part.paint.codes == ("8",) * len(body.faces)
    assert part.paint.used() == ["#00FF00"]
    assert len(part.mesh.faces) == len(body.faces)


def test_a_painted_part_keeps_its_faces_in_order_beside_plain_parts() -> None:
    body = _box(20, 10, 5)
    cap = _box(20, 10, 2)
    cap.apply_translation((0, 0, 5))
    # Only the first face of the body is painted; the cap shares the body's colour.
    objects = (
        _mesh_xml(body, 1)
        .replace('"/>', '" paint_color="8"/>', len(body.vertices) + 1)
        .replace(' paint_color="8"', "", len(body.vertices))
    )
    payload = _painted_project(objects + _mesh_xml(cap, 2))
    [obj] = read_objects(payload)
    plain, painted = obj.parts
    assert plain.colour == painted.colour == "#FF0000" and plain.paint is None
    assert painted.paint is not None
    assert painted.paint.codes == ("8",) + ("",) * (len(body.faces) - 1)
    assert np.allclose(painted.mesh.vertices[painted.mesh.faces[0]], body.vertices[body.faces[0]])


def _painted_project(objects: str) -> bytes:
    """`bambu_project`'s shape with the object file's meshes given as XML, both parts
    extruder 1, at the origin."""
    root = (
        f'<model unit="millimeter" xmlns="{CORE}" xmlns:p="{PROD}"><resources>'
        '<object id="10" name="Widget" type="model"><components>'
        '<component p:path="/3D/Objects/object_1.model" objectid="1"/>'
        '<component p:path="/3D/Objects/object_1.model" objectid="2"/>'
        '</components></object></resources><build><item objectid="10"/></build></model>'
    )
    settings = (
        '<config><object id="10"><metadata key="name" value="Widget"/>'
        '<metadata key="extruder" value="1"/><part id="1" subtype="normal_part"/>'
        '<part id="2" subtype="normal_part"/></object></config>'
    )
    return _zip(
        {
            "3D/3dmodel.model": root,
            "3D/Objects/object_1.model": (
                f'<model unit="millimeter" xmlns="{CORE}"><resources>{objects}</resources>'
                "<build/></model>"
            ),
            "Metadata/model_settings.config": settings,
            "Metadata/project_settings.config": json.dumps(
                {"filament_colour": ["#FF0000", "#00FF00"]}
            ),
        }
    )


@pytest.mark.parametrize(
    ("items", "extra", "colours", "match"),
    [
        ([at(0, 0)], ' paint_color="1C"', ("#FF0000", "#00FF00"), "extruder 4"),
        ([at(0, 0)], ' paint_color="1"', ("#FF0000", "#00FF00"), "cannot be read"),
        ([at(0, 0)], ' paint_color="8"', (), "no Bambu Studio filaments"),
    ],
)
def test_painting_that_cannot_be_carried_over_is_refused(
    items: list[str], extra: str, colours: tuple[str, ...], match: str
) -> None:
    payload = bambu_project(
        items=items,
        parts={1: (_box(20, 10, 5), "normal_part", None)},
        triangle_extra=extra,
        colours=colours,
    )
    with pytest.raises(UnreadableObjectsError, match=match):
        read_objects(payload)


def test_a_sliced_file_is_refused() -> None:
    payload = bambu_project(
        items=[at(0, 0)], parts={1: (_box(20, 10, 5), "normal_part", None)}, sliced=True
    )
    with pytest.raises(UnreadableObjectsError, match="sliced"):
        read_objects(payload)


def test_a_file_with_no_geometry_is_refused() -> None:
    payload = _zip({"3D/3dmodel.model": f'<model xmlns="{CORE}"><resources/><build/></model>'})
    with pytest.raises(UnreadableObjectsError, match="no object"):
        read_objects(payload)
    with pytest.raises(UnreadableObjectsError):
        read_objects(b"not a zip")


def _distinct(count: int) -> bytes:
    """``count`` copies of one part, each turned a different way: ``count`` objects."""
    return bambu_project(
        items=[
            f"{np.cos(i)} {np.sin(i)} 0 {-np.sin(i)} {np.cos(i)} 0 0 0 1 0 0 0"
            for i in range(count)
        ],
        parts={1: (_box(9, 5, 1), "normal_part", None)},
    )


def test_too_many_objects_is_refused() -> None:
    items = [f"0 1 0 -1 0 0 0 0 1 {i} 0 0" if i % 2 else at(i, 0) for i in range(4)]
    assert (
        len(
            read_objects(
                bambu_project(items=items, parts={1: (_box(9, 5, 1), "normal_part", None)})
            )
        )
        == 2
    )
    default = ReadBudget().max_objects
    with pytest.raises(UnreadableObjectsError, match=f"more than {default:,} distinct objects"):
        read_objects(_distinct(default + 1))


def test_a_refusal_names_its_budget_and_how_to_raise_it() -> None:
    with pytest.raises(UnreadableObjectsError) as refused:
        read_objects(_distinct(4), budget=ReadBudget(max_objects=3))
    message = str(refused.value)
    assert "max_objects read budget" in message
    assert "SCADBUDDY_READ_MAX_OBJECTS" in message
    assert "read_budget.max_objects" in message and "?max_objects=" in message


def test_a_larger_budget_reads_what_the_default_refuses() -> None:
    default = ReadBudget().max_objects
    many = _distinct(default + 1)
    with pytest.raises(UnreadableObjectsError):
        read_objects(many)
    assert len(read_objects(many, budget=ReadBudget(max_objects=default + 1))) == default + 1


def test_a_core_3mf_is_coloured_by_its_materials() -> None:
    mesh = _box(10, 10, 10)
    half = len(mesh.faces) // 2
    vertices = "".join(f'<vertex x="{v[0]}" y="{v[1]}" z="{v[2]}"/>' for v in mesh.vertices)
    triangles = "".join(
        f'<triangle v1="{f[0]}" v2="{f[1]}" v3="{f[2]}" pid="1" p1="{0 if i < half else 1}"/>'
        for i, f in enumerate(mesh.faces)
    )
    root = (
        f'<model unit="centimeter" xmlns="{CORE}"><resources><basematerials id="1">'
        '<base name="Red" displaycolor="#FF0000FF"/><base name="Blue" displaycolor="#0000FF"/>'
        f'</basematerials><object id="2" name="Cube" type="model"><mesh><vertices>{vertices}'
        f"</vertices><triangles>{triangles}</triangles></mesh></object></resources>"
        '<build><item objectid="2"/></build></model>'
    )
    [obj] = read_objects(_zip({"3D/3dmodel.model": root}))
    assert obj.name == "Cube" and obj.count == 1
    assert sorted(p.colour for p in obj.parts) == ["#0000FF", "#FF0000"]
    # centimetres
    joined = trimesh.util.concatenate([p.mesh for p in obj.parts])
    assert tuple(np.round(joined.extents, 3)) == (100, 100, 100)


MATERIAL = "http://schemas.microsoft.com/3dmanufacturing/material/2015/02"


def _two_tone(group: str) -> bytes:
    """A box whose triangles name group 1, half its index 0 and half its index 1."""
    mesh = _box(10, 10, 10)
    half = len(mesh.faces) // 2
    vertices = "".join(f'<vertex x="{v[0]}" y="{v[1]}" z="{v[2]}"/>' for v in mesh.vertices)
    triangles = "".join(
        f'<triangle v1="{f[0]}" v2="{f[1]}" v3="{f[2]}" pid="1" p1="{0 if i < half else 1}"/>'
        for i, f in enumerate(mesh.faces)
    )
    root = (
        f'<model xmlns="{CORE}" xmlns:m="{MATERIAL}"><resources>{group}'
        f'<object id="2" name="Cube" type="model"><mesh><vertices>{vertices}'
        f"</vertices><triangles>{triangles}</triangles></mesh></object></resources>"
        '<build><item objectid="2"/></build></model>'
    )
    return _zip({"3D/3dmodel.model": root})


def test_a_core_3mf_is_coloured_by_its_colour_group() -> None:
    """#2147: a tool that colours triangles by ``m:colorgroup`` arranges in its colours,
    not as one grey."""
    group = (
        '<m:colorgroup id="1"><m:color color="#FF0000FF"/><m:color color="#0000FF"/></m:colorgroup>'
    )
    [obj] = read_objects(_two_tone(group))
    assert sorted(p.colour for p in obj.parts) == ["#0000FF", "#FF0000"]


def test_a_texture_coloured_3mf_is_refused_rather_than_read_grey() -> None:
    """#2147: a texture's colours cannot be read, so the user is told, as for a part
    painted in PrusaSlicer."""
    group = (
        '<m:texture2dgroup id="1" texid="9"><m:tex2coord u="0" v="0"/>'
        '<m:tex2coord u="1" v="1"/></m:texture2dgroup>'
    )
    with pytest.raises(UnreadableObjectsError, match="texture"):
        read_objects(_two_tone(group))


def test_triangles_are_grouped_by_colour_in_the_order_their_colours_first_come() -> None:
    """Many materials, interleaved: the grouping is one sort, not a pass per colour."""
    colours = [f"#{i:06X}" for i in range(0, 3000 * 0x10, 0x10)]
    bases = "".join(f'<base name="m{i}" displaycolor="{c}"/>' for i, c in enumerate(colours))
    order = [i // 2 if i % 2 == 0 else len(colours) - 1 - i // 2 for i in range(len(colours))]
    triangles = "".join(
        f'<triangle v1="{3 * n}" v2="{3 * n + 1}" v3="{3 * n + 2}" pid="1" p1="{m}"/>'
        for n, m in enumerate(order)
    )
    vertices = "".join(
        f'<vertex x="{n}" y="{k}" z="{k * n % 7}"/>' for n in range(len(order)) for k in range(3)
    )
    root = (
        f'<model xmlns="{CORE}"><resources><basematerials id="1">{bases}</basematerials>'
        f'<object id="2" type="model"><mesh><vertices>{vertices}</vertices>'
        f"<triangles>{triangles}</triangles></mesh></object></resources>"
        '<build><item objectid="2"/></build></model>'
    )

    [obj] = read_objects(_zip({"3D/3dmodel.model": root}))

    assert [p.colour for p in obj.parts] == [colours[m] for m in order]
    assert all(len(p.mesh.faces) == 1 for p in obj.parts)


def test_scadbuddys_own_3mf_reads_back_as_one_object_per_plate(tmp_path: Path) -> None:
    red = ColourPart(1, "Color 1", "#FF0000", _box(10, 10, 4))
    blue = ColourPart(2, "Color 2", "#0000FF", _box(6, 6, 9))
    out = tmp_path / "model.3mf"
    write_plates_3mf(
        [PlateParts((red, blue), (1, 2)), PlateParts((blue,), (2,))],
        ["#FF0000", "#0000FF"],
        out,
        thumbnails=None,
    )
    objects = read_objects(out.read_bytes())
    assert [[p.colour for p in o.parts] for o in objects] == [["#FF0000", "#0000FF"], ["#0000FF"]]
    assert [o.count for o in objects] == [1, 1]


def test_an_stl_wrapped_by_the_print_path_is_one_white_object() -> None:
    buffer = io.BytesIO()
    _box(30, 20, 10).export(buffer, file_type="stl")
    [obj] = read_objects(stl_3mf(buffer.getvalue(), model_name="bracket"))
    assert obj.count == 1 and [p.colour for p in obj.parts] == ["#FFFFFF"]


def test_a_written_piece_loads_back_as_a_layout(tmp_path: Path) -> None:
    red = ColourPart(1, "Color 1", "#FF0000", _box(10, 10, 4))
    blue = ColourPart(2, "Color 2", "#0000FF", _box(6, 6, 9))
    out = tmp_path / "model.3mf"
    write_bambu_3mf([red, blue], out, thumbnails=None)
    [obj] = read_objects(out.read_bytes())
    piece = tmp_path / "piece"
    piece.mkdir()
    box = write_piece(piece, obj)
    layout = PlateLayout.load(piece / LAYOUT_NAME)
    assert [p.colour for p in layout.plates[0].parts] == ["#FF0000", "#0000FF"]
    assert tuple(round(v, 3) for v in box.size) == (10, 10, 9)
    assert len(layout.plates[0].parts[1].mesh.faces) == len(blue.mesh.faces)


def test_painting_counts_against_a_budget_each_time_its_mesh_is_placed() -> None:
    # A box has 12 faces, each painted "8": 12 digits each time a painted mesh is
    # placed. Two painted parts spend 24, so a budget of 20 takes one and not two.
    budget = ReadBudget(max_paint_digits=20)
    once = bambu_project(
        items=[at(0, 0)],
        parts={1: (_box(20, 10, 5), "normal_part", 1)},
        triangle_extra=' paint_color="8"',
    )
    assert read_objects(once, budget=budget)
    twice = bambu_project(
        items=[at(0, 0)],
        parts={1: (_box(20, 10, 5), "normal_part", 1), 2: (_box(20, 10, 5), "normal_part", 1)},
        triangle_extra=' paint_color="8"',
    )
    with pytest.raises(UnreadableObjectsError, match="max_paint_digits read budget"):
        read_objects(twice, budget=budget)


def test_a_painted_piece_loads_back_with_its_painting(tmp_path: Path) -> None:
    body = _box(20, 10, 5)
    payload = bambu_project(
        items=[at(0, 0)],
        parts={1: (body, "normal_part", 1)},
        triangle_extra=' paint_color="8"',
        colours=("#FF0000", "#00FF00", "#0000FF"),
    )
    [obj] = read_objects(payload)
    piece = tmp_path / "piece"
    piece.mkdir()
    write_piece(piece, obj)
    [part] = PlateLayout.load(piece / LAYOUT_NAME).plates[0].parts
    assert part.colour == "#FF0000"
    assert part.paint is not None
    # Renumbered to the piece's own materials, but still green.
    assert part.paint.used() == ["#00FF00"]
    assert len(part.paint.codes) == len(part.mesh.faces) == len(body.faces)


def _one_triangle(vertex: str, extra: str = "") -> bytes:
    root = (
        f'<model xmlns="{CORE}" xmlns:slic3rpe="http://schemas.slic3r.org/3mf/2017/06">'
        '<resources><object id="1" type="model"><mesh><vertices>'
        f'<vertex x="0" y="0" z="0"/><vertex x="1" y="0" z="0"/>{vertex}</vertices>'
        f'<triangles><triangle v1="0" v2="1" v3="2"{extra}/></triangles></mesh></object>'
        '</resources><build><item objectid="1"/></build></model>'
    )
    return _zip({"3D/3dmodel.model": root})


def test_a_non_finite_vertex_or_prusa_painting_is_refused() -> None:
    assert read_objects(_one_triangle('<vertex x="0" y="1" z="0"/>'))
    with pytest.raises(UnreadableObjectsError, match="finite"):
        read_objects(_one_triangle('<vertex x="nan" y="1" z="0"/>'))
    with pytest.raises(UnreadableObjectsError, match="painted in PrusaSlicer"):
        read_objects(_one_triangle('<vertex x="0" y="1" z="0"/>', ' slic3rpe:mmu_segmentation="4"'))


def _with(index: int, value: str) -> str:
    """The identity transform with value ``index`` (of 12) replaced."""
    values = ["1", "0", "0", "0", "1", "0", "0", "0", "1", "0", "0", "0"]
    values[index] = value
    return " ".join(values)


@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
@pytest.mark.parametrize("index", range(12))
def test_a_non_finite_transform_value_is_refused_wherever_it_sits(index: int, value: str) -> None:
    # Each of the 12 values (the 3x3 part and the translation), on a build item and on
    # a component.
    box: dict[int, tuple[trimesh.Trimesh, str, int | None]] = {
        1: (_box(20, 10, 5), "normal_part", None)
    }
    transform = _with(index, value)
    with pytest.raises(UnreadableObjectsError, match="finite"):
        read_objects(bambu_project(items=[transform], parts=box))
    with pytest.raises(UnreadableObjectsError, match="finite"):
        read_objects(bambu_project(items=[at(0, 0)], parts=box, component_transform=transform))


def test_finite_transforms_that_overflow_together_are_refused() -> None:
    box: dict[int, tuple[trimesh.Trimesh, str, int | None]] = {
        1: (_box(20, 10, 5), "normal_part", None)
    }
    huge = "1e200 0 0 0 1e200 0 0 0 1e200 0 0 0"
    # Combined: a build item and a component each finite, their product is not.
    with pytest.raises(UnreadableObjectsError, match="finite"):
        read_objects(bambu_project(items=[huge], parts=box, component_transform=huge))
    # Applied: a finite transform whose product with a vertex overflows.
    with pytest.raises(UnreadableObjectsError, match="finite"):
        read_objects(bambu_project(items=["1e308 0 0 0 1e308 0 0 0 1e308 0 0 0"], parts=box))


def test_a_plates_parts_are_read_with_the_same_refusal() -> None:
    box: dict[int, tuple[trimesh.Trimesh, str, int | None]] = {
        1: (_box(20, 10, 5), "normal_part", None)
    }
    with pytest.raises(UnreadableObjectsError, match="finite"):
        read_plate_parts(bambu_project(items=["1e308 0 0 0 1e308 0 0 0 1e308 0 0 0"], parts=box), 1)


def test_a_plates_parts_are_placed_as_the_file_places_them() -> None:
    body = _box(20, 10, 5)
    cap = _box(20, 10, 2)
    cap.apply_translation((0, 0, 5))
    payload = bambu_project(
        items=[at(50, 50), at(100, 50)],
        parts={1: (body, "normal_part", None), 2: (cap, "normal_part", 2)},
    )
    read = read_plate_parts(payload, 1)
    assert read.plates == 1
    assert [p.colour for p in read.parts] == ["#FF0000", "#00FF00"]
    joined = trimesh.util.concatenate([p.mesh for p in read.parts])
    assert tuple(np.round(joined.bounds[0], 3)) == (50, 50, 0)
    assert tuple(np.round(joined.bounds[1], 3)) == (120, 60, 7)
    with pytest.raises(NoSuchPlateError):
        read_plate_parts(payload, 2)


def test_each_plate_of_a_multi_plate_file_is_read_on_its_own(tmp_path: Path) -> None:
    red = ColourPart(1, "Color 1", "#FF0000", _box(10, 10, 4))
    blue = ColourPart(2, "Color 2", "#0000FF", _box(6, 6, 9))
    out = tmp_path / "model.3mf"
    write_plates_3mf(
        [PlateParts((red, blue), (1, 2)), PlateParts((blue,), (2,))],
        ["#FF0000", "#0000FF"],
        out,
        thumbnails=None,
    )
    first, second = (read_plate_parts(out.read_bytes(), plate) for plate in (1, 2))
    assert (first.plates, second.plates) == (2, 2)
    assert [p.colour for p in first.parts] == ["#FF0000", "#0000FF"]
    assert [p.colour for p in second.parts] == ["#0000FF"]
    with pytest.raises(NoSuchPlateError):
        read_plate_parts(out.read_bytes(), 3)


def test_a_plate_is_refused_as_the_objects_are() -> None:
    with pytest.raises(UnreadableObjectsError, match="max_visits read budget"):
        read_plate_parts(_fan_out(7, 10), 1, budget=ReadBudget(max_visits=1_000))
    with pytest.raises(UnreadableObjectsError, match="max_visits read budget"):
        read_plate_parts(_fan_out(7, 10), 1)


def _fan_out(levels: int, fan: int) -> bytes:
    """One triangle, named ``fan`` times by each of ``levels`` nested objects: a few KB
    that expands to ``fan ** levels`` meshes."""
    objects = [
        '<object id="1" type="model"><mesh><vertices><vertex x="0" y="0" z="0"/>'
        '<vertex x="1" y="0" z="0"/><vertex x="0" y="1" z="0"/></vertices>'
        '<triangles><triangle v1="0" v2="1" v3="2"/></triangles></mesh></object>'
    ]
    for level in range(2, levels + 2):
        components = f'<component objectid="{level - 1}"/>' * fan
        objects.append(
            f'<object id="{level}" type="model"><components>{components}</components></object>'
        )
    root = (
        f'<model xmlns="{CORE}"><resources>{"".join(objects)}</resources>'
        f'<build><item objectid="{levels + 1}"/></build></model>'
    )
    return _zip({"3D/3dmodel.model": root})


def test_components_that_fan_out_are_refused_before_they_are_expanded() -> None:
    # 10 ** 7 meshes from a file of a few KB: refused, not expanded. A lower budget than
    # the default only keeps the test quick; the default is the same kind of bound.
    with pytest.raises(UnreadableObjectsError, match="max_visits read budget"):
        read_objects(_fan_out(7, 10), budget=ReadBudget(max_visits=1_000))
    assert len(read_objects(_fan_out(2, 3))[0].parts[0].mesh.faces) == 9


def test_triangles_past_the_budget_are_refused() -> None:
    budget = ReadBudget(max_triangles=8)
    assert read_objects(_fan_out(1, 8), budget=budget)
    with pytest.raises(UnreadableObjectsError, match="more than 8 triangles"):
        read_objects(_fan_out(1, 9), budget=budget)


def test_an_archive_of_any_size_is_read_by_what_its_models_hold() -> None:
    # A real Bambu project carries thumbnails and attachments beside its models; their
    # size says nothing of what reading its objects costs, so it refuses nothing.
    payload = bambu_project(items=[at(0, 0)], parts={1: (_box(20, 10, 5), "normal_part", 1)})
    buffer = io.BytesIO(payload)
    with (
        zipfile.ZipFile(buffer, "a", zipfile.ZIP_DEFLATED) as archive,
        archive.open("Auxiliaries/huge.bin", "w", force_zip64=True) as entry,
    ):
        chunk = bytes(1024 * 1024)
        for _ in range(300):  # past the 256 MB the whole archive was once capped at
            entry.write(chunk)
    [obj] = read_objects(io.BytesIO(buffer.getvalue()))
    assert obj.count == 1


def test_a_model_storing_more_triangles_than_the_budget_stops_as_it_is_parsed() -> None:
    # Part 2 is never placed, so nothing expands it; storing it is what costs memory.
    payload = bambu_project(items=[at(0, 0)], parts={1: (_box(20, 10, 5), "normal_part", 1)})
    two = _zip(
        {
            "3D/3dmodel.model": (
                f'<model unit="millimeter" xmlns="{CORE}"><resources>'
                f"{_mesh_xml(_box(1, 1, 1), 1)}{_mesh_xml(_box(1, 1, 1), 2)}"
                '</resources><build><item objectid="1"/></build></model>'
            )
        }
    )
    assert read_objects(payload, budget=ReadBudget(max_triangles=12))
    assert read_objects(two, budget=ReadBudget(max_triangles=24))
    with pytest.raises(OverBudgetError, match=r"stores more than 23 triangles.*the file is"):
        read_objects(two, budget=ReadBudget(max_triangles=23))


def test_model_files_past_what_the_triangle_budget_allows_are_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("scadbuddy.render.read_budget.MODEL_BYTES_OVERHEAD", 4096)
    small = ReadBudget(max_triangles=12, max_paint_digits=1)
    padding = "<metadata name='note'>" + "x" * 8192 + "</metadata>"
    padded = _zip(
        {
            "3D/3dmodel.model": (
                f'<model unit="millimeter" xmlns="{CORE}">{padding}<resources>'
                f'{_mesh_xml(_box(1, 1, 1), 1)}</resources><build><item objectid="1"/>'
                "</build></model>"
            )
        }
    )
    with pytest.raises(OverBudgetError) as refused:
        read_objects(padded, budget=small)
    message = str(refused.value)
    # What limits it (the budget and its setting) and how large the file is.
    assert "inflate to 9," in message and "past the 7,169 bytes" in message
    assert "max_triangles read budget" in message and "SCADBUDDY_READ_MAX_TRIANGLES" in message
    assert f"(the file is {len(padded):,} bytes; its model files 9," in message
    assert read_objects(padded, budget=ReadBudget(max_triangles=100, max_paint_digits=1))


def test_a_text_stl_past_what_its_triangles_could_take_is_refused() -> None:
    text = io.BytesIO(b"solid big\n" + b" " * 2000 + b"\nendsolid big\n")
    check_stl(text, ReadBudget(max_triangles=10))  # 5,120 bytes allowed
    with pytest.raises(OverBudgetError, match="is a text STL of 2,"):
        check_stl(text, ReadBudget(max_triangles=3))
    assert text.tell() == 0


def test_vertices_are_budgeted_over_the_whole_file() -> None:
    # Nine vertices and one triangle: within three vertices for each allowed triangle
    # at a budget of 3, past it at 2, and said as vertices, not triangles.
    vertices = "".join(f'<vertex x="{i}" y="0" z="{i % 2}"/>' for i in range(9))
    payload = _one_triangle_model(vertices)
    assert read_objects(payload, budget=ReadBudget(max_triangles=3))
    with pytest.raises(OverBudgetError, match="stores more than 6 vertices"):
        read_objects(payload, budget=ReadBudget(max_triangles=2))


def _one_triangle_model(vertices: str) -> bytes:
    return _zip(
        {
            "3D/3dmodel.model": (
                f'<model unit="millimeter" xmlns="{CORE}"><resources><object id="1"'
                f' type="model"><mesh><vertices>{vertices}</vertices><triangles>'
                '<triangle v1="0" v2="1" v3="2"/></triangles></mesh></object></resources>'
                '<build><item objectid="1"/></build></model>'
            )
        }
    )


# --- #4015: a painted object that is also mirrored is carried over -------------------


def _code(bits: list[int]) -> str:
    """Bits, least significant first, as a ``paint_color``: hex digits last first."""
    digits = [
        "0123456789ABCDEF"[sum(bit << at for at, bit in enumerate(bits[start : start + 4]))]
        for start in range(0, len(bits), 4)
    ]
    return "".join(reversed(digits))


#: A triangle split on two sides into three pieces, painted 1, 2 and the part's own. Its
#: two pieces off the special corner are cut along a median, which a mirror moves to the
#: other median: no code says the mirrored triangle with its faces turned.
SPLIT = _code([0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0])
MIRROR = "-1 0 0 0 1 0 0 0 1 0 0 0"


def _triangles(parts: list[ColourPart]) -> dict[str, set[tuple[tuple[float, ...], ...]]]:
    """Each colour's printed triangles, each as its corners (rounded), in winding order
    from its lowest corner: equal for the same triangles facing the same way."""
    found: dict[str, set[tuple[tuple[float, ...], ...]]] = {}
    for part in painted_pieces([p for p in parts if not p.negative]):
        for face in np.asarray(part.mesh.vertices)[np.asarray(part.mesh.faces)]:
            corners = [tuple(round(float(v), 4) + 0.0 for v in corner) for corner in face]
            start = corners.index(min(corners))
            found.setdefault(part.colour, set()).add(tuple(corners[start:] + corners[:start]))
    return found


def _mirrored(
    found: dict[str, set[tuple[tuple[float, ...], ...]]],
) -> dict[str, set[tuple[tuple[float, ...], ...]]]:
    """``found`` mirrored in x, each triangle turned to face out again."""
    out: dict[str, set[tuple[tuple[float, ...], ...]]] = {}
    for colour, triangles in found.items():
        for triangle in triangles:
            corners = [(-x + 0.0, y, z) for x, y, z in reversed(triangle)]
            start = corners.index(min(corners))
            out.setdefault(colour, set()).add(tuple(corners[start:] + corners[:start]))
    return out


def _split_box(item: str) -> bytes:
    return bambu_project(
        items=[item],
        parts={1: (_box(20, 10, 5), "normal_part", None)},
        triangle_extra=f' paint_color="{SPLIT}"',
    )


def test_a_painted_mirrored_object_reads_facing_out_its_painting_where_it_prints() -> None:
    """Library file 2440's objects are mirrored (``-1 -0 -0 0 1 0 ...``) with painted
    eyes, and Arrange left them out. Its pieces now print the mirror image of the
    unmirrored object's, each facing out."""
    (plain,) = read_objects(_split_box(IDENTITY))
    (mirrored,) = read_objects(_split_box(MIRROR))

    (painted,) = [part for part in mirrored.parts if part.paint is not None]
    assert painted.paint is not None and painted.paint.reversed
    assert painted.mesh.volume > 0  # faces out, as every arranged part's do
    assert _triangles(mirrored.parts) == _mirrored(_triangles(plain.parts))
    # Filament 1 is the part's own colour: the split's pieces print in two.
    assert set(_triangles(plain.parts)) == {"#FF0000", "#00FF00"}


def test_a_mirrored_painting_is_written_mirrored_back_as_bambu_studio_keeps_it(
    tmp_path: Path,
) -> None:
    """The object file holds the source's triangles in their own corner order with their
    own codes, and the component mirrors them to where they print: what Bambu Studio
    reads, as it read the source's mirror in its item."""
    (mirrored,) = read_objects(_split_box(MIRROR))
    out = tmp_path / "arranged.3mf"
    write_plates_3mf(
        [PlateParts(tuple(mirrored.parts), tuple(range(1, len(mirrored.parts) + 1)))],
        ["#FF0000", "#00FF00"],
        out,
        thumbnails=None,
    )
    payload = out.read_bytes()

    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        root = archive.read("3D/3dmodel.model").decode()
    assert 'transform="-1 0 0 0 1 0 0 0 1 ' in root
    (again,) = read_objects(payload)
    # Read back through the component, as read from the source through its item.
    assert _at_origin(_triangles(again.parts)) == _at_origin(_triangles(mirrored.parts))
    # ScadBuddy's own reader puts it where it prints too, facing out.
    painted = next(p for p in parts_from_3mf(out) if p.colour == "#FF0000")
    assert painted.mesh.volume > 0


def _at_origin(
    found: dict[str, set[tuple[tuple[float, ...], ...]]],
) -> dict[str, set[tuple[tuple[float, ...], ...]]]:
    """``found`` moved so its lowest corner is at the origin: the plate placement aside."""
    low = np.min([c for triangles in found.values() for t in triangles for c in t], axis=0)
    return {
        colour: {
            tuple(
                tuple(round(float(v - o), 4) + 0.0 for v, o in zip(c, low, strict=True)) for c in t
            )
            for t in triangles
        }
        for colour, triangles in found.items()
    }


def test_a_mirrored_painting_survives_a_pieces_layout(tmp_path: Path) -> None:
    """Arrange keeps each object as a piece, its mesh file and ``layout.json``, until it
    packs them: the corner order is in the layout, which the mesh file cannot hold."""
    (mirrored,) = read_objects(_split_box(MIRROR))
    write_piece(tmp_path, mirrored)

    layout = PlateLayout.load(tmp_path / LAYOUT_NAME)

    (painted,) = [part for part in layout.plates[0].parts if part.paint is not None]
    assert painted.paint is not None and painted.paint.reversed
    assert _triangles(list(layout.plates[0].parts)) == _triangles(mirrored.parts)


def test_a_plate_with_a_mirrored_and_an_unmirrored_painting_of_one_colour() -> None:
    """#4074: a mirrored painting joins only its like, so the plate read has two parts of
    the one colour. Its consumers number parts by extruder slot, never key them by
    colour: the preview draws both, and the checks measure both."""
    payload = bambu_project(
        items=["1 0 0 0 1 0 0 0 1 0 0 0", "-1 0 0 0 1 0 0 0 1 60 0 0"],
        parts={1: (_box(20, 10, 5), "normal_part", None)},
        triangle_extra=f' paint_color="{SPLIT}"',
    )
    read = read_plate_parts(payload, 1)

    assert [part.colour for part in read.parts] == ["#FF0000", "#FF0000"]
    assert [part.paint is not None and part.paint.reversed for part in read.parts] == [
        False,
        True,
    ]
    assert len({part.material_index for part in read.parts}) == 2
    assert len({part.name for part in read.parts}) == 2
    with tempfile.TemporaryDirectory() as scratch:
        preview = Path(scratch) / "plate.glb"
        preview.write_bytes(plate_glb(payload, 1))
        drawn = read_glb(preview)
    # Both objects drawn, each in its painting's two colours.
    pieces = painted_pieces(read.parts)
    assert sorted((p.colour, len(p.mesh.faces)) for p in drawn) == sorted(
        (p.colour, len(p.mesh.faces)) for p in pieces
    )
    assert {p.colour for p in drawn} == {"#FF0000", "#00FF00"}
    analysis = analyze_geometry(read.parts, extruders=[part.material_index for part in read.parts])
    assert [part.part for part in analysis.parts] == [1, 2]
    assert all(part.triangles for part in analysis.parts)
