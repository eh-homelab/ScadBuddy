"""Flow scripts' own git repository, ``data/flows`` (#2503; spec
2026-10-11-flow-grants-and-git-design.md §6.1, §6.2).

A flow's script is source, so it is versioned as the templates' source is: in git, on
the core model history uses (``core/gitrepo.py``), but in a repository of its own, since
flows are not models. The layout is ``<name>/flow.py``, and ``<name>/inputs.json`` for a
flow that declares typed inputs (#2483): source, versioned with the script in the same
commit, never state (an approval timeout stays in Postgres). ``<name>/flow.json`` holds
the flow's display name and description (#3186): the flow's, not a version's, so each
change of them is a commit of its own, carrying ``ScadBuddy-Flow-Details: <name>``, and
neither a new version nor a restore changes them. A flow without the file is named by
its slug.

Every write takes an optional commit message, its subject, and an optional body (#3186);
without a message the subject says what the write did.

Every version is its own commit: a registration and a restore each make exactly one,
``--allow-empty`` when the script is unchanged. A commit carries two trailers,
``ScadBuddy-Flow: <name>`` and ``ScadBuddy-Flow-Version: <definition id>``, after the
author's (``core/authorship.py``). A version's number is its commit's position
among the commits with that name's ``ScadBuddy-Flow`` trailer, oldest first, and only
this module numbers one. :meth:`FlowHistory.register` holds the write lock across the
reconcile, the commit and the index insert, so two registrations of one name, in one
process or two, never interleave.
"""

from __future__ import annotations

import json
import re
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
#: A details change's commit (#3186): it names the flow, but is none of its versions.
DETAILS_TRAILER = "ScadBuddy-Flow-Details"
DETAILS_FILE = "flow.json"
#: A trailer line, as git writes the ones ScadBuddy adds.
_TRAILER_LINE = re.compile(r"^[A-Za-z0-9-]+: ")
_GITIGNORE = f"# Written by ScadBuddy.\n{LOCK_NAME}\n"
_FORMAT = (
    "--format=format:%x1e%H%x1f%aI%x1f%s"
    f"%x1f%(trailers:key={FLOW_TRAILER},valueonly,separator=%x2c)"
    f"%x1f%(trailers:key={VERSION_TRAILER},valueonly,separator=%x2c)"
    f"%x1f%(trailers:key={PRINCIPAL_TRAILER},valueonly,separator=%x2c)"
    f"%x1f%(trailers:key={SESSION_TRAILER},valueonly,separator=%x2c)"
    "%x1f%b"
)


def flow_path(name: str) -> str:
    return f"{name}/{FLOW_FILE}"


def inputs_path(name: str) -> str:
    return f"{name}/{INPUTS_FILE}"


def details_path(name: str) -> str:
    return f"{name}/{DETAILS_FILE}"


def _body_of(raw: str) -> str | None:
    """A commit's own body: ``%b`` without the trailers paragraph ScadBuddy adds."""
    paragraphs = [p for p in raw.strip().split("\n\n") if p.strip()]
    if paragraphs and all(_TRAILER_LINE.match(line) for line in paragraphs[-1].splitlines()):
        paragraphs.pop()
    text = "\n\n".join(paragraphs).strip()
    return text or None


def _message(message: str | None, default: str) -> str:
    return message if message is not None and message.strip() else default


