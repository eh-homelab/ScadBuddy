"""The models repository shared by API replicas over NFS (#2384, #2385).

Each "replica" here is its own `ModelHistory` (and `Catalogue`) on one directory, so
none shares another's in-process lock. NFS's stale reads are simulated where a test
needs them: a branch ref or an index showing what it held before another replica's
commit, as this replica's NFS client would serve them from its cache.
"""

from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from scadbuddy.core.paths import DataPaths
from scadbuddy.library import history as history_module
from scadbuddy.library.catalogue import Catalogue, ModelMeta, StaleVersionError
from scadbuddy.library.history import GitError, ModelHistory
from scadbuddy.library.repo_lock import PgRepoCoordinator, RepoLockError
from scadbuddy.render.pg_store import migrate
from scadbuddy.render.solids import WRAPPER_PREFIX
from tests.support.media import MemoryMediaStore

pytestmark = pytest.mark.requires_git


@pytest.fixture
def paths(tmp_path: Path) -> DataPaths:
    data = DataPaths(root=tmp_path / "data")
    data.models.mkdir(parents=True)
    return data


@pytest.fixture
def coordinated(pg_conninfo: str) -> PgRepoCoordinator:
    import psycopg

    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        migrate(conn)
    return PgRepoCoordinator(pg_conninfo)


def replica(paths: DataPaths, coordinator: PgRepoCoordinator | None = None) -> Catalogue:
    history = ModelHistory(
        paths.models, wrapper_prefix=WRAPPER_PREFIX, timeout=10.0, coordinator=coordinator
    )
    return Catalogue(paths, history, media_store=MemoryMediaStore(), wrapper_prefix=WRAPPER_PREFIX)


def history_of(catalogue: Catalogue) -> ModelHistory:
    assert catalogue.history is not None
    return catalogue.history


