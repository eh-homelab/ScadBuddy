"""Flow scripts' own git repository, ``data/flows`` (#2503; spec
2026-10-11-flow-grants-and-git-design.md §6.1, §6.2).

A flow's script is source, so it is versioned as the templates' source is: in git, on
the core model history uses (``core/gitrepo.py``), but in a repository of its own, since
flows are not models. The layout is ``<name>/flow.py``, and ``<name>/inputs.json`` for a
flow that declares typed inputs (#2483): source, versioned with the script in the same
commit, never state (an approval timeout stays in Postgres).

Every version is its own commit: a registration, a restore and a backfilled row each
make exactly one, ``--allow-empty`` when the script is unchanged. A commit carries two
trailers, ``ScadBuddy-Flow: <name>`` and ``ScadBuddy-Flow-Version: <definition id>``,
after the author's (``core/authorship.py``). A version's number is its commit's position
among the commits with that name's ``ScadBuddy-Flow`` trailer, oldest first, and only
this module numbers one. :meth:`FlowHistory.register` holds the write lock across the
reconcile, the commit and the index insert, so two registrations of one name, in one
process or two, never interleave.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from scadbuddy.core.authorship import PRINCIPAL_TRAILER, SESSION_TRAILER, AgentAuthor
from scadbuddy.core.gitrepo import (
    _FIELD,
    _RECORD,
    DEFAULT_TIMEOUT,
    GIT,
    LOCK_NAME,
    GitError,
    GitRepo,
    RepoCoordinator,
    RevisionNotFoundError,
)
from scadbuddy.flows.inputs import INPUTS_FILE

FLOW_FILE = "flow.py"
FLOW_TRAILER = "ScadBuddy-Flow"
VERSION_TRAILER = "ScadBuddy-Flow-Version"
#: A delete's commit (#2485): it names the flow, but is none of its versions.
DELETED_TRAILER = "ScadBuddy-Flow-Deleted"
_GITIGNORE = f"# Written by ScadBuddy.\n{LOCK_NAME}\n"
_FORMAT = (
    "--format=format:%x1e%H%x1f%aI%x1f%s"
    f"%x1f%(trailers:key={FLOW_TRAILER},valueonly,separator=%x2c)"
    f"%x1f%(trailers:key={VERSION_TRAILER},valueonly,separator=%x2c)"
    f"%x1f%(trailers:key={PRINCIPAL_TRAILER},valueonly,separator=%x2c)"
    f"%x1f%(trailers:key={SESSION_TRAILER},valueonly,separator=%x2c)"
)


def flow_path(name: str) -> str:
    return f"{name}/{FLOW_FILE}"


def inputs_path(name: str) -> str:
    return f"{name}/{INPUTS_FILE}"


@dataclass(frozen=True)
class FlowCommit:
    """One version's commit, as its trailers name it."""

    commit: str
    name: str
    version_id: str
    #: Its position among its flow's commits, oldest first: the version number.
    version: int
    date: datetime
    message: str
    #: Who the agent committed it for, when the agent did.
    agent: AgentAuthor | None

    @property
    def short(self) -> str:
        return self.commit[:7]


