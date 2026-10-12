"""Flow versions over git and Postgres (#2503, plan 2026-10-11-flow-grants-and-git.md
Tasks C3 and C5): registration under one lock, a failed insert, the backfill of the rows
written before flows were in git, and the reconciler."""

from __future__ import annotations

import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from psycopg.types.json import Jsonb

from scadbuddy.core.authorship import AGENT_AUTHOR_NAME, AgentAuthor, authored_as
from scadbuddy.core.gitrepo import AUTHOR_NAME, GitError, git_env
from scadbuddy.flows.backfill import backfill
from scadbuddy.flows.definitions import FlowDefinitions, ScriptMissingError, UnknownFlowError
from scadbuddy.flows.repository import FlowHistory
from scadbuddy.flows.store import FlowStore
from scadbuddy.render.projection import JobProjection
from scadbuddy.workflows.flow_models import FlowRecord

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
def definitions(root: Path, store: FlowStore) -> FlowDefinitions:
    return FlowDefinitions(FlowHistory(root), store)


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
    with store._pool.connection() as conn:
        conn.execute("UPDATE workflow_definitions SET script = 'edited in the row'")
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


def legacy(
    store: FlowStore,
    name: str,
    version: int,
    script: str,
    created_by: dict[str, Any],
    hour: int,
) -> str:
    """A row as registered before #2503: its script in Postgres, no commit."""
    version_id = str(uuid.uuid4())
    with store._pool.connection() as conn:
        conn.execute(
            "INSERT INTO workflow_definitions (id, name, version, script, created_by, created_at)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (
                version_id,
                name,
                version,
                script,
                Jsonb(created_by),
                datetime(2026, 10, 10, hour, tzinfo=UTC),
            ),
        )
    return version_id


async def legacy_run(store: FlowStore, run_id: str, definition_id: str) -> None:
    await store.insert_run(
        FlowRecord(
            run_id=run_id,
            definition_id=definition_id,
            version=1,
            name="swap",
            workflow_id=f"flow-{run_id}",
            workflow_run_id="wr",
            started_by=BROWSER,
        )
    )


async def test_the_backfill_commits_each_row_in_order_with_its_author_and_date(
    definitions: FlowDefinitions, store: FlowStore, root: Path
) -> None:
    # Created out of version order (the transaction's start, spec §6.4): ordered by
    # version anyway.
    s1 = legacy(store, "swap", 1, ONE, BROWSER, 3)
    s2 = legacy(store, "swap", 2, ONE, BROWSER, 2)
    s3 = legacy(store, "swap", 3, TWO, AGENT, 4)
    o1 = legacy(store, "other", 1, TWO, AGENT, 1)
    await legacy_run(store, "r1", s1)
    await legacy_run(store, "r3", s3)

    await definitions.start()

    # Seconds, not `%aI`, whose UTC spelling differs between git versions.
    log = git(root, "log", "--reverse", "--format=%an|%at|%s").splitlines()[1:]

    def at(hour: int) -> int:
        return int(datetime(2026, 10, 10, hour, tzinfo=UTC).timestamp())

    assert log == [
        f"{AGENT_AUTHOR_NAME}|{at(1)}|Register other v1",
        f"{AUTHOR_NAME}|{at(3)}|Register swap v1",
        f"{AUTHOR_NAME}|{at(2)}|Register swap v2",
        f"{AGENT_AUTHOR_NAME}|{at(4)}|Register swap v3",
    ]
    history = definitions.history
    for version_id, version in ((s1, 1), (s2, 2), (s3, 3)):
        commit = history.commit_for(version_id)
        assert commit is not None and history.version_of("swap", commit) == version
        got = await definitions.get(version_id)
        assert got is not None and (got.commit, got.version) == (commit, version)
    other = await definitions.get(o1)
    assert other is not None and other.script == TWO
    runs = {r.id: r.commit for r in await store.list_runs()}
    assert runs == {"r1": history.commit_for(s1), "r3": history.commit_for(s3)}

    # Again, as every start does: nothing left, nothing committed.
    await definitions.start()
    assert len(git(root, "log", "--format=%H").splitlines()) == 5


async def test_a_crash_between_a_backfill_commit_and_its_update_is_repaired(
    definitions: FlowDefinitions, store: FlowStore, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s1 = legacy(store, "swap", 1, ONE, BROWSER, 1)
    s2 = legacy(store, "swap", 2, TWO, BROWSER, 2)
    definitions.history.ensure_repo()
    set_commit = store.set_commit

    def crash(definition_id: str, commit: str) -> None:
        if definition_id == s2:
            raise GitError("the process died")
        set_commit(definition_id, commit)

    monkeypatch.setattr(store, "set_commit", crash)
    with pytest.raises(GitError):
        backfill(definitions.history, store)
    monkeypatch.undo()

    await definitions.start()
    assert len(git(root, "log", "--format=%H").splitlines()) == 3
    for version_id, version in ((s1, 1), (s2, 2)):
        got = await definitions.get(version_id)
        assert got is not None and got.version == version
        assert got.commit == definitions.history.commit_for(version_id)


async def test_a_registration_after_a_partly_failed_backfill_finishes_it_first(
    definitions: FlowDefinitions, store: FlowStore, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A backfill that failed at start (a git timeout on the third row) is finished by
    the next registration, under its lock, before it takes a number: never a number a
    legacy row still holds."""
    legacy(store, "swap", 1, ONE, BROWSER, 1)
    legacy(store, "swap", 2, TWO, BROWSER, 2)
    s3 = legacy(store, "swap", 3, ONE, AGENT, 3)
    history = definitions.history
    register_locked = history.register_locked
    failed: list[str] = []

    def flaky(name: str, version_id: str, *args: Any, **kwargs: Any) -> Any:
        if version_id == s3 and not failed:
            failed.append(version_id)
            raise GitError("timed out")
        return register_locked(name, version_id, *args, **kwargs)

    monkeypatch.setattr(history, "register_locked", flaky)
    await definitions.start()
    assert [c for _, _, c in rows(store)][2] is None

    four = await definitions.register("swap", TWO, BROWSER, approval_timeout_s=None)
    assert four.version == 4
    found = rows(store)
    assert [v for _, v, _ in found] == [1, 2, 3, 4]
    for _, version, commit in found:
        assert commit is not None and history.version_of("swap", commit) == version

    # And the next start has nothing to repair.
    await definitions.start()
    assert len(git(root, "log", "--format=%H").splitlines()) == 5
