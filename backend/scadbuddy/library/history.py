"""Model history: ``data/models`` is a git repository, and git is the version store.

Every catalogue action — create, source edit, metadata change, delete, seed,
restore — is exactly one commit. Nothing here keeps a parallel index of
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
import shutil
import subprocess
import tarfile
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

GIT = "git"
LOCK_NAME = ".scadbuddy-git.lock"
GITIGNORE_NAME = ".gitignore"
DEFAULT_LOG_LIMIT = 50
# Generous for a local repository, small enough that a stalled PVC cannot hold an
# executor slot for minutes. Overridable with ``SCADBUDDY_GIT_TIMEOUT``.
DEFAULT_TIMEOUT = 30.0
LOCK_POLL_SECONDS = 0.05
# git itself imposes no subject limit; this one keeps a listed revision readable
# and bounds what a caller can write into the history.
MAX_SUBJECT = 200

AUTHOR_NAME = "ScadBuddy"
AUTHOR_EMAIL = "scadbuddy@localhost"

# The initial branch is named rather than inherited: `init.defaultBranch` lives in
# the global config this module refuses to read, so without it git picks its
# compiled-in default and warns on every init.
DEFAULT_BRANCH = "main"

COMMIT_ID_PATTERN = r"^[0-9a-f]{7,40}$"

# git's own hash of the empty tree: what a root commit's diff is taken against,
# since it has no parent to compare with.
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

# ASCII record/unit separators, spelled as git's own `%xNN` escapes so the argv
# stays plain ASCII — an embedded NUL cannot be passed to execve at all — and
# neither byte can occur inside a commit field.
_RECORD = "\x1e"
_FIELD = "\x1f"
_LOG_FORMAT = "--format=format:%x1e%H%x1f%an%x1f%aI%x1f%s"


class GitError(RuntimeError):
    """A git invocation failed. ``stderr`` carries what it said."""

    def __init__(self, message: str, stderr: str = "") -> None:
        super().__init__(message)
        self.stderr = stderr


class GitUnavailableError(GitError):
    """No usable git repository — either no binary, or ``init`` never succeeded."""


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


class RevisionNotFoundError(KeyError):
    pass


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

    @property
    def short(self) -> str:
        return self.commit[:7]


def _gitignore_body(wrapper_prefix: str) -> str:
    return (
        "# Written by ScadBuddy. Everything here is regenerated from the model\n"
        "# source, so versioning it would only add noise to the history.\n"
        f"{wrapper_prefix}*.scad\n"
        f"{LOCK_NAME}\n"
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
    ) -> None:
        self.root = root
        self.git = git
        self.wrapper_prefix = wrapper_prefix
        self.timeout = timeout
        self._lock = threading.Lock()

    # ── plumbing ──────────────────────────────────────────────────────────────

    @property
    def available(self) -> bool:
        """A git binary exists and ``root`` is a repository."""
        return shutil.which(self.git) is not None and (self.root / ".git").is_dir()

    def _env(self) -> dict[str, str]:
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

    def _run(
        self,
        *args: str,
        check: bool = True,
        text: bool = True,
    ) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
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
        try:
            # A fixed argv with no shell: nothing here is interpolated from a request.
            completed = subprocess.run(
                command,
                cwd=self.root,
                env=self._env(),
                capture_output=True,
                text=text,
                check=False,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as error:
            raise GitTimeoutError(f"git {args[0]} timed out after {self.timeout:g}s") from error
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

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        """One writer at a time, across threads *and* processes.

        The thread lock alone would be enough for a single uvicorn worker; the
        ``flock`` is what keeps a second process (a shell on the volume, a future
        worker) from interleaving an ``add``/``commit`` pair with ours.

        Both waits are bounded by :attr:`timeout`, for the reason
        :class:`GitTimeoutError` gives: a blocking ``flock`` behind a wedged
        holder would pin an executor slot for as long as that holder lasts.
        ``fcntl.flock`` takes no deadline of its own, so the non-blocking form is
        retried against one.
        """
        deadline = time.monotonic() + self.timeout
        if not self._lock.acquire(timeout=self.timeout):
            raise GitTimeoutError(f"waited {self.timeout:g}s for the in-process write lock")
        try:
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
                try:
                    yield
                finally:
                    fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            self._lock.release()

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def ensure_repo(self) -> str | None:
        """Initialise the repository if it is not one yet, and commit what is there.

        Returns the commit id when this call created one, and ``None`` when there
        was nothing to commit OR when the repository could not be initialised at
        all -- the app has to boot either way. A models directory that already
        holds files becomes revision 1 rather than being left untracked, which is
        what makes the image's seed the first revision on a fresh volume.
        """
        if shutil.which(self.git) is None:
            logger.warning("git is not on PATH; model history is disabled", extra={"git": self.git})
            return None
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with self._exclusive():
                fresh = not (self.root / ".git").is_dir()
                if fresh:
                    self._run("init", f"--initial-branch={DEFAULT_BRANCH}", ".")
                gitignore = self.root / GITIGNORE_NAME
                body = _gitignore_body(self.wrapper_prefix)
                if not gitignore.is_file() or gitignore.read_text(encoding="utf-8") != body:
                    gitignore.write_text(body, encoding="utf-8")
                # On an existing repository anything left to commit is a catalogue
                # action whose own commit failed (it logs and carries on), picked up
                # at the next boot -- not the first revision, so not called one.
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

    # ── writing ───────────────────────────────────────────────────────────────

    def commit(self, message: str, *paths: str) -> str | None:
        """Stage ``paths`` and commit them. ``None`` when nothing actually changed."""
        with self._exclusive():
            return self._commit_locked(message, *paths)

    def _commit_locked(self, message: str, *paths: str) -> str | None:
        targets = list(paths) or ["."]
        self._stage(targets)
        # The commit carries no pathspec: exactly what was just staged is what is
        # committed, and a pathspec would fail for the same reason `add` can (see
        # `_stage`) on the one action that most needs to work -- delete.
        if not self._has_staged():
            return None
        self._run("commit", "--no-verify", "-m", subject_line(message))
        return self.head()

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
                slug = path.split("/", 1)[0]
                if slug and "/" in path:
                    newest.setdefault(slug, commit)
        return newest

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
        """A unified patch across ``revisions``, optionally scoped to one model."""
        args = ["diff", "--no-color", revisions.base, revisions.head]
        if slug is not None:
            args += ["--", slug]
        return self._out(*args)

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

    def _files_at(self, commit: str, slug: str) -> list[str]:
        return [
            line
            for line in self._out("ls-tree", "-r", "--name-only", commit, "--", slug).splitlines()
            if line
        ]

    def _tracked(self, slug: str) -> list[str]:
        return [line for line in self._out("ls-files", "--", slug).splitlines() if line]


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
        if len(fields) != 4:  # pragma: no cover - git always emits all four
            continue
        commit, author, date, tail = fields
        subject, _, status_block = tail.partition("\n")
        revisions.append(
            Revision(
                commit=commit.strip(),
                author=author,
                date=datetime.fromisoformat(date),
                message=subject,
                files=_parse_name_status(status_block),
            )
        )
    return revisions


#: The commit an existing repository's boot makes of changes a failed commit left.
RECOVERED_MESSAGE = "Recover uncommitted changes"


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


def summarise(slugs: Sequence[str]) -> str:
    """``a, b and c`` — for a seed commit's message."""
    names = list(slugs)
    if len(names) == 1:
        return names[0]
    return f"{', '.join(names[:-1])} and {names[-1]}"
