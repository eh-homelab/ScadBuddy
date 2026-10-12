"""Model history: ``data/models`` is a git repository, and git is the version store.

Every catalogue action — create, source edit, metadata change, delete, built-in
sync, restore — is exactly one commit. Nothing here keeps a parallel index of
revisions: ``git log``, ``git show`` and ``git diff`` are the read side, so an
operator with a shell on the volume sees precisely what the API serves.

Why shell out to ``git`` rather than dulwich or pygit2
------------------------------------------------------
The product surface of issue #90 *is* git porcelain — a log with changed files,
a unified diff between two revisions, a restore that is a new commit. Rebuilding
that on a library is a home-grown version store wearing a different hat, which is
the thing the issue rules out. The reference implementation also keeps the
repository ordinarily usable: whatever the server does, ``git log`` in ``/data/models``
says the same thing. The follow-ups both want the real client too — #93 vendors
libraries as ``git clone --depth 1`` checkouts, and the optional off-box push in
#90 wants git's own transports rather than a second implementation of them.
The cost is one ``apt`` package in the image; the risks are ownership and
ambient configuration, and both are closed below.

Every invocation is hermetic. ``GIT_CONFIG_GLOBAL``/``GIT_CONFIG_SYSTEM`` point at
``/dev/null`` so no operator's ``~/.gitconfig`` (or its hooks, aliases or
``commit.gpgsign``) can reach this repository, identity is supplied through the
environment rather than written to a config file (the container runs as uid 10001,
whose HOME is not the volume), and ``safe.directory`` is passed as command-line
configuration — which counts as *protected* scope, so it is honoured — because a
PVC's ownership need not match the runtime uid.

Remote push (the off-box copy in #90) is deliberately not implemented here. The
seam is :meth:`ModelHistory.commit`, which returns the new commit id, and a
``push`` would be one more ``_run`` with credentials from the settings store.
"""

from __future__ import annotations

import fcntl
import io
import logging
import os
import posixpath
import re
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from scadbuddy.core.authorship import (
    AGENT_AUTHOR_EMAIL,
    AGENT_AUTHOR_NAME,
    PRINCIPAL_TRAILER,
    SESSION_TRAILER,
    AgentAuthor,
    current_author,
)
from scadbuddy.core.paths import BUILTIN_DIR, BUILTIN_PREFIX, RENDERS_DIR_NAME, revalidate_dirs

logger = logging.getLogger(__name__)

GIT = "git"
LOCK_NAME = ".scadbuddy-git.lock"
GITIGNORE_NAME = ".gitignore"
DEFAULT_LOG_LIMIT = 50
# Generous for a local repository, small enough that a stalled PVC cannot hold an
# executor slot for minutes. Overridable with ``SCADBUDDY_GIT_TIMEOUT``.
DEFAULT_TIMEOUT = 30.0
LOCK_POLL_SECONDS = 0.05
#: The longest pause between two looks for the HEAD the last writer recorded.
FRESH_POLL_MAX_SECONDS = 1.0
#: What git says when HEAD moved between its read of it and its lock on it: on the
#: NFS share, a read of `.git` older than another replica's commit (#2385).
REF_LOCK_ERROR = "cannot lock ref"
#: How many times a `git commit` that hit `REF_LOCK_ERROR` is tried in all, and the
#: first pause between tries (doubled after each).
COMMIT_ATTEMPTS = 4
COMMIT_RETRY_SECONDS = 0.1
# git itself imposes no subject limit; this one keeps a listed revision readable
# and bounds what a caller can write into the history.
MAX_SUBJECT = 200
#: `maintain` packs the repository once it holds this many loose objects or packs.
#: Git's own `gc --auto` waits for 6700 loose objects, and on the NFS volume a
#: history walk over a few thousand (#2289: 3029, 0 packs) took 11 s.
MAINTAIN_LOOSE_OBJECTS = 256
MAINTAIN_PACKS = 20
#: A `gc` repacks the whole history: well past `DEFAULT_TIMEOUT` on a slow volume.
MAINTAIN_TIMEOUT = 600.0

AUTHOR_NAME = "ScadBuddy"
AUTHOR_EMAIL = "scadbuddy@localhost"

# The initial branch is named rather than inherited: `init.defaultBranch` lives in
# the global config this module refuses to read, so without it git picks its
# compiled-in default and warns on every init.
DEFAULT_BRANCH = "main"
#: The commit an existing repository's boot makes of changes a failed commit left.
RECOVERED_MESSAGE = "Recover uncommitted changes"

COMMIT_ID_PATTERN = r"^[0-9a-f]{7,40}$"

#: The subject the pre-#155 copy-if-absent seed committed a bundled model under:
#: ``Seed name-keychain and tag from the image``.
_SEED_SUBJECT = re.compile(r"^Seed .+ from the image$")
#: A full SHA-1 or SHA-256 object id, on its own (not part of a longer hex run).
_OBJECT_ID = re.compile(r"(?<![0-9a-f])(?:[0-9a-f]{64}|[0-9a-f]{40})(?![0-9a-f])")

# git's own hash of the empty tree: what a root commit's diff is taken against,
# since it has no parent to compare with.
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

# ASCII record/unit separators, spelled as git's own `%xNN` escapes so the argv
# stays plain ASCII — an embedded NUL cannot be passed to execve at all — and
# neither byte can occur inside a commit field.
_RECORD = "\x1e"
_FIELD = "\x1f"
# The agent's trailers (core/authorship.py, #252) sit before the subject, which has to
# stay last: `--name-status` appends its block after it.
_LOG_FORMAT = (
    "--format=format:%x1e%H%x1f%an%x1f%aI"
    f"%x1f%(trailers:key={PRINCIPAL_TRAILER},valueonly,separator=%x2c)"
    f"%x1f%(trailers:key={SESSION_TRAILER},valueonly,separator=%x2c)"
    "%x1f%s"
)


class GitError(RuntimeError):
    """A git invocation failed. ``stderr`` carries what it said."""

    def __init__(self, message: str, stderr: str = "") -> None:
        super().__init__(message)
        self.stderr = stderr


