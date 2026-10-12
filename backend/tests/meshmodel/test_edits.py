"""Scene edits (spec 2026-10-11 §5)."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pytest
import trimesh
from pydantic import TypeAdapter

from scadbuddy.meshmodel.compile import compile_scene
from scadbuddy.meshmodel.edits import (
    Edit,
    SceneEditError,
    apply_edits,
    removed_parameters,
)
from scadbuddy.meshmodel.frames import rotation
from scadbuddy.meshmodel.scene import (
    Add,
    Cut,
    Exposed,
    Joint,
    Part,
    Scene,
    SceneObject,
    SceneSource,
    Transform,
    text_sha256,
    value,
)
from tests.meshmodel.test_section import neck

SOURCE = SceneSource(kind="upload", name="garden.3mf", sha256="ab" * 32)
EDITS = TypeAdapter(list[Edit])


def mesh_of(path: str) -> trimesh.Trimesh:
    return neck()


def garden(n: int = 2) -> Scene:
    return Scene(
        source=SOURCE,
        objects=[
            SceneObject(
                id=f"o{i}",
                name="Carrot",
                parts=[Part(id="p1", mesh="meshes/o1-p1.3mf", colour="#F08A24")],
                transform=Transform(translate=(30.0 * i, 0, 0)),
            )
            for i in range(1, n + 1)
        ],
    )


def edits(*raw: dict[str, Any]) -> list[Any]:
    return EDITS.validate_python(list(raw))


def split(oid: str = "o1", z: float = 32.0, x: float = 30.0, **joint: object) -> dict[str, Any]:
    body: dict[str, Any] = {
        "edit": "split_at_plane",
        "object": oid,
        "point": [x, 0, z],
        "normal": [0, 0, 1],
    }
    if joint:
        body["joint"] = joint
    return body


def test_split_makes_two_objects_cut_on_each_side() -> None:
    result = apply_edits(garden(), edits(split()), mesh_of)
    lower, upper = result.objects[0], result.objects[1]
    assert (lower.id, upper.id) == ("o1", "o3")
    assert lower.ops[-1] == Cut(point=(0.0, 0.0, 32.0), normal=(0.0, 0.0, 1.0), keep="below")
    assert isinstance(upper.ops[-1], Cut) and upper.ops[-1].keep == "above"
    assert upper.transform == lower.transform


def test_split_touches_only_its_object() -> None:
    result = apply_edits(garden(), edits(split()), mesh_of)
    assert result.objects[2] == garden().objects[1]


def test_split_maps_the_world_plane_through_the_transform() -> None:
    scene = garden(1).model_copy(
        update={
            "objects": [
                garden(1)
                .objects[0]
                .model_copy(update={"transform": Transform(translate=(30, 0, 0), scale=(1, 1, 2))})
            ]
        }
    )
    result = apply_edits(scene, edits(split(z=64.0)), mesh_of)
    cut = result.objects[0].ops[-1]
    assert isinstance(cut, Cut)
    assert value(cut.point[2]) == pytest.approx(32.0)


def test_a_joint_puts_a_peg_on_the_top_and_a_socket_in_the_bottom() -> None:
    result = apply_edits(garden(1), edits(split(depth=5, clearance=0.2, key="hex")), mesh_of)
    lower, upper = result.objects
    socket, peg = lower.ops[-1], upper.ops[-1]
    assert isinstance(socket, Joint) and socket.role == "socket"
    assert isinstance(peg, Joint) and peg.role == "peg"
    assert socket.axis == peg.axis == (0.0, 0.0, -1.0)
    assert np.allclose(peg.at, (0, 0, 32), atol=1e-6)
    room = 3 * math.cos(math.pi / 64)
    assert value(peg.diameter) == pytest.approx(round(1.2 * room, 2))
    assert (value(peg.depth), value(peg.clearance), peg.key) == (5, 0.2, "hex")


def test_a_peg_that_does_not_fit_is_refused_with_the_room_there_is() -> None:
    with pytest.raises(SceneEditError, match=r"needs 3\.70 mm.*has 2\.99 mm"):
        apply_edits(garden(1), edits(split(diameter=5.8, clearance=0.0)), mesh_of)


def test_split_refuses_a_plane_that_misses() -> None:
    with pytest.raises(SceneEditError, match="does not cut o1"):
        apply_edits(garden(1), edits(split(z=500)), mesh_of)


def test_a_joint_through_a_negative_parts_hole_is_refused_as_a_ring() -> None:
    """The 1943 keychain's case: a plane through the keyring hole must not put a peg in
    the hole (review of #2452)."""
    holed = garden(1).model_copy(
        update={
            "objects": [
                garden(1)
                .objects[0]
                .model_copy(
                    update={
                        "parts": [
                            Part(id="p1", mesh="meshes/o1-p1.3mf", colour="#F08A24"),
                            Part(id="p2", mesh="meshes/o1-p2.3mf", colour="#F08A24", negative=True),
                        ]
                    }
                )
            ]
        }
    )
    hole = trimesh.creation.cylinder(radius=1.5, height=40, sections=64)
    hole.apply_translation((0, 0, 20))
    meshes = {"meshes/o1-p1.3mf": neck(), "meshes/o1-p2.3mf": hole}
    with pytest.raises(SceneEditError, match="centre"):
        apply_edits(holed, edits(split(depth=4)), meshes.__getitem__)


def test_joint_refuses_a_centroid_outside_the_section() -> None:
    ring = trimesh.creation.annulus(r_min=4, r_max=5, height=10, sections=64)
    with pytest.raises(SceneEditError, match="centre"):
        apply_edits(garden(1), edits(split(z=0.0, depth=4)), lambda _: ring)


def test_lay_flat_turns_the_direction_down_and_drops_to_the_bed() -> None:
    result = apply_edits(
        garden(1), edits({"edit": "lay_flat", "object": "o1", "normal": [1, 0, 0]}), mesh_of
    )
    t = result.objects[0].transform
    r = rotation([value(a) for a in t.rotate])
    assert np.allclose(r @ [1, 0, 0], [0, 0, -1], atol=1e-9)
    # Lying on its side, the body's radius 10 is its lowest point: lifted by 10.
    assert value(t.translate[2]) == pytest.approx(10.0, abs=1e-6)


def test_lay_flat_largest_flat_after_a_split_lays_the_cut_face_down() -> None:
    # A cone's top above z = 5 has no flat face but the cut (its base is cut away).
    cone = trimesh.creation.cone(radius=10, height=30, sections=64)
    result = apply_edits(
        garden(1),
        edits(split(z=5.0), {"edit": "lay_flat", "object": "o2", "normal": "largest_flat"}),
        lambda _: cone,
    )
    top = result.objects[1]
    # The cut face already faces down (-Z): no turn, and the part drops 5 mm to the bed.
    assert np.allclose(rotation([value(a) for a in top.transform.rotate]), np.eye(3), atol=1e-9)
    assert value(top.transform.translate[2]) == pytest.approx(-5.0)


def test_the_drop_to_the_bed_counts_a_peg() -> None:
    # The top half's peg points down 6 mm from the cut at z = 32: standing it on the cut
    # face puts the peg's tip, not the face, on the bed.
    result = apply_edits(
        garden(1),
        edits(split(depth=6), {"edit": "lay_flat", "object": "o2", "normal": [0, 0, -1]}),
        mesh_of,
    )
    assert value(result.objects[1].transform.translate[2]) == pytest.approx(-26.0)


def test_largest_flat_never_picks_a_cut_face_with_a_peg() -> None:
    cone = trimesh.creation.cone(radius=10, height=30, sections=64)
    with pytest.raises(SceneEditError, match="no flat face"):
        apply_edits(
            garden(1),
            edits(
                split(z=5.0, depth=2),
                {"edit": "lay_flat", "object": "o2", "normal": "largest_flat"},
            ),
            lambda _: cone,
        )


def test_edits_apply_in_order() -> None:
    result = apply_edits(
        garden(1), edits(split(), {"edit": "rename", "object": "o2", "name": "Leaves"}), mesh_of
    )
    assert [o.name for o in result.objects] == ["Carrot (bottom)", "Leaves"]


def test_an_unknown_object_is_refused() -> None:
    with pytest.raises(SceneEditError, match="no object o9"):
        apply_edits(garden(1), edits({"edit": "rename", "object": "o9", "name": "x"}), mesh_of)


def test_the_last_object_cannot_be_deleted() -> None:
    with pytest.raises(SceneEditError, match="last object"):
        apply_edits(garden(1), edits({"edit": "delete", "object": "o1"}), mesh_of)


def test_set_colour_add_subtract_custom_duplicate_remove_op() -> None:
    result = apply_edits(
        garden(1),
        edits(
            {"edit": "set_colour", "object": "o1", "part": "p1", "colour": "#3a7d2c"},
            {
                "edit": "add",
                "object": "o1",
                "primitive": "cylinder",
                "size": [4, 4, 6],
                "at": [30, 0, 33],
                "colour": "#0acc38",
            },
            {"edit": "subtract", "object": "o1", "primitive": "cube", "size": [2, 2, 2]},
            {"edit": "custom", "object": "o1", "scad": "children();"},
            {"edit": "remove_op", "object": "o1", "op": 2},
            {"edit": "duplicate", "object": "o1"},
        ),
        mesh_of,
    )
    first, copy = result.objects
    assert first.parts[0].colour == "#3A7D2C"
    assert [op.op for op in first.ops] == ["add", "subtract"]
    added = first.ops[0]
    assert isinstance(added, Add)
    assert added.transform.translate == (
        0.0,
        0.0,
        33.0,
    )  # world (30, 0, 33) in the object's frame
    assert added.colour == "#0ACC38"
    assert copy.id == "o2" and copy.ops == first.ops
    assert value(copy.transform.translate[0]) == pytest.approx(50.0)


def test_expose_and_unexpose() -> None:
    exposed = apply_edits(
        garden(1),
        edits(
            split(),
            {"edit": "expose", "object": "o1", "op": 0, "field": "point.2", "name": "cut_height"},
        ),
        mesh_of,
    )
    assert "cut_height = 32;" in compile_scene(exposed)
    back = apply_edits(
        exposed, edits({"edit": "unexpose", "object": "o1", "op": 0, "field": "point.2"}), mesh_of
    )
    assert removed_parameters(exposed, back) == ["cut_height"]


def test_a_jointed_cut_a_joint_and_a_jointed_scale_cannot_be_exposed() -> None:
    jointed = apply_edits(garden(1), edits(split(depth=5)), mesh_of)
    for refused in (
        {"edit": "expose", "object": "o1", "op": 0, "field": "point.2", "name": "cut_height"},
        {"edit": "expose", "object": "o1", "op": 1, "field": "diameter", "name": "peg"},
        {"edit": "expose", "object": "o2", "op": None, "field": "scale.2", "name": "tall"},
        {"edit": "transform", "object": "o2", "scale": [1, 1, 2]},
        # The same value, exposed: no change now, but a parameter later (review of #2388).
        {"edit": "transform", "object": "o2", "scale": [{"expose": "s", "value": 1}, 1, 1]},
    ):
        with pytest.raises(SceneEditError):
            apply_edits(jointed, edits(refused), mesh_of)


def test_split_with_a_joint_refuses_an_exposed_scale() -> None:
    """Expose first, then split: the halves would inherit a scale no fit check covers."""
    exposed = apply_edits(
        garden(1),
        edits({"edit": "expose", "object": "o1", "op": None, "field": "scale.0", "name": "w"}),
        mesh_of,
    )
    with pytest.raises(SceneEditError, match="unexpose the scale"):
        apply_edits(exposed, edits(split(depth=5)), mesh_of)


def test_a_peg_takes_the_colour_of_the_part_it_stands_on() -> None:
    two = garden(1).model_copy(
        update={
            "objects": [
                garden(1)
                .objects[0]
                .model_copy(
                    update={
                        "parts": [
                            Part(id="p1", mesh="meshes/o1-p1.3mf", colour="#F08A24"),
                            Part(id="p2", mesh="meshes/o1-p2.3mf", colour="#3A7D2C"),
                        ]
                    }
                )
            ]
        }
    )
    body = trimesh.creation.cylinder(radius=10, height=30, sections=64)
    body.apply_translation((0, 0, 15))
    stem = trimesh.creation.revolve(
        np.array([[0, 30], [3, 30], [3, 45], [0, 45]], dtype=float), sections=64
    )
    meshes = {"meshes/o1-p1.3mf": body, "meshes/o1-p2.3mf": stem}
    result = apply_edits(two, edits(split(depth=4)), meshes.__getitem__)
    peg = result.objects[1].ops[-1]
    assert isinstance(peg, Joint) and peg.role == "peg" and peg.colour == "#3A7D2C"


def test_an_op_index_past_the_end_is_refused() -> None:
    for edit in (
        {"edit": "expose", "object": "o1", "op": 3, "field": "point.2", "name": "h"},
        {"edit": "unexpose", "object": "o1", "op": 3, "field": "point.2"},
        {"edit": "remove_op", "object": "o1", "op": 3},
    ):
        with pytest.raises(SceneEditError, match="has no op 3"):
            apply_edits(garden(1), edits(edit), mesh_of)


def test_expose_on_the_transform() -> None:
    result = apply_edits(
        garden(1),
        edits(
            {
                "edit": "expose",
                "object": "o1",
                "op": None,
                "field": "scale.2",
                "name": "height_scale",
            }
        ),
        mesh_of,
    )
    assert result.objects[0].transform.scale[2] == Exposed(expose="height_scale", value=1.0)


def test_a_delete_reports_the_parameters_it_removes() -> None:
    scene = garden(2)
    first = scene.objects[0].model_copy(
        update={"transform": Transform(scale=(Exposed(expose="size", value=1), 1, 1))}
    )
    scene = scene.model_copy(update={"objects": [first, scene.objects[1]]})
    after = apply_edits(scene, edits({"edit": "delete", "object": "o1"}), mesh_of)
    assert removed_parameters(scene, after) == ["size"]


def test_hash_check_ignores_compiler_changes() -> None:
    # The recorded hash is of the text last written, so an edit recompiles and records
    # a new one; apply_edits itself never compares hashes (the kind's check does).
    scene = garden(1).model_copy(
        update={"generated_sha256": text_sha256("an older compiler's text")}
    )
    result = apply_edits(scene, edits({"edit": "rename", "object": "o1", "name": "x"}), mesh_of)
    assert result.generated_sha256 == scene.generated_sha256  # the kind records the new one


def flat() -> Scene:
    obj = (
        garden(1)
        .objects[0]
        .model_copy(update={"transform": Transform(translate=(30, 0, 0), scale=(1, 1, 0))})
    )
    return garden(1).model_copy(update={"objects": [obj]})


def test_an_object_with_a_zero_scale_has_no_plane_to_cut_or_lie_on() -> None:
    for edit in (
        split(),
        {"edit": "lay_flat", "object": "o1", "normal": [1, 0, 0]},
        {"edit": "add", "object": "o1", "primitive": "cube", "size": [1, 1, 1], "at": [30, 0, 5]},
    ):
        with pytest.raises(SceneEditError, match="o1 has a zero scale"):
            apply_edits(flat(), edits(edit), mesh_of)


def test_a_transform_cannot_set_a_zero_scale() -> None:
    with pytest.raises(SceneEditError, match="o1 cannot take a zero scale"):
        apply_edits(
            garden(1), edits({"edit": "transform", "object": "o1", "scale": [1, 0, 1]}), mesh_of
        )
