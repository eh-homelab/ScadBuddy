"""Mesh files in the blob store, a pointer in git (#7313): a mesh model's part meshes
stay in the working tree, git tracks `<name>.3mf.blob` beside each, and every path
that writes a revision into a directory fills the meshes back in from the store."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from scadbuddy.core.paths import DataPaths
from scadbuddy.library.catalogue import Catalogue, ModelMeta
from scadbuddy.library.history import GitError, ModelHistory
from scadbuddy.library.mesh_blobs import CONVERT_MESSAGE, MeshPointer
from scadbuddy.meshmodel.operations import SCENE_FILE
from scadbuddy.render.jobs import export_revision
from scadbuddy.render.solids import WRAPPER_PREFIX
from tests.support.media import MemoryMediaStore
from tests.support.meshes import MemoryMeshStore

pytestmark = pytest.mark.requires_git

MESH_A = b"PK\x03\x04mesh A" * 100
MESH_B = b"PK\x03\x04mesh B" * 50


@pytest.fixture
def models(tmp_path: Path) -> Path:
    directory = tmp_path / "data" / "models"
    directory.mkdir(parents=True)
    return directory


@pytest.fixture
def store() -> MemoryMeshStore:
    return MemoryMeshStore()


@pytest.fixture
def history(models: Path, store: MemoryMeshStore) -> ModelHistory:
    history = ModelHistory(models, wrapper_prefix=WRAPPER_PREFIX)
    history.meshes = store
    history.write_pointers = True
    history.ensure_repo()
    return history


def _write(models: Path, path: str, data: bytes) -> None:
    target = models / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def _tracked(history: ModelHistory, commit: str = "HEAD") -> list[str]:
    return history._out("ls-tree", "-r", "--name-only", commit).splitlines()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _mesh_model(models: Path, slug: str, mesh: bytes = MESH_A) -> None:
    _write(models, f"{slug}/model.scad", b'import("meshes/o1-p1.3mf");\n')
    _write(models, f"{slug}/meshes/o1-p1.3mf", mesh)


def _legacy_commit(history: ModelHistory, slug: str, *paths: str) -> str:
    """A commit made before #7313: the mesh's own bytes tracked in git."""
    history._run("add", "-f", "--", *paths)
    history._run("commit", "--no-verify", "-m", f"Add {slug}")
    head = history.head()
    assert head is not None
    return head


def test_a_commit_tracks_a_pointer_and_stores_the_bytes(
    models: Path, history: ModelHistory, store: MemoryMeshStore
) -> None:
    _mesh_model(models, "carrot")
    history.commit("Add carrot", "carrot")

    tracked = _tracked(history)
    assert "carrot/meshes/o1-p1.3mf.blob" in tracked
    assert "carrot/meshes/o1-p1.3mf" not in tracked
    pointer = MeshPointer.loads((models / "carrot/meshes/o1-p1.3mf.blob").read_bytes())
    assert pointer == MeshPointer(sha256=_sha(MESH_A), size=len(MESH_A))
    assert store.blobs[_sha(MESH_A)] == MESH_A
    # The working tree keeps the bytes every reader uses.
    assert (models / "carrot/meshes/o1-p1.3mf").read_bytes() == MESH_A
    # And no object in the repository holds them.
    mesh_id = history._out("hash-object", "--", "carrot/meshes/o1-p1.3mf").strip()
    assert mesh_id not in history._out("rev-list", "--objects", "--all")


def test_a_builtins_mesh_is_a_pointer_too(
    models: Path, history: ModelHistory, store: MemoryMeshStore
) -> None:
    _mesh_model(models, "_builtin/demo")
    history.commit("Sync demo", "_builtin/demo")
    tracked = _tracked(history)
    assert "_builtin/demo/meshes/o1-p1.3mf.blob" in tracked
    assert "_builtin/demo/meshes/o1-p1.3mf" not in tracked
    assert _sha(MESH_A) in store.blobs


def test_an_unchanged_mesh_is_not_stored_again(
    models: Path, history: ModelHistory, store: MemoryMeshStore
) -> None:
    _mesh_model(models, "carrot")
    history.commit("Add carrot", "carrot")
    _write(models, "carrot/model.scad", b"// edited\n")
    history.commit("Edit carrot", "carrot")
    assert store.puts == 1


def test_a_changed_mesh_moves_its_pointer(
    models: Path, history: ModelHistory, store: MemoryMeshStore
) -> None:
    _mesh_model(models, "carrot")
    history.commit("Add carrot", "carrot")
    _write(models, "carrot/meshes/o1-p1.3mf", MESH_B)
    history.commit("Edit carrot", "carrot")
    pointer = MeshPointer.loads(history.show("HEAD", "carrot/meshes/o1-p1.3mf.blob"))
    assert pointer.sha256 == _sha(MESH_B)


