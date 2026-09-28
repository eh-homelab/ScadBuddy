"""The git store under ``data/models``, exercised against a real repository."""

from __future__ import annotations

import fcntl
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from scadbuddy.core.paths import DataPaths
from scadbuddy.library.catalogue import Catalogue, ModelMeta, ModelPatch
from scadbuddy.library.history import (
    DEFAULT_TIMEOUT,
    GITIGNORE_NAME,
    LOCK_NAME,
    MAX_SUBJECT,
    RECOVERED_MESSAGE,
    GitTimeoutError,
    ModelHistory,
    RevisionNotFoundError,
    subject_line,
)
from scadbuddy.library.slugs import MAX_SLUG_LENGTH
from scadbuddy.render.jobs import prune_revision_exports
from scadbuddy.render.render_cache import RENDERS_DIR_NAME
from scadbuddy.render.solids import WRAPPER_PREFIX

pytestmark = pytest.mark.requires_git


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


def test_a_restart_names_leftover_changes_for_what_they_are(
    models: Path, history: ModelHistory
) -> None:
    """A catalogue commit that failed leaves the tree dirty; the next boot commits it,
    and must not call revision N the initial one (#134)."""
    write_model(models, "keychain", "cube(10);\n")
    history.ensure_repo()
    write_model(models, "tag", "cube(5);\n")

    recovered = history.ensure_repo()

    assert recovered is not None
    assert [entry.message for entry in history.log()][:2] == [
        RECOVERED_MESSAGE,
        "Initial revision",
    ]


def test_a_repository_with_no_commit_yet_still_gets_its_initial_revision(
    models: Path, history: ModelHistory
) -> None:
    """A crash between `git init` and the first commit leaves `.git` with no history;
    what the next boot commits there is the first revision, not a recovery."""
    write_model(models, "keychain", "cube(10);\n")
    subprocess.run(["git", "init", "--quiet", str(models)], check=True)

    assert history.ensure_repo() is not None
    assert [entry.message for entry in history.log()] == ["Initial revision"]


def test_ensure_repo_survives_a_models_path_it_cannot_create(tmp_path: Path) -> None:
    """A git failure other than a missing binary must not take the app down.

    The app boots and serves models whether or not history works, so an
    uninitialisable repository degrades exactly like an absent `git`: nothing is
    versioned and the history routes answer 503.
    """
    blocked = tmp_path / "models"
    blocked.write_text("not a directory", encoding="utf-8")
    history = ModelHistory(blocked, wrapper_prefix=WRAPPER_PREFIX)

    assert history.ensure_repo() is None
    assert not history.available


def test_ensure_repo_survives_a_broken_dot_git(models: Path) -> None:
    (models / ".git").mkdir()
    (models / ".git" / "HEAD").write_text("not a ref\n", encoding="utf-8")
    history = ModelHistory(models, wrapper_prefix=WRAPPER_PREFIX)

    assert history.ensure_repo() is None


def test_ensure_repo_ignores_the_render_wrapper(models: Path, history: ModelHistory) -> None:
    write_model(models, "keychain", "cube(10);\n")
    history.ensure_repo()
    assert WRAPPER_PREFIX in (models / GITIGNORE_NAME).read_text(encoding="utf-8")

    (models / "keychain" / f"{WRAPPER_PREFIX}abc123.scad").write_text("// wrapper\n")

    assert history.commit("should record nothing", "keychain") is None


def test_ensure_repo_ignores_the_kept_renders(models: Path, history: ModelHistory) -> None:
    """A finished render is kept under its template (`render_cache`); it is derived
    from the source, so it must never move the template's revision."""
    write_model(models, "keychain", "cube(10);\n")
    history.ensure_repo()

    entry = models / "keychain" / RENDERS_DIR_NAME / "0123abcd"
    entry.mkdir(parents=True)
    (entry / "model.3mf").write_bytes(b"3mf")
    (entry / "render.json").write_text("{}\n", encoding="utf-8")

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


def test_last_commits_answers_the_whole_catalogue_in_one_walk(
    models: Path, history: ModelHistory
) -> None:
    """Listing the catalogue must not fork `git log` once per model."""
    write_model(models, "keychain", "cube(10);\n")
    write_model(models, "plate", "sphere(5);\n")
    history.ensure_repo()
    write_model(models, "keychain", "cube(20);\n")
    history.commit("Edit keychain source", "keychain")

    newest = history.last_commits()

    assert newest == {
        "keychain": history.last_commit("keychain"),
        "plate": history.last_commit("plate"),
    }
    assert newest["keychain"] != newest["plate"]


