# backend/tests/meshmodel/test_paint.py
"""Paint transfer (spec 2026-10-11 §7): a painted part's codes carried onto its
rendered solid. The "renders" here are what OpenSCAD's op-free render of a part was
measured to be (meshmodel/paint.py's docstring): the source triangles in world
coordinates, to 6 decimals, each starting at another vertex."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
import trimesh

from scadbuddy.meshmodel import paint as paint_module
from scadbuddy.meshmodel.compile import compile_scene
from scadbuddy.meshmodel.ingest import scene_from_objects
from scadbuddy.meshmodel.meshes import mesh_loader
from scadbuddy.meshmodel.paint import (
    HAND_EDITED,
    OLD_PAINT_NOTE,
    CarriedPaint,
    Source,
    carry_paint,
    paint_warnings,
    placement,
    read_carried,
    scene_notes,
    transfer,
    turned,
    with_params,
    write_carried,
)
from scadbuddy.meshmodel.scene import (
    SCENE_NAME,
    Exposed,
    Scene,
    SceneObject,
    SceneSource,
    Transform,
    dump_scene,
    text_sha256,
)
from scadbuddy.render.objects3mf import ReadObject
from scadbuddy.render.split import ColourPart, Paint

SOURCE = SceneSource(kind="upload", name="box.3mf", sha256="ab" * 32)
ORANGE, GREEN, PURPLE = "#F08A24", "#3A7D2C", "#7B2FBE"
# A real split code from Bambuddy library file 688: leaves of extruder 0 and 1.
MIXED = "00400644002044A3003003"
# Split in two, both halves extruder 2 (green).
SPLIT_GREEN = "881"


def painted_box() -> tuple[trimesh.Trimesh, tuple[str, ...]]:
    """A 10 mm cube in 48 triangles (vertices at the mid-planes too), painted in turn
    with nothing, green, part-orange and split-green."""
    mesh = trimesh.creation.box((10, 10, 10)).subdivide()
    cycle = ("", "8", MIXED, SPLIT_GREEN)
    return mesh, tuple(cycle[i % 4] for i in range(len(mesh.faces)))


def body(count: int = 1) -> list[ReadObject]:
    mesh, codes = painted_box()
    paint = Paint(codes, (ORANGE, GREEN))
    return [ReadObject(name="Box", count=count, parts=[ColourPart(0, "Body", ORANGE, mesh, paint)])]


def save(directory: Path, scene: Scene) -> Scene:
    """Compile ``scene`` into ``directory``: model.scad and scene.json, its hash recorded."""
    text = compile_scene(scene)
    scene = scene.model_copy(update={"generated_sha256": text_sha256(text)})
    (directory / "model.scad").write_text(text)
    (directory / SCENE_NAME).write_text(dump_scene(scene))
    return scene


def written(directory: Path, read: list[ReadObject]) -> Scene:
    scene, files = scene_from_objects(read, SOURCE)
    for name, data in files.items():
        (directory / name).parent.mkdir(parents=True, exist_ok=True)
        (directory / name).write_bytes(data)
    return save(directory, scene)


def rendered(
    directory: Path, scene: Scene, colour: str, *, mirror: bool = False
) -> trimesh.Trimesh:
    """What OpenSCAD's op-free render of the scene's ``colour`` parts looks like: world
    coordinates to 6 decimals, each face starting at another vertex; wound the other
    way where the object is mirrored (``mirror``), as the render re-orients it."""
    load = mesh_loader(directory)
    meshes = []
    for obj in scene.objects:
        m, t = placement(obj)
        for part in obj.parts:
            if part.colour != colour:
                continue
            mesh = load(part.mesh)
            faces = np.asarray(mesh.faces)
            if mirror:
                faces = faces[:, ::-1]
            rolled = np.array([np.roll(face, -(i % 3)) for i, face in enumerate(faces)])
            world = np.round(np.asarray(mesh.vertices) @ m.T + t, 6)
            meshes.append(trimesh.Trimesh(vertices=world, faces=rolled, process=False))
    joined = trimesh.util.concatenate(meshes)
    assert isinstance(joined, trimesh.Trimesh)
    return joined


def world_triangles(directory: Path, scene: Scene) -> np.ndarray:
    load = mesh_loader(directory)
    out = []
    for obj in scene.objects:
        m, t = placement(obj)
        mesh = load(obj.parts[0].mesh)
        out.append((np.asarray(mesh.vertices) @ m.T + t)[np.asarray(mesh.faces)])
    return np.concatenate(out)


def carried_one(
    directory: Path, output: trimesh.Trimesh, colour: str = ORANGE, **params: float
) -> CarriedPaint:
    found, _ = carry_paint(directory, params, [ColourPart(1, "Body", colour, output)])
    assert found[0] is not None
    return found[0]


def test_an_op_free_part_keeps_every_code_verbatim(tmp_path: Path) -> None:
    scene = written(tmp_path, body())
    _, codes = painted_box()
    output = rendered(tmp_path, scene, ORANGE)
    carried = carried_one(tmp_path, output)
    assert carried.paint.codes == codes
    assert carried.paint.colours == (ORANGE, GREEN)
    # Turned back, every face starts where its source triangle does.
    assert np.allclose(
        turned(output, carried.turns).triangles, world_triangles(tmp_path, scene), atol=1e-5
    )
    assert set(carried.turns.tolist()) == {0, 1, 2}


def test_a_cut_part_keeps_its_uncut_faces_and_gives_cut_faces_its_own_colour(
    tmp_path: Path,
) -> None:
    """Spec §10: the box cut level at its middle (z = 5): the faces below keep their
    codes, the cap the cut made takes the part's own colour."""
    scene = written(tmp_path, body())
    _, codes = painted_box()
    whole = rendered(tmp_path, scene, ORANGE)
    below = (whole.triangles[:, :, 2] <= 5 + 1e-9).all(axis=1)
    kept = trimesh.Trimesh(vertices=whole.vertices, faces=whole.faces[below], process=False)
    cap = trimesh.Trimesh(
        vertices=[[-5, -5, 5], [5, -5, 5], [5, 5, 5], [-5, 5, 5]],
        faces=[[0, 1, 2], [0, 2, 3]],
        process=False,
    )
    output = trimesh.util.concatenate([kept, cap])
    assert isinstance(output, trimesh.Trimesh)
    carried = carried_one(tmp_path, output)
    expected = [code for code, keep in zip(codes, below, strict=True) if keep]
    assert list(carried.paint.codes) == [*expected, "", ""]