def git(models: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=models, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.mark.requires_postgres
def test_two_replicas_commit_in_turn_when_their_flock_does_not_hold(
    paths: DataPaths, coordinated: PgRepoCoordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The flock is all that serialised replicas before, and nothing proves it holds
    # across NFS clients; the Postgres lock has to on its own.
    monkeypatch.setattr(fcntl, "flock", lambda *_args: None)
    first, second = replica(paths, coordinated), replica(paths, coordinated)
    assert history_of(first).ensure_repo() is not None
    errors: list[BaseException] = []

    def write(catalogue: Catalogue, prefix: str) -> None:
        try:
            for index in range(6):
                catalogue.create(f"{prefix}-{index}", "cube(1);\n", ModelMeta(name=prefix))
        except BaseException as error:
            errors.append(error)

    threads = [
        threading.Thread(target=write, args=(first, "a")),
        threading.Thread(target=write, args=(second, "b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    tracked = set(git(paths.models, "ls-tree", "--name-only", "HEAD").splitlines())
    assert {f"{p}-{i}" for p in "ab" for i in range(6)} <= tracked
    # One linear history: 12 creates on the initial revision, each with one parent.
    assert git(paths.models, "rev-list", "--count", "HEAD") == "13"
    assert git(paths.models, "rev-list", "--min-parents=2", "HEAD") == ""


@pytest.mark.requires_postgres
def test_a_replica_with_a_stale_head_waits_for_the_recorded_one_and_its_stale_base_conflicts(
    paths: DataPaths, coordinated: PgRepoCoordinator
) -> None:
    writer, stale = replica(paths, coordinated), replica(paths, coordinated)
    history_of(writer).ensure_repo()
    base = writer.create("keychain", "cube(10);\n", ModelMeta(name="Keychain")).version
    assert base is not None
    writer.write_source("keychain", "cube(20);\n", expected_version=base)
    moved = history_of(writer).head()
    assert moved is not None

    # This replica's view of the branch is from before the edit, until its cache
    # expires -- shortened here from NFS's 60 s.
    ref = paths.models / ".git" / "refs" / "heads" / "main"
    ref.write_text(f"{base}\n")
    expiry = threading.Timer(0.5, lambda: ref.write_text(f"{moved}\n"))
    expiry.start()
    try:
        with pytest.raises(StaleVersionError) as refused:
            stale.write_source("keychain", "cube(30);\n", expected_version=base)
    finally:
        expiry.join()

    assert refused.value.current == moved
    assert history_of(writer).head() == moved
    assert paths.model_source("keychain").read_text() == "cube(20);\n"


def test_a_stale_index_does_not_undo_another_replicas_commit(paths: DataPaths) -> None:
    first, second = replica(paths), replica(paths)
    history_of(first).ensure_repo()
    first.create("keychain", "cube(10);\n", ModelMeta(name="Keychain"))
    index = paths.models / ".git" / "index"
    before = index.read_bytes()
    first.write_source("keychain", "cube(20);\n")
    # The second replica reads the index as it was before that edit.
    index.write_bytes(before)

    second.create("tag", "cube(5);\n", ModelMeta(name="Tag"))

    assert git(paths.models, "show", "HEAD:keychain/model.scad") == "cube(20);"
    changed = git(paths.models, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD")
    assert changed.split() == ["tag/model.json", "tag/model.scad"]


def _failing_commits(
    history: ModelHistory, times: int, stderr: str
) -> Callable[..., subprocess.CompletedProcess[Any]]:
    run = history._run
    failures = {"left": times}

    def fake(*args: str, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        if args and args[0] == "commit" and failures["left"] > 0:
            failures["left"] -= 1
            raise GitError(f"git commit failed: {stderr}", stderr)
        return run(*args, **kwargs)

    return fake


def test_a_commit_git_could_not_lock_head_for_is_tried_again(
    paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalogue = replica(paths)
    history = history_of(catalogue)
    history.ensure_repo()
    parent = history.head()
    monkeypatch.setattr(history_module, "COMMIT_RETRY_SECONDS", 0.01)
    stale = "fatal: cannot lock ref 'HEAD': is at e35f9c4 but expected c31e166"
    monkeypatch.setattr(history, "_run", _failing_commits(history, 2, stale))

    record = catalogue.create("keychain", "cube(10);\n", ModelMeta(name="Keychain"))

    assert record.version is not None
    assert record.version == history.head()
    assert history.parent(record.version) == parent


def test_a_based_edit_whose_head_moved_under_the_lock_is_checked_again_and_refused(
    paths: DataPaths, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    catalogue = replica(paths)
    history = history_of(catalogue)
    history.ensure_repo()
    base = catalogue.create("keychain", "cube(10);\n", ModelMeta(name="Keychain")).version
    assert base is not None
    monkeypatch.setattr(history_module, "COMMIT_RETRY_SECONDS", 0.01)
    run = history._run
    moved = {"done": False}

    def commit_elsewhere(*args: str, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        if args and args[0] == "commit" and not moved["done"]:
            moved["done"] = True
            # Something commits to the model without the lock, through an index of
            # its own, and git then cannot lock HEAD for this commit.
            env = {**os.environ, "GIT_INDEX_FILE": str(tmp_path / "elsewhere-index")}
            (paths.model_dir("keychain") / "notes.txt").write_text("by hand\n")
            for step in (
                ["read-tree", "HEAD"],
                ["add", "keychain/notes.txt"],
                ["-c", "user.name=x", "-c", "user.email=x@x", "commit", "-qm", "by hand"],
            ):
                subprocess.run(["git", *step], cwd=paths.models, env=env, check=True)
            stderr = "fatal: cannot lock ref 'HEAD': is at 1111111 but expected 2222222"
            raise GitError(f"git commit failed: {stderr}", stderr)
        return run(*args, **kwargs)

    monkeypatch.setattr(history, "_run", commit_elsewhere)
    with pytest.raises(StaleVersionError):
        catalogue.write_source("keychain", "cube(20);\n", expected_version=base)

    assert paths.model_source("keychain").read_text() == "cube(10);\n"
    assert history.log("keychain", limit=1)[0].message == "by hand"


def test_a_create_whose_commit_never_lands_fails_and_leaves_nothing(
    paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalogue = replica(paths)
    history = history_of(catalogue)
    history.ensure_repo()
    head = history.head()
    monkeypatch.setattr(history_module, "COMMIT_RETRY_SECONDS", 0.01)
    stale = "fatal: cannot lock ref 'HEAD': is at e35f9c4 but expected c31e166"
    monkeypatch.setattr(history, "_run", _failing_commits(history, 99, stale))

    with pytest.raises(GitError):
        catalogue.create("keychain", "cube(10);\n", ModelMeta(name="Keychain"))

    assert not paths.model_dir("keychain").exists()
    assert history.head() == head


class _Unrecordable:
    """A cross-replica lock whose database goes away between commit and record."""

    expected: str | None = None

    def record(self, commit: str) -> None:
        raise RepoLockError(f"could not record {commit[:7]} as the models HEAD")


class _LosesPostgres:
    @contextmanager
    def hold(self, timeout: float) -> Iterator[_Unrecordable]:
        yield _Unrecordable()


def test_a_commit_that_landed_is_made_even_when_its_head_cannot_be_recorded(
    paths: DataPaths,
) -> None:
    # Review of #2492: failing it would roll the change back on disk under a history
    # that keeps it, and tell the caller it failed.
    history = ModelHistory(
        paths.models, wrapper_prefix=WRAPPER_PREFIX, coordinator=_LosesPostgres()
    )
    catalogue = Catalogue(
        paths, history, media_store=MemoryMediaStore(), wrapper_prefix=WRAPPER_PREFIX
    )
    history.ensure_repo()

    record = catalogue.create("keychain", "cube(10);\n", ModelMeta(name="Keychain"))
    catalogue.write_source("keychain", "cube(20);\n", expected_version=record.version)

    assert history.log("keychain", limit=1)[0].message == "Edit keychain source"
    assert paths.model_source("keychain").read_text() == "cube(20);\n"
    assert history.clean("keychain")


def test_a_commit_git_reports_but_head_does_not_show_is_an_error(
    paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalogue = replica(paths)
    history = history_of(catalogue)
    history.ensure_repo()
    run = history._run

    def silent(*args: str, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        if args and args[0] == "commit":
            return subprocess.CompletedProcess(list(args), 0, "", "")
        return run(*args, **kwargs)

    monkeypatch.setattr(history, "_run", silent)
    with pytest.raises(GitError, match="not a new commit"):
        catalogue.create("keychain", "cube(10);\n", ModelMeta(name="Keychain"))
    assert not paths.model_dir("keychain").exists()


def test_a_model_another_replica_just_made_is_found_once_its_directory_is_revalidated(
    paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalogue = replica(paths)
    history_of(catalogue).ensure_repo()
    catalogue.create("keychain", "cube(10);\n", ModelMeta(name="Keychain"))
    source = paths.model_source("keychain")
    # This replica's NFS client cached "no such entry" under `models/`: the lookup
    # answers from it until `models/` is opened, and the client revalidates it.
    revalidated: list[Path] = []
    real_open = os.open
    real_is_file = Path.is_file

    def opening(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        revalidated.append(Path(path))
        return real_open(path, flags, *args, **kwargs)

    def cached_is_file(self: Path) -> bool:
        if self == source and paths.models not in revalidated:
            return False
        return real_is_file(self)

    monkeypatch.setattr(os, "open", opening)
    monkeypatch.setattr(Path, "is_file", cached_is_file)

    assert catalogue.exists("keychain")
    assert catalogue.record("keychain").slug == "keychain"


def test_a_model_that_is_not_there_is_still_not_found(paths: DataPaths) -> None:
    catalogue = replica(paths)
    assert not catalogue.exists("keychain")
    shutil.rmtree(paths.models)
    assert not catalogue.exists("keychain")
