"""A mesh model rendered by the real OpenSCAD (#2146, spec 2026-10-11 §2, §10): the
import of a colourless part 3MF in `color()`, `set_colour`, and a split with a joint
whose socket is the peg grown by the clearance. Runs only where `openscad` is on PATH:
the image (`docker build --target test`)."""

from __future__ import annotations

import math
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
import trimesh
from pydantic import TypeAdapter

from scadbuddy.meshmodel.compile import compile_scene
from scadbuddy.meshmodel.edits import Edit, apply_edits
from scadbuddy.meshmodel.ingest import scene_from_objects
from scadbuddy.meshmodel.scene import Joint, Scene, SceneSource, value
from scadbuddy.meshmodel.section import measure
from scadbuddy.render.objects3mf import ReadObject
from scadbuddy.render.split import DEFAULT_MATERIAL_NAME, ColourPart, split_by_material
from tests.conftest import openscad_binary
from tests.meshmodel.test_section import neck

pytestmark = pytest.mark.requires_openscad

SOURCE = SceneSource(kind="upload", name="neck.stl", sha256="ab" * 32)
EDITS = TypeAdapter(list[Edit])


def written(directory: Path) -> Scene:
    read = [ReadObject(name="Carrot", count=1, parts=[ColourPart(0, "Body", "#F08A24", neck())])]
    scene, files = scene_from_objects(read, SOURCE)
    for name, data in files.items():
        (directory / name).parent.mkdir(parents=True, exist_ok=True)
        (directory / name).write_bytes(data)
    return scene


def mesh_of(directory: Path) -> Callable[[str], trimesh.Trimesh]:
    def load(path: str) -> trimesh.Trimesh:
        mesh = trimesh.load(directory / path, file_type="3mf", force="mesh")
        assert isinstance(mesh, trimesh.Trimesh)
        return mesh

    return load


def render(directory: Path, scene: Scene) -> list[ColourPart]:
    (directory / "model.scad").write_text(compile_scene(scene))
    binary = openscad_binary()
    assert binary is not None
    subprocess.run(
        [
            binary,
            "--backend=Manifold",
            "-o",
            str(directory / "out.3mf"),
            str(directory / "model.scad"),
        ],
        check=True,
        capture_output=True,
        timeout=300,
    )
    return [part for part in split_by_material(directory / "out.3mf") if len(part.mesh.faces)]


def polygon_area(sides: int, radius: float) -> float:
    return 0.5 * sides * radius**2 * math.sin(2 * math.pi / sides)


def test_an_imported_part_renders_in_the_scenes_colour(tmp_path: Path) -> None:
    assert [p.colour for p in render(tmp_path, written(tmp_path))] == ["#F08A24"]


def test_set_colour_changes_the_render(tmp_path: Path) -> None:
    scene = written(tmp_path)
    recoloured = apply_edits(
        scene,
        EDITS.validate_python(
            [{"edit": "set_colour", "object": "o1", "part": "p1", "colour": "#00FF00"}]
        ),
        mesh_of(tmp_path),
    )
    assert [p.colour for p in render(tmp_path, recoloured)] == ["#00FF00"]


def test_a_joint_renders_two_closed_parts_and_a_socket_grown_by_the_clearance(
    tmp_path: Path,
) -> None:
    # One object rests on z = 0 centred on the origin, so world and object frames agree.
    split = apply_edits(
        written(tmp_path),
        EDITS.validate_python(
            [
                {
                    "edit": "split_at_plane",
                    "object": "o1",
                    "point": [0, 0, 32],
                    "normal": [0, 0, 1],
                    "joint": {"depth": 2, "clearance": 0.2, "key": "none"},
                }
            ]
        ),
        mesh_of(tmp_path),
    )
    bodies: dict[str, trimesh.Trimesh] = {}
    for obj in split.objects:
        parts = render(tmp_path, split.model_copy(update={"objects": [obj]}))
        # The cut faces and the socket's wall take the piece's colour: no Default
        # material, no colour the scene does not have.
        assert [(p.name, p.colour) for p in parts if p.name == DEFAULT_MATERIAL_NAME] == []
        assert [p.colour for p in parts] == ["#F08A24"], obj.id
        body = trimesh.util.concatenate([p.mesh for p in parts])
        assert isinstance(body, trimesh.Trimesh)
        # split_by_material keeps a vertex per triangle corner; weld them to test closure.
        body.merge_vertices()
        bodies[obj.id] = body
        assert bodies[obj.id].is_watertight, obj.id
    socket = split.objects[0].ops[-1]
    assert isinstance(socket, Joint) and socket.role == "socket"
    # 1 mm below the cut, the lower part is the neck (a 64-gon of radius 3) less the
    # socket (a 48-gon of radius d/2 + clearance).
    section = measure(bodies["o1"], (0, 0, 31), (0, 0, 1))
    assert section is not None
    hole = value(socket.diameter) / 2 + value(socket.clearance)
    assert section.area == pytest.approx(polygon_area(64, 3) - polygon_area(48, hole), rel=0.02)