class GitUnavailableError(GitError):
    """No usable git repository — either no binary, or ``init`` never succeeded."""


class HeadMovedError(GitError):
    """HEAD is not where this writer found it on taking the write lock: something
    committed without the lock. What was checked against the old HEAD -- an edit's
    ``base`` (#813) -- has to be checked again."""


class GitTimeoutError(GitError):
    """A git call, or the wait for the write lock, outlived its deadline.

    Unbounded is the dangerous default here. Every git call runs on the default
    ``ThreadPoolExecutor`` via ``asyncio.to_thread``, which ``/healthz`` and the
    render polls share, and this repository lives on a PVC that Velero snapshots:
    one ``fsync`` parked behind a block-storage stall would hold an executor slot
    for as long as the stall lasts, and enough of them make the whole app look
    hung rather than one commit slow. A deadline turns that into an ordinary
    :class:`GitError`, which the callers already log and degrade around.
    """


#: Git's modes for a regular file and for a symlink in a tree entry.
FILE_MODES = ("100644", "100755")
SYMLINK_MODE = "120000"
TREE_MODE = "040000"
#: As the kernel's own limit on symlinks followed in one lookup (`ELOOP`).
MAX_SYMLINK_HOPS = 40
#: The longest symlink target read: Linux's PATH_MAX.
MAX_PATH_BYTES = 4096


class RevisionNotFoundError(KeyError):
    pass


class BlobTooLargeError(ValueError):
    """A file at a revision is larger than the caller will read."""


@dataclass(frozen=True)
class FileChange:
    """One entry of ``git log --name-status``: ``A``/``M``/``D`` plus the path."""

    status: str
    path: str


@dataclass(frozen=True)
class RevisionRange:
    """A diff's two endpoints, both resolved to full object ids."""

    base: str
    head: str


@dataclass(frozen=True)
class Revision:
    commit: str
    author: str
    date: datetime
    message: str
    files: list[FileChange]
    #: Who the agent committed it for (core/authorship.py), when the agent did.
    agent: AgentAuthor | None = None

    @property
    def short(self) -> str:
        return self.commit[:7]


class RepoHold(Protocol):
    """The cross-replica write lock on the models repository, while it is held."""

    #: The HEAD the last writer recorded, or None before any did.
    expected: str | None

    def record(self, commit: str) -> None:
        """Record ``commit`` as HEAD, before the lock is released."""


class RepoCoordinator(Protocol):
    """Takes :class:`RepoHold`: one writer across every replica sharing the volume
    (`library/repo_lock.py`'s Postgres one in the app)."""

    def hold(self, timeout: float) -> AbstractContextManager[RepoHold]: ...


def git_env() -> dict[str, str]:
    """The hermetic environment every git call runs in -- see the module docstring.

    Shared with the library checkouts (#93), which clone with the same client.
    """
    return {
        # Inherited, not pinned: `available` resolves the binary with
        # `shutil.which` against this PATH, so a pinned one would make the
        # probe pass and every call then fail. Hermeticity here is about
        # git's CONFIG, not about where the binary lives.
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": AUTHOR_NAME,
        "GIT_AUTHOR_EMAIL": AUTHOR_EMAIL,
        "GIT_COMMITTER_NAME": AUTHOR_NAME,
        "GIT_COMMITTER_EMAIL": AUTHOR_EMAIL,
        # Some git paths still want a HOME even with both config files nulled;
        # the runtime user's own, never the volume.
        "HOME": os.environ.get("HOME", "/tmp"),
    }


def _gitignore_body(wrapper_prefix: str) -> str:
    # Every transient file a render drops beside a model: the colour wrappers and,
    # since #204, the uploaded assets staged for `import()` -- which are not .scad
    # -- and what the legacy queue kept under the template (`RENDERS_DIR_NAME`).
    transient = f"{wrapper_prefix}*" if wrapper_prefix else "*.scad"
    return (
        "# Written by ScadBuddy. Everything here is regenerated from the model\n"
        "# source, so versioning it would only add noise to the history.\n"
        f"{transient}\n"
        f"{RENDERS_DIR_NAME}/\n"
        f"{LOCK_NAME}\n"
        "# Template videos (#274) stay out of the history so the repository stays\n"
        "# small; their order in model.json is still committed. Images are kept.\n"
        "*/media/*.mp4\n"
        "*/media/*.webm\n"
        "_builtin/*/media/*.mp4\n"
        "_builtin/*/media/*.webm\n"
    )


