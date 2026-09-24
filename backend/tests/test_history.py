"""The git store under ``data/models``, exercised against a real repository."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from scadbuddy.core.paths import DataPaths
from scadbuddy.library.catalogue import Catalogue, ModelMeta, ModelPatch
from scadbuddy.library.history import (
    GITIGNORE_NAME,
    MAX_SUBJECT,
    ModelHistory,
    RevisionNotFoundError,
    subject_line,
    summarise,
)
from scadbuddy.render.solids import WRAPPER_PREFIX

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not on PATH")


@pytest.fixture
def models(tmp_path: Path) -> Path:
    directory = tmp_path / "data" / "models"
    directory.mkdir(parents=True)
    return directory


@pytest.fixture
def history(models: Path) -> ModelHistory:
    return ModelHistory(models, wrapper_prefix=WRAPPER_PREFIX)


def write_model(models: Path, slug: str, source: str) -> None:
    directory = models / slug
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "model.scad").write_text(source, encoding="utf-8")


def test_ensure_repo_makes_whatever_is_already_there_revision_one(
    models: Path, history: ModelHistory
) -> None:
    write_model(models, "keychain", "cube(10);\n")

    commit = history.ensure_repo()

    assert commit is not None
    assert history.available
    revisions = history.log("keychain")
    assert [revision.message for revision in revisions] == ["Initial revision"]
    assert [change.path for change in revisions[0].files] == ["keychain/model.scad"]


def test_ensure_repo_is_idempotent(models: Path, history: ModelHistory) -> None:
    write_model(models, "keychain", "cube(10);\n")
    first = history.ensure_repo()

    assert history.ensure_repo() is None
    assert history.head() == first


def test_ensure_repo_ignores_the_render_wrapper(models: Path, history: ModelHistory) -> None:
    write_model(models, "keychain", "cube(10);\n")
    history.ensure_repo()
    assert WRAPPER_PREFIX in (models / GITIGNORE_NAME).read_text(encoding="utf-8")

    (models / "keychain" / f"{WRAPPER_PREFIX}abc123.scad").write_text("// wrapper\n")

    assert history.commit("should record nothing", "keychain") is None


def test_a_commit_per_change_and_none_for_a_no_op(models: Path, history: ModelHistory) -> None:
    write_model(models, "keychain", "cube(10);\n")
    history.ensure_repo()

    write_model(models, "keychain", "cube(20);\n")
    second = history.commit("Edit keychain source", "keychain")

    assert second is not None
    assert history.commit("Edit keychain source", "keychain") is None
    assert [revision.message for revision in history.log("keychain")] == [
        "Edit keychain source",
        "Initial revision",
    ]


def test_history_is_scoped_to_one_model(models: Path, history: ModelHistory) -> None:
    write_model(models, "keychain", "cube(10);\n")
    history.ensure_repo()
    write_model(models, "plate", "sphere(5);\n")
    history.commit("Add plate", "plate")

    assert [revision.message for revision in history.log("keychain")] == ["Initial revision"]
    assert [revision.message for revision in history.log("plate")] == ["Add plate"]


def test_last_commit_is_the_models_own_revision(models: Path, history: ModelHistory) -> None:
    write_model(models, "keychain", "cube(10);\n")
    first = history.ensure_repo()
    write_model(models, "plate", "sphere(5);\n")
    second = history.commit("Add plate", "plate")

    # A commit against another model leaves this one where it was, which is why an
    # output stamps the model's own revision rather than the repository HEAD.
    assert history.head() == second
    assert history.last_commit("keychain") == first


def test_show_reads_a_file_at_a_revision(models: Path, history: ModelHistory) -> None:
    write_model(models, "keychain", "cube(10);\n")
    first = history.ensure_repo()
    assert first is not None
    write_model(models, "keychain", "cube(20);\n")
    history.commit("Edit keychain source", "keychain")

    assert history.show(first, "keychain/model.scad") == b"cube(10);\n"
    assert history.show("HEAD", "keychain/model.scad") == b"cube(20);\n"


def test_show_rejects_an_unknown_revision(models: Path, history: ModelHistory) -> None:
    write_model(models, "keychain", "cube(10);\n")
    history.ensure_repo()

    with pytest.raises(RevisionNotFoundError):
        history.show("0" * 40, "keychain/model.scad")


def test_diff_defaults_to_the_parent_and_handles_the_root_commit(
    models: Path, history: ModelHistory
) -> None:
    write_model(models, "keychain", "cube(10);\n")
    first = history.ensure_repo()
    write_model(models, "keychain", "cube(20);\n")
    second = history.commit("Edit keychain source", "keychain")
    assert first is not None and second is not None

    patch = history.diff(None, second, "keychain")
    assert "-cube(10);" in patch
    assert "+cube(20);" in patch
    assert [change.status for change in history.diff_files(None, second, "keychain")] == ["M"]

    # The root commit has no parent, so it diffs against the empty tree.
    root_patch = history.diff(None, first, "keychain")
    assert "+cube(10);" in root_patch
    assert [change.status for change in history.diff_files(None, first, "keychain")] == ["A"]


def test_diff_between_two_arbitrary_revisions(models: Path, history: ModelHistory) -> None:
    write_model(models, "keychain", "cube(10);\n")
    first = history.ensure_repo()
    write_model(models, "keychain", "cube(20);\n")
    history.commit("Edit once", "keychain")
    write_model(models, "keychain", "cube(30);\n")
    third = history.commit("Edit twice", "keychain")
    assert first is not None and third is not None

    patch = history.diff(first, third, "keychain")
    assert "-cube(10);" in patch
    assert "+cube(30);" in patch


def test_export_writes_an_ordinary_model_directory(
    models: Path, history: ModelHistory, tmp_path: Path
) -> None:
    write_model(models, "keychain", "cube(10);\n")
    (models / "keychain" / "model.json").write_text('{"name": "Keychain"}\n', encoding="utf-8")
    first = history.ensure_repo()
    write_model(models, "keychain", "cube(20);\n")
    history.commit("Edit keychain source", "keychain")
    assert first is not None

    destination = tmp_path / "export"
    history.export("keychain", first, destination)

    assert (destination / "model.scad").read_text(encoding="utf-8") == "cube(10);\n"
    assert (destination / "model.json").is_file()


def test_export_rejects_a_model_absent_from_that_revision(
    models: Path, history: ModelHistory, tmp_path: Path
) -> None:
    write_model(models, "keychain", "cube(10);\n")
    first = history.ensure_repo()
    write_model(models, "plate", "sphere(5);\n")
    history.commit("Add plate", "plate")
    assert first is not None

    with pytest.raises(RevisionNotFoundError):
        history.export("plate", first, tmp_path / "export")


def test_restore_is_a_new_commit_that_also_removes_later_files(
    models: Path, history: ModelHistory
) -> None:
    write_model(models, "keychain", "cube(10);\n")
    first = history.ensure_repo()
    write_model(models, "keychain", "cube(20);\n")
    (models / "keychain" / "helper.scad").write_text("// helper\n", encoding="utf-8")
    history.commit("Edit keychain and add a helper", "keychain")
    assert first is not None

    restored = history.restore("keychain", first)

    assert (models / "keychain" / "model.scad").read_text(encoding="utf-8") == "cube(10);\n"
    assert not (models / "keychain" / "helper.scad").exists()
    messages = [revision.message for revision in history.log("keychain")]
    assert messages[0] == f"Restore keychain to {first[:7]}"
    # A restore never rewrites: the revision it undid is still in the history.
    assert "Edit keychain and add a helper" in messages
    assert history.last_commit("keychain") == restored


def test_restore_of_the_current_revision_changes_nothing(
    models: Path, history: ModelHistory
) -> None:
    write_model(models, "keychain", "cube(10);\n")
    first = history.ensure_repo()
    # A commit against a different model moves the repository HEAD away, so a
    # no-op restore has to answer with THIS model's revision, not the HEAD.
    write_model(models, "plate", "sphere(5);\n")
    history.commit("Add plate", "plate")
    assert first is not None

    assert history.restore("keychain", first) == first
    assert history.head() != first


def test_the_repository_reads_the_same_from_a_plain_git(
    models: Path, history: ModelHistory
) -> None:
    """The point of shelling out: nothing here is a private format."""
    write_model(models, "keychain", "cube(10);\n")
    history.ensure_repo()

    log = subprocess.run(
        ["git", "log", "--format=%s%x09%an"],
        cwd=models,
        capture_output=True,
        text=True,
        check=True,
    )
    assert log.stdout.strip() == "Initial revision\tScadBuddy"


def test_a_control_byte_in_a_message_cannot_desync_the_log(
    models: Path, history: ModelHistory
) -> None:
    """`_parse_log` splits on ASCII RS/US, and the subject is caller-supplied.

    One of either byte in a message would move every later record boundary and
    silently drop the malformed chunks, so a corrupted history would read as a
    shorter one with no error anywhere.
    """
    write_model(models, "keychain", "cube(10);\n")
    history.ensure_repo()
    write_model(models, "keychain", "cube(20);\n")

    history.commit("bad\x1emessage\x1fhere\nand a second line", "keychain")

    revisions = history.log("keychain")
    assert len(revisions) == 2
    assert revisions[0].message == "bad message here and a second line"


def test_a_message_is_flattened_to_one_bounded_line() -> None:
    assert subject_line("  keep   it  tidy \n") == "keep it tidy"
    assert subject_line("\x00\x1f") == "(no message)"
    assert subject_line("") == "(no message)"
    assert len(subject_line("x" * (MAX_SUBJECT * 2))) == MAX_SUBJECT


def test_summarise_reads_as_a_sentence() -> None:
    assert summarise(["a"]) == "a"
    assert summarise(["a", "b"]) == "a and b"
    assert summarise(["a", "b", "c"]) == "a, b and c"


# ── the catalogue's side of it ────────────────────────────────────────────────


@pytest.fixture
def catalogue(tmp_path: Path) -> Catalogue:
    paths = DataPaths(tmp_path / "data")
    paths.ensure()
    history = ModelHistory(paths.models, wrapper_prefix=WRAPPER_PREFIX)
    history.ensure_repo()
    return Catalogue(paths, history)


def test_every_catalogue_action_is_exactly_one_commit(catalogue: Catalogue) -> None:
    assert catalogue.history is not None
    catalogue.create("keychain", "cube(10);\n", ModelMeta(name="Keychain"))
    catalogue.write_source("keychain", "cube(20);\n")
    catalogue.update("keychain", ModelPatch(description="nicer"))

    messages = [revision.message for revision in catalogue.history.log("keychain")]
    assert messages == ["Update keychain metadata", "Edit keychain source", "Add keychain"]


def test_a_record_carries_the_revision_it_is_at(catalogue: Catalogue) -> None:
    record = catalogue.create("keychain", "cube(10);\n", ModelMeta(name="Keychain"))
    assert record.version is not None

    edited = catalogue.write_source("keychain", "cube(20);\n")
    assert edited.version is not None
    assert edited.version != record.version


def test_writing_source_drops_the_stale_cached_schema(catalogue: Catalogue) -> None:
    catalogue.create("keychain", "cube(10);\n", ModelMeta(name="Keychain"))
    raw = catalogue.read_raw_meta("keychain")
    raw["schema"] = {"title": "stale", "source_sha256": "0" * 64, "parameters": [], "groups": []}
    catalogue.write_raw_meta("keychain", raw)

    catalogue.write_source("keychain", "cube(20);\n")

    assert "schema" not in catalogue.read_raw_meta("keychain")


def test_deleting_a_model_is_a_commit(catalogue: Catalogue) -> None:
    assert catalogue.history is not None
    catalogue.create("keychain", "cube(10);\n", ModelMeta(name="Keychain"))
    catalogue.delete("keychain")

    revisions = catalogue.history.log("keychain")
    assert revisions[0].message == "Delete keychain"
    assert sorted(change.path for change in revisions[0].files) == [
        "keychain/model.json",
        "keychain/model.scad",
    ]
    assert {change.status for change in revisions[0].files} == {"D"}


def test_deleting_an_unversioned_model_still_succeeds(catalogue: Catalogue) -> None:
    """A models directory can predate the repository; a delete must not blow up on it."""
    slug = "legacy"
    directory = catalogue.paths.model_dir(slug)
    directory.mkdir(parents=True)
    (directory / "model.scad").write_text("cube(1);\n", encoding="utf-8")

    catalogue.delete(slug)

    assert not directory.exists()


def test_seeding_records_the_seed_as_a_commit(catalogue: Catalogue, tmp_path: Path) -> None:
    assert catalogue.history is not None
    seed = tmp_path / "seed"
    (seed / "keychain").mkdir(parents=True)
    (seed / "keychain" / "model.scad").write_text("cube(10);\n", encoding="utf-8")

    assert catalogue.seed(seed) == ["keychain"]

    assert catalogue.history.log("keychain")[0].message == "Seed keychain from the image"
    # A re-seed skips what is already there, so it produces no second commit.
    assert catalogue.seed(seed) == []


def test_a_restore_moves_the_records_revision(catalogue: Catalogue) -> None:
    first = catalogue.create("keychain", "cube(10);\n", ModelMeta(name="Keychain")).version
    catalogue.write_source("keychain", "cube(20);\n")
    assert first is not None

    assert catalogue.history is not None
    catalogue.history.restore("keychain", first)

    record = catalogue.record("keychain")
    assert catalogue.paths.model_source("keychain").read_text(encoding="utf-8") == "cube(10);\n"
    assert record.version is not None
    assert record.version != first
