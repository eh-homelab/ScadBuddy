"""Flow scripts' own git repository, ``data/flows`` (#2503).

A flow's script is source, so it is versioned as the templates' source is: in git, on
the core model history uses (``core/gitstore.py``), but in a repository of its own,
since flows are not models. The layout is ``<name>/flow.py``. Registering a flow, or
restoring an old version, is one commit, authored as model history authors one: the
agent (with its principal and session as trailers) when the agent made it, else
ScadBuddy. Postgres keeps only state: which commit a version id names, the head of each
flow, and the commit each run ran (``flows/store.py``).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from scadbuddy.core.authorship import current_author
from scadbuddy.core.gitstore import (
    _RECORD,
    DEFAULT_LOG_LIMIT,
    DEFAULT_TIMEOUT,
    GIT,
    LOCK_NAME,
    GitRepository,
    Revision,
    RevisionNotFoundError,
    RevisionRange,
)

FLOW_FILE = "flow.py"
#: The trailer an imported version's commit names its version id by (#2503's import of
#: the versions Postgres held), so an import that stopped half way finds what it did.
IMPORT_TRAILER = "ScadBuddy-Flow-Version"
_GITIGNORE = f"# Written by ScadBuddy.\n{LOCK_NAME}\n"


def flow_path(name: str) -> str:
    return f"{name}/{FLOW_FILE}"


class FlowHistory(GitRepository):
    """The git repository under ``data/flows``."""

    def __init__(self, root: Path, *, git: str = GIT, timeout: float = DEFAULT_TIMEOUT) -> None:
        super().__init__(root, git=git, timeout=timeout)

    def ensure_repo(self) -> str | None:
        """Initialise the repository if it is not one yet; None when it cannot be."""
        return self._ensure(_GITIGNORE, "flow history")

    # ── writing ───────────────────────────────────────────────────────────────

    def save_locked(
        self,
        name: str,
        script: str,
        message: str,
        *,
        date: datetime | None = None,
        trailers: Sequence[str] = (),
    ) -> str | None:
        """Write ``name``'s script and commit it as the current author, under
        :meth:`write_lock`. None when the script is the one committed already."""
        directory = self.root / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / FLOW_FILE).write_text(script, encoding="utf-8")
        self._stage([name])
        if not self._has_staged():
            return None
        return self._commit_staged(message, current_author(), trailers=trailers, date=date)

    def restore_locked(self, name: str, commit: str) -> str | None:
        """Put ``name`` back as it was at ``commit`` (resolved), as a new commit, under
        :meth:`write_lock`. None when it is already identical."""
        return self._restore_locked(name, commit, f"Restore {name} to {commit[:7]}")

    # ── reading ───────────────────────────────────────────────────────────────

    def script_at(self, name: str, commit: str) -> str:
        """The flow's script at ``commit``; :class:`RevisionNotFoundError` without one."""
        return self.show(commit, flow_path(name)).decode("utf-8")

    def own_commit(self, name: str, commit: str) -> str:
        """The flow's version at ``commit``: the last commit at or before it that changed
        the flow. :class:`RevisionNotFoundError` when it did not exist then."""
        found = self.last_commit(name, at=self.resolve(commit))
        if found is None:
            raise RevisionNotFoundError(f"{name!r} does not exist at {commit}")
        return found

    def version_of(self, name: str, commit: str) -> int:
        """The version number ``commit`` is of the flow: its place in the commit order."""
        return self.count(commit, name)

    def history(self, name: str, *, limit: int = DEFAULT_LOG_LIMIT) -> list[Revision]:
        """The flow's commits, newest first."""
        return self.log(name, limit=limit)

    def revision(self, commit: str) -> Revision:
        [found] = self.log(rev=commit, limit=1)
        return found

    def diff_flow(self, name: str, revisions: RevisionRange) -> str:
        """The patch of the flow's directory, its headers naming ``flow.py``."""
        return self.diff(revisions, name, relative=f"{name}/")

    def heads(self) -> dict[str, str]:
        """Each flow's newest commit, from one walk of the history."""
        completed = self._run("log", "--format=%x1e%H", "--name-only", check=False)
        assert isinstance(completed.stdout, str)
        if completed.returncode != 0:
            return {}
        newest: dict[str, str] = {}
        for record in completed.stdout.split(_RECORD):
            commit, _, paths = record.strip().partition("\n")
            for path in paths.splitlines() if commit else []:
                name, slash, _ = path.partition("/")
                if slash:
                    newest.setdefault(name, commit)
        return newest

    def imported(self) -> dict[str, str]:
        """The commits the import made, by the version id each names."""
        completed = self._run(
            "log",
            f"--format=%H%x1f%(trailers:key={IMPORT_TRAILER},valueonly,separator=%x2c)",
            check=False,
        )
        assert isinstance(completed.stdout, str)
        if completed.returncode != 0:
            return {}
        found: dict[str, str] = {}
        for line in completed.stdout.splitlines():
            commit, _, version_id = line.partition("\x1f")
            if version_id:
                found.setdefault(version_id.strip(), commit)
        return found