def test_last_commits_keys_a_built_in_by_its_own_id(models: Path, history: ModelHistory) -> None:
    """Not by its first path component, which would fold every built-in into one."""
    write_model(models, "_builtin/keychain", "cube(10);\n")
    write_model(models, "_builtin/plate", "sphere(5);\n")
    history.ensure_repo()
    write_model(models, "_builtin/keychain", "cube(20);\n")
    history.commit("Sync built-in templates from the image", "_builtin")

    newest = history.last_commits()

    assert newest == {
        "builtin:keychain": history.last_commit("_builtin/keychain"),
        "builtin:plate": history.last_commit("_builtin/plate"),
    }
    assert newest["builtin:keychain"] != newest["builtin:plate"]


def test_last_commits_ignores_files_at_the_repository_root(
    models: Path, history: ModelHistory
) -> None:
    """`.gitignore` lives beside the models and is not one of them."""
    write_model(models, "keychain", "cube(10);\n")
    history.ensure_repo()

    assert set(history.last_commits()) == {"keychain"}


def test_last_commits_is_empty_before_the_first_commit(models: Path) -> None:
    history = ModelHistory(models, wrapper_prefix=WRAPPER_PREFIX)

    assert history.last_commits() == {}


def test_merge_file_merges_clean_edits_and_counts_conflicts(history: ModelHistory) -> None:
    base = "a = 1;\nb = 2;\nc = 3;\nd = 4;\ne = 5;\n"
    ours = base.replace("a = 1;", "a = 10;")

    merged, conflicts = history.merge_file(
        ours, base, base.replace("e = 5;", "e = 50;"), labels=("ours", "base", "theirs")
    )
    assert (merged, conflicts) == (ours.replace("e = 5;", "e = 50;"), 0)

    marked, conflicts = history.merge_file(
        ours, base, base.replace("a = 1;", "a = 11;"), labels=("ours", "base", "theirs")
    )
    assert conflicts == 1
    assert marked.startswith("<<<<<<< ours\na = 10;\n||||||| base\na = 1;\n=======\na = 11;\n")


def test_files_at_lists_a_directory_relative_to_itself(models: Path, history: ModelHistory) -> None:
    write_model(models, "_builtin/keychain", "cube(10);\n")
    (models / "_builtin/keychain/README.md").write_text("hi\n", encoding="utf-8")
    commit = history.ensure_repo()
    assert commit is not None

    assert history.files_at(commit, "_builtin/keychain") == ["README.md", "model.scad"]


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

    patch = history.diff(history.revision_range(None, second), "keychain")
    assert "-cube(10);" in patch
    assert "+cube(20);" in patch
    assert [
        change.status
        for change in history.diff_files(history.revision_range(None, second), "keychain")
    ] == ["M"]

    # The root commit has no parent, so it diffs against the empty tree.
    root_patch = history.diff(history.revision_range(None, first), "keychain")
    assert "+cube(10);" in root_patch
    assert [
        change.status
        for change in history.diff_files(history.revision_range(None, first), "keychain")
    ] == ["A"]


def test_a_diff_through_a_file_git_takes_for_text_but_is_not_utf8_still_renders(
    models: Path, history: ModelHistory
) -> None:
    """A thumbnail with the PNG signature and no NUL is all `_require_png` asks of
    one (#179), and git diffs it as text -- the 0x89 must not fail the whole patch,
    in the versions diff or in an upstream preview's tree diff (#239)."""
    write_model(models, "keychain", "cube(10);\n")
    first = history.ensure_repo()
    (models / "keychain" / "thumbnail.png").write_bytes(b"\x89PNG\r\n\x1a\nno nul here")
    second = history.commit("Set keychain thumbnail", "keychain")
    assert first is not None and second is not None

    patch = history.diff(history.revision_range(None, second), "keychain")
    tree_patch = history.diff_dirs(first, "keychain", second, "keychain", label="keychain")

    for shown in (patch, tree_patch):
        assert "thumbnail.png" in shown
        assert "\ufffdPNG" in shown