class FlowHistory(GitRepo):
    """The git repository under ``data/flows``."""

    label = "flow history"

    def __init__(
        self,
        root: Path,
        *,
        git: str = GIT,
        timeout: float = DEFAULT_TIMEOUT,
        coordinator: RepoCoordinator | None = None,
    ) -> None:
        super().__init__(root, git=git, timeout=timeout, coordinator=coordinator)

    def _gitignore_body(self) -> str:
        return _GITIGNORE

    # ── writing ───────────────────────────────────────────────────────────────

    def register(
        self,
        name: str,
        version_id: str,
        index: Callable[[str, int], None],
        *,
        script: str | None = None,
        inputs: dict[str, Any] | None = None,
        restore: str | None = None,
        reconcile: Callable[[], object] | None = None,
        date: datetime | None = None,
    ) -> tuple[str, int]:
        """One new version of ``name``: ``script`` with its ``inputs`` schema (none when
        None), or the script and schema at the commit ``restore``. Under the write lock:
        ``reconcile`` (index what an earlier failed insert left), the commit, then
        ``index(commit, version)``. The commit and its version number."""
        with self.write_lock():
            if reconcile is not None:
                reconcile()
            return self.register_locked(
                name, version_id, index, script=script, inputs=inputs, restore=restore, date=date
            )

    def register_locked(
        self,
        name: str,
        version_id: str,
        index: Callable[[str, int], None],
        *,
        script: str | None = None,
        inputs: dict[str, Any] | None = None,
        restore: str | None = None,
        date: datetime | None = None,
    ) -> tuple[str, int]:
        version = len(self.versions(name)) + 1
        trailers = [f"{FLOW_TRAILER}: {name}", f"{VERSION_TRAILER}: {version_id}"]
        if restore is not None:
            target = self.resolve(restore)
            commit = self._restore_locked(
                name,
                target,
                f"Restore {name} to {target[:7]}",
                trailers=trailers,
                allow_empty=True,
            )
        else:
            assert script is not None
            directory = self.root / name
            directory.mkdir(parents=True, exist_ok=True)
            (directory / FLOW_FILE).write_text(script, encoding="utf-8")
            schema = directory / INPUTS_FILE
            if inputs is None:
                schema.unlink(missing_ok=True)
            else:
                schema.write_text(json.dumps(inputs, indent=2) + "\n", encoding="utf-8")
            commit = self._commit_locked(
                f"Register {name} v{version}", name, trailers=trailers, allow_empty=True, date=date
            )
        if commit is None:  # pragma: no cover - an allowed-empty commit always lands
            raise GitError(f"no commit for {name} v{version}")
        index(commit, version)
        return commit, version

    def delete(self, name: str, record: Callable[[str], None]) -> str:
        """Remove ``<name>/`` as one commit, ``Delete <name>``, carrying
        ``ScadBuddy-Flow-Deleted: <name>`` (#2485). Not a version: its versions stay in
        the history, each script readable at its commit, and a restore of one brings the
        flow back as its next version. Under the write lock: the commit, then
        ``record(commit)``. A flow already gone commits nothing and records the commit
        that removed it. :class:`RevisionNotFoundError` for a name with no version."""
        with self.write_lock():
            if not self.versions(name):
                raise RevisionNotFoundError(f"no flow named {name!r}")
            commit: str | None = None
            if self._tracked(name):
                self._run("rm", "-r", "-q", "--", name)
                commit = self._commit_locked(
                    f"Delete {name}", name, trailers=[f"{DELETED_TRAILER}: {name}"]
                )
            if commit is None:
                commit = self.last_commit(name)
            assert commit is not None  # a flow with a version has a commit touching it
            record(commit)
            return commit

    # ── reading ───────────────────────────────────────────────────────────────

    def commits(self) -> list[FlowCommit]:
        """Every version's commit, each flow's numbered oldest first; newest first."""
        completed = self._run("log", _FORMAT, check=False)
        assert isinstance(completed.stdout, str)
        if completed.returncode != 0:
            unborn = self._run("rev-parse", "--verify", "--quiet", "HEAD", check=False)
            if unborn.returncode == 1 and not unborn.stdout:
                return []
            # Never "no versions": the next registration would number itself v1.
            raise GitError(f"git log failed: {completed.stderr}", str(completed.stderr))
        found: list[tuple[str, str, str, datetime, str, AgentAuthor | None]] = []
        for record in completed.stdout.split(_RECORD):
            if not record.strip():
                continue
            fields = record.split(_FIELD)
            if len(fields) != 7:  # pragma: no cover - git always emits all seven
                continue
            commit, when, subject, name, version_id, principal, session = fields
            name, version_id = name.strip(), version_id.strip()
            if not name or not version_id:
                continue
            agent = (
                AgentAuthor(principal=principal.strip() or None, session=session.strip() or None)
                if principal.strip() or session.strip()
                else None
            )
            found.append(
                (commit.strip(), name, version_id, datetime.fromisoformat(when), subject, agent)
            )
        counts: dict[str, int] = {}
        for _, name, *_rest in found:
            counts[name] = counts.get(name, 0) + 1
        numbered: list[FlowCommit] = []
        for commit, name, version_id, date, subject, agent in found:
            numbered.append(
                FlowCommit(commit, name, version_id, counts[name], date, subject, agent)
            )
            counts[name] -= 1
        return numbered

    def versions(self, name: str) -> list[FlowCommit]:
        """The flow's versions, newest first."""
        return [c for c in self.commits() if c.name == name]

    def version_of(self, name: str, commit: str) -> int:
        """The version number ``commit`` is of the flow; :class:`RevisionNotFoundError`
        when it is not one of the flow's commits."""
        return self.version_at(name, commit).version

    def version_at(self, name: str, commit: str) -> FlowCommit:
        resolved = self.resolve(commit)
        for found in self.versions(name):
            if found.commit == resolved:
                return found
        raise RevisionNotFoundError(f"{commit} is not a version of {name!r}")

    def commit_for(self, version_id: str) -> str | None:
        """The commit whose ``ScadBuddy-Flow-Version`` trailer names ``version_id``."""
        return next((c.commit for c in self.commits() if c.version_id == version_id), None)

    def script_at(self, name: str, commit: str) -> str:
        """The flow's script at ``commit``; :class:`RevisionNotFoundError` without one."""
        return self.show(commit, flow_path(name)).decode("utf-8")

    def inputs_at(self, name: str, commit: str) -> dict[str, Any] | None:
        """The flow's inputs schema at ``commit``; None for a version that declares none
        (#2483)."""
        self.script_at(name, commit)  # a version that is not there is not "no inputs"
        try:
            found: dict[str, Any] = json.loads(self.show(commit, inputs_path(name)))
        except RevisionNotFoundError:
            return None
        return found

    def diff_flow(self, name: str, base: str | None, head: str) -> tuple[str, str, str]:
        """The flow's script from ``base`` (its version before ``head`` when None) to
        ``head``, as a patch naming ``flow.py``: the base, the head and the patch."""
        versions = self.versions(name)
        target = self.version_at(name, head)
        if base is None:
            older = [v for v in versions if v.version == target.version - 1]
            base = older[0].commit if older else None
        else:
            base = self.version_at(name, base).commit
        revisions = self.revision_range(base, target.commit)
        return revisions.base, revisions.head, self.diff(revisions, name, relative=f"{name}/")
