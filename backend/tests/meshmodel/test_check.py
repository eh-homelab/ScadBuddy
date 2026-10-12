from pathlib import Path

from scadbuddy.meshmodel.check import problems
from scadbuddy.meshmodel.operations import compiled
from scadbuddy.meshmodel.scene import Part, Scene, SceneObject, SceneSource, dump_scene


def write(directory: Path) -> None:
    scene, text = compiled(
        Scene(
            source=SceneSource(kind="upload", name="x.3mf", sha256="ab" * 32),
            objects=[
                SceneObject(
                    id="o1",
                    name="X",
                    parts=[Part(id="p1", mesh="meshes/o1-p1.3mf", colour="#FFFFFF")],
                )
            ],
        )
    )
    (directory / "scene.json").write_text(dump_scene(scene))
    (directory / "model.scad").write_text(text)


def test_a_freshly_compiled_model_has_no_problems(tmp_path: Path) -> None:
    write(tmp_path)
    assert problems(tmp_path) == []


def test_an_edited_model_scad_is_named(tmp_path: Path) -> None:
    write(tmp_path)
    (tmp_path / "model.scad").write_text(
        (tmp_path / "model.scad").read_text().replace("o1();", "o1(); cube(1);")
    )
    assert len(problems(tmp_path)) == 2