def test_a_default_diff_resolves_its_endpoints_once(
    models: Path, history: ModelHistory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The panel asks for a diff on every row click, so the cost is pinned here.

    ``revision_range`` exists precisely so the patch, the file list and the base
    echoed back to the UI share one resolution; an implementation that resolved
    per call spawned a dozen processes to answer one request.
    """
    write_model(models, "keychain", "cube(10);\n")
    history.ensure_repo()
    write_model(models, "keychain", "cube(20);\n")
    second = history.commit("Edit keychain source", "keychain")
    assert second is not None

    inner = history._run
    commands: list[str] = []

    def spy(*args: str, **kwargs: object) -> object:
        commands.append(args[0])
        return inner(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(history, "_run", spy)
    revisions = history.revision_range(None, second)
    history.diff(revisions, "keychain")
    history.diff_files(revisions, "keychain")

    # One rev-parse for the head, one for its parent, then the two diffs.
    assert commands == ["rev-parse", "rev-parse", "diff", "diff"]


def test_a_stalled_git_call_times_out_rather_than_hanging(tmp_path: Path, models: Path) -> None:
    """Every git call shares the executor `/healthz` runs on, so none may be unbounded."""
    stalled = tmp_path / "slow-git"
    stalled.write_text("#!/bin/sh\nsleep 30\n")
    stalled.chmod(0o755)
    history = ModelHistory(models, git=str(stalled), wrapper_prefix=WRAPPER_PREFIX, timeout=0.2)

    with pytest.raises(GitTimeoutError) as caught:
        history.log("keychain")
    assert "timed out after 0.2s" in str(caught.value)


def test_the_write_lock_wait_is_bounded_too(models: Path, history: ModelHistory) -> None:
    """A wedged holder must not pin an executor slot for as long as it lasts."""
    write_model(models, "keychain", "cube(10);\n")
    history.ensure_repo()
    history.timeout = 0.2

    # A second open file description on the same lock: flock is per-description,
    # so this blocks the repository's own acquisition exactly as another process
    # would, without needing one.
    with (models / LOCK_NAME).open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        write_model(models, "keychain", "cube(20);\n")
        with pytest.raises(GitTimeoutError):
            history.commit("Edit keychain source", "keychain")
        fcntl.flock(held, fcntl.LOCK_UN)

    # The lock is free again, so the very same commit now lands.
    history.timeout = DEFAULT_TIMEOUT
    assert history.commit("Edit keychain source", "keychain") is not None


def test_diff_between_two_arbitrary_revisions(models: Path, history: ModelHistory) -> None:
    write_model(models, "keychain", "cube(10);\n")
    first = history.ensure_repo()
    write_model(models, "keychain", "cube(20);\n")
    history.commit("Edit once", "keychain")
    write_model(models, "keychain", "cube(30);\n")
    third = history.commit("Edit twice", "keychain")
    assert first is not None and third is not None

    patch = history.diff(history.revision_range(first, third), "keychain")
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
    assert subject_line("a\x7fb\x85c\x9fd") == "a b c d"
    assert subject_line("\x7f\x80\x9f") == "(no message)"
    assert subject_line("") == "(no message)"
    assert len(subject_line("x" * (MAX_SUBJECT * 2))) == MAX_SUBJECT


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


def test_a_legacy_schema_key_is_retired_from_model_json(catalogue: Catalogue) -> None:
    """Volumes written before the cache moved out of `models/` still carry one.

    Planted by writing the file directly, because `write_raw_meta` is exactly
    what drops it.
    """
    catalogue.create("keychain", "cube(10);\n", ModelMeta(name="Keychain"))
    meta_path = catalogue.paths.model_meta("keychain")
    legacy = json.loads(meta_path.read_text(encoding="utf-8"))
    legacy["schema"] = {"title": "stale", "source_sha256": "0" * 64, "parameters": []}
    meta_path.write_text(json.dumps(legacy), encoding="utf-8")
    assert "schema" in json.loads(meta_path.read_text(encoding="utf-8"))

    catalogue.update("keychain", ModelPatch(description="nicer"))

    assert "schema" not in json.loads(meta_path.read_text(encoding="utf-8"))


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


def _bundle(root: Path, slug: str, source: str) -> Path:
    directory = root / slug
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "model.scad").write_text(source, encoding="utf-8")
    return directory


def test_syncing_mirrors_the_image_as_one_commit(catalogue: Catalogue, tmp_path: Path) -> None:
    assert catalogue.history is not None
    image = tmp_path / "image"
    _bundle(image, "keychain", "cube(10);\n")
    (_bundle(image, "tag", "cube(5);\n") / ".gitignore").write_text("x\n", encoding="utf-8")

    assert catalogue.sync_builtins(image) is not None

    mirror = catalogue.paths.models / "_builtin"
    assert (mirror / "keychain" / "model.scad").read_text(encoding="utf-8") == "cube(10);\n"
    # Dotfiles are repo furniture, not template content.
    assert not (mirror / "tag" / ".gitignore").exists()
    assert [revision.message for revision in catalogue.history.log("_builtin")] == [
        "Sync built-in templates from the image"
    ]
    # An unchanged image is no commit at all.
    assert catalogue.sync_builtins(image) is None
    assert len(catalogue.history.log("_builtin")) == 1


def test_an_unchanged_built_in_is_not_rewritten(catalogue: Catalogue, tmp_path: Path) -> None:
    """Every boot syncs; one that finds nothing changed must not write to the PVC."""
    image = tmp_path / "image"
    _bundle(image, "keychain", "cube(10);\n")
    _bundle(image, "tag", "cube(5);\n")
    catalogue.sync_builtins(image)
    mirror = catalogue.paths.models / "_builtin"
    before = {path: path.stat() for path in mirror.rglob("*")}
    (image / "tag" / "model.scad").write_text("cube(6);\n", encoding="utf-8")

    catalogue.sync_builtins(image)

    keychain = {path: stat for path, stat in before.items() if "keychain" in path.parts}
    assert keychain
    for path, stat in keychain.items():
        assert (path.stat().st_ino, path.stat().st_mtime_ns) == (stat.st_ino, stat.st_mtime_ns)
    assert (mirror / "tag" / "model.scad").read_text(encoding="utf-8") == "cube(6);\n"


def test_an_unchanged_built_in_is_judged_by_its_stats_alone(
    catalogue: Catalogue, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Matching sizes and mtimes settle a boot's sync without reading a byte (#206);
    a file whose mtime moved but whose bytes did not is still no change."""
    image = tmp_path / "image"
    _bundle(image, "keychain", "cube(10);\n")
    catalogue.sync_builtins(image)
    mirror = catalogue.paths.models / "_builtin" / "keychain" / "model.scad"
    before = mirror.stat()

    with monkeypatch.context() as patched:

        def refuse(path: Path) -> bytes:
            raise AssertionError(f"read {path}")

        patched.setattr(Path, "read_bytes", refuse)
        assert catalogue.sync_builtins(image) is None

    os.utime(image / "keychain" / "model.scad", ns=(before.st_atime_ns, before.st_mtime_ns + 10**9))
    assert catalogue.sync_builtins(image) is None
    assert (mirror.stat().st_ino, mirror.stat().st_mtime_ns) == (before.st_ino, before.st_mtime_ns)


def test_a_bundled_directory_that_is_no_slug_is_skipped(
    catalogue: Catalogue, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """No route could reach a built-in whose name is no slug (#197)."""
    image = tmp_path / "image"
    _bundle(image, "keychain", "cube(10);\n")
    bad = ["Key_Chain", "-tag", "x" * (MAX_SLUG_LENGTH + 1)]
    for name in bad:
        _bundle(image, name, "cube(5);\n")

    with caplog.at_level(logging.WARNING):
        assert catalogue.sync_builtins(image) is not None

    mirror = catalogue.paths.models / "_builtin"
    assert sorted(path.name for path in mirror.iterdir()) == ["keychain"]
    skipped = [
        str(getattr(record, "slug", ""))
        for record in caplog.records
        if record.getMessage().startswith("not a usable slug")
    ]
    assert sorted(skipped) == sorted(bad)


def test_syncing_overwrites_and_drops_what_the_image_no_longer_has(
    catalogue: Catalogue, tmp_path: Path
) -> None:
    assert catalogue.history is not None
    image = tmp_path / "image"
    _bundle(image, "keychain", "cube(10);\n")
    (_bundle(image, "tag", "cube(5);\n") / "part.scad").write_text("x=1;\n", encoding="utf-8")
    catalogue.sync_builtins(image)
    mirror = catalogue.paths.models / "_builtin"
    # Nothing but the sync writes the mirror; a stray edit is overwritten too.
    (mirror / "keychain" / "model.scad").write_text("tampered\n", encoding="utf-8")
    (image / "tag" / "part.scad").unlink()
    (image / "tag" / "model.scad").write_text("cube(6);\n", encoding="utf-8")
    shutil.rmtree(image / "keychain")

    assert catalogue.sync_builtins(image) is not None

    assert not (mirror / "keychain").exists()
    assert not (mirror / "tag" / "part.scad").exists()
    assert (mirror / "tag" / "model.scad").read_text(encoding="utf-8") == "cube(6);\n"
    assert len(catalogue.history.log("_builtin")) == 2


def test_a_template_of_mine_is_never_touched_by_the_sync(
    catalogue: Catalogue, tmp_path: Path
) -> None:
    catalogue.create("keychain", "// mine\n", ModelMeta(name="Mine"))
    image = tmp_path / "image"
    _bundle(image, "keychain", "// the image\n")

    catalogue.sync_builtins(image)

    assert catalogue.paths.model_source("keychain").read_text(encoding="utf-8") == "// mine\n"
    assert catalogue.paths.model_source("builtin:keychain").read_text(encoding="utf-8") == (
        "// the image\n"
    )
    records = {record.slug: record.origin for record in catalogue.list_models()}
    assert records == {"builtin:keychain": "builtin", "keychain": "mine"}


def test_a_built_in_model_json_never_sets_origin_url(catalogue: Catalogue, tmp_path: Path) -> None:
    """Only a URL import sets `origin_url` (#179); a built-in keeps the rest of its file,
    and its mirror stays byte-identical to the image."""
    image = tmp_path / "image"
    (image / "keychain").mkdir(parents=True)
    (image / "keychain" / "model.scad").write_text("cube(10);\n", encoding="utf-8")
    meta = {"name": "Keychain", "source": "inspired", "origin_url": "javascript:alert(1)"}
    (image / "keychain" / "model.json").write_text(json.dumps(meta), encoding="utf-8")

    assert catalogue.sync_builtins(image) is not None

    record = catalogue.record("builtin:keychain")
    assert (record.name, record.source, record.origin_url) == ("Keychain", "inspired", None)
    # Dropped on read, not rewritten: a rewrite would make every boot re-sync it.
    assert catalogue.sync_builtins(image) is None


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


def test_a_filesystem_failure_never_fails_the_action_itself(
    catalogue: Catalogue, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The files are written before the commit, so a lock-file `OSError` has to
    degrade the same way a `GitError` does.

    Otherwise a PVC that went read-only after boot 500s a source edit that has
    already landed on disk, telling the client it failed when it did not.
    """
    catalogue.create("keychain", "cube(10);\n", ModelMeta(name="Keychain"))
    assert catalogue.history is not None
    monkeypatch.setattr(
        catalogue.history,
        "commit",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("read-only file system")),
    )

    record = catalogue.write_source("keychain", "cube(20);\n")

    assert catalogue.paths.model_source("keychain").read_text(encoding="utf-8") == "cube(20);\n"
    assert record.slug == "keychain"


def test_revision_exports_are_evicted_by_last_use(tmp_path: Path) -> None:
    """They are a cache, and nothing else ever removes one.

    `cache/schema/` is one file per slug overwritten in place, but every distinct
    revision anyone opens "Customize this version" on writes a directory that
    would otherwise live on the PVC forever.
    """
    paths = DataPaths(tmp_path / "data")
    paths.ensure()
    fresh = paths.model_revision_dir("keychain", "a" * 40)
    stale = paths.model_revision_dir("keychain", "b" * 40)
    for directory in (fresh, stale):
        directory.mkdir(parents=True)
        (directory / "model.scad").write_text("cube(1);\n", encoding="utf-8")
    os.utime(stale, (0, 0))

    removed = prune_revision_exports(paths, ttl=3600.0)

    assert removed == [f"keychain/{'b' * 40}"]
    assert fresh.is_dir()
    assert not stale.exists()


def test_pruning_leaves_an_absent_cache_alone(tmp_path: Path) -> None:
    assert prune_revision_exports(DataPaths(tmp_path / "nothing"), ttl=1.0) == []


def test_a_built_in_that_cannot_be_replaced_keeps_its_mirror_and_the_rest_sync(
    catalogue: Catalogue, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """One built-in's failed swap is logged and skipped; it never stops the sync (or the boot)."""
    image = tmp_path / "image"
    _bundle(image, "keychain", "cube(10);\n")
    _bundle(image, "tag", "cube(5);\n")
    catalogue.sync_builtins(image)
    mirror = catalogue.paths.models / "_builtin"
    (image / "keychain" / "model.scad").write_text("cube(11);\n", encoding="utf-8")
    (image / "tag" / "model.scad").write_text("cube(6);\n", encoding="utf-8")
    real_replace = os.replace

    def fail_to_retire_tag(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        if Path(source) == mirror / "tag":
            raise PermissionError("EACCES")
        real_replace(source, target)

    with patch("scadbuddy.library.catalogue.os.replace", fail_to_retire_tag):
        assert catalogue.sync_builtins(image) is not None

    assert (mirror / "keychain" / "model.scad").read_text(encoding="utf-8") == "cube(11);\n"
    assert (mirror / "tag" / "model.scad").read_text(encoding="utf-8") == "cube(5);\n"
    assert [getattr(record, "slug", None) for record in caplog.records if record.exc_info] == [
        "tag"
    ]
    # The staged copy went to the tombstones, not into the mirror, and is cleared.
    assert sorted(path.name for path in mirror.iterdir()) == ["keychain", "tag"]
    assert list(catalogue.paths.tombstones.iterdir()) == []
    # The next boot retries it.
    assert catalogue.sync_builtins(image) is not None
    assert (mirror / "tag" / "model.scad").read_text(encoding="utf-8") == "cube(6);\n"


def test_a_failed_swap_puts_the_previous_mirror_back(
    catalogue: Catalogue, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    image = tmp_path / "image"
    _bundle(image, "tag", "cube(5);\n")
    catalogue.sync_builtins(image)
    mirror = catalogue.paths.models / "_builtin"
    (image / "tag" / "model.scad").write_text("cube(6);\n", encoding="utf-8")
    real_replace = os.replace

    def fail_to_install(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        if Path(target) == mirror / "tag" and not Path(source).name.endswith(".old"):
            raise OSError("EIO")
        real_replace(source, target)

    with patch("scadbuddy.library.catalogue.os.replace", fail_to_install):
        assert catalogue.sync_builtins(image) is None

    assert (mirror / "tag" / "model.scad").read_text(encoding="utf-8") == "cube(5);\n"
    assert "could not sync a built-in template" in caplog.text


def test_a_mirror_that_cannot_be_made_does_not_raise(
    catalogue: Catalogue, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    image = tmp_path / "image"
    _bundle(image, "tag", "cube(5);\n")
    catalogue.paths.builtins.write_text("not a directory\n", encoding="utf-8")

    assert catalogue.sync_builtins(image) is None
    assert "could not sync built-in templates" in caplog.text


KEYCHAIN = 'label = "hi";\nfont = "Lobster Two";\nheight = 2;\nsize = 10;\ncube(size);\n'


def _seeded(catalogue: Catalogue, slug: str, source: str, message: str | None = None) -> str:
    """A model as the pre-#155 seed left it: copied in, committed as ``Seed … from the image``."""
    assert catalogue.history is not None
    directory = catalogue.paths.model_dir(slug)
    directory.mkdir(parents=True)
    (directory / "model.scad").write_text(source, encoding="utf-8")
    (directory / "model.json").write_text(json.dumps({"name": slug}), encoding="utf-8")
    commit = catalogue.history.commit(message or f"Seed {slug} from the image", slug)
    assert commit is not None
    return commit


def _merge(catalogue: Catalogue, slug: str) -> str:
    """Three-way merge the built-in into a linked template, as an upstream update does."""
    assert catalogue.history is not None
    upstream = catalogue.record(slug).upstream
    assert upstream is not None and upstream.base is not None
    base = catalogue.paths.cache / "merge-base.scad"
    base.write_bytes(catalogue.history.show(upstream.base, f"{upstream.path}/model.scad"))
    ours = catalogue.paths.model_source(slug)
    theirs = catalogue.paths.model_source(upstream.id)
    merged = subprocess.run(
        ["git", "merge-file", "-p", str(ours), str(base), str(theirs)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert merged.returncode == 0, merged.stdout
    return merged.stdout


def test_a_seeded_template_becomes_a_duplicate_of_its_built_in(
    catalogue: Catalogue, tmp_path: Path
) -> None:
    assert catalogue.history is not None
    seed = _seeded(catalogue, "name-keychain", KEYCHAIN)
    image = tmp_path / "image"
    _bundle(image, "name-keychain", KEYCHAIN.replace("size = 10", "size = 12"))
    catalogue.sync_builtins(image)

    assert catalogue.link_seeded() is not None

    record = catalogue.record("name-keychain")
    assert record.upstream is not None
    assert record.upstream.model_dump() == {
        "id": "builtin:name-keychain",
        "path": "name-keychain",
        "base": seed,
        "dismissed": None,
    }
    assert record.origin == "mine"
    assert catalogue.history.log()[0].message == "Link seeded templates to their built-ins"
    # Nothing renamed: the slug, its source and the seeded revision all stand.
    assert sorted(entry.slug for entry in catalogue.list_models()) == [
        "builtin:name-keychain",
        "name-keychain",
    ]
    assert catalogue.paths.model_source("name-keychain").read_text(encoding="utf-8") == KEYCHAIN
    assert catalogue.history.resolve(seed) == seed
    # Unedited: merges clean to the current built-in.
    assert _merge(catalogue, "name-keychain") == KEYCHAIN.replace("size = 10", "size = 12")


def test_a_seeded_templates_update_diffs_from_where_the_source_was_seeded(
    catalogue: Catalogue, tmp_path: Path
) -> None:
    """#236: base is the seed commit at ``<slug>``, the built-in lives at
    ``_builtin/<slug>``; the preview's patch is the change, not the whole file added."""
    _seeded(catalogue, "name-keychain", KEYCHAIN)
    image = tmp_path / "image"
    _bundle(image, "name-keychain", KEYCHAIN.replace("size = 10", "size = 12"))
    catalogue.sync_builtins(image)
    catalogue.link_seeded()

    status = catalogue.upstream_status("name-keychain")

    assert status.upstream.path == "name-keychain"
    assert status.preview is not None
    patch = status.preview.patch
    assert "--- a/name-keychain/model.scad\n+++ b/name-keychain/model.scad\n" in patch
    assert [
        line for line in patch.splitlines() if line[:1] in "+-" and line[:3] not in {"---", "+++"}
    ] == [
        "-size = 10;",
        "+size = 12;",
    ]
    assert "new file" not in patch
    assert "model.json" not in patch


def test_an_edited_seeded_template_keeps_its_edits_through_the_merge(
    catalogue: Catalogue, tmp_path: Path
) -> None:
    _seeded(catalogue, "name-keychain", KEYCHAIN)
    catalogue.write_source("name-keychain", KEYCHAIN.replace('"hi"', '"mine"'))
    image = tmp_path / "image"
    _bundle(image, "name-keychain", KEYCHAIN.replace("size = 10", "size = 12"))
    catalogue.sync_builtins(image)

    catalogue.link_seeded()

    assert _merge(catalogue, "name-keychain") == (
        KEYCHAIN.replace('"hi"', '"mine"').replace("size = 10", "size = 12")
    )


def test_linking_is_idempotent(catalogue: Catalogue, tmp_path: Path) -> None:
    assert catalogue.history is not None
    _seeded(catalogue, "name-keychain", KEYCHAIN, "Seed name-keychain and tag from the image")
    image = tmp_path / "image"
    _bundle(image, "name-keychain", KEYCHAIN)
    catalogue.sync_builtins(image)
    assert catalogue.link_seeded() is not None
    head = catalogue.history.head()
    meta = catalogue.paths.model_meta("name-keychain").read_bytes()

    assert catalogue.link_seeded() is None

    assert catalogue.history.head() == head
    assert catalogue.paths.model_meta("name-keychain").read_bytes() == meta


def test_a_template_with_no_seed_commit_is_left_alone(
    catalogue: Catalogue, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    assert catalogue.history is not None
    # Uploaded under a built-in's slug; and seeded, deleted, then uploaded again.
    catalogue.create("tag", "cube(1);\n", ModelMeta(name="Mine"))
    _seeded(catalogue, "name-keychain", KEYCHAIN)
    catalogue.delete("name-keychain")
    catalogue.create("name-keychain", "cube(2);\n", ModelMeta(name="Mine too"))
    image = tmp_path / "image"
    _bundle(image, "tag", "cube(5);\n")
    _bundle(image, "name-keychain", KEYCHAIN)
    catalogue.sync_builtins(image)
    head = catalogue.history.head()

    with caplog.at_level("INFO"):
        assert catalogue.link_seeded() is None

    assert catalogue.record("tag").upstream is None
    assert catalogue.record("name-keychain").upstream is None
    assert catalogue.history.head() == head
    skipped = [
        getattr(record, "slug", None) for record in caplog.records if "not seeded" in record.msg
    ]
    assert sorted(slug for slug in skipped if slug) == ["name-keychain", "tag"]


def test_a_template_that_already_has_an_upstream_is_left_alone(
    catalogue: Catalogue, tmp_path: Path
) -> None:
    assert catalogue.history is not None
    _seeded(catalogue, "name-keychain", KEYCHAIN)
    image = tmp_path / "image"
    _bundle(image, "name-keychain", KEYCHAIN)
    _bundle(image, "tag", "cube(5);\n")
    catalogue.sync_builtins(image)
    raw = catalogue.read_raw_meta("name-keychain")
    raw["upstream"] = {"id": "builtin:tag", "path": "_builtin/tag", "base": None, "dismissed": None}
    catalogue.write_raw_meta("name-keychain", raw)
    catalogue.history.commit("Point it elsewhere", "name-keychain")
    head = catalogue.history.head()

    assert catalogue.link_seeded() is None

    upstream = catalogue.record("name-keychain").upstream
    assert upstream is not None and upstream.id == "builtin:tag"
    assert catalogue.history.head() == head


def test_one_template_that_cannot_be_linked_does_not_stop_the_rest(
    catalogue: Catalogue, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _seeded(catalogue, "name-keychain", KEYCHAIN)
    _seeded(catalogue, "tag", "cube(5);\n")
    image = tmp_path / "image"
    _bundle(image, "name-keychain", KEYCHAIN)
    _bundle(image, "tag", "cube(5);\n")
    catalogue.sync_builtins(image)
    catalogue.paths.model_meta("tag").write_text("{not json", encoding="utf-8")

    assert catalogue.link_seeded() is not None

    assert catalogue.record("name-keychain").upstream is not None
    assert [getattr(record, "slug", None) for record in caplog.records if record.exc_info] == [
        "tag"
    ]


def test_a_freshly_linked_seeded_template_has_no_update_until_its_built_in_changes(
    catalogue: Catalogue, tmp_path: Path
) -> None:
    assert catalogue.history is not None
    _seeded(catalogue, "name-keychain", KEYCHAIN)
    image = tmp_path / "image"
    _bundle(image, "name-keychain", KEYCHAIN)
    catalogue.sync_builtins(image)

    catalogue.link_seeded()

    status = catalogue.upstream_status("name-keychain")
    assert status.state == "current"
    assert status.upstream.path == "_builtin/name-keychain"
    assert status.upstream.base == status.revision
    assert _merge(catalogue, "name-keychain") == KEYCHAIN


def test_a_linked_seeded_template_takes_a_built_in_update(
    catalogue: Catalogue, tmp_path: Path
) -> None:
    _seeded(catalogue, "name-keychain", KEYCHAIN)
    catalogue.write_source("name-keychain", KEYCHAIN.replace('"hi"', '"mine"'))
    image = tmp_path / "image"
    _bundle(image, "name-keychain", KEYCHAIN)
    catalogue.sync_builtins(image)
    catalogue.link_seeded()
    _bundle(image, "name-keychain", KEYCHAIN.replace("size = 10", "size = 12"))
    catalogue.sync_builtins(image)

    status = catalogue.upstream_status("name-keychain")
    assert status.state == "update"
    assert status.preview is not None and status.preview.clean

    record, plan = catalogue.merge_upstream("name-keychain")

    assert plan.conflicts == 0
    assert catalogue.paths.model_source("name-keychain").read_text(encoding="utf-8") == (
        KEYCHAIN.replace('"hi"', '"mine"').replace("size = 10", "size = 12")
    )
    assert record.upstream is not None
    assert record.upstream.path == "_builtin/name-keychain"
    assert record.upstream.base == status.revision
    assert catalogue.upstream_status("name-keychain").state == "current"


def test_a_crlf_template_takes_a_built_in_update_and_keeps_its_line_endings(
    catalogue: Catalogue, tmp_path: Path
) -> None:
    crlf = KEYCHAIN.replace("\n", "\r\n")
    _seeded(catalogue, "name-keychain", crlf)
    catalogue.write_source("name-keychain", crlf.replace('"hi"', '"mine"'))
    image = tmp_path / "image"
    _bundle(image, "name-keychain", crlf)
    catalogue.sync_builtins(image)
    catalogue.link_seeded()
    _bundle(image, "name-keychain", crlf.replace("size = 10", "size = 12"))
    catalogue.sync_builtins(image)

    status = catalogue.upstream_status("name-keychain")
    assert status.preview is not None and status.preview.clean

    _, plan = catalogue.merge_upstream("name-keychain")

    assert plan.conflicts == 0
    assert catalogue.paths.model_source("name-keychain").read_bytes() == (
        crlf.replace('"hi"', '"mine"').replace("size = 10", "size = 12").encode()
    )
