"""Which scene object a point of a render lies in (spec 2026-10-11 §9)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import trimesh
from pydantic import TypeAdapter

from scadbuddy.meshmodel.edits import Edit, apply_edits
from scadbuddy.meshmodel.locate import attribute, object_at
from scadbuddy.meshmodel.meshes import part_loader
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
    load = part_loader(tmp_path)
    left, right = (value(o.transform.translate[0]) for o in scene.objects)
    assert object_at(scene, load, (left, 0, 32)) == "o1"
    assert object_at(scene, load, (right, 0, 32)) == "o2"
    assert object_at(scene, load, (left, 0, 60)) is None


def test_a_split_object_holds_each_half_on_its_side(tmp_path: Path) -> None:
    scene = carrots(tmp_path, count=1)
    load = part_loader(tmp_path)
    edit = {"edit": "split_at_plane", "object": "o1", "point": [0, 0, 20], "normal": [0, 0, 1]}
    split = apply_edits(scene, EDITS.validate_python([edit]), load)
    assert object_at(split, load, (0, 0, 10)) == "o1"
    assert object_at(split, load, (0, 0, 32)) == "o2"


def test_a_turned_object_is_found_in_its_own_frame(tmp_path: Path) -> None:
    scene = carrots(tmp_path, count=1)
    lying = scene.objects[0].model_copy(update={"transform": Transform(rotate=(0.0, 90.0, 0.0))})
    scene = scene.model_copy(update={"objects": [lying]})
    # rotate y 90 sends +z to +x: the neck (z 32) lies at x 32.
    assert object_at(scene, part_loader(tmp_path), (32, 0, 0)) == "o1"
    assert object_at(scene, part_loader(tmp_path), (0, 0, 32)) is None


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
    scene = carrots(tmp_path, count=1, mesh=carrot())
    moved = scene.objects[0].model_copy(
        update={"transform": Transform(translate=(40.0, -15.0, 0.0), rotate=(0.0, 90.0, 0.0))}
    )
    scene = scene.model_copy(update={"objects": [moved]})
    m, t = placement(moved)
    load = part_loader(tmp_path)
    # The carrot lying on its side: its neck (object z 30..35) is world x 30..35.
    world = trimesh.Trimesh(
        vertices=np.asarray(load(moved.parts[0].mesh).vertices) @ m.T + t,
        faces=load(moved.parts[0].mesh).faces,
        process=False,
    )
    [joint] = analyze_geometry([ColourPart(1, "Body", ORANGE, world)]).weak_joints
    assert object_at(scene, load, joint.centroid) == "o1"
    edit = {**joint.suggestion.model_dump(), "object": "o1"}
    split = apply_edits(scene, EDITS.validate_python([edit]), load)
    cut = split.objects[0].ops[-1]
    local = np.asarray([value(x) for x in cut.point])  # type: ignore[union-attr]
    assert m @ local + t == pytest.approx(joint.centroid, abs=1e-6)
