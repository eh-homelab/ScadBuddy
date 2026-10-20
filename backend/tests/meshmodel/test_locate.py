"""Which scene object a point of a render lies in (spec 2026-10-11 §9)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import trimesh
from pydantic import TypeAdapter

from scadbuddy.meshmodel.edits import Edit, apply_edits
from scadbuddy.meshmodel.locate import attribute, object_at
from scadbuddy.meshmodel.meshes import mesh_loader
from scadbuddy.meshmodel.paint import placement
from scadbuddy.meshmodel.scene import SCENE_NAME, Scene, Transform, load_scene, value
from scadbuddy.render.geometry import analyze_geometry
from scadbuddy.render.objects3mf import ReadObject
from scadbuddy.render.split import ColourPart
from tests.meshmodel.test_paint import ORANGE, written
from tests.meshmodel.test_section import neck
from tests.test_geometry import carrot

EDITS = TypeAdapter(list[Edit])


def carrots(directory: Path, count: int = 2, mesh: trimesh.Trimesh | None = None) -> Scene:
    written(
        directory,
        [ReadObject("Carrot", count, [ColourPart(0, "Body", ORANGE, mesh or neck())])],
    )
    return load_scene((directory / SCENE_NAME).read_text())


def test_each_object_holds_its_own_points(tmp_path: Path) -> None:
    scene = carrots(tmp_path)
    load = mesh_loader(tmp_path)
    left, right = (value(o.transform.translate[0]) for o in scene.objects)
    assert object_at(scene, load, (left, 0, 32)) == "o1"
    assert object_at(scene, load, (right, 0, 32)) == "o2"
    assert object_at(scene, load, (left, 0, 60)) is None


def test_a_split_object_holds_each_half_on_its_side(tmp_path: Path) -> None:
    scene = carrots(tmp_path, count=1)
    load = mesh_loader(tmp_path)
    edit = {"edit": "split_at_plane", "object": "o1", "point": [0, 0, 20], "normal": [0, 0, 1]}
    split = apply_edits(scene, EDITS.validate_python([edit]), load)
    assert object_at(split, load, (0, 0, 10)) == "o1"
    assert object_at(split, load, (0, 0, 32)) == "o2"


def test_a_turned_object_is_found_in_its_own_frame(tmp_path: Path) -> None:
    scene = carrots(tmp_path, count=1)
    lying = scene.objects[0].model_copy(update={"transform": Transform(rotate=(0.0, 90.0, 0.0))})
    scene = scene.model_copy(update={"objects": [lying]})
    # rotate y 90 sends +z to +x: the neck (z 32) lies at x 32.
    assert object_at(scene, mesh_loader(tmp_path), (32, 0, 0)) == "o1"
    assert object_at(scene, mesh_loader(tmp_path), (0, 0, 32)) is None


def test_attribute_names_the_object_of_each_weak_joint(tmp_path: Path) -> None:
    carrots(tmp_path, count=1, mesh=carrot())
    analysis = analyze_geometry([ColourPart(1, "Body", ORANGE, carrot())])
    [joint] = attribute(analysis, tmp_path).weak_joints
    assert joint.suggestion.object == "o1"


def test_attribute_leaves_a_hand_edited_or_plain_model_alone(tmp_path: Path) -> None:
    analysis = analyze_geometry([ColourPart(1, "Body", ORANGE, carrot())])
    assert analysis.weak_joints
    assert attribute(analysis, tmp_path) == analysis  # no scene
    carrots(tmp_path, count=1, mesh=carrot())
    (tmp_path / "model.scad").write_text("cube(1);\n")
    assert attribute(analysis, tmp_path) == analysis


def test_a_suggestion_cuts_a_moved_and_turned_object_at_the_joints_world_centroid(
    tmp_path: Path,
) -> None:
    """The suggestion is in world coordinates; applied to an object that is turned and
    moved, the cut must land on the weak joint, not on the object's own frame's z."""
    # Off its own axis, so the turn about z moves the neck: a cut that ignored the
    # rotation would land 12 mm away.
    scene = carrots(tmp_path, count=1, mesh=carrot().apply_translation((12.0, 0.0, 0.0)))
    moved = scene.objects[0].model_copy(
        update={"transform": Transform(translate=(40.0, -15.0, 3.0), rotate=(0.0, 0.0, 90.0))}
    )
    scene = scene.model_copy(update={"objects": [moved]})
    m, t = placement(moved)
    load = mesh_loader(tmp_path)
    # Turned about the object's z and raised: the neck (object z 30..35) is world z 33..38.
    world = trimesh.Trimesh(
        vertices=np.asarray(load(moved.parts[0].mesh).vertices) @ m.T + t,
        faces=load(moved.parts[0].mesh).faces,
        process=False,
    )
    [joint] = analyze_geometry([ColourPart(1, "Body", ORANGE, world)]).weak_joints
    assert object_at(scene, load, joint.centroid) == "o1"
    edit = {**joint.suggestion.model_dump(), "object": "o1"}
    split = apply_edits(scene, EDITS.validate_python([edit]), load)
    [cut] = (op for op in split.objects[0].ops if hasattr(op, "point"))
    local = np.asarray([value(x) for x in cut.point])
    assert m @ local + t == pytest.approx(joint.centroid, abs=1e-6)


@pytest.mark.parametrize("small_first", [False, True], ids=["big-first", "small-first"])
def test_overlapping_boxes_name_the_object_whose_mesh_holds_the_point(
    tmp_path: Path, small_first: bool
) -> None:
    """Two objects of similar size whose grown boxes overlap: a point inside the larger
    one's mesh is also in the smaller one's box, and the smaller box must not win."""
    scene = carrots(tmp_path, mesh=trimesh.creation.box((10, 10, 10)))
    big = Transform(translate=(0.0, 0.0, 0.0))
    small = Transform(translate=(9.6, 0.0, 0.0), scale=(0.9, 0.9, 0.9))
    first, second = (small, big) if small_first else (big, small)
    scene = scene.model_copy(
        update={
            "objects": [
                scene.objects[0].model_copy(update={"transform": first}),
                scene.objects[1].model_copy(update={"transform": second}),
            ]
        }
    )
    load = mesh_loader(tmp_path)
    big_id, small_id = ("o2", "o1") if small_first else ("o1", "o2")
    # Inside the big one's mesh (x -5..5), within the small one's box (x 4.6..14.6).
    assert object_at(scene, load, (4.9, 0, 0)) == big_id
    # Inside the small one's mesh (x 5.1..14.1), within the big one's box (x -5.5..5.5).
    assert object_at(scene, load, (5.2, 0, 0)) == small_id


def _wedge() -> trimesh.Trimesh:
    """A prism whose cross-section is a thin triangle, (0, -5), (0, 5), (20, 0), 10 high:
    its tip's two faces turn nearly opposite ways."""
    section = [(0.0, -5.0), (0.0, 5.0), (20.0, 0.0)]
    vertices = [(x, y, z) for z in (0.0, 10.0) for x, y in section]
    faces = [(0, 1, 2), (3, 5, 4), (0, 3, 4), (0, 4, 1), (1, 4, 5), (1, 5, 2), (2, 5, 3), (2, 3, 0)]
    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
    assert mesh.is_watertight and mesh.volume > 0
    return mesh