def test_a_deleted_mesh_deletes_its_pointer(models: Path, history: ModelHistory) -> None:
    _mesh_model(models, "carrot")
    history.commit("Add carrot", "carrot")
    (models / "carrot/meshes/o1-p1.3mf").unlink()
    history.commit("Edit carrot", "carrot")
    assert not (models / "carrot/meshes/o1-p1.3mf.blob").exists()
    assert "carrot/meshes/o1-p1.3mf.blob" not in _tracked(history)


def test_a_commit_with_a_mesh_and_no_store_is_refused(models: Path, history: ModelHistory) -> None:
    history.meshes = None
    _mesh_model(models, "carrot")
    with pytest.raises(GitError, match="blob store"):
        history.commit("Add carrot", "carrot")


def test_an_export_fills_the_meshes_in(models: Path, history: ModelHistory, tmp_path: Path) -> None:
    _mesh_model(models, "carrot")
    first = history.commit("Add carrot", "carrot")
    assert first is not None
    _write(models, "carrot/meshes/o1-p1.3mf", MESH_B)
    history.commit("Edit carrot", "carrot")

    dest = tmp_path / "export"
    export_revision(history, "carrot", first, dest)
    assert (dest / "meshes/o1-p1.3mf").read_bytes() == MESH_A
    # An ordinary model directory: no pointer in it.
    assert not (dest / "meshes/o1-p1.3mf.blob").exists()


def test_an_export_of_a_revision_from_before_the_pointers_keeps_its_meshes(
    models: Path, history: ModelHistory, tmp_path: Path
) -> None:
    _mesh_model(models, "carrot")
    legacy = _legacy_commit(history, "carrot", "carrot")
    dest = tmp_path / "export"
    history.export("carrot", legacy, dest)
    assert (dest / "meshes/o1-p1.3mf").read_bytes() == MESH_A


def test_an_export_whose_mesh_the_store_lacks_fails(
    models: Path, history: ModelHistory, store: MemoryMeshStore, tmp_path: Path
) -> None:
    _mesh_model(models, "carrot")
    commit = history.commit("Add carrot", "carrot")
    assert commit is not None
    store.blobs.clear()
    with pytest.raises(GitError):
        history.export("carrot", commit, tmp_path / "export")


def test_a_restore_puts_a_pointer_revisions_meshes_back(
    models: Path, history: ModelHistory
) -> None:
    _mesh_model(models, "carrot")
    first = history.commit("Add carrot", "carrot")
    assert first is not None
    _write(models, "carrot/meshes/o1-p1.3mf", MESH_B)
    _write(models, "carrot/meshes/o2-p1.3mf", MESH_B)
    history.commit("Edit carrot", "carrot")

    history.restore("carrot", first)
    assert (models / "carrot/meshes/o1-p1.3mf").read_bytes() == MESH_A
    # A mesh the revision did not have leaves, and its pointer with it.
    assert not (models / "carrot/meshes/o2-p1.3mf").exists()
    tracked = _tracked(history)
    assert "carrot/meshes/o2-p1.3mf.blob" not in tracked
    pointer = MeshPointer.loads(history.show("HEAD", "carrot/meshes/o1-p1.3mf.blob"))
    assert pointer.sha256 == _sha(MESH_A)
    assert history.clean("carrot")


def test_a_restore_of_a_revision_from_before_the_pointers_stores_its_meshes(
    models: Path, history: ModelHistory, store: MemoryMeshStore
) -> None:
    _mesh_model(models, "carrot")
    legacy = _legacy_commit(history, "carrot", "carrot")
    # The start's conversion, then a later edit.
    history.ensure_repo()
    _write(models, "carrot/meshes/o1-p1.3mf", MESH_B)
    history.commit("Edit carrot", "carrot")

    history.restore("carrot", legacy)
    assert (models / "carrot/meshes/o1-p1.3mf").read_bytes() == MESH_A
    tracked = _tracked(history)
    assert "carrot/meshes/o1-p1.3mf" not in tracked
    assert "carrot/meshes/o1-p1.3mf.blob" in tracked
    assert store.blobs[_sha(MESH_A)] == MESH_A


