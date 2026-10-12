"""Flow scripts in their own git repository (#2503): `FlowDefinitions` over `FlowHistory`
(`data/flows`) and the Postgres index, and the import of the versions Postgres held."""

from __future__ import annotations

import subprocess
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from psycopg.types.json import Jsonb

from scadbuddy.core.authorship import AGENT_AUTHOR_NAME, AgentAuthor, authored_as
from scadbuddy.core.gitstore import AUTHOR_NAME, GitError, RevisionNotFoundError, git_env
from scadbuddy.flows.definitions import FlowDefinitions, UnknownFlowError
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
def jobs(pg_conninfo: str) -> Iterator[JobProjection]:
    store = JobProjection(pg_conninfo, pool_size=2)
    store.open()
    try:
        yield store
    finally:
        store.close()


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


def subjects(root: Path) -> list[str]:
    return git(root, "log", "--format=%s").splitlines()


async def test_a_registration_is_one_commit_of_name_flow_py(
    definitions: FlowDefinitions, root: Path
) -> None:
    await definitions.start()
    one = await definitions.register("swap", ONE, BROWSER, approval_timeout_s=None)
    assert (root / "swap" / "flow.py").read_text() == ONE
    assert one.commit == git(root, "rev-parse", "HEAD").strip()
    assert (one.version, one.script, one.created_by) == (1, ONE, BROWSER)
    assert git(root, "log", "-1", "--format=%an", "--name-only").split() == [
        AUTHOR_NAME.split()[0],
        "swap/flow.py",
    ]

    again = await definitions.register("swap", ONE, BROWSER, approval_timeout_s=0)
    assert (again.id, again.commit, again.version) == (one.id, one.commit, 1)
    assert again.approval_timeout_s == 0
    two = await definitions.register("swap", TWO, BROWSER, approval_timeout_s=None)
    assert (two.version, two.id != one.id) == (2, True)
    assert subjects(root) == ["Register swap", "Register swap", "Initial revision"]

    # A version id still names its own commit; the list has each flow at its head.
    got = await definitions.get(one.id)
    assert got is not None and got.script == ONE
    assert [(d.name, d.version) for d in await definitions.flows()] == [("swap", 2)]


async def test_an_agent_registration_is_authored_as_model_history_authors_one(
    definitions: FlowDefinitions, root: Path
) -> None:
    await definitions.start()
    with authored_as(AgentAuthor(principal="token:p1", session="s1")):
        made = await definitions.register("swap", ONE, AGENT, approval_timeout_s=None)
    assert made.created_by == AGENT
    log = git(root, "log", "-1", "--format=%an%n%(trailers:only)")
    assert log.splitlines()[0] == AGENT_AUTHOR_NAME
    assert "ScadBuddy-Agent-Principal: token:p1" in log
    assert "ScadBuddy-Agent-Session: s1" in log
    [version] = await definitions.versions(made.id, limit=10)
    assert (version.created_by, version.id, version.current) == (AGENT, made.id, True)


async def test_history_script_at_a_commit_diff_and_restore(
    definitions: FlowDefinitions, root: Path
) -> None:
    await definitions.start()
    one = await definitions.register("swap", ONE, BROWSER, approval_timeout_s=30)
    await definitions.register("other", ONE, BROWSER, approval_timeout_s=None)
    two = await definitions.register("swap", TWO, BROWSER, approval_timeout_s=30)
    assert one.commit is not None and two.commit is not None

    listed = await definitions.versions(one.id, limit=10)
    assert [(v.version, v.commit, v.id, v.current) for v in listed] == [
        (2, two.commit, two.id, True),
        (1, one.commit, one.id, False),
    ]

    at = await definitions.at(two.id, one.commit[:7])
    assert (at.id, at.script, at.version) == (one.id, ONE, 1)

    diff = await definitions.diff(one.id, two.commit, None)
    assert (diff.base, diff.head) == (one.commit, two.commit)
    assert "--- a/flow.py" in diff.patch
    assert "-    return 1" in diff.patch and "+    return 2" in diff.patch

    restored = await definitions.restore(two.id, one.commit, BROWSER)
    assert (restored.version, restored.script, restored.approval_timeout_s) == (3, ONE, 30)
    assert restored.commit not in (one.commit, two.commit)
    assert subjects(root)[0] == f"Restore swap to {one.commit[:7]}"
    # Restoring the script it has already is no commit.
    again = await definitions.restore(two.id, one.commit, BROWSER)
    assert (again.id, again.version) == (restored.id, 3)
    assert [d.version for d in await definitions.flows() if d.name == "swap"] == [3]

    with pytest.raises(UnknownFlowError):
        await definitions.at(str(uuid.uuid4()), one.commit)
    with pytest.raises(RevisionNotFoundError):
        await definitions.at(one.id, "0" * 40)