def test_a_retriangulated_face_takes_only_a_whole_colour(tmp_path: Path) -> None:
    """A face cut into three about its centre (a new triangulation in the same plane):
    from a green face, green; from a split-green face, green; from a face painted only
    in part, the part's own colour."""
    scene = written(tmp_path, body())
    _, codes = painted_box()
    whole = rendered(tmp_path, scene, ORANGE)
    picked = {codes.index("8"): "8", codes.index(SPLIT_GREEN): "8", codes.index(MIXED): ""}
    keep = [i for i in range(len(whole.faces)) if i not in picked]
    vertices = [np.asarray(whole.vertices)]
    faces = [np.asarray(whole.faces)[keep]]
    expected = [codes[i] for i in keep]
    for face, colour in picked.items():
        a, b, c = whole.triangles[face]
        centre = (a + b + c) / 3
        base = sum(len(v) for v in vertices)
        vertices.append(np.array([a, b, c, centre]))
        faces.append(np.array([[0, 1, 3], [1, 2, 3], [2, 0, 3]]) + base)
        expected += [colour] * 3
    output = trimesh.Trimesh(
        vertices=np.concatenate(vertices), faces=np.concatenate(faces), process=False
    )
    assert list(carried_one(tmp_path, output).paint.codes) == expected


def test_copies_of_one_painted_mesh_are_each_painted(tmp_path: Path) -> None:
    scene = written(tmp_path, body(count=2))
    assert scene.objects[0].parts[0].mesh == scene.objects[1].parts[0].mesh
    _, codes = painted_box()
    carried = carried_one(tmp_path, rendered(tmp_path, scene, ORANGE))
    assert carried.paint.codes == codes + codes


def test_an_exposed_translate_carries_the_paint_to_where_it_renders(tmp_path: Path) -> None:
    scene = written(tmp_path, body())
    moved = scene.objects[0].transform.model_copy(
        update={"translate": (Exposed(expose="box_x", value=0.0), 0.0, 0.0)}
    )
    scene = save(
        tmp_path,
        scene.model_copy(
            update={"objects": [scene.objects[0].model_copy(update={"transform": moved})]}
        ),
    )
    _, codes = painted_box()
    output = rendered(tmp_path, with_params(scene, {"box_x": 30}), ORANGE)
    assert carried_one(tmp_path, output, box_x=30).paint.codes == codes
    # At the scene's own value the render is 30 mm away from every source triangle.
    found, _ = carry_paint(tmp_path, {}, [ColourPart(1, "Body", ORANGE, output)])
    assert found == [None]