class ModelHistory:
    """The git repository under ``data/models``.

    Sync on purpose: the methods shell out, so an ``async`` caller hands them to
    :func:`asyncio.to_thread` rather than this class pretending to be awaitable.
    """

    def __init__(
        self,
        root: Path,
        *,
        git: str = GIT,
        wrapper_prefix: str = "",
        timeout: float = DEFAULT_TIMEOUT,
        coordinator: RepoCoordinator | None = None,
    ) -> None:
        self.root = root
        self.git = git
        self.wrapper_prefix = wrapper_prefix
        self.timeout = timeout
        self._lock = threading.Lock()
        #: The write lock across replicas, and the HEAD each writer leaves behind
        #: (#2385). None (tests, a single process): the ``flock`` alone serialises,
        #: and nothing checks this process's view of HEAD against another's commit.
        self.coordinator = coordinator
        #: The cross-replica hold while this instance has the write lock.
        self._hold: RepoHold | None = None
        git_dir = root / ".git"
        #: What `revalidate_dirs` opens before every git call: where HEAD, the
        #: index, ``packed-refs`` and the branch refs are replaced by rename.
        self._git_dirs = (root, git_dir, git_dir / "refs", git_dir / "refs" / "heads")
        #: Called with each new commit and the templates it touched, after the write
        #: lock is released -- how the app publishes ``version.committed``. It must
        #: not raise; a failure is logged, never reported as a failed commit.
        self.on_commit: Callable[[str, list[str]], None] | None = None

    # ── plumbing ──────────────────────────────────────────────────────────────

    @property
    def available(self) -> bool:
        """A git binary exists and ``root`` is a repository."""
        return shutil.which(self.git) is not None and (self.root / ".git").is_dir()

    def _run(
        self,
        *args: str,
        check: bool = True,
        text: bool = True,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
        # `None` is the sentinel, so an explicit 0 is honoured (#2343).
        limit = self.timeout if timeout is None else timeout
        command = [
            self.git,
            "--no-pager",
            "-c",
            f"safe.directory={self.root}",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.autocrlf=false",
            *args,
        ]
        # Another replica's commit is visible to this call, not 60 s later (#2385).
        revalidate_dirs(*self._git_dirs)
        try:
            # A fixed argv with no shell: nothing here is interpolated from a request.
            completed = subprocess.run(
                command,
                cwd=self.root,
                env=env or git_env(),
                capture_output=True,
                text=text,
                check=False,
                timeout=limit,
            )
        except subprocess.TimeoutExpired as error:
            raise GitTimeoutError(f"git {args[0]} timed out after {limit:g}s") from error
        except OSError as error:
            raise GitUnavailableError(f"could not run {self.git!r}: {error}") from error
        if check and completed.returncode != 0:
            stderr = completed.stderr if text else completed.stderr.decode("utf-8", "replace")
            raise GitError(f"git {args[0]} failed: {stderr.strip()}", stderr)
        return completed

    def _out(self, *args: str, check: bool = True) -> str:
        completed = self._run(*args, check=check)
        assert isinstance(completed.stdout, str)
        return completed.stdout

    def _patch(self, *args: str) -> str:
        """A ``git diff`` for display. Git prints a file it does not take for binary
        byte for byte -- a PNG thumbnail with no NUL in it, which is all `_require_png`
        checks for -- so undecodable bytes are replaced, not the whole diff refused."""
        completed = self._run(*args, text=False)
        assert isinstance(completed.stdout, bytes)
        return completed.stdout.decode("utf-8", "replace")

    @contextmanager
    def _exclusive(self, *, adopt: bool = False) -> Iterator[None]:
        """One writer at a time, across threads, processes *and* replicas, on a
        current view of the repository.

        The thread lock alone would be enough for a single uvicorn worker; the
        ``flock`` is what keeps a second process (a shell on the volume) from
        interleaving an ``add``/``commit`` pair with ours. Across the API's replicas,
        which share the volume over NFS, the :attr:`coordinator`'s lock serialises
        too, and it carries the HEAD the last writer left: a lock alone is not
        enough there, because this replica's NFS client can go on serving ``.git``
        as it was before another's commit (#2385). So the HEAD git reports is
        waited for until it is that one (:meth:`_settle`) before anything is read
        for a check or written -- an edit's ``base`` (#813) is checked against the
        repository as it is, not as this replica last cached it.

        Every wait is bounded by :attr:`timeout`, for the reason
        :class:`GitTimeoutError` gives: a blocking ``flock`` behind a wedged
        holder would pin an executor slot for as long as that holder lasts.
        ``fcntl.flock`` takes no deadline of its own, so the non-blocking form is
        retried against one. ``adopt`` (boot only) takes the HEAD found as current
        when the recorded one never shows -- a volume restored to an older history.
        """
        deadline = time.monotonic() + self.timeout
        if not self._lock.acquire(timeout=self.timeout):
            raise GitTimeoutError(f"waited {self.timeout:g}s for the in-process write lock")
        try:
            held: AbstractContextManager[RepoHold | None] = (
                nullcontext() if self.coordinator is None else self.coordinator.hold(self.timeout)
            )
            with held as hold:
                lock_path = self.root / LOCK_NAME
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                with lock_path.open("w") as handle:
                    while True:
                        try:
                            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except BlockingIOError:
                            if time.monotonic() >= deadline:
                                raise GitTimeoutError(
                                    f"waited {self.timeout:g}s for {lock_path}"
                                ) from None
                            time.sleep(LOCK_POLL_SECONDS)
                    self._hold = hold
                    try:
                        self._settle(deadline, adopt=adopt)
                        yield
                    finally:
                        self._hold = None
                        fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            self._lock.release()

    def _settle(self, deadline: float, *, adopt: bool = False) -> None:
        """Under the write lock: wait until HEAD is the one the last writer recorded,
        then make the index HEAD's tree.

        A HEAD that descends from the recorded one is current too: a commit made on
        the volume by hand, outside the lock. The index is reset because it is a file
        of its own, as stale or as current as HEAD: a commit built from an old one
        would silently undo every change made since. ``read-tree --reset`` keeps the stat
        data of the entries that match, so nothing is hashed again; what a failed
        commit left staged is unstaged, and the next boot's recovery commits it.
        """
        if not (self.root / ".git").is_dir():
            return
        hold = self._hold
        expected = None if hold is None else hold.expected
        pause = LOCK_POLL_SECONDS
        while True:
            head = self.head()
            if expected is None or head == expected or self._descends(head, expected):
                break
            if time.monotonic() >= deadline:
                if not adopt:
                    raise GitTimeoutError(
                        f"the models repository here is at {head[:7] if head else 'no revision'},"
                        f" but another replica committed {expected[:7]}: still not visible"
                        f" after {self.timeout:g}s"
                    )
                logger.warning(
                    "the recorded HEAD of the models repository never showed; taking this one",
                    extra={"recorded": expected, "head": head},
                )
                if hold is not None and head is not None:
                    hold.record(head)
                break
            time.sleep(pause)
            pause = min(pause * 2, FRESH_POLL_MAX_SECONDS)
        if head is not None:
            self._run("read-tree", "--reset", "HEAD")

    def _descends(self, head: str | None, ancestor: str) -> bool:
        """Whether ``head`` is a later commit than ``ancestor``; False when either is
        not an object this view of the repository has."""
        if head is None:
            return False
        completed = self._run("merge-base", "--is-ancestor", ancestor, head, check=False)
        return completed.returncode == 0

    def write_lock(self) -> AbstractContextManager[None]:
        """The write lock every commit takes, held for a change that commits nothing:
        a built-in's added media (#722) is outside the repository, but its
        read-modify-write must not interleave with another replica's."""
        return self._exclusive()

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def ensure_repo(self) -> str | None:
        """Initialise the repository if it is not one yet, and commit what is there.

        Returns the commit id when this call created one, and ``None`` when there
        was nothing to commit OR when the repository could not be initialised at
        all -- the app has to boot either way. A models directory that already
        holds files becomes revision 1 rather than being left untracked, which is
        what makes the first built-in sync land on top of revision 1 on a fresh volume.

        On a repository that already has history, anything left to commit is a
        catalogue action whose own commit failed, and it is committed as
        ``RECOVERED_MESSAGE`` rather than being called the initial revision (#134).
        """
        if shutil.which(self.git) is None:
            logger.warning("git is not on PATH; model history is disabled", extra={"git": self.git})
            return None
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with self._exclusive(adopt=True):
                if not (self.root / ".git").is_dir():
                    self._run("init", f"--initial-branch={DEFAULT_BRANCH}", ".")
                # No commit yet, not "no .git": a crash between `init` and the first
                # commit leaves a repository with no history, and what the next boot
                # commits there is still the first revision.
                fresh = (
                    self._run("rev-parse", "--verify", "--quiet", "HEAD", check=False).returncode
                    != 0
                )
                gitignore = self.root / GITIGNORE_NAME
                body = _gitignore_body(self.wrapper_prefix)
                if not gitignore.is_file() or gitignore.read_text(encoding="utf-8") != body:
                    gitignore.write_text(body, encoding="utf-8")
                message = "Initial revision" if fresh else RECOVERED_MESSAGE
                return self._commit_locked(message, ".")
        except (GitError, OSError):
            # A missing binary is not the only way this fails, and the others are
            # the ones that would hurt: a models directory uid 10001 cannot write
            # (`safe.directory` answers git\'s ownership check, not the
            # filesystem\'s), a full disk, a half-written `.git` from a previous
            # crash. None of that is a reason to refuse to serve models, so it
            # degrades exactly like a missing binary does -- `available` reports
            # False and the history routes answer 503.
            logger.exception(
                "could not initialise the models repository; model history is disabled",
                extra={"models": str(self.root)},
            )
            return None

    def object_counts(self) -> dict[str, int]:
        """``git count-objects -v`` as numbers: ``count`` (loose), ``packs``, ``size-pack``…"""
        counts: dict[str, int] = {}
        for line in self._out("count-objects", "-v").splitlines():
            key, _, value = line.partition(":")
            if value.strip().isdigit():
                counts[key.strip()] = int(value)
        return counts

    def maintain(self) -> bool:
        """Pack the repository when it has piled up loose objects or packs (#2289).

        Every listing walks the whole history (`last_commits`), and each loose object
        is a file read on the volume. Not under the write lock: ``gc`` is safe beside
        a commit (it is what git runs after one), and holding the lock would stall
        every save for the length of a repack. True when it packed.
        """
        if not self.available:
            return False
        counts = self.object_counts()
        if (
            counts.get("count", 0) < MAINTAIN_LOOSE_OBJECTS
            and counts.get("packs", 0) < MAINTAIN_PACKS
        ):
            return False
        self._run("gc", "--quiet", timeout=MAINTAIN_TIMEOUT)
        return True

    # ── writing ───────────────────────────────────────────────────────────────

    def commit(
        self,
        message: str,
        *paths: str,
        prepare: Callable[[], None] | None = None,
        rollback: Callable[[], None] | None = None,
    ) -> str | None:
        """Stage ``paths`` and commit them. ``None`` when nothing actually changed.

        ``prepare`` runs under the write lock first, which makes a read-modify-write
        -- of a ``model.json``, say -- atomic with its commit. ``rollback`` runs under
        the same lock if the commit then fails, before the error is raised, so what
        ``prepare`` wrote can be undone before another write sees it.

        When HEAD turns out to have moved since the lock was taken
        (:class:`HeadMovedError`), a change that can be rolled back is: and then,
        once the repository has settled, ``prepare`` runs again, so whatever it
        checks -- an edit's ``base`` -- is checked against the new HEAD and refuses
        as it would have. A change that cannot be rolled back raises.
        """
        with self._exclusive():
            for attempt in range(1, COMMIT_ATTEMPTS + 1):
                if prepare is not None:
                    prepare()
                try:
                    created = self._commit_locked(message, *paths)
                    break
                except HeadMovedError:
                    if rollback is None or attempt == COMMIT_ATTEMPTS:
                        if rollback is not None:
                            rollback()
                        raise
                    rollback()
                    logger.warning("HEAD moved under the write lock; checking again")
                    self._settle(time.monotonic() + self.timeout)
                except (GitError, OSError):
                    if rollback is not None:
                        rollback()
                    raise
            touched = self._touched_by(created)
        self._announce(created, touched)
        return created

    def _commit_locked(self, message: str, *paths: str) -> str | None:
        targets = list(paths) or ["."]
        self._stage(targets)
        # The commit carries no pathspec: exactly what was just staged is what is
        # committed, and a pathspec would fail for the same reason `add` can (see
        # `_stage`) on the one action that most needs to work -- delete.
        if not self._has_staged():
            return None
        parent = self.head()
        args = ["commit", "--no-verify", "-m", subject_line(message)]
        env: dict[str, str] | None = None
        author = current_author()
        if author is not None:
            # Authored as the agent, committed as ScadBuddy, with who and where as
            # trailers (#252, core/authorship.py). Each value was checked to be one
            # printable token, so each trailer is exactly one line.
            trailers = [
                f"{name}: {value}"
                for name, value in (
                    (PRINCIPAL_TRAILER, author.principal),
                    (SESSION_TRAILER, author.session),
                )
                if value
            ]
            args += ["-m", "\n".join(trailers)]
            env = {
                **git_env(),
                "GIT_AUTHOR_NAME": AGENT_AUTHOR_NAME,
                "GIT_AUTHOR_EMAIL": AGENT_AUTHOR_EMAIL,
            }
        self._commit_with_retry(args, env, parent)
        return self._landed(parent)

    def _commit_with_retry(
        self, args: list[str], env: dict[str, str] | None, parent: str | None
    ) -> None:
        """``git commit``, again after a pause when git could not lock HEAD because it
        read an older HEAD than the one it found under its lock (#2385). That is a
        stale read, not a race: the write lock is held. If HEAD really has moved
        from ``parent`` meanwhile, :class:`HeadMovedError`."""
        pause = COMMIT_RETRY_SECONDS
        for attempt in range(1, COMMIT_ATTEMPTS + 1):
            try:
                self._run(*args, env=env)
                return
            except GitError as error:
                if REF_LOCK_ERROR not in error.stderr or attempt == COMMIT_ATTEMPTS:
                    raise
                logger.warning(
                    "git could not lock HEAD; trying the commit again", extra={"attempt": attempt}
                )
            time.sleep(pause)
            pause *= 2
            head = self.head()
            if head != parent:
                raise HeadMovedError(
                    f"HEAD moved from {parent[:7] if parent else 'no revision'}"
                    f" to {head[:7] if head else 'no revision'} under the write lock"
                )

    def _landed(self, parent: str | None) -> str:
        """The commit just made, once HEAD is shown to be a new commit on ``parent``,
        and recorded as HEAD for the other replicas. A commit git reported but HEAD
        does not show is an error, never a revision answered as made."""
        created = self.head()
        if created is None or created == parent or self._parent_of(created) != parent:
            raise GitError(
                f"git commit succeeded, but HEAD is {created[:7] if created else 'no revision'},"
                f" not a new commit on {parent[:7] if parent else 'no revision'}"
            )
        if self._hold is not None:
            self._hold.record(created)
        return created

    def _touched_by(self, commit: str | None) -> list[str]:
        """The templates ``commit`` changed, when anyone is listening for it."""
        if commit is None or self.on_commit is None:
            return []
        try:
            names = self._out(
                "diff-tree", "--no-commit-id", "--name-only", "-r", "--root", commit
            ).splitlines()
        except GitError:
            logger.exception("could not list what a commit touched", extra={"commit": commit})
            return []
        touched: list[str] = []
        for name in names:
            model_id = _model_id(name.strip())
            if model_id is not None and model_id not in touched:
                touched.append(model_id)
        return touched

    def _announce(self, commit: str | None, touched: list[str]) -> None:
        if commit is None or self.on_commit is None:
            return
        try:
            self.on_commit(commit, touched)
        except Exception:
            logger.exception("a commit listener failed", extra={"commit": commit})

    def _stage(self, targets: list[str]) -> None:
        """``git add -A`` over the paths an action touched.

        A pathspec that matches neither the worktree nor the index is fatal to
        ``git add``, and that is a legitimate state here: deleting a model that
        predates the repository leaves a slug that is neither tracked nor on disk.
        Every other failure is real and propagates.
        """
        staged = self._run("add", "-A", "--", *targets, check=False)
        if staged.returncode == 0:
            return
        stderr = staged.stderr if isinstance(staged.stderr, str) else ""
        if "did not match any files" in stderr:
            return
        raise GitError(f"git add failed: {stderr.strip()}", stderr)

    def _has_staged(self) -> bool:
        """Is there anything to commit? An unborn branch has no HEAD to diff against."""
        if self.head() is None:
            return bool(self._out("ls-files", "--cached").strip())
        return self._run("diff", "--cached", "--quiet", check=False).returncode != 0

    def restore(self, slug: str, commit: str) -> str:
        """Put ``slug`` back as it was at ``commit``, as a new commit. Never a rewrite."""
        resolved = self.resolve(commit)
        with self._exclusive():
            present = set(self._files_at(resolved, slug))
            if not present:
                raise RevisionNotFoundError(f"{slug!r} does not exist at {commit}")
            self._run("checkout", resolved, "--", slug)
            # Whatever the model has gained since then has to go, or "restore" would
            # only ever be a merge of the two revisions.
            for path in self._tracked(slug):
                if path not in present:
                    (self.root / path).unlink(missing_ok=True)
            created = self._commit_locked(f"Restore {slug} to {resolved[:7]}", slug)
            touched = self._touched_by(created)
        self._announce(created, touched)
        if created is None:
            # Already identical: the caller still wants a revision id to point
            # at, and it is this model's own, not the repository HEAD -- which
            # may be a commit against a different model entirely.
            return self.last_commit(slug) or resolved
        return created

    # ── reading ───────────────────────────────────────────────────────────────

    def head(self) -> str | None:
        completed = self._run("rev-parse", "HEAD", check=False)
        assert isinstance(completed.stdout, str)
        return completed.stdout.strip() or None if completed.returncode == 0 else None

    def resolve(self, commit: str) -> str:
        """Full commit id for a revision, or :class:`RevisionNotFoundError`."""
        completed = self._run(
            "rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}", check=False
        )
        assert isinstance(completed.stdout, str)
        resolved = completed.stdout.strip()
        if completed.returncode != 0 or not resolved:
            raise RevisionNotFoundError(commit)
        return resolved

    def touched(self, commit: str, path: str) -> bool:
        """Whether ``commit`` (a full id, as :meth:`resolve` answers) changed ``path``."""
        completed = self._run("log", "-1", "--format=%H", commit, "--", path, check=False)
        assert isinstance(completed.stdout, str)
        return completed.returncode == 0 and completed.stdout.strip() == commit

    def tree(self, commit: str, path: str) -> str | None:
        """The object id of ``path`` at ``commit`` (a directory's is its tree id);
        ``None`` where it is absent."""
        completed = self._run("rev-parse", "--verify", "--quiet", f"{commit}:{path}", check=False)
        assert isinstance(completed.stdout, str)
        return completed.stdout.strip() or None if completed.returncode == 0 else None

    def clean(self, path: str) -> bool:
        """Whether the working tree under ``path`` is as HEAD has it: nothing edited,
        staged, deleted or untracked."""
        completed = self._run(
            "status", "--porcelain", "--untracked-files=all", "--", path, check=False
        )
        assert isinstance(completed.stdout, str)
        return completed.returncode == 0 and not completed.stdout.strip()

    def last_commit(self, slug: str) -> str | None:
        """The revision a model is currently at: the last commit that touched it.

        Not the repository HEAD — a commit against another model leaves this one
        at the same revision, and an output's ``model_version`` has to name an
        entry in *this* model's history.
        """
        completed = self._run("log", "-1", "--format=%H", "--", slug, check=False)
        assert isinstance(completed.stdout, str)
        if completed.returncode != 0:
            return None
        return completed.stdout.strip() or None

    def last_two_commits(self, slug: str) -> tuple[str | None, str | None]:
        """:meth:`last_commit` and the commit before it in the model's own history
        (#1071: what its current revision was made from; None for its first commit),
        from ONE ``git log`` (#2158): two runs could straddle a commit to the model, and
        then name a "previous" that is not the current revision's parent."""
        completed = self._run("log", "-2", "--format=%H", "--", slug, check=False)
        assert isinstance(completed.stdout, str)
        if completed.returncode != 0:
            return None, None
        commits = completed.stdout.split()
        return (
            commits[0] if commits else None,
            commits[1] if len(commits) > 1 else None,
        )

    def seed_commit(self, slug: str) -> str | None:
        """The ``Seed … from the image`` commit ``slug`` as it stands was copied in by.

        Walks the model's own history newest first. ``None`` when the model was
        made some other way -- uploaded, duplicated, or recreated after a delete --
        since an older seed of the same slug is then not its origin. The subject
        alone is not trusted: any commit can carry it, so the seed must also be
        authored and committed as ScadBuddy.
        """
        log = self._out("log", "--format=%H%x1f%an%x1f%ae%x1f%cn%x1f%ce%x1f%s", "--", slug)
        ours = [AUTHOR_NAME, AUTHOR_EMAIL, AUTHOR_NAME, AUTHOR_EMAIL]
        for line in log.splitlines():
            commit, *identity, subject = line.split(_FIELD, 5)
            if _SEED_SUBJECT.match(subject) and identity == ours:
                return commit
            if subject in (f"Add {slug}", f"Delete {slug}") or subject.endswith(f" as {slug}"):
                return None
        return None

    def blobs(self, commit: str, path: str) -> dict[str, str]:
        """Each file under ``path`` at ``commit``, relative to ``path``, to its blob id."""
        blobs: dict[str, str] = {}
        for line in self._out("ls-tree", "-r", commit, "--", f"{path}/").splitlines():
            meta, _, name = line.partition("\t")
            blobs[name.removeprefix(f"{path}/")] = meta.split()[2]
        return blobs

    def last_commits(self) -> dict[str, str]:
        """Every model's current revision, from ONE walk of the history.

        `last_commit` per model turns a catalogue listing into N forks, and each
        one walks the shared history until it hits a path match -- so the cost
        per model grows with every OTHER model's commits too. One `git log` with
        `--name-only`, newest first, answers the whole page: the first time a
        slug appears is by definition its newest commit.
        """
        completed = self._run("log", "--format=%x1e%H", "--name-only", check=False)
        assert isinstance(completed.stdout, str)
        if completed.returncode != 0:
            return {}
        newest: dict[str, str] = {}
        for record in completed.stdout.split(_RECORD):
            commit, _, paths = record.strip().partition("\n")
            if not commit:
                continue
            for path in paths.splitlines():
                model_id = _model_id(path)
                if model_id:
                    newest.setdefault(model_id, commit)
        return newest

    def object_ids_in(self, *pathspecs: str) -> set[str]:
        """Every full object id (40 or 64 hex digits) written into any version of
        the files ``pathspecs`` match, on any ref, deleted files included.

        ONE ``git log -p`` over the whole history, not a read per revision. Each
        value a file ever held appears on an added line of some diff, so scanning
        every line of every diff finds them all; it cannot tell a library pin from
        any other id in those files, and so errs towards finding too many.
        """
        if self.head() is None:
            return set()
        text = self._patch(
            "log",
            "--all",
            "-p",
            "-m",
            "--format=",
            "--no-color",
            "--no-ext-diff",
            "--unified=0",
            "--",
            *pathspecs,
        )
        return set(_OBJECT_ID.findall(text))

    def log(self, slug: str | None = None, *, limit: int = DEFAULT_LOG_LIMIT) -> list[Revision]:
        args = ["log", f"--max-count={limit}", _LOG_FORMAT, "--name-status", "--no-renames"]
        if slug is not None:
            args += ["--", slug]
        completed = self._run(*args, check=False)
        assert isinstance(completed.stdout, str)
        if completed.returncode != 0:
            # An empty repository has no HEAD to walk; that is "no revisions yet".
            return []
        return _parse_log(completed.stdout)

    def show(self, commit: str, path: str) -> bytes:
        """One file's bytes at a revision."""
        resolved = self.resolve(commit)
        completed = self._run("show", f"{resolved}:{path}", check=False, text=False)
        if completed.returncode != 0:
            raise RevisionNotFoundError(f"{path!r} does not exist at {commit}")
        assert isinstance(completed.stdout, bytes)
        return completed.stdout

    def read_blob(self, commit: str, path: str, *, limit: int, root: str | None = None) -> bytes:
        """The bytes of the file ``path`` at a revision, at most ``limit`` of them.

        The revision is resolved once; the tree entry gives the blob's id and size, so
        one over ``limit`` is refused (:class:`BlobTooLargeError`) without reading it,
        and the bytes read are that blob's by id, the one that was sized.

        A symlink (mode 120000, a blob holding its target's path), at the end of the
        path or as a directory along it, is followed only with a ``root``, and only
        while each target, relative and resolved against the link's directory, stays
        under it -- as the working tree's reader follows a link inside a model's
        directory and refuses one out of it. Without a root it is no file.
        :class:`RevisionNotFoundError` when no regular file is there (a directory, a
        symlink that leaves ``root`` or loops included); :class:`GitError` when git
        cannot read the tree.
        """
        resolved = self.resolve(commit)
        missing = RevisionNotFoundError(f"{path!r} is not a file at {commit}")
        current = path
        for _ in range(MAX_SYMLINK_HOPS + 1):
            segments = current.split("/")
            prefixes = ["/".join(segments[: i + 1]) for i in range(len(segments))]
            # Every prefix in one tree read: a plain file costs one, a missing one too.
            entries = self._tree_entries(resolved, prefixes)
            for index, prefix in enumerate(prefixes):
                entry = entries.get(prefix)
                if entry is None:
                    raise missing
                mode, oid, size = entry
                if mode == SYMLINK_MODE:
                    if root is None or size > MAX_PATH_BYTES:
                        raise missing
                    target = self._blob(oid).decode("utf-8", "replace")
                    followed = _follow(prefix, target, segments[index + 1 :], root)
                    if followed is None:
                        raise missing
                    current = followed
                    break
                if index < len(prefixes) - 1:
                    if mode != TREE_MODE:
                        raise missing
                elif mode in FILE_MODES:
                    if size > limit:
                        raise BlobTooLargeError(f"{path!r} at {commit} is over {limit} bytes")
                    return self._blob(oid)
                else:
                    raise missing
        raise missing

    def _tree_entries(self, resolved: str, paths: list[str]) -> dict[str, tuple[str, str, int]]:
        """Each of ``paths`` that exists at ``resolved``, as its mode, object id and size
        (0 for a directory), from one ``ls-tree``. :class:`GitError` when git cannot read
        the tree."""
        listing = self._out("ls-tree", "-z", "-l", "-t", resolved, "--", *paths)
        entries: dict[str, tuple[str, str, int]] = {}
        # `<mode> <type> <object> <size>\t<path>\0`, size `-` for a tree; `-t` lists a
        # directory itself as well as what is under it on the way to a deeper path.
        for record in listing.split("\0"):
            meta, _, name = record.partition("\t")
            fields = meta.split()
            if len(fields) == 4 and fields[0] in (*FILE_MODES, SYMLINK_MODE, TREE_MODE):
                size = int(fields[3]) if fields[3].isdigit() else 0
                entries[name] = (fields[0], fields[2], size)
        return entries

    def _blob(self, oid: str) -> bytes:
        completed = self._run("cat-file", "blob", oid, text=False)
        assert isinstance(completed.stdout, bytes)
        return completed.stdout

    def parent(self, commit: str) -> str | None:
        return self._parent_of(self.resolve(commit))

    def _parent_of(self, resolved: str) -> str | None:
        """As :meth:`parent`, for a caller that already holds a resolved id."""
        completed = self._run("rev-parse", "--verify", "--quiet", f"{resolved}^", check=False)
        assert isinstance(completed.stdout, str)
        return completed.stdout.strip() or None if completed.returncode == 0 else None

    def revision_range(self, base: str | None, head: str) -> RevisionRange:
        """Resolve a diff's two endpoints, once.

        ``base`` of ``None`` means the revision's parent -- or the empty tree when
        it is the root commit, which has none. Both endpoints come back resolved,
        so a caller that wants the patch *and* the file list (and to name the base
        it actually used) pays for the resolution a single time: the pair is what
        :meth:`diff` and :meth:`diff_files` take.
        """
        resolved_head = self.resolve(head)
        resolved_base = (
            self.resolve(base) if base else (self._parent_of(resolved_head) or EMPTY_TREE)
        )
        return RevisionRange(resolved_base, resolved_head)

    def diff(self, revisions: RevisionRange, slug: str | None = None) -> str:
        """A unified patch across ``revisions``, optionally scoped to one model.

        A built-in's headers name ``<slug>/...``, as a template of mine's do: the
        ``_builtin/`` mirror is where it is stored, not a path the API reports.
        """
        args = ["diff", "--no-color"]
        if slug is not None and slug.startswith(f"{BUILTIN_DIR}/"):
            args.append(f"--relative={BUILTIN_DIR}/")
        args += [revisions.base, revisions.head]
        if slug is not None:
            args += ["--", slug]
        return self._patch(*args)

    def diff_dirs(
        self,
        base: str | None,
        base_path: str,
        head: str,
        head_path: str,
        *,
        label: str,
        exclude: tuple[str, ...] = (),
    ) -> str:
        """A unified patch from ``base_path`` at ``base`` to ``head_path`` at ``head``.

        The two directories are compared as trees, so a directory that moved between
        the revisions diffs file by file rather than as one removed and one added.
        Headers name ``<label>/...``; a side that does not exist is the empty tree.
        """
        args = ["diff", "--no-color", f"--src-prefix=a/{label}/", f"--dst-prefix=b/{label}/"]
        args += [self._tree(base, base_path), self._tree(head, head_path)]
        if exclude:
            args += ["--", ".", *(f":(exclude){name}" for name in exclude)]
        return self._patch(*args)

    def _tree(self, commit: str | None, path: str) -> str:
        if commit is None:
            return EMPTY_TREE
        completed = self._run("rev-parse", "--verify", "--quiet", f"{commit}:{path}", check=False)
        assert isinstance(completed.stdout, str)
        return completed.stdout.strip() if completed.returncode == 0 else EMPTY_TREE

    def diff_files(self, revisions: RevisionRange, slug: str | None = None) -> list[FileChange]:
        """The ``--name-status`` summary for the same pair :meth:`diff` patches."""
        args = ["diff", "--name-status", "--no-renames", revisions.base, revisions.head]
        if slug is not None:
            args += ["--", slug]
        return _parse_name_status(self._out(*args))

    def export(self, slug: str, commit: str, dest: Path) -> None:
        """Write ``slug``'s tree at ``commit`` into ``dest`` (the slug prefix stripped).

        Used to render an old revision without restoring it: the destination is an
        ordinary model directory, so the schema cache and the renderer work on it
        unchanged.
        """
        resolved = self.resolve(commit)
        completed = self._run("archive", "--format=tar", resolved, slug, check=False, text=False)
        if completed.returncode != 0:
            raise RevisionNotFoundError(f"{slug!r} does not exist at {commit}")
        assert isinstance(completed.stdout, bytes)
        dest.mkdir(parents=True, exist_ok=True)
        prefix = f"{slug}/"
        with tarfile.open(fileobj=io.BytesIO(completed.stdout)) as archive:
            members = [
                _reparent(member, prefix)
                for member in archive.getmembers()
                if member.name.startswith(prefix) and member.name != prefix
            ]
            archive.extractall(dest, members=members, filter="data")

    def files_at(self, commit: str, path: str) -> list[str]:
        """The files under ``path`` at ``commit``, relative to ``path``."""
        prefix = f"{path}/"
        return [name[len(prefix) :] for name in self._files_at(commit, path)]

    def merge_file(
        self, ours: str, base: str, theirs: str, *, labels: tuple[str, str, str]
    ) -> tuple[str, int]:
        """``git merge-file -p --diff3`` over three texts: the result and its conflict count.

        Needs no repository -- the three sides are temporary files -- but runs through
        the same hermetic invocation as everything else here.
        """
        with tempfile.TemporaryDirectory(prefix="scadbuddy-merge-") as scratch:
            sides = []
            for name, text in (("ours", ours), ("base", base), ("theirs", theirs)):
                side = Path(scratch) / name
                side.write_bytes(text.encode())
                sides.append(str(side))
            label_args = [arg for label in labels for arg in ("-L", label)]
            # Bytes both ways: text mode would turn a CRLF result into LF.
            completed = self._run(
                "merge-file", "-p", "--diff3", *label_args, *sides, check=False, text=False
            )
        assert isinstance(completed.stdout, bytes)
        # The exit status is the number of conflicts (capped at 127); a negative
        # one -- 255 as an unsigned status -- is a failure to merge at all.
        if completed.returncode < 0 or completed.returncode > 127:
            stderr = completed.stderr.decode("utf-8", "replace")
            raise GitError(f"git merge-file failed: {stderr.strip()}", stderr)
        return completed.stdout.decode(), completed.returncode

    def _files_at(self, commit: str, slug: str) -> list[str]:
        return [
            line
            for line in self._out("ls-tree", "-r", "--name-only", commit, "--", slug).splitlines()
            if line
        ]

    def _tracked(self, slug: str) -> list[str]:
        return [line for line in self._out("ls-files", "--", slug).splitlines() if line]


def _follow(link: str, target: str, rest: list[str], root: str) -> str | None:
    """The path a symlink at ``link`` leads to, with ``rest`` after it, or None when
    its target is absolute, leaves ``root`` or names a hidden segment under it (a
    dot-file or dot-directory, as no plain path may)."""
    if target.startswith("/"):
        return None
    followed = posixpath.normpath(posixpath.join(posixpath.dirname(link), target))
    if not followed.startswith(f"{root}/"):
        return None
    if any(part.startswith(".") for part in followed[len(root) + 1 :].split("/")):
        return None
    return "/".join([followed, *rest])


def _model_id(path: str) -> str | None:
    """The template a repository path belongs to: its first component, or
    ``builtin:<second>`` under the mirror. ``None`` for a file at the root."""
    parts = path.split("/")
    if parts[0] == BUILTIN_DIR:
        return f"{BUILTIN_PREFIX}{parts[1]}" if len(parts) > 2 else None
    return parts[0] if len(parts) > 1 else None


def _reparent(member: tarfile.TarInfo, prefix: str) -> tarfile.TarInfo:
    member = member.replace(name=member.name[len(prefix) :], deep=False)
    return member


def _parse_name_status(text: str) -> list[FileChange]:
    changes: list[FileChange] = []
    for line in text.splitlines():
        status, _, path = line.partition("\t")
        if status and path:
            changes.append(FileChange(status=status.strip(), path=path.strip()))
    return changes


def _parse_log(text: str) -> list[Revision]:
    """One record per ``\x1e``; the last field carries the subject and the name-status
    block, which ``--name-status`` appends after the formatted line."""
    revisions: list[Revision] = []
    for record in text.split(_RECORD):
        if not record.strip():
            continue
        fields = record.split(_FIELD)
        if len(fields) != 6:  # pragma: no cover - git always emits all six
            continue
        commit, author, date, principal, session, tail = fields
        subject, _, status_block = tail.partition("\n")
        revisions.append(
            Revision(
                commit=commit.strip(),
                author=author,
                date=datetime.fromisoformat(date),
                message=subject,
                files=_parse_name_status(status_block),
                agent=AgentAuthor(principal=principal or None, session=session or None)
                if principal or session
                else None,
            )
        )
    return revisions


def _is_control(character: str) -> bool:
    """C0, DEL and C1: none of them belongs in a one-line subject."""
    return character < " " or "\x7f" <= character <= "\x9f"


def subject_line(message: str) -> str:
    """Flatten a commit message to one printable line.

    `_parse_log` splits `git log` output on ASCII RS/US, which nothing can emit
    in a hash, an author or a date -- but `%s` is the SUBJECT, and the subject is
    caller-supplied text (`PUT /models/{slug}/source` takes a `message`). One RS
    or US in it would desync every later record boundary and silently drop the
    malformed chunks, so a garbled history would read as a shorter one with no
    error anywhere. Control bytes are dropped rather than escaped: this is a
    one-line summary, and a commit message is not a place to smuggle bytes
    through. A blank result would make `git commit` fail, so it falls back.
    """
    flattened = "".join(" " if _is_control(character) else character for character in message)
    collapsed = " ".join(flattened.split())
    return collapsed[:MAX_SUBJECT] if collapsed else "(no message)"
