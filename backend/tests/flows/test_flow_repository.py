"""`FlowHistory` (#2503, plan 2026-10-11-flow-grants-and-git.md Task C2): `data/flows`,
one commit per version, numbered by its position among its flow's commits."""

from __future__ import annotations

import subprocess
import threading
import uuid
from pathlib import Path

import pytest

from scadbuddy.core.authorship import AGENT_AUTHOR_NAME, AgentAuthor, authored_as
from scadbuddy.core.gitrepo import LOCK_NAME, GitError, RevisionNotFoundError, git_env
from scadbuddy.flows.repository import FlowHistory

pytestmark = pytest.mark.requires_git

ONE = "import asyncio\nasync def main():\n    return 1\nasyncio.run(main())\n"
TWO = ONE.replace("return 1", "return 2")


@pytest.fixture
def history(tmp_path: Path) -> FlowHistory:
    made = FlowHistory(tmp_path / "data" / "flows")
    made.ensure_repo()
    return made


def register(history: FlowHistory, name: str, script: str) -> tuple[str, str, int]:
    version_id = str(uuid.uuid4())
    indexed: list[tuple[str, int]] = []
    commit, version = history.register(
        name, version_id, lambda c, v: indexed.append((c, v)), script=script
    )
    assert indexed == [(commit, version)]
    return version_id, commit, version


def test_a_registration_is_one_commit_with_its_author_and_flow_trailers(
    history: FlowHistory,
) -> None:
    with authored_as(AgentAuthor(principal="token:p1", session="s1")):
        version_id, commit, version = register(history, "swap", ONE)
    assert version == 1 and commit == history.head()
    assert (history.root / "swap" / "flow.py").read_text() == ONE
    [revision] = history.log("swap")
    assert (revision.author, revision.message) == (AGENT_AUTHOR_NAME, "Register swap v1")
    trailers = history._out("log", "-1", "--format=%(trailers:only)")
    assert f"ScadBuddy-Flow: swap\nScadBuddy-Flow-Version: {version_id}" in trailers
    assert "ScadBuddy-Agent-Principal: token:p1" in trailers
    assert history.commit_for(version_id) == commit
    assert history.script_at("swap", commit) == ONE
    # Its lock is its own, in data/flows.
    assert (history.root / LOCK_NAME).exists()


def test_an_unchanged_script_is_still_a_new_commit_and_version(history: FlowHistory) -> None:
    _, first, _ = register(history, "swap", ONE)
    register(history, "other", TWO)
    _, again, version = register(history, "swap", ONE)
    assert again != first and version == 2
    assert [v.version for v in history.versions("swap")] == [2, 1]
    assert history.version_of("swap", again) == 2
    assert history.version_of("swap", first[:7]) == 1
    with pytest.raises(RevisionNotFoundError):
        history.version_of("other", first)


def test_restore_to_the_heads_own_content_is_a_new_commit(history: FlowHistory) -> None:
    _, first, _ = register(history, "swap", ONE)
    register(history, "swap", TWO)
    version_id = str(uuid.uuid4())
    restored, version = history.register("swap", version_id, lambda c, v: None, restore=first)
    assert version == 3 and history.script_at("swap", restored) == ONE
    assert history.log("swap", limit=1)[0].message == f"Restore swap to {first[:7]}"
    again, version = history.register("swap", str(uuid.uuid4()), lambda c, v: None, restore=first)
    assert version == 4 and again != restored
    assert history.commit_for(version_id) == restored


def test_a_diff_names_flow_py_and_defaults_to_the_version_before(history: FlowHistory) -> None:
    _, first, _ = register(history, "swap", ONE)
    register(history, "other", ONE)
    _, second, _ = register(history, "swap", TWO)
    base, head, patch = history.diff_flow("swap", None, second)
    assert (base, head) == (first, second)
    assert "--- a/flow.py" in patch and "+    return 2" in patch
    _, _, back = history.diff_flow("swap", second, first)
    assert "+    return 1" in back


def test_concurrent_registrations_each_get_their_own_commits_position(
    history: FlowHistory,
) -> None:
    results: list[tuple[str, int]] = []
    errors: list[BaseException] = []

    def one(n: int) -> None:
        try:
            _, commit, version = register(history, "swap", ONE.replace("1", str(n)))
            results.append((commit, version))
        except BaseException as error:  # pragma: no cover - reported below
            errors.append(error)

    threads = [threading.Thread(target=one, args=(n,)) for n in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert sorted(v for _, v in results) == [1, 2, 3, 4, 5, 6]
    for commit, version in results:
        assert history.version_of("swap", commit) == version


def test_a_failed_log_is_an_error_not_an_empty_history(
    history: FlowHistory, monkeypatch: pytest.MonkeyPatch
) -> None:
    register(history, "swap", ONE)
    run = history._run

    def failing(*args: str, **kwargs: object) -> object:
        if args[0] == "log":
            args = ("log", "--no-such-option")
        return run(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(history, "_run", failing)
    with pytest.raises(GitError):
        register(history, "swap", TWO)


def test_a_repository_with_no_commit_has_no_versions(tmp_path: Path) -> None:
    root = tmp_path / "flows"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], env=git_env(), check=True)
    assert FlowHistory(root).commits() == []
