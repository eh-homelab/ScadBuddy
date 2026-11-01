"""Pushing the model and flow history to an off-box git remote (#4974; #90's remote push).

Each repository goes to a branch of its own at the one remote Settings names
(:data:`BRANCHES`): ``data/models`` to ``models``, ``data/flows`` to ``flows``. A push
is asked for once a commit lands (`GitRepo.after_commit`, after the write lock is
released) and runs on a thread of its own, so it never holds up or fails a save; one
already running for that repository runs once more when it ends, rather than once per
commit. Whatever is still behind (the remote was down, a replica stopped mid-push) is
pushed by the housekeeping sweep (`workflows/housekeeping.py` `GIT_REMOTE_SWEEP`, on the
prune Schedule, so within `PRUNE_INTERVAL` even with the sweeps off).

Every push is of HEAD's commit id to ``refs/heads/<branch>``, never a force: a remote
that is on another history (a volume restored to an older one) refuses it, and Settings
shows that as the lag and the error, rather than ScadBuddy rewriting the off-box copy.
``git_remote_pushes`` (`store.py`) records the last commit each push landed, which is
how far behind the remote is (:meth:`RemotePush.lag`). The credential reaches git only
through the child's environment (`GitRepo.push`), and every error is scrubbed of it
before it is logged or stored.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import Protocol

from pydantic import BaseModel

from scadbuddy.core.gitrepo import GitError, GitRepo
from scadbuddy.git_remote.store import PushRecord

logger = logging.getLogger(__name__)

#: Each repository's branch at the remote.
BRANCHES: Mapping[str, str] = {"models": "models", "flows": "flows"}
#: The most of git's answer an error keeps.
MAX_ERROR = 2000
SCRUBBED = "[redacted]"


@dataclass(frozen=True)
class RemoteTarget:
    """Where to push, and as whom. ``repr`` never shows the token."""

    url: str
    username: str | None = None
    token: str | None = None

    def __repr__(self) -> str:
        return f"RemoteTarget(url={self.url!r}, username={self.username!r})"


class Records(Protocol):
    """What a push records (`store.PushRecords`)."""

    def get(self, repository: str) -> PushRecord | None: ...

    def pushed(self, repository: str, url: str, commit: str) -> None: ...

    def failed(self, repository: str, url: str, error: str) -> None: ...


class PushError(Exception):
    """A push failed; the message is git's, scrubbed of the credential."""


class RepositoryLag(BaseModel):
    """How far one repository's off-box copy is behind it."""

    repository: str
    #: The branch it is pushed to at the remote.
    branch: str
    #: The repository's newest commit; None before its first.
    head: str | None = None
    #: The last commit a push landed at the remote set now.
    pushed_commit: str | None = None
    pushed_at: datetime | None = None
    #: When a push of it was last tried.
    attempted_at: datetime | None = None
    #: Why the last try failed; None once one lands.
    error: str | None = None
    #: Commits the remote does not have yet.
    behind: int = 0
    #: When the oldest of them was committed: the lag.
    oldest_unpushed_at: datetime | None = None


def scrub(text: str, secret: str | None) -> str:
    """``text`` without ``secret``, cut to :data:`MAX_ERROR`."""
    if secret:
        text = text.replace(secret, SCRUBBED)
    return text[:MAX_ERROR]


class _Coalesced:
    """Runs ``work`` on a thread of its own; asked again while it runs, it runs once
    more when it ends."""

    def __init__(self, work: Callable[[], None], name: str) -> None:
        self._work = work
        self._name = name
        self._lock = threading.Lock()
        self._running = False
        self._again = False

    def fire(self) -> None:
        with self._lock:
            if self._running:
                self._again = True
                return
            self._running = True
        threading.Thread(target=self._loop, name=self._name, daemon=True).start()

    def _loop(self) -> None:
        while True:
            try:
                self._work()
            except Exception:
                logger.exception("a git remote push failed unexpectedly")
            with self._lock:
                if not self._again:
                    self._running = False
                    return
                self._again = False


