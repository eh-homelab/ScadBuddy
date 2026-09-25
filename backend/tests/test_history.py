"""The git store under ``data/models``, exercised against a real repository."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
from pathlib import Path

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
    summarise,
)
from scadbuddy.render.jobs import prune_revision_exports
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
