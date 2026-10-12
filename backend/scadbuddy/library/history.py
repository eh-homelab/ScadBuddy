"""Model history: ``data/models`` is a git repository, and git is the version store.

Every catalogue action — create, source edit, metadata change, delete, built-in
sync, restore — is exactly one commit. Nothing here keeps a parallel index of
revisions: ``git log``, ``git show`` and ``git diff`` are the read side, so an
operator with a shell on the volume sees precisely what the API serves.

How ScadBuddy talks to git -- why it shells out to ``git`` rather than dulwich or
pygit2, and how every call is kept hermetic -- is ``core/gitstore.py``, which the
flow scripts' repository (#2503) shares. What is here is the model-specific part: the
layout (a template per directory, built-ins under ``_builtin/``), the ``.gitignore``
of render byproducts, the commit listener, and the catalogue's own reads.
"""

from __future__ import annotations

import io
import logging
import posixpath
import re
import tarfile
import tempfile
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path

from scadbuddy.core.gitstore import _FIELD, _RECORD, AUTHOR_EMAIL, AUTHOR_NAME, GitRepository
from scadbuddy.core.gitstore import COMMIT_ID_PATTERN as COMMIT_ID_PATTERN
from scadbuddy.core.gitstore import DEFAULT_BRANCH as DEFAULT_BRANCH
from scadbuddy.core.gitstore import DEFAULT_LOG_LIMIT as DEFAULT_LOG_LIMIT
from scadbuddy.core.gitstore import DEFAULT_TIMEOUT as DEFAULT_TIMEOUT
from scadbuddy.core.gitstore import EMPTY_TREE as EMPTY_TREE
from scadbuddy.core.gitstore import GIT as GIT
from scadbuddy.core.gitstore import GITIGNORE_NAME as GITIGNORE_NAME
from scadbuddy.core.gitstore import LOCK_NAME as LOCK_NAME
from scadbuddy.core.gitstore import MAINTAIN_LOOSE_OBJECTS as MAINTAIN_LOOSE_OBJECTS
from scadbuddy.core.gitstore import MAINTAIN_PACKS as MAINTAIN_PACKS
from scadbuddy.core.gitstore import MAINTAIN_TIMEOUT as MAINTAIN_TIMEOUT
from scadbuddy.core.gitstore import MAX_SUBJECT as MAX_SUBJECT
from scadbuddy.core.gitstore import RECOVERED_MESSAGE as RECOVERED_MESSAGE
from scadbuddy.core.gitstore import FileChange as FileChange
from scadbuddy.core.gitstore import GitError as GitError
from scadbuddy.core.gitstore import GitTimeoutError as GitTimeoutError
from scadbuddy.core.gitstore import GitUnavailableError as GitUnavailableError
from scadbuddy.core.gitstore import Revision as Revision
from scadbuddy.core.gitstore import RevisionNotFoundError as RevisionNotFoundError
from scadbuddy.core.gitstore import RevisionRange as RevisionRange
from scadbuddy.core.gitstore import git_env as git_env
from scadbuddy.core.gitstore import subject_line as subject_line
from scadbuddy.core.paths import BUILTIN_DIR, BUILTIN_PREFIX, RENDERS_DIR_NAME

logger = logging.getLogger(__name__)

#: The subject the pre-#155 copy-if-absent seed committed a bundled model under:
#: ``Seed name-keychain and tag from the image``.
_SEED_SUBJECT = re.compile(r"^Seed .+ from the image$")
#: A full SHA-1 or SHA-256 object id, on its own (not part of a longer hex run).
_OBJECT_ID = re.compile(r"(?<![0-9a-f])(?:[0-9a-f]{64}|[0-9a-f]{40})(?![0-9a-f])")

#: Git's modes for a regular file and for a symlink in a tree entry.
FILE_MODES = ("100644", "100755")
SYMLINK_MODE = "120000"
TREE_MODE = "040000"
#: As the kernel's own limit on symlinks followed in one lookup (`ELOOP`).
MAX_SYMLINK_HOPS = 40
#: The longest symlink target read: Linux's PATH_MAX.
MAX_PATH_BYTES = 4096


class BlobTooLargeError(ValueError):
    """A file at a revision is larger than the caller will read."""


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


class ModelHistory(GitRepository):
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
        super().__init__(root, git=git, timeout=timeout)
        self.wrapper_prefix = wrapper_prefix
        #: Called with each new commit and the templates it touched, after the write
        #: lock is released -- how the app publishes ``version.committed``. It must
        #: not raise; a failure is logged, never reported as a failed commit.
        self.on_commit: Callable[[str, list[str]], None] | None = None

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
        return self._ensure(_gitignore_body(self.wrapper_prefix), "model history")

    def maintain(self, *, loose_objects: int | None = None, packs: int | None = None) -> bool:
        """Pack the repository when it has piled up loose objects or packs (#2289):
        :meth:`GitRepository.maintain` at this module's thresholds."""
        return super().maintain(
            loose_objects=MAINTAIN_LOOSE_OBJECTS if loose_objects is None else loose_objects,
            packs=MAINTAIN_PACKS if packs is None else packs,
        )

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
        """
        with self._exclusive():
            if prepare is not None:
                prepare()
            try:
                created = self._commit_locked(message, *paths)
            except (GitError, OSError):
                if rollback is not None:
                    rollback()
                raise
            touched = self._touched_by(created)
        self._announce(created, touched)
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

    def restore(self, slug: str, commit: str) -> str:
        """Put ``slug`` back as it was at ``commit``, as a new commit. Never a rewrite."""
        resolved = self.resolve(commit)
        with self._exclusive():
            created = self._restore_locked(slug, resolved, f"Restore {slug} to {resolved[:7]}")
            touched = self._touched_by(created)
        self._announce(created, touched)
        if created is None:
            # Already identical: the caller still wants a revision id to point
            # at, and it is this model's own, not the repository HEAD -- which
            # may be a commit against a different model entirely.
            return self.last_commit(slug) or resolved
        return created

    # ── reading ───────────────────────────────────────────────────────────────

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

    def previous_commit(self, slug: str) -> str | None:
        """The commit before :meth:`last_commit` in the model's own history (#1071):
        what its current revision was made from. None for its first commit."""
        completed = self._run("log", "-2", "--format=%H", "--", slug, check=False)
        assert isinstance(completed.stdout, str)
        if completed.returncode != 0:
            return None
        commits = completed.stdout.split()
        return commits[1] if len(commits) > 1 else None

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

    def diff(
        self, revisions: RevisionRange, slug: str | None = None, *, relative: str | None = None
    ) -> str:
        """A unified patch across ``revisions``, optionally scoped to one model.

        A built-in's headers name ``<slug>/...``, as a template of mine's do: the
        ``_builtin/`` mirror is where it is stored, not a path the API reports.
        """
        if relative is None and slug is not None and slug.startswith(f"{BUILTIN_DIR}/"):
            relative = f"{BUILTIN_DIR}/"
        return super().diff(revisions, slug, relative=relative)

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
