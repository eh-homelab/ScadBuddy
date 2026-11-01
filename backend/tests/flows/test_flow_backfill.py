"""Flow versions over git and Postgres (#2503, plan 2026-10-11-flow-grants-and-git.md
Tasks C3 and C5): registration under one lock, a failed insert and the reconciler."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from scadbuddy.core.authorship import AgentAuthor, authored_as
from scadbuddy.core.gitrepo import GitError, git_env
from scadbuddy.flows.definitions import FlowDefinitions, ScriptMissingError, UnknownFlowError
from scadbuddy.flows.repository import FlowHistory
from scadbuddy.flows.states import FlowStates
from scadbuddy.flows.store import FlowStore
from scadbuddy.render.projection import JobProjection

pytestmark = [pytest.mark.requires_postgres, pytest.mark.requires_git]

ONE = "import asyncio\nasync def main():\n    return 1\nasyncio.run(main())\n"
TWO = ONE.replace("return 1", "return 2")
BROWSER = {"kind": "browser"}
AGENT = {"kind": "agent", "principal": "token:p1", "session": "s1"}


@pytest.fixture
def store(jobs: JobProjection) -> FlowStore:
    return FlowStore(jobs.pool)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "data" / "flows"


@pytest.fixture
def definitions(root: Path, store: FlowStore, jobs: JobProjection) -> FlowDefinitions:
    return FlowDefinitions(FlowHistory(root), store, FlowStates(jobs.pool))


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", f"safe.directory={root}", *args],
        cwd=root,
        env=git_env(),
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def rows(store: FlowStore) -> list[tuple[str, int, str | None]]:
    with store._pool.connection() as conn:
        found = conn.execute(
            "SELECT name, version, commit FROM workflow_definitions ORDER BY name, version"
        ).fetchall()
    return [(r["name"], r["version"], r["commit"]) for r in found]


async def test_register_commits_then_indexes_and_an_unchanged_script_is_a_new_version(
    definitions: FlowDefinitions, store: FlowStore, root: Path
) -> None:
    await definitions.start()
    one = await definitions.register("swap", ONE, BROWSER, approval_timeout_s=None)
    # Only the approval timeout changed: still a version of its own, never a
    # `UNIQUE (name, version)` failure.
    two = await definitions.register("swap", ONE, BROWSER, approval_timeout_s=0)
    assert (one.version, two.version, two.approval_timeout_s) == (1, 2, 0)
    assert one.commit != two.commit and two.commit == git(root, "rev-parse", "HEAD").strip()
    assert rows(store) == [("swap", 1, one.commit), ("swap", 2, two.commit)]
    got = await definitions.get(one.id)
    assert got is not None and got.script == ONE

    listed = await definitions.versions("swap", limit=10)
    assert [(v.version, v.id, v.current) for v in listed] == [
        (2, two.id, True),
        (1, one.id, False),
    ]
    with pytest.raises(UnknownFlowError):
        await definitions.versions("nothing", limit=10)


async def test_the_script_is_read_from_git_and_a_missing_commit_is_refused(
    definitions: FlowDefinitions, store: FlowStore
) -> None:
    await definitions.start()
    made = await definitions.register("swap", ONE, BROWSER, approval_timeout_s=None)
    got = await definitions.get(made.id)
    assert got is not None and got.script == ONE
    with store._pool.connection() as conn:
        conn.execute("UPDATE workflow_definitions SET commit = %s", ("f" * 40,))
    with pytest.raises(ScriptMissingError):
        await definitions.get(made.id)


async def test_restore_is_a_new_version_with_that_versions_script(
    definitions: FlowDefinitions,
) -> None:
    await definitions.start()
    one = await definitions.register("swap", ONE, BROWSER, approval_timeout_s=30)
    two = await definitions.register("swap", TWO, BROWSER, approval_timeout_s=None)
    assert one.commit is not None and two.commit is not None
    restored = await definitions.restore("swap", one.commit[:7], AGENT)
    assert (restored.version, restored.script, restored.approval_timeout_s) == (3, ONE, 30)
    assert restored.created_by == AGENT
    diff = await definitions.diff("swap", two.commit, None)
    assert (diff.base, diff.head) == (one.commit, two.commit)
    assert await definitions.source("swap", one.commit) == ONE


async def test_a_failed_insert_leaves_its_commit_which_keeps_its_position(
    definitions: FlowDefinitions, store: FlowStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec §6.2 "A failed insert": the next registration commits after it, and the
    reconciler indexes it at its own number, so nothing collides."""
    await definitions.start()
    await definitions.register("swap", ONE, BROWSER, approval_timeout_s=None)
    insert = store.insert_version

    def failing(*args: Any, **kwargs: Any) -> Any:
        raise GitError("the database went away")

    monkeypatch.setattr(store, "insert_version", failing)
    # As a request from the agent is: its author on the commit, its record on the row.
    with authored_as(AgentAuthor(principal="token:p1", session="s1")), pytest.raises(GitError):
        await definitions.register("swap", TWO, AGENT, approval_timeout_s=None)
    monkeypatch.setattr(store, "insert_version", insert)
    assert [v for _, v, _ in rows(store)] == [1]

    three = await definitions.register("swap", ONE, BROWSER, approval_timeout_s=None)
    assert three.version == 3
    found = rows(store)
    assert [v for _, v, _ in found] == [1, 2, 3]
    orphan = (await definitions.versions("swap", limit=10))[1]
    got = await definitions.get(orphan.id)
    assert got is not None and (got.version, got.script, got.created_by) == (2, TWO, AGENT)


async def test_an_orphan_of_a_flow_back_in_the_tree_is_indexed_as_not_deleted(
    definitions: FlowDefinitions, store: FlowStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2646: a restore after a delete commits `<name>/` back but its insert fails; the
    reconciler that indexes it also clears the delete, as the insert would have."""
    await definitions.start()
    one = await definitions.register("swap", ONE, BROWSER, approval_timeout_s=None)
    await definitions.delete("swap", BROWSER)
    insert = store.insert_version

    def failing(*args: Any, **kwargs: Any) -> Any:
        raise GitError("the database went away")

    monkeypatch.setattr(store, "insert_version", failing)
    with pytest.raises(GitError):
        await definitions.restore("swap", str(one.commit), BROWSER)
    monkeypatch.setattr(store, "insert_version", insert)
    assert definitions.states.state("swap").deleted_at is not None

    await definitions.register("other", ONE, BROWSER, approval_timeout_s=None)
    assert [v for n, v, _ in rows(store) if n == "swap"] == [1, 2]
    assert definitions.states.state("swap").deleted_at is None


async def test_an_orphan_of_a_flow_deleted_after_it_stays_deleted(
    definitions: FlowDefinitions, store: FlowStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The orphan predates the delete: the flow is not in the tree, so it stays deleted."""
    await definitions.start()
    await definitions.register("swap", ONE, BROWSER, approval_timeout_s=None)
    insert = store.insert_version

    def failing(*args: Any, **kwargs: Any) -> Any:
        raise GitError("the database went away")

    monkeypatch.setattr(store, "insert_version", failing)
    with pytest.raises(GitError):
        await definitions.register("swap", TWO, BROWSER, approval_timeout_s=None)
    monkeypatch.setattr(store, "insert_version", insert)
    await definitions.delete("swap", BROWSER)

    await definitions.register("other", ONE, BROWSER, approval_timeout_s=None)
    assert [v for n, v, _ in rows(store) if n == "swap"] == [1, 2]
    assert definitions.states.state("swap").deleted_at is not None