def test_a_restore_whose_mesh_the_store_lacks_changes_nothing(
    models: Path, history: ModelHistory, store: MemoryMeshStore
) -> None:
    _mesh_model(models, "carrot")
    first = history.commit("Add carrot", "carrot")
    assert first is not None
    _write(models, "carrot/meshes/o1-p1.3mf", MESH_B)
    head = history.commit("Edit carrot", "carrot")
    del store.blobs[_sha(MESH_A)]

    with pytest.raises(GitError):
        history.restore("carrot", first)
    assert history.head() == head
    assert (models / "carrot/meshes/o1-p1.3mf").read_bytes() == MESH_B
    assert history.clean("carrot")


def test_the_start_converts_meshes_tracked_as_bytes(
    models: Path, history: ModelHistory, store: MemoryMeshStore
) -> None:
    _mesh_model(models, "carrot")
    _mesh_model(models, "_builtin/demo", MESH_B)
    _legacy_commit(history, "carrot", "carrot", "_builtin/demo")

    converted = history.ensure_repo()
    assert converted is not None
    assert history.log("carrot")[0].message == CONVERT_MESSAGE
    tracked = _tracked(history)
    assert "carrot/meshes/o1-p1.3mf" not in tracked
    assert "_builtin/demo/meshes/o1-p1.3mf" not in tracked
    assert "carrot/meshes/o1-p1.3mf.blob" in tracked
    assert "_builtin/demo/meshes/o1-p1.3mf.blob" in tracked
    assert store.blobs[_sha(MESH_A)] == MESH_A
    assert store.blobs[_sha(MESH_B)] == MESH_B
    assert (models / "carrot/meshes/o1-p1.3mf").read_bytes() == MESH_A
    # Once: the next start finds nothing to do.
    assert history.ensure_repo() is None


def test_the_start_without_a_store_leaves_tracked_meshes_alone(models: Path) -> None:
    history = ModelHistory(models, wrapper_prefix=WRAPPER_PREFIX)
    history.ensure_repo()
    _mesh_model(models, "carrot")
    _legacy_commit(history, "carrot", "carrot")
    history.ensure_repo()
    assert "carrot/meshes/o1-p1.3mf" in _tracked(history)


def test_a_pointer_newer_than_a_mesh_of_its_size_is_not_trusted(
    models: Path, history: ModelHistory
) -> None:
    """A pointer is never taken on file times alone: a revision naming other bytes
    than the mesh it was committed with could not be noticed later."""
    _mesh_model(models, "carrot")
    history.commit("Add carrot", "carrot")
    other = bytes(reversed(MESH_A))
    _write(models, "carrot/meshes/o1-p1.3mf", other)
    os.utime(models / "carrot/meshes/o1-p1.3mf", (1, 1))
    history.commit("Edit carrot", "carrot")
    pointer = MeshPointer.loads(history.show("HEAD", "carrot/meshes/o1-p1.3mf.blob"))
    assert pointer.sha256 == _sha(other)


def test_a_start_fills_in_meshes_the_volume_lost(models: Path, history: ModelHistory) -> None:
    """A volume restored from the off-box copy holds the pointers and no mesh."""
    _mesh_model(models, "carrot")
    head = history.commit("Add carrot", "carrot")
    (models / "carrot/meshes/o1-p1.3mf").unlink()
    assert history.ensure_repo() is None
    assert history.head() == head
    assert (models / "carrot/meshes/o1-p1.3mf").read_bytes() == MESH_A


def test_a_start_keeps_the_pointer_of_a_mesh_the_store_lacks(
    models: Path, history: ModelHistory, store: MemoryMeshStore
) -> None:
    _mesh_model(models, "carrot")
    head = history.commit("Add carrot", "carrot")
    (models / "carrot/meshes/o1-p1.3mf").unlink()
    store.blobs.clear()
    assert history.ensure_repo() is None
    assert history.head() == head
    assert "carrot/meshes/o1-p1.3mf.blob" in _tracked(history)


def test_a_restore_over_a_mesh_tracked_as_bytes_keeps_it(
    models: Path, history: ModelHistory
) -> None:
    _mesh_model(models, "carrot")
    first = history.commit("Add carrot", "carrot")
    assert first is not None
    # HEAD holds the same mesh as bytes, as a replica on the old code can leave it.
    (models / "carrot/meshes/o1-p1.3mf.blob").unlink()
    history._run("add", "-A", "--", "carrot")
    _legacy_commit(history, "carrot", "carrot/meshes/o1-p1.3mf")
    history.restore("carrot", first)
    assert (models / "carrot/meshes/o1-p1.3mf").read_bytes() == MESH_A
    assert "carrot/meshes/o1-p1.3mf.blob" in _tracked(history)


