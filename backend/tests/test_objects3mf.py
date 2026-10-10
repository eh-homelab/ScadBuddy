"""#1863 — a library 3MF's objects, as Arrange reads them."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import numpy as np
import pytest
import trimesh

from scadbuddy.render import objects3mf
from scadbuddy.render.bambu3mf import (
    PlateParts,
    stl_3mf,
    write_bambu_3mf,
    write_plates_3mf,
)
from scadbuddy.render.geometry import NoSuchPlateError
from scadbuddy.render.jobs import LAYOUT_NAME, PlateLayout
from scadbuddy.render.objects3mf import (
    MAX_OBJECTS,
    UnreadableObjectsError,
    read_objects,
    read_plate_parts,
    write_piece,
)
from scadbuddy.render.split import ColourPart

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
        "Metadata/project_settings.config": json.dumps({"filament_colour": list(colours)}),
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


def test_a_modifier_is_dropped_and_a_negative_part_refused() -> None:
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
    negative = bambu_project(
        items=[at(0, 0)],
        parts={1: (_box(20, 10, 5), "normal_part", None), 2: (_box(5, 5, 5), "negative_part", 1)},
    )
    with pytest.raises(UnreadableObjectsError, match="negative"):
        read_objects(negative)


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
        (["-1 0 0 0 1 0 0 0 1 0 0 0"], ' paint_color="8"', ("#FF0000", "#00FF00"), "mirrored"),
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
    with pytest.raises(UnreadableObjectsError, match=str(MAX_OBJECTS)):
        read_objects(
            bambu_project(
                items=[
                    f"{np.cos(i)} {np.sin(i)} 0 {-np.sin(i)} {np.cos(i)} 0 0 0 1 0 0 0"
                    for i in range(MAX_OBJECTS + 1)
                ],
                parts={1: (_box(9, 5, 1), "normal_part", None)},
            )
        )


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


def test_painting_counts_against_a_budget_each_time_its_mesh_is_placed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A box has 12 faces, each painted "8": 12 digits each time a painted mesh is
    # placed. Two painted parts spend 24, so a budget of 20 takes one and not two.
    monkeypatch.setattr(objects3mf, "MAX_PAINT_DIGITS", 20)
    once = bambu_project(
        items=[at(0, 0)],
        parts={1: (_box(20, 10, 5), "normal_part", 1)},
        triangle_extra=' paint_color="8"',
    )
    assert read_objects(once)
    twice = bambu_project(
        items=[at(0, 0)],
        parts={1: (_box(20, 10, 5), "normal_part", 1), 2: (_box(20, 10, 5), "normal_part", 1)},
        triangle_extra=' paint_color="8"',
    )
    with pytest.raises(UnreadableObjectsError, match="too much painting"):
        read_objects(twice)


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


def test_a_plate_is_refused_as_the_objects_are(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(objects3mf, "MAX_VISITS", 1_000)
    mirrored = bambu_project(
        items=["-1 0 0 0 1 0 0 0 1 0 0 0"],
        parts={1: (_box(20, 10, 5), "normal_part", None)},
        triangle_extra=' paint_color="8"',
    )
    with pytest.raises(UnreadableObjectsError, match="painted and mirrored"):
        read_plate_parts(mirrored, 1)
    with pytest.raises(UnreadableObjectsError, match="too many"):
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


def test_components_that_fan_out_are_refused_before_they_are_expanded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 10 ** 7 meshes from a file of a few KB: refused, not expanded. A lower cap than
    # the default only keeps the test quick; the default is the same kind of bound.
    monkeypatch.setattr(objects3mf, "MAX_VISITS", 1_000)
    with pytest.raises(UnreadableObjectsError, match="too many"):
        read_objects(_fan_out(7, 10))
    assert len(read_objects(_fan_out(2, 3))[0].parts[0].mesh.faces) == 9


def test_triangles_past_the_cap_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(objects3mf, "MAX_TRIANGLES", 8)
    assert read_objects(_fan_out(1, 8))
    with pytest.raises(UnreadableObjectsError, match="too many"):
        read_objects(_fan_out(1, 9))
