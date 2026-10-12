"""The catalogue's writes for mesh models (#2146): a create with files beside the
source, and a locked read-modify-write of several files as one revision."""

from __future__ import annotations

from pathlib import Path

import pytest

from scadbuddy.core.paths import DataPaths
from scadbuddy.library.catalogue import Catalogue, ModelMeta, UnsafeModelPathError
from scadbuddy.library.history import GitError, ModelHistory
from scadbuddy.meshmodel.scene import SceneSource
from scadbuddy.render.solids import WRAPPER_PREFIX
from tests.support.media import MemoryMediaStore

pytestmark = pytest.mark.requires_git

SOURCE = SceneSource(kind="upload", name="carrot.3mf", sha256="ef" * 32)


@pytest.fixture
def catalogue(tmp_path: Path) -> Catalogue:
    paths = DataPaths(tmp_path / "data")
    paths.ensure()
    history = ModelHistory(paths.models, wrapper_prefix=WRAPPER_PREFIX)
    history.ensure_repo()
    return Catalogue(paths, history, media_store=MemoryMediaStore(), wrapper_prefix=WRAPPER_PREFIX)


def _log(catalogue: Catalogue) -> list[str]:
    assert catalogue.history is not None
    return [revision.message for revision in catalogue.history.log("carrot")]


def test_a_create_writes_its_files_in_the_same_revision(catalogue: Catalogue) -> None:
    record = catalogue.create(
        "carrot",
        "o1();\n",
        ModelMeta(name="Carrot", mesh_source=SOURCE),
        files={"meshes/o1-p1.3mf": b"PK\x03\x04binary", "scene.json": b"{}\n"},
    )
    directory = catalogue.paths.model_dir("carrot")
    assert (directory / "meshes" / "o1-p1.3mf").read_bytes() == b"PK\x03\x04binary"
    assert catalogue.read_raw_meta("carrot")["mesh_source"]["name"] == "carrot.3mf"
    assert record.version is not None
    assert len(_log(catalogue)) == 1


def test_a_model_without_a_mesh_source_has_no_key(catalogue: Catalogue) -> None:
    catalogue.create("carrot", "o1();\n", ModelMeta(name="Carrot"))
    assert "mesh_source" not in catalogue.read_raw_meta("carrot")


@pytest.mark.parametrize("name", ["../escape", "/abs", "meshes/../../x", ""])
def test_a_create_refuses_a_path_outside_the_model(catalogue: Catalogue, name: str) -> None:
    with pytest.raises(UnsafeModelPathError):
        catalogue.create("carrot", "o1();\n", ModelMeta(name="Carrot"), files={name: b"x"})
    assert not catalogue.exists("carrot")


def test_update_files_writes_and_removes_as_one_revision(catalogue: Catalogue) -> None:
    catalogue.create("carrot", "a\n", ModelMeta(name="Carrot"), files={"scene.json": b"1"})
    seen: list[bytes] = []

    def compute(directory: Path) -> dict[str, bytes | None]:
        seen.append((directory / "scene.json").read_bytes())
        return {"scene.json": None, "model.scad": b"b\n"}

    before = len(_log(catalogue))
    catalogue.update_files("carrot", compute, message="Edit the scene")
    directory = catalogue.paths.model_dir("carrot")
    assert seen == [b"1"]
    assert not (directory / "scene.json").exists()
    assert (directory / "model.scad").read_text() == "b\n"
    log = _log(catalogue)
    assert len(log) == before + 1 and log[0] == "Edit the scene"


def test_update_files_writes_nothing_when_compute_raises(catalogue: Catalogue) -> None:
    catalogue.create("carrot", "a\n", ModelMeta(name="Carrot"))
    before = len(_log(catalogue))

    def compute(directory: Path) -> dict[str, bytes | None]:
        raise ValueError("refused")

    with pytest.raises(ValueError, match="refused"):
        catalogue.update_files("carrot", compute, message="never")
    assert len(_log(catalogue)) == before


def test_update_files_refuses_a_path_outside_the_model(catalogue: Catalogue) -> None:
    catalogue.create("carrot", "a\n", ModelMeta(name="Carrot"))
    with pytest.raises(UnsafeModelPathError):
        catalogue.update_files("carrot", lambda _d: {"../x": b"1"}, message="no")


def test_update_files_restores_earlier_writes_when_a_later_one_fails(catalogue: Catalogue) -> None:
    catalogue.create("carrot", "a\n", ModelMeta(name="Carrot"), files={"one.txt": b"old"})
    directory = catalogue.paths.model_dir("carrot")
    (directory / "blocked").mkdir()
    (directory / "blocked" / "keep").write_bytes(b"x")
    before = _log(catalogue)

    def compute(_d: Path) -> dict[str, bytes | None]:
        return {"one.txt": b"new", "fresh.txt": b"f", "blocked": b"boom"}

    with pytest.raises(OSError):
        catalogue.update_files("carrot", compute, message="never")
    assert (directory / "one.txt").read_bytes() == b"old"
    assert not (directory / "fresh.txt").exists()
    assert _log(catalogue) == before


def test_update_files_restores_the_files_when_the_commit_fails(
    catalogue: Catalogue, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalogue.create("carrot", "a\n", ModelMeta(name="Carrot"), files={"one.txt": b"old"})
    assert catalogue.history is not None
    directory = catalogue.paths.model_dir("carrot")
    before = _log(catalogue)

    def fail(*_args: object, **_kwargs: object) -> str:
        raise GitError("boom")

    monkeypatch.setattr(catalogue.history, "_commit_locked", fail)
    with pytest.raises(GitError):
        catalogue.update_files(
            "carrot", lambda _d: {"one.txt": b"new", "fresh.txt": b"f"}, message="x"
        )
    assert (directory / "one.txt").read_bytes() == b"old"
    assert not (directory / "fresh.txt").exists()
    assert _log(catalogue) == before