def test_the_conversion_stores_meshes_before_it_takes_the_write_lock(
    models: Path, history: ModelHistory, store: MemoryMeshStore
) -> None:
    """Another replica's commit waits only `git_timeout` for the lock."""
    _mesh_model(models, "carrot")
    _legacy_commit(history, "carrot", "carrot")
    held: list[bool] = []
    put = store.put

    def watched(*args: Any) -> Any:
        held.append(history._lock.locked())
        return put(*args)

    store.put = watched  # type: ignore[assignment,method-assign]
    history.ensure_repo()
    assert held[0] is False
    assert history.log("carrot")[0].message == CONVERT_MESSAGE


@pytest.fixture
def reader(models: Path, store: MemoryMeshStore) -> ModelHistory:
    """A pod that reads pointers but does not turn them on: the release before the one
    that does, still serving while that one rolls out (#7313)."""
    reader = ModelHistory(models, wrapper_prefix=WRAPPER_PREFIX)
    reader.meshes = store
    reader.write_pointers = False
    return reader


def test_a_reader_changes_nothing_in_a_repository_without_pointers(
    models: Path, reader: ModelHistory, store: MemoryMeshStore
) -> None:
    """Safe beside pods that do not know pointers: git and `.gitignore` are as theirs."""
    reader.ensure_repo()
    _mesh_model(models, "carrot")
    first = reader.commit("Add carrot", "carrot")
    assert first is not None
    assert "carrot/meshes/o1-p1.3mf" in _tracked(reader)
    assert "meshes" not in (models / ".gitignore").read_text()
    assert not (models / "carrot/meshes/o1-p1.3mf.blob").exists()
    assert store.blobs == {}
    assert reader.ensure_repo() is None
    _write(models, "carrot/meshes/o1-p1.3mf", MESH_B)
    reader.commit("Edit carrot", "carrot")
    reader.restore("carrot", first)
    assert (models / "carrot/meshes/o1-p1.3mf").read_bytes() == MESH_A
    assert reader.clean("carrot")


def test_the_default_is_a_reader(models: Path) -> None:
    """The release that writes pointers is a later one: this one only reads them."""
    assert ModelHistory(models).write_pointers is False


def test_a_reader_exports_a_pointer_revision_with_its_meshes(
    models: Path, history: ModelHistory, reader: ModelHistory, tmp_path: Path
) -> None:
    """What it exports is cached by revision for every pod, after the roll too."""
    _mesh_model(models, "carrot")
    commit = history.commit("Add carrot", "carrot")
    assert commit is not None
    dest = tmp_path / "export"
    export_revision(reader, "carrot", commit, dest)
    assert (dest / "meshes/o1-p1.3mf").read_bytes() == MESH_A


def test_a_reader_with_no_mesh_caches_no_export(
    models: Path, history: ModelHistory, reader: ModelHistory, tmp_path: Path
) -> None:
    _mesh_model(models, "carrot")
    commit = history.commit("Add carrot", "carrot")
    assert commit is not None
    reader.meshes = None
    dest = tmp_path / "export"
    with pytest.raises(GitError):
        export_revision(reader, "carrot", commit, dest)
    assert not dest.exists()


def test_a_reader_restores_a_pointer_revision(
    models: Path, history: ModelHistory, reader: ModelHistory
) -> None:
    _mesh_model(models, "carrot")
    first = history.commit("Add carrot", "carrot")
    assert first is not None
    _write(models, "carrot/meshes/o1-p1.3mf", MESH_B)
    _write(models, "carrot/meshes/o2-p1.3mf", MESH_B)
    history.commit("Edit carrot", "carrot")
    reader.restore("carrot", first)
    assert (models / "carrot/meshes/o1-p1.3mf").read_bytes() == MESH_A
    assert not (models / "carrot/meshes/o2-p1.3mf").exists()
    assert "carrot/meshes/o1-p1.3mf" not in _tracked(reader)


def test_a_reader_that_starts_after_the_conversion_puts_no_mesh_back(
    models: Path, history: ModelHistory, reader: ModelHistory
) -> None:
    """A crash or a rollback starts it on a converted repository."""
    _mesh_model(models, "carrot")
    _legacy_commit(history, "carrot", "carrot")
    head = history.ensure_repo()
    ignored = (models / ".gitignore").read_text()
    assert reader.ensure_repo() is None
    assert reader.head() == head
    assert (models / ".gitignore").read_text() == ignored
    _write(models, "carrot/meshes/o2-p1.3mf", MESH_B)
    reader.commit("Edit carrot", "carrot")
    assert [p for p in _tracked(reader) if p.endswith(".3mf")] == []
    assert "carrot/meshes/o2-p1.3mf.blob" in _tracked(reader)