@dataclass(frozen=True)
class FlowDetails:
    """A flow's display name and description (#3186), from ``<name>/flow.json``."""

    display_name: str | None = None
    description: str | None = None


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
    #: The commit message's body, without its trailers (#3186); None for none.
    body: str | None = None

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
        message: str | None = None,
        body: str | None = None,
        display_name: str | None = None,
        description: str | None = None,
    ) -> tuple[str, int]:
        """One new version of ``name``: ``script`` with its ``inputs`` schema (none when
        None), or the script and schema at the commit ``restore``. Under the write lock:
        ``reconcile`` (index what an earlier failed insert left), the commit, then
        ``index(commit, version)``, then, when ``display_name`` or ``description`` changes
        the flow's details, their own commit (#3186). The version's commit and number."""
        with self.write_lock():
            if reconcile is not None:
                reconcile()
            found = self.register_locked(
                name,
                version_id,
                index,
                script=script,
                inputs=inputs,
                restore=restore,
                message=message,
                body=body,
            )
            self._set_details_locked(name, display_name, description)
            return found

    def register_locked(
        self,
        name: str,
        version_id: str,
        index: Callable[[str, int], None],
        *,
        script: str | None = None,
        inputs: dict[str, Any] | None = None,
        restore: str | None = None,
        message: str | None = None,
        body: str | None = None,
    ) -> tuple[str, int]:
        version = len(self.versions(name)) + 1
        trailers = [f"{FLOW_TRAILER}: {name}", f"{VERSION_TRAILER}: {version_id}"]
        if restore is not None:
            target = self.resolve(restore)
            commit = self._restore_version_locked(
                name,
                target,
                _message(message, f"Restore {name} to {target[:7]}"),
                trailers=trailers,
                body=body,
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
                _message(message, f"Register {name} v{version}"),
                name,
                trailers=trailers,
                allow_empty=True,
                body=body,
            )
        if commit is None:  # pragma: no cover - an allowed-empty commit always lands
            raise GitError(f"no commit for {name} v{version}")
        index(commit, version)
        return commit, version

    def _restore_version_locked(
        self, name: str, target: str, message: str, *, trailers: list[str], body: str | None
    ) -> str | None:
        """``<name>/`` as it was at ``target``, as one commit, keeping the flow's details
        as they are now (#3186): they are the flow's, not the version's. A deleted flow
        comes back with the ones it had at ``target``."""
        present = set(self._files_at(target, name))
        if not present:
            raise RevisionNotFoundError(f"{name!r} does not exist at {target}")
        was_present = self.present(name)
        details = self.root / details_path(name)
        kept = details.read_bytes() if details.is_file() else None
        self._run("checkout", target, "--", name)
        for tracked in self._tracked(name):
            if tracked not in present:
                (self.root / tracked).unlink(missing_ok=True)
        if was_present:
            if kept is None:
                details.unlink(missing_ok=True)
            else:
                details.write_bytes(kept)
        return self._commit_locked(message, name, trailers=trailers, allow_empty=True, body=body)

    def set_details(
        self,
        name: str,
        *,
        display_name: str | None = None,
        description: str | None = None,
        message: str | None = None,
        body: str | None = None,
    ) -> str | None:
        """Change the flow's display name or description (#3186): ``None`` leaves one as
        it is, an empty string clears it. One commit of its own, under the write lock;
        None when nothing changed. :class:`RevisionNotFoundError` for a flow with no
        version, or one not in the tree (deleted)."""
        with self.write_lock():
            if not self.versions(name) or not self.present(name):
                raise RevisionNotFoundError(f"no flow named {name!r}")
            return self._set_details_locked(name, display_name, description, message, body)

    def _set_details_locked(
        self,
        name: str,
        display_name: str | None,
        description: str | None,
        message: str | None = None,
        body: str | None = None,
    ) -> str | None:
        if display_name is None and description is None:
            return None
        was = self.details(name)
        now = FlowDetails(
            display_name=was.display_name if display_name is None else display_name.strip() or None,
            description=was.description if description is None else description.strip() or None,
        )
        if now == was:
            return None
        path = self.root / details_path(name)
        if now == FlowDetails():
            path.unlink(missing_ok=True)
        else:
            fields = {"display_name": now.display_name, "description": now.description}
            kept = {key: value for key, value in fields.items() if value is not None}
            path.write_text(json.dumps(kept, indent=2, ensure_ascii=False) + "\n", "utf-8")
        if now.display_name != was.display_name:
            default = (
                f"Rename {name} to {now.display_name}"
                if now.display_name
                else f"Clear the display name of {name}"
            )
        else:
            default = f"Describe {name}" if now.description else f"Clear the description of {name}"
        return self._commit_locked(
            _message(message, default),
            details_path(name),
            trailers=[f"{DETAILS_TRAILER}: {name}"],
            body=body,
        )

    def delete(
        self,
        name: str,
        record: Callable[[str], None],
        *,
        message: str | None = None,
        body: str | None = None,
    ) -> str:
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
                    _message(message, f"Delete {name}"),
                    name,
                    trailers=[f"{DELETED_TRAILER}: {name}"],
                    body=body,
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
        found: list[tuple[str, str, str, datetime, str, AgentAuthor | None, str | None]] = []
        for record in completed.stdout.split(_RECORD):
            if not record.strip():
                continue
            fields = record.split(_FIELD)
            if len(fields) != 8:  # pragma: no cover - git always emits all eight
                continue
            commit, when, subject, name, version_id, principal, session, raw_body = fields
            name, version_id = name.strip(), version_id.strip()
            if not name or not version_id:
                continue
            agent = (
                AgentAuthor(principal=principal.strip() or None, session=session.strip() or None)
                if principal.strip() or session.strip()
                else None
            )
            found.append(
                (
                    commit.strip(),
                    name,
                    version_id,
                    datetime.fromisoformat(when),
                    subject,
                    agent,
                    _body_of(raw_body),
                )
            )
        counts: dict[str, int] = {}
        for _, name, *_rest in found:
            counts[name] = counts.get(name, 0) + 1
        numbered: list[FlowCommit] = []
        for commit, name, version_id, date, subject, agent, body in found:
            numbered.append(
                FlowCommit(commit, name, version_id, counts[name], date, subject, agent, body)
            )
            counts[name] -= 1
        return numbered

    def present(self, name: str) -> bool:
        """Whether ``<name>/`` is in the tree: not deleted, as git has it."""
        return bool(self._tracked(name))

    def details(self, name: str) -> FlowDetails:
        """The flow's display name and description (#3186), from ``<name>/flow.json`` in
        the tree; none for a flow without one, or one whose file is not an object of
        strings (named by its slug, as before the file existed)."""
        try:
            found = json.loads((self.root / details_path(name)).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            return FlowDetails()
        if not isinstance(found, dict):
            return FlowDetails()

        def field(key: str) -> str | None:
            value = found.get(key)
            return value if isinstance(value, str) and value.strip() else None

        return FlowDetails(display_name=field("display_name"), description=field("description"))

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
