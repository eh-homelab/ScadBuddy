"""Reading objects into a scene (spec 2026-10-11 §4)."""

from __future__ import annotations

import io
import json
import zipfile

import numpy as np
import trimesh

from scadbuddy.meshmodel.ingest import part_mesh_3mf, scene_from_objects
from scadbuddy.meshmodel.scene import SceneSource, value
from scadbuddy.render.objects3mf import ReadObject
from scadbuddy.render.split import ColourPart, Paint

SOURCE = SceneSource(kind="upload", name="garden.3mf", sha256="cd" * 32)


def part(index: int, colour: str, mesh: trimesh.Trimesh, paint: Paint | None = None) -> ColourPart:
    return ColourPart(index, f"Part {index}", colour, mesh, paint)


def box(x: float, size: float = 10.0) -> trimesh.Trimesh:
    mesh: trimesh.Trimesh = trimesh.creation.box((size, size, size))
    mesh.apply_translation((x, 7.0, 20.0))  # anywhere: ingest recentres
    return mesh


def load(data: bytes) -> trimesh.Trimesh:
    mesh = trimesh.load(io.BytesIO(data), file_type="3mf", force="mesh")
    assert isinstance(mesh, trimesh.Trimesh)
    return mesh


def test_a_part_file_is_a_colourless_closed_3mf() -> None:
    data = part_mesh_3mf(trimesh.creation.box((2, 3, 4)))
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert {"[Content_Types].xml", "_rels/.rels", "3D/3dmodel.model"} <= set(archive.namelist())
        model = archive.read("3D/3dmodel.model").decode()
    assert "basematerials" not in model and "pid=" not in model
    back = load(data)
    assert back.is_watertight and np.allclose(back.extents, (2, 3, 4))


def test_the_same_mesh_gives_the_same_bytes() -> None:
    assert part_mesh_3mf(trimesh.creation.box((1, 1, 1))) == part_mesh_3mf(
        trimesh.creation.box((1, 1, 1))
    )


def test_objects_become_a_row_resting_on_the_bed() -> None:
    read = [
        ReadObject(
            name="Carrot",
            count=1,
            parts=[part(0, "#F08A24", box(0)), part(1, "#3A7D2C", box(0, 4))],
        ),
        ReadObject(name="Sign", count=1, parts=[part(0, "#FFFFFF", box(50))]),
    ]
    scene, files = scene_from_objects(read, SOURCE)
    assert [o.id for o in scene.objects] == ["o1", "o2"]
    assert [p.mesh for p in scene.objects[0].parts] == ["meshes/o1-p1.3mf", "meshes/o1-p2.3mf"]
    assert set(files) == {"meshes/o1-p1.3mf", "meshes/o1-p2.3mf", "meshes/o2-p1.3mf"}
    first = load(files["meshes/o1-p1.3mf"])
    assert first.bounds[0][2] == 0.0  # resting on z = 0
    assert np.allclose(first.bounds.mean(axis=0)[:2], (0, 0))  # centred in x and y
    xs = [float(value(o.transform.translate[0])) for o in scene.objects]
    # Two 10 mm objects 5 mm apart, centred on the origin: at -7.5 and 7.5.
    assert np.allclose(xs, (-7.5, 7.5))


def test_copies_share_meshes() -> None:
    read = [ReadObject(name="Carrot", count=3, parts=[part(0, "#F08A24", box(0))])]
    scene, files = scene_from_objects(read, SOURCE)
    assert [o.id for o in scene.objects] == ["o1", "o2", "o3"]
    assert {o.parts[0].mesh for o in scene.objects} == {"meshes/o1-p1.3mf"}
    assert list(files) == ["meshes/o1-p1.3mf"]
    assert len({o.transform.translate[0] for o in scene.objects}) == 3


def test_a_painted_part_keeps_its_codes_beside_its_mesh() -> None:
    mesh = trimesh.creation.box((1, 1, 1))
    paint = Paint(codes=("",) * (len(mesh.faces) - 1) + ("4",), colours=("#F08A24", "#3A7D2C"))
    scene, files = scene_from_objects(
        [ReadObject(name="Carrot", count=1, parts=[part(0, "#F08A24", mesh, paint)])], SOURCE
    )
    assert scene.objects[0].parts[0].painted
    assert json.loads(files["meshes/o1-p1.paint.json"])["codes"][-1] == "4"
    assert any("paint" in note for note in scene.notes)


def test_a_negative_part_is_kept_as_one() -> None:
    hole = ColourPart(1, "Negative part", "#f08a24", box(0), negative=True)
    scene, _ = scene_from_objects(
        [ReadObject(name="Keychain", count=1, parts=[part(0, "#F08A24", box(0)), hole])], SOURCE
    )
    assert [p.negative for p in scene.objects[0].parts] == [False, True]
    assert scene.objects[0].parts[1].colour == "#F08A24"  # upper-cased like any part's


def test_reading_notes_are_kept() -> None:
    scene, _ = scene_from_objects(
        [
            ReadObject(
                name="Carrot",
                count=1,
                parts=[part(0, "#F08A24", box(0))],
                notes=["dropped a modifier"],
            )
        ],
        SOURCE,
    )
    assert "Carrot: dropped a modifier" in scene.notes