def test_a_mirrored_part_keeps_only_whole_colours_and_says_so(tmp_path: Path) -> None:
    # That OpenSCAD renders a mirrored object's faces reversed is an assumption here;
    # the image test (Task 6) measures it.
    scene = written(tmp_path, body())
    flipped = scene.objects[0].model_copy(update={"transform": Transform(scale=(-1.0, 1.0, 1.0))})
    scene = save(tmp_path, scene.model_copy(update={"objects": [flipped]}))
    _, codes = painted_box()
    found, warnings = carry_paint(
        tmp_path,
        {},
        [ColourPart(1, "Body", ORANGE, rendered(tmp_path, scene, ORANGE, mirror=True))],
    )
    assert found[0] is not None
    whole = {"": "", "8": "8", SPLIT_GREEN: "8", MIXED: ""}
    assert list(found[0].paint.codes) == [whole[code] for code in codes]
    assert warnings == paint_warnings(scene)
    assert len(warnings) == 1 and warnings[0].startswith("o1 (Box) is mirrored")


def test_a_recoloured_part_keeps_its_painted_colours(tmp_path: Path) -> None:
    """set_colour: the part renders purple; its painted faces keep their codes, which
    name orange and green, so both still get a filament (Task 4)."""
    scene = written(tmp_path, body())
    part = scene.objects[0].parts[0].model_copy(update={"colour": PURPLE})
    scene = save(
        tmp_path,
        scene.model_copy(
            update={"objects": [scene.objects[0].model_copy(update={"parts": [part]})]}
        ),
    )
    _, codes = painted_box()
    carried = carried_one(tmp_path, rendered(tmp_path, scene, PURPLE), colour=PURPLE)
    assert carried.paint.codes == codes
    assert carried.paint.used() == [ORANGE, GREEN]


def test_a_hand_edited_model_carries_no_paint_and_says_so(tmp_path: Path) -> None:
    scene = written(tmp_path, body())
    output = rendered(tmp_path, scene, ORANGE)
    (tmp_path / "model.scad").write_text("cube(10);\n")
    assert carry_paint(tmp_path, {}, [ColourPart(1, "Body", ORANGE, output)]) == (
        [None],
        [HAND_EDITED],
    )


def test_a_model_without_a_scene_or_paint_is_left_alone(tmp_path: Path) -> None:
    part = ColourPart(1, "Body", ORANGE, trimesh.creation.box((10, 10, 10)))
    assert carry_paint(tmp_path, {}, [part]) == ([None], [])
    plain = [ReadObject(name="Box", count=1, parts=[ColourPart(0, "Body", ORANGE, part.mesh)])]
    written(tmp_path, plain)
    assert carry_paint(tmp_path, {}, [part]) == ([None], [])


def test_another_colours_part_is_left_alone(tmp_path: Path) -> None:
    scene = written(tmp_path, body())
    found, _ = carry_paint(
        tmp_path, {}, [ColourPart(1, "Leaves", GREEN, rendered(tmp_path, scene, ORANGE))]
    )
    assert found == [None]


def test_carried_paint_round_trips_through_its_file(tmp_path: Path) -> None:
    scene = written(tmp_path, body())
    output = rendered(tmp_path, scene, ORANGE)
    carried = carried_one(tmp_path, output)
    write_carried(tmp_path / "paint-1-1.json", carried)
    mesh, paint = read_carried(tmp_path / "paint-1-1.json", output)
    assert paint == carried.paint
    assert np.array_equal(mesh.faces, turned(output, carried.turns).faces)
    with pytest.raises(ValueError, match="another mesh"):
        read_carried(tmp_path / "paint-1-1.json", trimesh.creation.box((1, 1, 1)))


def test_scene_notes_drop_the_phase_one_paint_note() -> None:
    scene, _ = scene_from_objects(body(), SOURCE)
    old = scene.model_copy(
        update={
            "notes": [
                f"Box: painted in Bambu Studio; {OLD_PAINT_NOTE}, so each part prints in its own "
                "colour",
                "Box: kept",
            ]
        }
    )
    assert scene_notes(old) == ["Box: kept"]