@pytest.mark.parametrize("mirrored", [False, True], ids=["placed", "mirrored"])
@pytest.mark.parametrize("side", [1.0, -1.0], ids=["left", "right"])
def test_a_point_just_past_a_thin_edge_is_outside_it(
    tmp_path: Path, side: float, mirrored: bool
) -> None:
    """#3749: the point's nearest feature is the wedge's tip, an edge between two faces
    that turn nearly opposite ways, and the side of either face alone can say inside.
    The block around it holds the point; the wedge, whose box is smaller, does not.
    Mirrored in y, the wedge is the same shape wound the other way."""
    written(
        tmp_path,
        [
            ReadObject("Wedge", 1, [ColourPart(0, "Body", ORANGE, _wedge())]),
            ReadObject(
                "Block", 1, [ColourPart(0, "Body", ORANGE, trimesh.creation.box((40,) * 3))]
            ),
        ],
    )
    scene = load_scene((tmp_path / SCENE_NAME).read_text())
    wedge, block = (
        o.model_copy(update={"transform": Transform(scale=scale)})
        for o, scale in zip(
            scene.objects, [(1.0, -1.0 if mirrored else 1.0, 1.0), (1.0, 1.0, 1.0)], strict=True
        )
    )
    scene = scene.model_copy(update={"objects": [wedge, block]})
    # Recentred, the tip is at x 10, y 0. The point is off it, mostly along the normal of
    # the face on its own side, so the other face's plane has it behind.
    normal = np.array([5.0, -20.0 * side]) / np.hypot(5.0, 20.0)
    other = np.array([5.0, 20.0 * side]) / np.hypot(5.0, 20.0)
    x, y = np.array([10.0, 0.0]) + 0.4 * normal + 0.05 * other
    assert object_at(scene, mesh_loader(tmp_path), (x, y, 5.0)) == "o2"
    # Inside the wedge, near its tip, it is the wedge's.
    assert object_at(scene, mesh_loader(tmp_path), (9.0, 0.0, 5.0)) == "o1"
