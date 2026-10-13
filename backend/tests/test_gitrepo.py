"""The shared git core (#2503, plan 2026-10-11-flow-grants-and-git.md Task C1), on a bare
`GitRepo`: what model history and the flows repository both stand on."""

from __future__ import annotations

import fcntl
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scadbuddy.core.authorship import AGENT_AUTHOR_NAME, AgentAuthor, authored_as
from scadbuddy.core.gitrepo import (
    AUTHOR_NAME,
    LOCK_NAME,
    GitRepo,
    GitTimeoutError,
    RevisionNotFoundError,
)

pytestmark = pytest.mark.requires_git


@pytest.fixture
def repo(tmp_path: Path) -> GitRepo:
    made = GitRepo(tmp_path / "repo")
    made.ensure_repo()
    return made


def write(repo: GitRepo, path: str, text: str) -> None:
    (repo.root / path).parent.mkdir(parents=True, exist_ok=True)
    (repo.root / path).write_text(text)


def test_a_commit_carries_the_authors_trailers_then_its_own(repo: GitRepo) -> None:
    write(repo, "a/f.txt", "one\n")
    with authored_as(AgentAuthor(principal="token:p1", session="s1")):
        commit = repo.commit("Add a", "a", trailers=["X-Thing: 1"])
    assert commit is not None and commit == repo.head()
    [revision] = repo.log("a")
    assert (revision.author, revision.message) == (AGENT_AUTHOR_NAME, "Add a")
    assert revision.agent == AgentAuthor(principal="token:p1", session="s1")
    body = repo._out("log", "-1", "--format=%(trailers:only)")
    assert body.split("\n")[:3] == [
        "ScadBuddy-Agent-Principal: token:p1",
        "ScadBuddy-Agent-Session: s1",
        "X-Thing: 1",
    ]


def test_an_unchanged_commit_is_none_unless_empty_is_allowed(repo: GitRepo) -> None:
    write(repo, "a/f.txt", "one\n")
    first = repo.commit("Add a", "a")
    assert repo.commit("Again", "a") is None
    again = repo.commit("Again", "a", allow_empty=True)
    assert again is not None and repo.parent(again) == first


def test_a_date_is_the_author_and_committer_date(repo: GitRepo) -> None:
    when = datetime(2026, 10, 10, 1, 2, 3, tzinfo=UTC)
    write(repo, "a/f.txt", "one\n")
    repo.commit("Add a", "a", date=when)
    [revision] = repo.log("a")
    assert revision.date == when
    # Seconds, not `%cI`, whose UTC spelling (`Z` or `+00:00`) differs between git versions.
    assert repo._out("log", "-1", "--format=%ct").strip() == str(int(when.timestamp()))
    assert revision.author == AUTHOR_NAME


def test_show_diff_and_restore_as_a_new_commit(repo: GitRepo) -> None:
    write(repo, "a/f.txt", "one\n")
    first = repo.commit("one", "a")
    write(repo, "a/f.txt", "two\n")
    write(repo, "a/g.txt", "new\n")
    second = repo.commit("two", "a")
    assert first is not None and second is not None
    assert repo.show(first, "a/f.txt") == b"one\n"
    with pytest.raises(RevisionNotFoundError):
        repo.show(first, "a/g.txt")
    patch = repo.diff(repo.revision_range(first, second), "a", relative="a/")
    assert "--- a/f.txt" in patch and "+two" in patch

    restored = repo.restore("a", first)
    assert restored not in (first, second) and repo.parent(restored) == second
    assert (repo.root / "a/f.txt").read_text() == "one\n"
    assert not (repo.root / "a/g.txt").exists()
    # The same content again: no commit, unless one is wanted.
    assert repo.restore("a", first) == restored
    forced = repo.restore("a", first, "Again", allow_empty=True)
    assert repo.parent(forced) == restored


def test_the_write_lock_wait_is_bounded(repo: GitRepo) -> None:
    repo.timeout = 0.2
    with (repo.root / LOCK_NAME).open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        write(repo, "a/f.txt", "one\n")
        with pytest.raises(GitTimeoutError):
            repo.commit("Add a", "a")
        fcntl.flock(held, fcntl.LOCK_UN)