def legacy(
    store: FlowStore,
    name: str,
    version: int,
    script: str,
    created_by: dict[str, Any],
    at: datetime,
    *,
    timeout: int | None = None,
) -> str:
    """A version as Postgres held it before #2503, and the index row the migration made."""
    version_id = str(uuid.uuid4())
    with store._pool.connection() as conn:
        conn.execute(
            "INSERT INTO workflow_definition_versions"
            " (id, name, version, script, approval_timeout_s, created_by, created_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (version_id, name, version, script, timeout, Jsonb(created_by), at),
        )
        conn.execute(
            "INSERT INTO workflow_definitions (name, id, approval_timeout_s) VALUES (%s, %s, %s)"
            " ON CONFLICT (name) DO UPDATE SET id = EXCLUDED.id,"
            " approval_timeout_s = EXCLUDED.approval_timeout_s",
            (name, version_id, timeout),
        )
    return version_id


def at(hour: int) -> datetime:
    return datetime(2026, 10, 10, hour, tzinfo=UTC)


async def legacy_run(store: FlowStore, run_id: str, definition_id: str, version: int) -> None:
    await store.insert_run(
        FlowRecord(
            run_id=run_id,
            definition_id=definition_id,
            version=version,
            name="swap",
            workflow_id=f"flow-{run_id}",
            workflow_run_id="wr",
            started_by=BROWSER,
        )
    )


async def test_the_versions_postgres_held_become_commits_oldest_first(
    definitions: FlowDefinitions, store: FlowStore, root: Path
) -> None:
    s1 = legacy(store, "swap", 1, ONE, BROWSER, at(1))
    o1 = legacy(store, "other", 1, TWO, AGENT, at(2))
    # Registered again unchanged: one commit, as a registration now is.
    s2 = legacy(store, "swap", 2, ONE, BROWSER, at(3))
    s3 = legacy(store, "swap", 3, TWO, AGENT, at(4), timeout=0)
    await legacy_run(store, "r1", s1, 1)
    await legacy_run(store, "r3", s3, 3)

    await definitions.start()

    initial, *log = git(root, "log", "--reverse", "--format=%an|%aI|%s").splitlines()
    assert initial.endswith("|Initial revision")
    assert log == [
        f"{AUTHOR_NAME}|2026-10-10T01:00:00+00:00|Register swap",
        f"{AGENT_AUTHOR_NAME}|2026-10-10T02:00:00+00:00|Register other",
        f"{AGENT_AUTHOR_NAME}|2026-10-10T04:00:00+00:00|Register swap",
    ]
    one, same, three = [await definitions.get(i) for i in (s1, s2, s3)]
    assert one is not None and same is not None and three is not None
    assert (same.commit, same.version, same.script) == (one.commit, 1, ONE)
    assert (three.version, three.script, three.created_by) == (2, TWO, AGENT)
    other = await definitions.get(o1)
    assert other is not None and other.created_by == AGENT
    # The script left Postgres; the runs name their commits; the head is the newest.
    with store._pool.connection() as conn:
        held = conn.execute(
            "SELECT count(*) AS n FROM workflow_definition_versions WHERE script IS NOT NULL"
        ).fetchone()
    assert held is not None and held["n"] == 0
    runs = {r.id: (r.commit, r.version) for r in await store.list_runs()}
    assert runs == {"r1": (one.commit, 1), "r3": (three.commit, 2)}
    heads = {d.name: (d.id, d.approval_timeout_s) for d in await definitions.flows()}
    assert heads == {"swap": (s3, 0), "other": (o1, None)}

    # Again, as every start does: nothing left to import, nothing committed.
    await definitions.start()
    assert len(subjects(root)) == 4


async def test_an_import_that_failed_half_way_makes_no_commit_twice(
    definitions: FlowDefinitions, store: FlowStore, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy(store, "swap", 1, ONE, BROWSER, at(1))
    s2 = legacy(store, "swap", 2, TWO, BROWSER, at(2))
    history = definitions.history
    counted = history.version_of
    calls = 0

    def fail_second(name: str, commit: str) -> int:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise GitError("the volume went away")
        return counted(name, commit)

    monkeypatch.setattr(history, "version_of", fail_second)
    await definitions.start()
    # Both versions were committed; Postgres rolled back, so neither is imported yet.
    assert subjects(root) == ["Register swap", "Register swap", "Initial revision"]
    not_yet = await definitions.get(s2)
    assert not_yet is not None and (not_yet.commit, not_yet.script) == (None, TWO)

    monkeypatch.undo()
    await definitions.start()
    assert subjects(root) == ["Register swap", "Register swap", "Initial revision"]
    done = await definitions.get(s2)
    assert done is not None and (done.version, done.script) == (2, TWO)
    assert done.commit == git(root, "rev-parse", "HEAD").strip()


async def test_a_commit_the_index_missed_becomes_the_head_at_start(
    definitions: FlowDefinitions, root: Path
) -> None:
    """A registration whose index update failed, or an operator's own commit."""
    await definitions.start()
    one = await definitions.register("swap", ONE, BROWSER, approval_timeout_s=5)
    (root / "swap" / "flow.py").write_text(TWO)
    git(root, "commit", "-qam", "Edit by hand")

    await definitions.start()

    [head] = await definitions.flows()
    assert (head.version, head.approval_timeout_s, head.id != one.id) == (2, 5, True)
    got = await definitions.get(head.id)
    assert got is not None and got.script == TWO