def test_a_rollback_after_the_pointers_left_is_healed_by_the_next_commit(
    models: Path, history: ModelHistory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Staging removes a deleted mesh's pointer; the action's rollback puts back the
    mesh alone, and the next commit writes the pointer again."""
    _mesh_model(models, "carrot")
    history.commit("Add carrot", "carrot")
    mesh = models / "carrot/meshes/o1-p1.3mf"

    def refuse(*_args: Any) -> None:
        raise GitError("refused")

    with monkeypatch.context() as patched:
        patched.setattr(history, "_commit_with_retry", refuse)
        with pytest.raises(GitError, match="refused"):
            history.commit(
                "Edit carrot",
                "carrot",
                prepare=mesh.unlink,
                rollback=lambda: _write(models, "carrot/meshes/o1-p1.3mf", MESH_A),
            )
    assert mesh.read_bytes() == MESH_A
    _write(models, "carrot/model.scad", b"// edited\n")
    history.commit("Edit carrot", "carrot")
    pointer = MeshPointer.loads(history.show("HEAD", "carrot/meshes/o1-p1.3mf.blob"))
    assert pointer.sha256 == _sha(MESH_A)


def test_the_gitignore_keeps_meshes_out(models: Path, history: ModelHistory) -> None:
    body = (models / ".gitignore").read_text()
    assert "*/meshes/*.3mf" in body.splitlines()
    assert "_builtin/*/meshes/*.3mf" in body.splitlines()


def test_a_scene_write_may_name_a_pointer() -> None:
    assert SCENE_FILE.fullmatch("meshes/o1-p1.3mf.blob")


@pytest.fixture
def catalogue(tmp_path: Path, store: MemoryMeshStore) -> Catalogue:
    paths = DataPaths(tmp_path / "data")
    paths.ensure()
    history = ModelHistory(paths.models, wrapper_prefix=WRAPPER_PREFIX)
    history.meshes = store
    history.write_pointers = True
    history.ensure_repo()
    return Catalogue(paths, history, media_store=MemoryMediaStore(), wrapper_prefix=WRAPPER_PREFIX)


def test_an_unchanged_builtin_with_a_mesh_is_not_copied_again(
    catalogue: Catalogue, tmp_path: Path
) -> None:
    """The mirror has a pointer beside the mesh; the image has the mesh alone."""
    bundled = tmp_path / "bundled"
    _mesh_model(bundled, "demo")
    assert catalogue.sync_builtins(bundled) is not None
    mesh = catalogue.paths.models / "_builtin/demo/meshes/o1-p1.3mf"
    first = mesh.stat().st_ino
    assert mesh.with_name("o1-p1.3mf.blob").is_file()
    assert catalogue.sync_builtins(bundled) is None
    assert mesh.stat().st_ino == first
    # A changed mesh still is.
    _mesh_model(bundled, "demo", MESH_B)
    assert catalogue.sync_builtins(bundled) is not None
    assert mesh.read_bytes() == MESH_B


def test_a_duplicate_copies_the_meshes(catalogue: Catalogue) -> None:
    catalogue.create(
        "carrot", "o1();\n", ModelMeta(name="Carrot"), files={"meshes/o1-p1.3mf": MESH_A}
    )
    catalogue.duplicate("carrot", "parsnip", "Parsnip")
    directory = catalogue.paths.model_dir("parsnip")
    assert (directory / "meshes/o1-p1.3mf").read_bytes() == MESH_A
    assert catalogue.history is not None
    assert "parsnip/meshes/o1-p1.3mf.blob" in _tracked(catalogue.history)


def test_a_merge_from_the_upstream_takes_its_new_mesh(catalogue: Catalogue) -> None:
    catalogue.create(
        "carrot", "o1();\n", ModelMeta(name="Carrot"), files={"meshes/o1-p1.3mf": MESH_A}
    )
    catalogue.duplicate("carrot", "parsnip", "Parsnip")
    catalogue.update_files(
        "carrot",
        lambda _d: {"meshes/o1-p1.3mf": MESH_B, "meshes/o2-p1.3mf": MESH_B},
        message="Edit the scene",
    )

    _, plan = catalogue.merge_upstream("parsnip")
    assert sorted(plan.preview.taken) == ["meshes/o1-p1.3mf", "meshes/o2-p1.3mf"]
    directory = catalogue.paths.model_dir("parsnip")
    assert (directory / "meshes/o1-p1.3mf").read_bytes() == MESH_B
    assert (directory / "meshes/o2-p1.3mf").read_bytes() == MESH_B
    assert catalogue.history is not None
    pointer = json.loads(catalogue.history.show("HEAD", "parsnip/meshes/o2-p1.3mf.blob"))
    assert pointer["sha256"] == _sha(MESH_B)