def test_an_unreadable_painting_is_a_warning_not_a_failure(tmp_path: Path) -> None:
    scene = written(tmp_path, body())
    output = rendered(tmp_path, scene, ORANGE)
    (tmp_path / "meshes" / "o1-p1.paint.json").unlink()
    found, warnings = carry_paint(tmp_path, {}, [ColourPart(1, "Body", ORANGE, output)])
    assert found == [None]
    assert len(warnings) == 1 and "missing or unreadable" in warnings[0]
    (tmp_path / "meshes" / "o1-p1.paint.json").write_text("{not json")
    found, warnings = carry_paint(tmp_path, {}, [ColourPart(1, "Body", ORANGE, output)])
    assert found == [None]
    assert len(warnings) == 1 and "missing or unreadable" in warnings[0]


def test_a_painting_naming_an_extruder_it_has_no_colour_for_costs_only_that_part(
    tmp_path: Path,
) -> None:
    scene = written(tmp_path, body())
    output = rendered(tmp_path, scene, ORANGE)
    path = tmp_path / "meshes" / "o1-p1.paint.json"
    document = json.loads(path.read_text())
    path.write_text(json.dumps({**document, "colours": document["colours"][:1]}))
    found, warnings = carry_paint(tmp_path, {}, [ColourPart(1, "Body", ORANGE, output)])
    assert found == [None]
    assert len(warnings) == 1 and "o1 p1" in warnings[0] and "own colour" in warnings[0]


def _cap_output(tmp_path: Path) -> tuple[trimesh.Trimesh, Source]:
    scene = written(tmp_path, body())
    _, codes = painted_box()
    output = rendered(tmp_path, scene, ORANGE)
    # Every face of the output is shifted off its source, so none is verbatim.
    shifted = trimesh.Trimesh(
        vertices=np.asarray(output.vertices) + np.array([0.001, 0.0, 0.0]),
        faces=output.faces,
        process=False,
    )
    mesh = mesh_loader(tmp_path)("meshes/o1-p1.3mf")
    source = Source(np.asarray(mesh.vertices), np.asarray(mesh.faces), codes, (ORANGE, GREEN))
    return shifted, source


def test_too_many_candidate_pairs_leave_the_own_colour_and_say_so(tmp_path: Path) -> None:
    shifted, source = _cap_output(tmp_path)
    problems: list[str] = []
    codes, _, verbatim = transfer(shifted, source, problems, max_pairs=1)
    assert verbatim == 0 and set(codes) == {""}
    assert len(problems) == 1 and "own colour" in problems[0]
    # Within the budget the same faces take their sources' whole colours.
    assert any(transfer(shifted, source, [])[0])


def test_a_crowded_cell_carries_no_painting_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shifted, source = _cap_output(tmp_path)

    def crowded(*_: object) -> np.ndarray:
        raise ValueError("a cell holds 65 distinct points")

    monkeypatch.setattr(paint_module, "match_points", crowded)
    problems: list[str] = []
    codes, _, verbatim = transfer(shifted, source, problems)
    assert verbatim == 0 and set(codes) == {""}
    assert len(problems) == 1 and "not carried" in problems[0]


def test_touching_parts_still_carry_every_code_verbatim(tmp_path: Path) -> None:
    """Two painted copies of one mesh sharing a plane: their coincident points are one
    source point, and every face's code survives (none falls back to a whole colour)."""
    scene = written(tmp_path, body(count=2))
    first, second = scene.objects

    def shift(obj: SceneObject, x: float) -> SceneObject:
        moved = obj.transform.model_copy(update={"translate": (x, 0.0, 0.0)})
        return obj.model_copy(update={"transform": moved})

    scene = save(
        tmp_path, scene.model_copy(update={"objects": [shift(first, 0.0), shift(second, 10.0)]})
    )
    _, codes = painted_box()
    output = rendered(tmp_path, scene, ORANGE)
    assert carried_one(tmp_path, output).paint.codes == codes + codes


def test_importing_paint_does_not_load_the_render_or_the_api() -> None:
    """`render/jobs.py` imports paint at the top, so paint must not import jobs."""
    code = (
        "import sys, scadbuddy.meshmodel.paint;"
        "bad = [m for m in sys.modules if m == 'scadbuddy.render.jobs' "
        "or m == 'scadbuddy.api' or m.startswith('scadbuddy.api.')];"
        "sys.exit(1 if bad else 0)"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