class RemotePush:
    """Pushes each of ``repos`` (by :data:`BRANCHES` name) to the remote ``target``
    names, when it names one."""

    def __init__(
        self,
        repos: Mapping[str, GitRepo],
        records: Records,
        target: Callable[[], RemoteTarget | None],
    ) -> None:
        self.repos = dict(repos)
        self.records = records
        self.target = target
        self._triggers = {
            name: _Coalesced(partial(self._push_logged, name), f"git-push-{name}")
            for name in self.repos
        }

    def watch(self) -> None:
        """Ask for a push of each repository after each of its commits."""
        for name, repo in self.repos.items():
            repo.after_commit = self._requester(name)

    def _requester(self, name: str) -> Callable[[str], None]:
        def request(_commit: str) -> None:
            self.request(name)

        return request

    def request(self, name: str) -> None:
        """A push of ``name`` in the background, when a remote is set. Never blocks."""
        if self.target() is not None:
            self._triggers[name].fire()

    def _push_logged(self, name: str) -> None:
        try:
            self.push(name)
        except PushError:
            # Recorded for Settings; the sweep tries again.
            logger.warning("could not push to the git remote", extra={"repository": name})

    def _ahead(self, repo: GitRepo, record: PushRecord | None, url: str, head: str) -> bool:
        """Whether ``record`` says ``url`` already has ``head`` (or a later commit)."""
        return (
            record is not None
            and record.url == url
            and record.pushed_commit is not None
            and repo.is_ancestor(head, record.pushed_commit)
        )

    def push(self, name: str) -> bool:
        """Push ``name``'s HEAD; True when a push landed one. False with no remote set,
        no history, or nothing new. :class:`PushError` when git refused or failed."""
        target = self.target()
        repo = self.repos[name]
        if target is None or not repo.available:
            return False
        head = repo.head()
        if head is None or self._ahead(repo, self.records.get(name), target.url, head):
            return False
        try:
            repo.push(
                target.url,
                head,
                f"refs/heads/{BRANCHES[name]}",
                username=target.username,
                password=target.token,
            )
        except GitError as error:
            message = scrub(str(error), target.token)
            # Another replica's push of a later commit is why this one was refused.
            if not self._ahead(repo, self.records.get(name), target.url, head):
                self.records.failed(name, target.url, message)
            raise PushError(message) from None
        if not self._ahead(repo, self.records.get(name), target.url, head):
            self.records.pushed(name, target.url, head)
        logger.info("pushed to the git remote", extra={"repository": name, "commit": head})
        return True

    def push_all(self) -> list[str]:
        """Push every repository; the ones that landed something. Every one is tried,
        then :class:`PushError` names those that failed."""
        pushed: list[str] = []
        failed: list[str] = []
        for name in self.repos:
            try:
                if self.push(name):
                    pushed.append(name)
            except PushError:
                failed.append(name)
        if failed:
            raise PushError(f"could not push {', '.join(failed)} to the git remote")
        return pushed

    def lag(self) -> list[RepositoryLag]:
        """How far behind the remote set now is, per repository. Empty with none set."""
        target = self.target()
        if target is None:
            return []
        lags: list[RepositoryLag] = []
        for name, repo in self.repos.items():
            record = self.records.get(name)
            if record is not None and record.url != target.url:
                record = None
            head = repo.head() if repo.available else None
            behind, oldest = (0, None)
            pushed = None if record is None else record.pushed_commit
            if head is not None:
                behind, oldest = repo.unpushed(pushed, head)
            lags.append(
                RepositoryLag(
                    repository=name,
                    branch=BRANCHES[name],
                    head=head,
                    pushed_commit=pushed,
                    pushed_at=None if record is None else record.pushed_at,
                    attempted_at=None if record is None else record.attempted_at,
                    error=None if record is None else record.error,
                    behind=behind,
                    oldest_unpushed_at=oldest,
                )
            )
        return lags
