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
(and of any credential in a URL git echoes) before it is logged or stored.

A refusal no retry can fix (#7388: a size limit, a pre-receive hook, a non-fast-forward,
an auth refusal; :func:`explain`) records the refused head (``refused_commit``), and that
head is not pushed again until it moves or :data:`REFUSED_BACKOFF` has passed. A
transport failure (a timeout, no connection, a 5xx) is tried again at every sweep.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Protocol

from pydantic import BaseModel

from scadbuddy.core.gitrepo import GitError, GitRepo, GitTimeoutError
from scadbuddy.git_remote.store import PushRecord

logger = logging.getLogger(__name__)

#: Each repository's branch at the remote.
BRANCHES: Mapping[str, str] = {"models": "models", "flows": "flows"}
#: The most of git's answer an error keeps.
MAX_ERROR = 2000
SCRUBBED = "[redacted]"
#: How long a head the remote refused for good waits before it is pushed again (#7388).
#: On prod each refused push of ``models`` took about 110 s and uploaded its whole
#: history, once per 5-minute sweep (Tempo, 2026-11-24). Six hours makes that 1 try in
#: 72. A fix that moves no head (a new token, a raised limit, a remote branch reset) is
#: picked up within six hours, or at once by the next commit; a new URL at once.
REFUSED_BACKOFF = timedelta(hours=6)

#: git's and the remote's answers that no retry of the same push can change.
_LASTING = (
    "this exceeds",
    "pre-receive hook declined",
    "[remote rejected]",
    "[rejected]",
    "non-fast-forward",
    "authentication failed",
    "returned error: 401",
    "returned error: 403",
    "returned error: 404",
    "repository not found",
    "permission to",
    "does not appear to be a git repository",
)
#: GitHub's (and GitLab's) size refusal: ``File <path> is <n> MB; this exceeds``.
_TOO_LARGE = re.compile(r"File (\S+) is ([\d.]+ ?[KMG]i?B); this exceeds")
#: A URL's ``user:password@``.
_URL_CREDENTIAL = re.compile(r"(?<=://)[^/\s@]+@")
#: The remote's own lines that say nothing about why.
_NOISE = ("trace:", "see ", "gh001:")


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

    def failed(
        self, repository: str, url: str, error: str, refused_commit: str | None = None
    ) -> None: ...


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
    #: When the head the remote refused for good is pushed again, if it has not moved
    #: by then (#7388); None when the next sweep tries.
    retry_after: datetime | None = None
    #: Commits the remote does not have yet.
    behind: int = 0
    #: When the oldest of them was committed: the lag.
    oldest_unpushed_at: datetime | None = None


def scrub(text: str, secret: str | None) -> str:
    """``text`` without ``secret`` or a URL's credential, cut to :data:`MAX_ERROR`."""
    if secret:
        text = text.replace(secret, SCRUBBED)
    return _URL_CREDENTIAL.sub(f"{SCRUBBED}@", text)[:MAX_ERROR]


def explain(stderr: str) -> tuple[str | None, bool]:
    """Why git says a push failed, in one short line, and whether no retry of the same
    push can fix it. The line is None for a failure that is not a refusal."""
    lasting = any(marker in stderr.lower() for marker in _LASTING)
    if not lasting:
        return None, False
    too_large = _TOO_LARGE.search(stderr)
    if too_large is not None:
        path, size = too_large.groups()
        return f"the remote refused {path} ({size}): over its file size limit", True
    lines = [line.strip() for line in stderr.splitlines()]
    remote = [
        line.removeprefix("remote:").strip().removeprefix("error:").strip()
        for line in lines
        if line.startswith("remote: error:")
    ]
    refs = [" ".join(line.removeprefix("!").split()) for line in lines if line.startswith("! [")]
    fatal = [line.removeprefix("fatal:").strip() for line in lines if line.startswith("fatal:")]
    errors = [line.removeprefix("error:").strip() for line in lines if line.startswith("error:")]
    for found in (remote, refs, fatal, errors):
        said = [line for line in found if line and not line.lower().startswith(_NOISE)]
        if said:
            return f"the remote refused it: {said[0]}", True
    return "the remote refused it", True


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

    @staticmethod
    def _retry_after(record: PushRecord | None, url: str) -> datetime | None:
        """When ``record``'s refused head may be pushed to ``url`` again; None when it
        names no refusal there."""
        if record is None or record.url != url or record.refused_commit is None:
            return None
        return record.attempted_at + REFUSED_BACKOFF

    def push(self, name: str) -> bool:
        """Push ``name``'s HEAD; True when a push landed one. False with no remote set,
        no history, nothing new, or a head the remote refused for good within
        :data:`REFUSED_BACKOFF`. :class:`PushError` when git refused or failed."""
        target = self.target()
        repo = self.repos[name]
        if target is None or not repo.available:
            return False
        head = repo.head()
        record = self.records.get(name)
        if head is None or self._ahead(repo, record, target.url, head):
            return False
        retry_after = self._retry_after(record, target.url)
        if (
            record is not None
            and retry_after is not None
            and record.refused_commit == head
            and datetime.now(UTC) < retry_after
        ):
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
            cause, lasting = (
                (None, False) if isinstance(error, GitTimeoutError) else explain(error.stderr)
            )
            if cause is not None:
                message = scrub(f"{cause}\n{message}", target.token)
            # Another replica's push of a later commit is why this one was refused.
            if not self._ahead(repo, self.records.get(name), target.url, head):
                self.records.failed(name, target.url, message, head if lasting else None)
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
                    retry_after=(
                        self._retry_after(record, target.url)
                        if record is not None and record.refused_commit == head
                        else None
                    ),
                    behind=behind,
                    oldest_unpushed_at=oldest,
                )
            )
        return lags
