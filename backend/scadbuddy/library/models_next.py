"""Build and check a second models repository, its meshes in Git LFS (#7314 step 3; spec
``docs/superpowers/specs/2026-11-24-models-repository-lfs-design.md``).

``python -m scadbuddy.library.models_next build`` makes ``data/models-next`` from
``data/models``; ``… check`` compares the two. Both run in the API's image with its own
database credential, and only read the old repository and the database. ``… push`` sends
the new one to a trial branch of the git remote that Settings names. Nothing here serves
the new repository, or changes a row.

What the new history holds
--------------------------
A first commit of ``.gitignore`` and ``.gitattributes`` (what `ModelHistory.ensure_repo`
writes), then one commit per version that is kept: each (slug, commit) an order item, an
order revision or a listing pins, the current version of each template and built-in, and
the upstream revisions that the ``model.json`` of any of these names.
A commit changes its own template only. It has the old commit's author, committer, dates
and message, and an ``Imported-From: <old id>`` trailer. So the map of old ids to new ones
is in git; `derived_map` reads it, and ``.git/scadbuddy-models-next.json`` is a copy.

Versions of one template with the same tree, one after the other, are one commit with a
trailer each: a commit that changed nothing would not be a revision of the template
(`ModelHistory.touched`). A template that is pinned and gone since is committed, and then
removed as its delete removed it.

The order is the old history's, pins, current versions and built-ins together. A
``model.json`` can only name an upstream commit that is older than itself, so that commit
is already in the new history when the file is written, and its new id is known: `base`
and `dismissed` are pointed at it, in the pinned versions too. An id the new history does
not have (the old one has lost the revision) is left as it is: it resolves to nothing, which
`upstream.plan_merge` takes as no base (#7534). Nothing else in a ``model.json`` is a
commit of this repository; a library's pin is of the library's own.

A run always builds from nothing, beside the destination, and then takes its place. So a
second run, with the old repository further on, gives what a first run would. It replaces
only what it made itself, with nothing committed, changed or left in it since.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, NamedTuple, Self

from scadbuddy.core.gitrepo import (
    _FIELD,
    _RECORD,
    AUTHOR_EMAIL,
    AUTHOR_NAME,
    DEFAULT_BRANCH,
    GITATTRIBUTES_NAME,
    GITIGNORE_NAME,
    LOCK_NAME,
    GitError,
    git_env,
)
from scadbuddy.core.paths import (
    BUILTIN_DIR,
    BUILTIN_PREFIX,
    MODEL_META_NAME,
    RENDERS_DIR_NAME,
    DataPaths,
    model_path,
)
from scadbuddy.git_remote.push import RemoteTarget, scrub
from scadbuddy.library.history import (
    LFS_POINTER,
    LFS_SIZE,
    SYMLINK_MODE,
    ModelHistory,
    _is_mesh,
    _model_id,
)
from scadbuddy.render.solids import WRAPPER_PREFIX

if TYPE_CHECKING:
    from scadbuddy.core.settings import Settings

#: The map, kept in the new repository's ``.git``. It also marks what a run may replace.
MAP_NAME = "scadbuddy-models-next.json"
TRAILER = "Imported-From"
#: What a trial push's branch starts with. The app pushes to `git_remote.push.BRANCHES`.
TRIAL_BRANCH = "models-trial"
#: GitHub warns of a file over this size, and refuses one over 100 MB:
#: https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-large-files-on-github
MAX_BLOB_BYTES = 50 * 1024 * 1024
#: One git call here may read or write the whole repository, over NFS.
TIMEOUT = 3600.0
CHUNK = 1024 * 1024
#: git-lfs takes no larger file for a pointer.
MAX_POINTER_BYTES = 1024
#: The root files the repository itself makes.
ROOT_FILES = (GITIGNORE_NAME, GITATTRIBUTES_NAME)
TREE_MODE = "40000"
GITLINK_MODE = "160000"
#: The tables whose rows pin a version: what an order, a quote, a test print or a listing
#: renders from. A quote and a print attempt name a row of `order_item_revisions`.
PLACES = ("order_items", "order_item_revisions", "storefront_listings")
#: The fields of ``upstream`` in a ``model.json`` that hold a commit of this repository.
UPSTREAM_FIELDS = ("base", "dismissed")

_COMMIT = re.compile(r"[0-9a-f]{40}")
_TRIAL = re.compile(rf"{TRIAL_BRANCH}(-[a-z0-9]+)*")
_OID = re.compile(rb"^oid sha256:([0-9a-f]{64})$", re.MULTILINE)
_TRAILER_LINE = re.compile(rb"[A-Za-z0-9][A-Za-z0-9-]*: .+")
_UPSTREAM_ID = re.compile(rb'("(?:base|dismissed)"\s*:\s*")[0-9a-f]{40}(?=")')

#: (slug, old full id) to the new full id.
Ids = dict[tuple[str, str], str]


class RefusedError(RuntimeError):
    """The destination, or the old repository, is not one to build with."""


class BuildError(RuntimeError):
    """The old repository holds what the new one cannot."""


@dataclass(frozen=True)
class Pin:
    """A version that a row names, and the tables whose rows do."""

    slug: str
    commit: str
    places: tuple[str, ...] = ()

    def __str__(self) -> str:
        where = f" ({', '.join(self.places)})" if self.places else ""
        return f"{self.slug} at {self.commit}{where}"


@dataclass
class Report:
    head: str = ""
    ids: Ids = field(default_factory=dict)
    #: Pins the old repository does not have.
    unresolved: list[Pin] = field(default_factory=list)
    #: Upstream ids the new history has no commit for, left as they were.
    unmapped: list[str] = field(default_factory=list)
    #: The ignored data files copied.
    copied: list[str] = field(default_factory=list)


@dataclass
class Result:
    differences: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class Entry(NamedTuple):
    mode: str
    oid: str


class Commit(NamedTuple):
    author: bytes
    committer: bytes
    message: bytes


class Pointer(NamedTuple):
    sha256: str
    size: int

    @property
    def body(self) -> bytes:
        return LFS_POINTER + f"oid sha256:{self.sha256}\nsize {self.size}\n".encode()


class Seen(NamedTuple):
    """A blob read to its end. ``head`` is all of it when it is small."""

    sha256: str
    size: int
    head: bytes

    @property
    def pointer(self) -> Pointer | None:
        return _pointer(self.head) if self.size <= MAX_POINTER_BYTES else None


def _pointer(data: bytes) -> Pointer | None:
    oid, size = _OID.search(data), LFS_SIZE.search(data)
    if not data.startswith(LFS_POINTER) or oid is None or size is None:
        return None
    return Pointer(oid[1].decode(), int(size[1]))


def _read_env() -> dict[str, str]:
    # No call may refresh the index of a repository it only reads.
    return {**git_env(), "GIT_OPTIONAL_LOCKS": "0"}


class _Repo(ModelHistory):
    def __init__(self, root: Path) -> None:
        super().__init__(root, wrapper_prefix=WRAPPER_PREFIX, timeout=TIMEOUT)

    def _run(
        self, *args: str, env: dict[str, str] | None = None, **options: Any
    ) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
        return super()._run(*args, env=env or _read_env(), **options)

    def bytes_out(self, *args: str) -> bytes:
        completed = self._run(*args, text=False)
        assert isinstance(completed.stdout, bytes)
        return completed.stdout

    def lfs_object(self, sha256: str) -> Path:
        return self.root / ".git" / "lfs" / "objects" / sha256[:2] / sha256[2:4] / sha256

    @property
    def map_path(self) -> Path:
        return self.root / ".git" / MAP_NAME


# ── reading a repository ──────────────────────────────────────────────────────


class _Objects:
    """A repository's objects, through one ``git cat-file --batch``."""

    def __init__(self, repo: _Repo) -> None:
        self.repo = repo
        self._files: dict[str, dict[str, Entry]] = {}
        self._seen: dict[str, Seen] = {}
        self._process = subprocess.Popen(
            repo.command("cat-file", "--batch"),
            cwd=repo.root,
            env=_read_env(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        assert self._process.stdin is not None and self._process.stdout is not None
        self._in: IO[bytes] = self._process.stdin
        self._out: IO[bytes] = self._process.stdout

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self._in.close()
        self._out.close()
        self._process.wait()

    def _ask(self, spec: str) -> tuple[str, str, int] | None:
        """The object's id, type and size; its bytes follow, and must be read. None where
        there is none."""
        if "\n" in spec:
            return None
        self._in.write(os.fsencode(spec) + b"\n")
        self._in.flush()
        answer = self._out.readline()
        if not answer:
            raise GitError(f"git cat-file --batch ended at {spec!r}")
        words = answer.split()
        if len(words) != 3 or not words[2].isdigit():
            return None
        return words[0].decode(), words[1].decode(), int(words[2])

    def _chunks(self, size: int) -> Iterator[bytes]:
        left = size
        while left:
            chunk = self._out.read(min(left, CHUNK))
            if not chunk:
                raise GitError("git cat-file --batch ended in an object")
            left -= len(chunk)
            yield chunk
        self._out.read(1)  # the newline after it

    def read(self, spec: str, kind: str) -> tuple[str, bytes] | None:
        """``spec``'s id and bytes; None where it is no ``kind``."""
        found = self._ask(spec)
        if found is None:
            return None
        data = b"".join(self._chunks(found[2]))
        return (found[0], data) if found[1] == kind else None

    def _need(self, spec: str, kind: str) -> bytes:
        found = self.read(spec, kind)
        if found is None:
            raise GitError(f"{spec} is not a {kind} in {self.repo.root}")
        return found[1]

    def blob(self, oid: str) -> bytes:
        return self._need(oid, "blob")

    def commit_id(self, commit: str) -> str | None:
        found = self.read(f"{commit}^{{commit}}", "commit")
        return None if found is None else found[0]

    def commit(self, commit: str) -> Commit:
        head, _, message = self._need(commit, "commit").partition(b"\n\n")
        lines = [line.split(b" ", 1) for line in head.splitlines()]
        named = {line[0]: line[1] for line in lines if len(line) == 2 and line[0]}
        return Commit(named[b"author"], named[b"committer"], message)

    def tree(self, commit: str, slug: str) -> str | None:
        """The tree of template ``slug`` at ``commit``; None where it is not there."""
        found = self.read(f"{commit}:{model_path(slug)}", "tree")
        return None if found is None else found[0]

    def entries(self, tree: str) -> dict[str, Entry]:
        """What a tree itself lists."""
        data, at = self._need(tree, "tree"), 0
        found: dict[str, Entry] = {}
        while at < len(data):
            space = data.index(b" ", at)
            end = data.index(b"\0", space)
            mode, name = data[at:space].decode(), os.fsdecode(data[space + 1 : end])
            at = end + 21
            found[name] = Entry(mode, data[end + 1 : at].hex())
        return found

    def files(self, tree: str) -> dict[str, Entry]:
        """Each file under a tree, by its path there."""
        if tree not in self._files:
            found: dict[str, Entry] = {}
            for name, entry in self.entries(tree).items():
                if entry.mode == TREE_MODE:
                    under = self.files(entry.oid)
                    found.update({f"{name}/{path}": below for path, below in under.items()})
                else:
                    found[name] = entry
            self._files[tree] = found
        return self._files[tree]

    def root_files(self, commit: str) -> dict[str, Entry]:
        """Each file at ``commit`` but the repository's own."""
        tree = self.read(f"{commit}^{{tree}}", "tree")
        if tree is None:
            raise GitError(f"{commit} has no tree in {self.repo.root}")
        return {p: e for p, e in self.files(tree[0]).items() if p not in ROOT_FILES}

    def copy_blob(
        self, oid: str, out: IO[bytes], begin: Callable[[int], object] | None = None
    ) -> Seen:
        """Write a blob's bytes to ``out``; ``begin`` is told its size first."""
        found = self._ask(oid)
        if found is None or found[1] != "blob":
            raise GitError(f"{oid} is not a blob in {self.repo.root}")
        if begin is not None:
            begin(found[2])
        digest, head = hashlib.sha256(), b""
        for chunk in self._chunks(found[2]):
            digest.update(chunk)
            head += chunk[: MAX_POINTER_BYTES - len(head)]
            out.write(chunk)
        seen = self._seen[oid] = Seen(digest.hexdigest(), found[2], head)
        return seen

    def seen(self, oid: str) -> Seen:
        if oid not in self._seen:
            with open(os.devnull, "wb") as out:
                self.copy_blob(oid, out)
        return self._seen[oid]

    def digest(self, path: str, entry: Entry) -> str | None:
        """The sha256 of a file's real bytes: a mesh's where git holds its pointer. None
        where its LFS object is missing."""
        if entry.mode == GITLINK_MODE:
            return f"submodule {entry.oid}"
        seen = self.seen(entry.oid)
        if _is_mesh(path) and entry.mode != SYMLINK_MODE and seen.pointer is not None:
            return self.lfs_digest(seen.pointer.sha256)
        return seen.sha256

    def lfs_digest(self, sha256: str) -> str | None:
        key = f"lfs {sha256}"
        if key not in self._seen:
            target = self.repo.lfs_object(sha256)
            if not target.is_file():
                return None
            self._seen[key] = Seen(_file_digest(target), target.stat().st_size, b"")
        return self._seen[key].sha256


def _file_digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _walk(repo: _Repo, *args: str) -> Iterator[tuple[str, str, list[str]]]:
    """Each commit up to HEAD, newest first: its id, its ``Imported-From`` values and the
    paths it changed."""
    shape = f"%x1e%H%x1f%(trailers:key={TRAILER},valueonly,separator=%x2c)%x1f"
    # A rename names the new path only, and the old one may be another template's.
    flags = ("-z", f"--format={shape}", "--name-only", "--no-renames")
    log = repo.bytes_out("log", *flags, *args)
    for record in os.fsdecode(log).split(_RECORD)[1:]:
        commit, trailer, names = record.split(_FIELD, 2)
        yield commit, trailer, [name for name in names.strip("\0\n").split("\0") if name]


def _slugs(names: Iterable[str]) -> list[str]:
    found = (_model_id(name) for name in names)
    return list(dict.fromkeys(slug for slug in found if slug is not None))


def derived_map(root: Path) -> Ids:
    """The map of old ids to new ones, read from the new history's trailers. A commit that
    removes a template is no version of it."""
    repo = _Repo(root)
    found: Ids = {}
    with _Objects(repo) as objects:
        for commit, trailer, names in _walk(repo):
            old_ids = _COMMIT.findall(trailer)
            for slug in _slugs(names) if old_ids else []:
                if objects.tree(commit, slug) is not None:
                    found.update({(slug, old): commit for old in old_ids})
    return found


# ── what the new history is made of ───────────────────────────────────────────


@dataclass
class _Version:
    """One commit to make: a template's tree, or its removal (``tree`` None)."""

    slug: str
    tree: str | None
    #: The old commits it stands for.
    olds: list[str]
    #: The old commit whose author, dates and message it takes.
    source: str
    #: Where it stands in the old history.
    position: tuple[int, int]
    #: The message, where it is not ``source``'s.
    message: bytes | None = None


@dataclass
class _Plan:
    head: str
    versions: list[_Version]
    unresolved: list[Pin]
    #: Each template HEAD has, and the last commit that changed it.
    current: dict[str, str]
    #: The tree of each (slug, old commit) that is kept.
    wanted: dict[tuple[str, str], str]


def _plan(objects: _Objects, pins: Sequence[Pin]) -> _Plan:
    repo = objects.repo
    head = repo.head()
    if head is None:
        raise RefusedError(f"{repo.root} has no commit")
    order = repo._out("rev-list", "--topo-order", "--reverse", head).split()
    index = {commit: at for at, commit in enumerate(order)}
    last: dict[str, str] = {}
    for commit, _, names in _walk(repo, head):
        for slug in _slugs(names):
            last.setdefault(slug, commit)
    trees = _templates(objects, head)

    def position(commit: str) -> tuple[int, int]:
        """A commit HEAD does not reach is put before those it does."""
        if commit in index:
            return 1, index[commit]
        return 0, int(objects.commit(commit).committer.split()[-2])

    unresolved: list[Pin] = []
    pinned: dict[str, dict[str, str]] = {}
    for pin in pins:
        commit_id = objects.commit_id(pin.commit)
        tree = None if commit_id is None else objects.tree(commit_id, pin.slug)
        if commit_id is None or tree is None:
            unresolved.append(pin)
        else:
            pinned.setdefault(pin.slug, {})[commit_id] = tree

    _keep_upstreams(objects, [*trees.values()], pinned)
    versions: list[_Version] = []
    for slug in sorted({*trees, *pinned}):
        line: list[_Version] = []
        held = pinned.get(slug, {})
        for commit in sorted(held, key=position):
            _add(line, slug, commit, held[commit], position(commit))
        if slug in trees:
            newest = last.get(slug, head)
            _add(line, slug, newest, trees[slug], position(newest))
        elif slug in last:
            _add(line, slug, last[slug], None, position(last[slug]))
        else:
            gone = line[-1]
            subject = f"Delete {slug}\n".encode()
            line.append(_Version(slug, None, [], gone.source, gone.position, subject))
        versions += line
    # Stable, so the versions of one template stay in their own order.
    versions.sort(key=lambda version: version.position)
    current = {slug: last.get(slug, head) for slug in trees}
    wanted = {
        (slug, commit): tree for slug, held in pinned.items() for commit, tree in held.items()
    }
    wanted.update({(slug, commit): trees[slug] for slug, commit in current.items()})
    return _Plan(head, versions, unresolved, current, wanted)


def _keep_upstreams(objects: _Objects, trees: list[str], pinned: dict[str, dict[str, str]]) -> None:
    """Pin what each kept version's ``model.json`` names as its upstream's revision: without
    its base, a duplicate merges as one that has none, and all that differs conflicts."""
    left = [*trees, *(tree for held in pinned.values() for tree in held.values())]
    seen: set[str] = set()
    while left:
        tree = left.pop()
        if tree in seen:
            continue
        seen.add(tree)
        entry = objects.files(tree).get(MODEL_META_NAME)
        if entry is None or entry.mode == SYMLINK_MODE:
            continue
        for slug, commit in _upstream_refs(objects.blob(entry.oid)).values():
            found = objects.commit_id(commit)
            below = None if found is None else objects.tree(found, slug)
            # One that is gone is reported when the version that names it is written.
            if found is not None and below is not None:
                pinned.setdefault(slug, {})[found] = below
                left.append(below)


def _add(
    line: list[_Version], slug: str, commit: str, tree: str | None, at: tuple[int, int]
) -> None:
    """Add a version to its template's line: to the last one, where the tree is the same."""
    if not line or tree is None or line[-1].tree != tree:
        after = max(at, line[-1].position) if line else at
        line.append(_Version(slug, tree, [] if tree is None else [commit], commit, after))
        return
    same = line[-1]
    if commit not in same.olds:
        same.olds.append(commit)
    # The older of the two is the commit that made the tree.
    if at < same.position and (len(line) == 1 or line[-2].position <= at):
        same.source, same.position = commit, at


def _templates(objects: _Objects, commit: str) -> dict[str, str]:
    """Each template and built-in at ``commit``, and its tree."""
    root = objects.read(f"{commit}^{{tree}}", "tree")
    assert root is not None
    found: dict[str, str] = {}
    for name, entry in objects.entries(root[0]).items():
        if entry.mode != TREE_MODE:
            continue
        if name != BUILTIN_DIR:
            found[name] = entry.oid
            continue
        for builtin, below in objects.entries(entry.oid).items():
            if below.mode == TREE_MODE:
                found[f"{BUILTIN_PREFIX}{builtin}"] = below.oid
    return found


# ── the upstream ids in a model.json ──────────────────────────────────────────


def _json(raw: bytes) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _upstream_refs(raw: bytes) -> dict[str, tuple[str, str]]:
    """Each upstream field of ``model.json`` that holds an id: the template it is a
    revision of, and the id."""
    body = _json(raw)
    upstream = body.get("upstream") if isinstance(body, dict) else None
    if not isinstance(upstream, dict):
        return {}
    # The base is of where the source lived then (`Upstream.path`); a dismissed revision
    # is one of where it lives now.
    owners = {
        "base": _model_id(f"{upstream.get('path')}/{MODEL_META_NAME}"),
        "dismissed": upstream.get("id"),
    }
    found: dict[str, tuple[str, str]] = {}
    for name in UPSTREAM_FIELDS:
        value, owner = upstream.get(name), owners[name]
        if isinstance(value, str) and _COMMIT.fullmatch(value) and isinstance(owner, str):
            found[name] = (owner, value)
    return found


def _upstream_ids(raw: bytes, ids: Mapping[tuple[str, str], str]) -> dict[str, tuple[str, str]]:
    """Each upstream field of ``model.json`` that holds an id: the id, and the new one
    ("" where the new history has none)."""
    refs = _upstream_refs(raw)
    return {name: (value, ids.get((owner, value), "")) for name, (owner, value) in refs.items()}


def _expected(raw: bytes, ids: Mapping[tuple[str, str], str]) -> Any:
    """``model.json`` as it must read with the new ids."""
    body = copy.deepcopy(_json(raw))
    for name, (_, new) in _upstream_ids(raw, ids).items():
        if new:
            body["upstream"][name] = new
    return body


def _repointed(raw: bytes, ids: Mapping[tuple[str, str], str]) -> bytes:
    """``model.json`` with the new ids, every other byte as it was."""
    out = raw
    for name, (value, new) in _upstream_ids(raw, ids).items():
        if new:
            place = re.compile(rb'("%b"\s*:\s*")%b(?=")' % (name.encode(), value.encode()))
            out = place.sub(rb"\g<1>" + new.encode(), out)
    if out != raw and _json(out) != _expected(raw, ids):
        raise BuildError(f"a {MODEL_META_NAME} holds an upstream id in more than one place")
    return out


# ── the builder ───────────────────────────────────────────────────────────────


class _Importer:
    """Commits to a fresh repository, through one ``git fast-import``."""

    def __init__(self, repo: _Repo) -> None:
        self.repo = repo
        self._mark = 0
        self._errors = tempfile.TemporaryFile()  # noqa: SIM115 (closed in __exit__)
        self._process = subprocess.Popen(
            repo.command("fast-import", "--quiet", "--done", "--date-format=raw"),
            cwd=repo.root,
            env=git_env(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._errors,
        )
        assert self._process.stdin is not None and self._process.stdout is not None
        self.stdin: IO[bytes] = self._process.stdin
        self._answers: IO[bytes] = self._process.stdout

    def __enter__(self) -> Self:
        return self

    def __exit__(self, kind: object, *_: object) -> None:
        try:
            if kind is None:
                self._tell(b"done\n")
                self.stdin.close()
                if self._process.wait() != 0:
                    raise self.failed()
        finally:
            if self._process.poll() is None:
                self._process.kill()
                self._process.wait()
            self._answers.close()
            self._errors.close()

    def failed(self) -> BuildError:
        self._errors.seek(0)
        return BuildError(f"git fast-import failed: {self._errors.read().decode(errors='replace')}")

    def _tell(self, *parts: bytes) -> None:
        try:
            self.stdin.write(b"".join(parts))
        except BrokenPipeError:
            raise self.failed() from None

    def begin_blob(self, size: int) -> None:
        """Begin a blob of ``size`` bytes, which the caller writes to ``stdin``."""
        self._mark += 1
        self._tell(b"blob\nmark :%d\ndata %d\n" % (self._mark, size))

    def end_blob(self) -> int:
        self._tell(b"\n")
        return self._mark

    def blob(self, data: bytes) -> int:
        self.begin_blob(len(data))
        self._tell(data)
        return self.end_blob()

    def commit(
        self,
        source: Commit,
        message: bytes,
        files: Mapping[str, tuple[str, int]],
        *,
        remove: str | None = None,
    ) -> str:
        """Commit ``files`` (a path to its mode and blob mark) in place of ``remove``."""
        self._mark += 1
        lines = [
            b"commit refs/heads/%b\nmark :%d\n" % (DEFAULT_BRANCH.encode(), self._mark),
            b"author %b\ncommitter %b\n" % (source.author, source.committer),
            b"data %d\n%b\n" % (len(message), message),
        ]
        if remove is not None:
            lines.append(b"D %b\n" % _quoted(remove))
        for path, (mode, mark) in files.items():
            lines.append(b"M %b :%d %b\n" % (mode.encode(), mark, _quoted(path)))
        self._tell(*lines, b"\nget-mark :%d\n" % self._mark)
        self.stdin.flush()
        answer = self._answers.readline().decode().strip()
        if not answer:
            self._process.wait()
            raise self.failed()
        return answer


def _quoted(path: str) -> bytes:
    raw = os.fsencode(path)
    return b'"%b"' % raw.replace(b"\\", b"\\\\").replace(b'"', b'\\"').replace(b"\n", b"\\n")


def _message(message: bytes, olds: Iterable[str]) -> bytes:
    """A commit's message with its trailers: in its own trailer block where it has one, as
    git reads only the last paragraph for them."""
    body = message.rstrip(b"\n")
    lines = b"\n".join(f"{TRAILER}: {old}".encode() for old in olds)
    if not lines:
        return body + b"\n"
    first, _, _ = body.partition(b"\n\n")
    block = body.rsplit(b"\n\n", 1)[-1]
    joined = block != first and all(map(_TRAILER_LINE.fullmatch, block.splitlines()))
    return body + (b"\n" if joined else b"\n\n") + lines + b"\n"


class _Builder:
    def __init__(self, objects: _Objects, importer: _Importer, report: Report) -> None:
        self.objects = objects
        self.importer = importer
        self.report = report
        self._marks: dict[tuple[str, bool], int] = {}

    def begin(self, versions: Sequence[_Version]) -> None:
        """The first commit: the repository's own files, dated as the oldest version."""
        repo = self.importer.repo
        attributes = repo._gitattributes_body()
        assert attributes is not None
        date = b"0 +0000"
        if versions:
            date = b" ".join(self.objects.commit(versions[0].source).committer.split()[-2:])
        ours = f"{AUTHOR_NAME} <{AUTHOR_EMAIL}> ".encode() + date
        bodies = {GITIGNORE_NAME: repo._gitignore_body(), GITATTRIBUTES_NAME: attributes}
        files = {n: ("100644", self.importer.blob(body.encode())) for n, body in bodies.items()}
        self.importer.commit(Commit(ours, ours, b""), b"Initial revision\n", files)

    def add(self, version: _Version) -> None:
        source = self.objects.commit(version.source)
        path = model_path(version.slug)
        if version.tree is None:
            message = version.message or _message(source.message, [version.source])
            self.importer.commit(source, message, {}, remove=path)
            return
        files: dict[str, tuple[str, int]] = {}
        for name, entry in self.objects.files(version.tree).items():
            full = f"{path}/{name}"
            if name == MODEL_META_NAME and entry.mode != SYMLINK_MODE:
                mark = self._meta(version, entry)
            else:
                mark = self._mark(full, entry)
            files[full] = (entry.mode, mark)
        message = _message(source.message, version.olds)
        new = self.importer.commit(source, message, files, remove=path)
        self.report.ids.update({(version.slug, old): new for old in version.olds})

    def _meta(self, version: _Version, entry: Entry) -> int:
        raw, ids = self.objects.blob(entry.oid), self.report.ids
        for name, (value, new) in _upstream_ids(raw, ids).items():
            if not new:
                at = version.olds[-1]
                self.report.unmapped.append(f"{version.slug} at {at}: upstream {name} {value}")
        return self.importer.blob(_repointed(raw, ids))

    def _mark(self, path: str, entry: Entry) -> int:
        if entry.mode == GITLINK_MODE:
            raise BuildError(f"{path} is a git submodule, whose files are not in git")
        mesh = _is_mesh(path) and entry.mode != SYMLINK_MODE
        key = (entry.oid, mesh)
        if key not in self._marks:
            self._marks[key] = self._mesh(path, entry.oid) if mesh else self._plain(entry.oid)
        return self._marks[key]

    def _plain(self, oid: str) -> int:
        try:
            self.objects.copy_blob(oid, self.importer.stdin, self.importer.begin_blob)
        except BrokenPipeError:
            raise self.importer.failed() from None
        return self.importer.end_blob()

    def _mesh(self, path: str, oid: str) -> int:
        """A mesh's bytes into ``.git/lfs``, and its pointer into git. An empty one has no
        pointer, as with git-lfs itself."""
        repo = self.importer.repo
        scratch = repo.root / ".git" / "lfs" / "tmp"
        scratch.mkdir(parents=True, exist_ok=True)
        held = scratch / uuid.uuid4().hex
        with held.open("wb") as out:
            seen = self.objects.copy_blob(oid, out)
        pointer = seen.pointer
        if pointer is not None:
            # The old repository has it in LFS already.
            source = self.objects.repo.lfs_object(pointer.sha256)
            if not source.is_file() or _file_digest(source) != pointer.sha256:
                raise BuildError(f"{path}: its LFS object {pointer.sha256} is missing or damaged")
            shutil.copyfile(source, held)
        elif seen.size:
            pointer = Pointer(seen.sha256, seen.size)
        else:
            held.unlink()
            return self.importer.blob(b"")
        target = repo.lfs_object(pointer.sha256)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(held, target)
        return self.importer.blob(pointer.body)


def build(old: Path, dest: Path, pins: Sequence[Pin], *, live: Path) -> Report:
    """Make ``dest`` from the repository ``old``, in place of an earlier run's output."""
    source = _Repo(old)
    target = _destination(source, dest, live)
    if target.exists():
        _may_replace(target)
    staged = target.with_name(f"{target.name}.building-{uuid.uuid4().hex}")
    try:
        staged.mkdir()
        report = _make(source, _Repo(staged), pins)
        _put(staged, target)
    finally:
        shutil.rmtree(staged, ignore_errors=True)
    return report


def _destination(source: _Repo, dest: Path, live: Path) -> Path:
    if not source.available or source.head() is None:
        raise RefusedError(f"{source.root} is not a repository with a commit")
    if source._gitattributes_body() is None:
        raise RefusedError("git-lfs is not on PATH")
    if dest.is_symlink():
        raise RefusedError(f"{dest} is a link")
    target = dest.resolve()
    for home in (live.resolve(), source.root.resolve()):
        if target == home or home in target.parents or target in home.parents:
            raise RefusedError(f"{dest} is, is in or holds the models directory {home}")
    if not target.parent.is_dir():
        raise RefusedError(f"{target.parent} is not a directory")
    return target


def _may_replace(target: Path) -> None:
    """Only an empty directory, or what a run made, with nothing committed, changed or
    left in it since. An ignored file there is a copy or a render's."""
    if not target.is_dir():
        raise RefusedError(f"{target} is not a directory")
    if not any(target.iterdir()):
        return
    repo = _Repo(target)
    held = _held(repo)
    head = held.get("head")
    if not isinstance(head, str) or not repo.available or repo.head() != head:
        raise RefusedError(f"{target} is not empty, and not as this command left it")
    copied = set(held.get("copied", []))
    status = os.fsdecode(repo.bytes_out("status", "--porcelain", "-z", "--untracked-files=all"))
    # A copied file that only the old repository ignores is untracked here.
    left = [line for line in status.split("\0") if line and line[3:] not in copied]
    if left:
        raise RefusedError(f"{target} holds changes this command did not make: {left[0]}")


def _held(repo: _Repo) -> dict[str, Any]:
    """The map file, or nothing."""
    try:
        held = json.loads(repo.map_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return held if isinstance(held, dict) else {}


def _make(source: _Repo, repo: _Repo, pins: Sequence[Pin]) -> Report:
    report = Report()
    repo._run("init", "--quiet", f"--initial-branch={DEFAULT_BRANCH}", ".")
    with _Objects(source) as objects:
        plan = _plan(objects, pins)
        report.unresolved = plan.unresolved
        with _Importer(repo) as importer:
            builder = _Builder(objects, importer, report)
            builder.begin(plan.versions)
            for version in plan.versions:
                builder.add(version)
    shutil.rmtree(repo.root / ".git" / "lfs" / "tmp", ignore_errors=True)
    repo._run("reset", "--quiet", "--hard")
    report.copied = _copy_data(source, repo)
    head = repo.head()
    assert head is not None
    report.head = head
    held = {
        "head": head,
        "old_head": plan.head,
        "copied": report.copied,
        "map": [
            {"slug": slug, "old": old, "new": new}
            for (slug, old), new in sorted(report.ids.items())
        ],
        "unresolved": [
            {"slug": pin.slug, "commit": pin.commit, "places": pin.places}
            for pin in report.unresolved
        ],
    }
    repo.map_path.write_text(json.dumps(held, indent=1) + "\n", encoding="utf-8")
    return report


def _copy_data(source: _Repo, repo: _Repo) -> list[str]:
    """Copy the ignored files that are data (videos), not what a render leaves."""
    listed = os.fsdecode(
        source.bytes_out("ls-files", "-z", "--others", "--ignored", "--exclude-standard")
    )
    copied = [name for name in listed.split("\0") if name and not _is_derived(name)]
    for name in copied:
        target = repo.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source.root / name, target, follow_symlinks=False)
    return copied


def _is_derived(path: str) -> bool:
    parts = path.split("/")
    return (
        RENDERS_DIR_NAME in parts
        or parts[-1] == LOCK_NAME
        or any(part.startswith(WRAPPER_PREFIX) for part in parts)
        # An open file an NFS client deleted.
        or parts[-1].startswith(".nfs")
    )


def _put(staged: Path, target: Path) -> None:
    """``staged`` in the place of ``target``."""
    if not target.exists():
        os.rename(staged, target)
        return
    _may_replace(target)
    retired = target.with_name(f"{target.name}.retired-{uuid.uuid4().hex}")
    os.rename(target, retired)
    try:
        os.rename(staged, target)
    except OSError:
        os.rename(retired, target)
        raise
    shutil.rmtree(retired)


# ── the trial push ────────────────────────────────────────────────────────────


def push_trial(dest: Path, branch: str, target: RemoteTarget, *, live: Path) -> str:
    """Push what a build made, meshes first, to a trial branch at ``target``; its HEAD.
    Never a force, and never a branch that ScadBuddy itself pushes to."""
    if not _TRIAL.fullmatch(branch):
        raise RefusedError(f"{branch} is not a trial branch ({TRIAL_BRANCH}…)")
    if dest.resolve() == live.resolve():
        raise RefusedError(f"{dest} is the models directory")
    repo = _Repo(dest)
    head = repo.head() if repo.available else None
    if head is None:
        raise RefusedError(f"{dest} is not a repository with a commit")
    _may_replace(dest)
    ref = f"refs/heads/{branch}"
    repo.push(target.url, head, ref, username=target.username, password=target.token)
    return head


# ── the check ─────────────────────────────────────────────────────────────────


class _File(NamedTuple):
    mode: str
    digest: Callable[[], str | None]
    read: Callable[[], bytes]


def check(old: Path, new: Path, pins: Sequence[Pin]) -> Result:
    """Compare the repository ``new`` with the one it was built from."""
    source, repo = _Repo(old), _Repo(new)
    if not repo.available or repo.head() is None:
        return Result([f"{new} is not a repository with a commit"])
    with _Objects(source) as before, _Objects(repo) as after:
        return _Checker(before, after, _plan(before, pins)).run()


class _Checker:
    def __init__(self, before: _Objects, after: _Objects, plan: _Plan) -> None:
        self.before = before
        self.after = after
        self.plan = plan
        self.repo = after.repo
        self.result = Result()
        self.ids = derived_map(self.repo.root)
        order = self.repo._out("rev-list", "--topo-order", "--reverse", "HEAD").split()
        self.order = {commit: at for at, commit in enumerate(order)}

    def run(self) -> Result:
        self._map()
        self._versions()
        self._current()
        self._meshes()
        self._blobs()
        self._state()
        for pin in self.plan.unresolved:
            self.result.notes.append(f"{pin} does not resolve in the old repository")
        return self.result

    def _differ(self, line: str) -> None:
        if line not in self.result.differences:
            self.result.differences.append(line)

    def _map(self) -> None:
        held, head = _held(self.repo), self.repo.head()
        if held.get("head") != head:
            self._differ(f"HEAD is {head}, and not the {held.get('head')} the builder made")
        entries = held.get("map")
        listed = {
            (str(entry.get("slug")), str(entry.get("old"))): str(entry.get("new"))
            for entry in (entries if isinstance(entries, list) else [])
            if isinstance(entry, dict)
        }
        for key in sorted({*listed, *self.ids}):
            if listed.get(key) != self.ids.get(key):
                self._differ(
                    f"{MAP_NAME}: {key[0]} at {key[1]} is {listed.get(key)},"
                    f" and the trailers say {self.ids.get(key)}"
                )

    # ── the files ─────────────────────────────────────────────────────────────

    def _versions(self) -> None:
        for (slug, old), tree in self.plan.wanted.items():
            commit = self.ids.get((slug, old))
            made = None if commit is None else self.after.tree(commit, slug)
            if commit is None or made is None:
                self._differ(f"{slug} at {old}: no commit in the new history")
                continue
            path = model_path(slug)
            self._compare(
                old,
                _git_files(self.before, self.before.files(tree), path),
                _git_files(self.after, self.after.files(made), path),
                partial(self._ids_at, commit),
            )
        for slug, old in sorted(set(self.ids) - set(self.plan.wanted)):
            self.result.notes.append(f"{slug} at {old} is kept, and nothing needs it now")

    def _current(self) -> None:
        """The current version of every template: in git, and on the volume."""
        tracked = self.before.root_files(self.plan.head)
        self._compare(
            "HEAD",
            _git_files(self.before, tracked),
            _git_files(self.after, self.after.root_files("HEAD")),
            self._ids_of,
        )
        volume = [_volume_files(root, tracked) for root in (self.before.repo.root, self.repo.root)]
        self._compare("volume", volume[0], volume[1], self._ids_of)

    def _ids_before(self, commit: str | None) -> Ids:
        """The ids a ``model.json`` committed at ``commit`` can name."""
        at = len(self.order) if commit is None else self.order[commit]
        return {key: new for key, new in self.ids.items() if self.order[new] < at}

    def _ids_at(self, commit: str, _slug: str) -> Ids:
        return self._ids_before(commit)

    def _ids_of(self, slug: str) -> Ids:
        return self._ids_before(self.ids.get((slug, self.plan.current.get(slug, ""))))

    def _compare(
        self,
        label: str,
        before: Mapping[str, _File],
        after: Mapping[str, _File],
        ids: Callable[[str], Ids],
    ) -> None:
        for path in sorted({*before, *after}):
            was, now = before.get(path), after.get(path)
            if was is None:
                self._differ(f"{path} ({label}): not in the old repository")
                continue
            if now is None:
                self._differ(f"{path} ({label}): missing in the new repository")
                continue
            if was.mode != now.mode:
                self._differ(f"{path} ({label}): its mode is {now.mode}, not {was.mode}")
                continue
            slug = _model_id(path)
            if slug is not None and path == f"{model_path(slug)}/{MODEL_META_NAME}":
                fault = _meta_fault(was.read(), now.read(), ids(slug))
            else:
                fault = None if was.digest() == now.digest() else "the bytes differ"
            if fault is not None:
                self._differ(f"{path} ({label}): {fault}")

    # ── the repository ────────────────────────────────────────────────────────

    def _meshes(self) -> None:
        pointers: dict[str, Pointer] = {}
        done: set[str] = set()
        for commit in self.order:
            for path, entry in self.after.root_files(commit).items():
                if not _is_mesh(path) or entry.mode == SYMLINK_MODE or entry.oid in done:
                    continue
                done.add(entry.oid)
                seen = self.after.seen(entry.oid)
                if seen.pointer is not None and seen.pointer.body == seen.head:
                    pointers[seen.pointer.sha256] = seen.pointer
                elif seen.size:
                    self._differ(f"{path} at {commit}: not an LFS pointer")
        for sha256, pointer in sorted(pointers.items()):
            digest = self.after.lfs_digest(sha256)
            if digest is None:
                self._differ(f"LFS object {sha256}: missing")
            elif digest != sha256:
                self._differ(f"LFS object {sha256}: its hash is {digest}")
            elif self.repo.lfs_object(sha256).stat().st_size != pointer.size:
                self._differ(f"LFS object {sha256}: its size is not the pointer's")

    def _blobs(self) -> None:
        shape = "--batch-check=%(objecttype) %(objectsize) %(objectname)"
        # An object that cannot be read is `git fsck`'s to name.
        listed = self.repo._out("cat-file", "--batch-all-objects", shape, check=False)
        for words in map(str.split, listed.splitlines()):
            if len(words) == 3 and words[0] == "blob" and int(words[1]) > MAX_BLOB_BYTES:
                self._differ(f"blob {words[2]}: {words[1]} bytes, over {MAX_BLOB_BYTES}")

    def _state(self) -> None:
        bodies = {
            GITIGNORE_NAME: self.repo._gitignore_body(),
            GITATTRIBUTES_NAME: self.repo._gitattributes_body(),
        }
        for name, body in bodies.items():
            found = self.after.read(f"HEAD:{name}", "blob")
            if found is None or found[1] != (body or "").encode():
                self._differ(f"{name} (HEAD): not what a fresh repository has")
        status = ("status", "--porcelain", "-z", "--untracked-files=all", "--no-renames")
        for line in os.fsdecode(self.repo.bytes_out(*status)).split("\0"):
            if line:
                self._differ(f"{line[3:]}: not committed ({line[:2].strip()})")
        fsck = self.repo._run("fsck", "--full", "--no-dangling", "--no-progress", check=False)
        if fsck.returncode != 0:
            assert isinstance(fsck.stdout, str) and isinstance(fsck.stderr, str)
            said = " ".join(f"{fsck.stdout} {fsck.stderr}".split())
            self._differ(f"git fsck: {said or fsck.returncode}")


def _git_files(
    objects: _Objects, entries: Mapping[str, Entry], under: str = ""
) -> dict[str, _File]:
    prefix = f"{under}/" if under else ""
    return {
        f"{prefix}{name}": _File(
            entry.mode,
            partial(objects.digest, f"{prefix}{name}", entry),
            partial(objects.blob, entry.oid),
        )
        for name, entry in entries.items()
    }


def _volume_files(root: Path, tracked: Mapping[str, Entry]) -> dict[str, _File]:
    """Each file on the volume but the repository's own and what a render leaves. A tracked
    file's mode is git's to keep."""
    found: dict[str, _File] = {}
    for at, folders, names in os.walk(root):
        here = Path(at)
        if here == root and ".git" in folders:
            folders.remove(".git")
        links = [name for name in folders if (here / name).is_symlink()]
        for name in [*names, *links]:
            path = here / name
            relative = path.relative_to(root).as_posix()
            if relative in ROOT_FILES or _is_derived(relative):
                continue
            linked = path.is_symlink()
            mode = "link" if linked else "file"
            if not linked and relative not in tracked and path.stat().st_mode & 0o100:
                mode = "executable"
            digest = partial(_link, path) if linked else partial(_file_digest, path)
            found[relative] = _File(mode, digest, path.read_bytes)
    return found


def _link(path: Path) -> str:
    return f"link to {os.readlink(path)}"


def _meta_fault(was: bytes, now: bytes, ids: Ids) -> str | None:
    """Why ``now`` is not the ``model.json`` ``was`` with the new ids in its upstream."""
    expected, body = _expected(was, ids), _json(now)
    if was == now and body == expected:
        return None
    if not isinstance(expected, dict) or not isinstance(body, dict):
        return "the bytes differ"
    if body.get("upstream") != expected.get("upstream"):
        return f"its upstream is {body.get('upstream')}, not {expected.get('upstream')}"
    if body != expected or _UPSTREAM_ID.sub(rb"\1", was) != _UPSTREAM_ID.sub(rb"\1", now):
        return "the bytes differ beside its upstream"
    return None


# ── the pins, and the commands ────────────────────────────────────────────────


def read_pins(conninfo: str) -> list[Pin]:
    """Every (slug, commit) an order item, an order revision or a listing names."""
    import psycopg
    from psycopg import sql

    # The one client is the API's own, and it is told to read only.
    each = sql.SQL(" UNION ").join(
        sql.SQL("SELECT slug, commit, {} AS place FROM {} WHERE commit IS NOT NULL").format(
            sql.Literal(place), sql.Identifier(place)
        )
        for place in PLACES
    )
    query = sql.SQL(
        "SELECT slug, commit, array_agg(place ORDER BY place) FROM ({}) AS pins"
        " GROUP BY slug, commit ORDER BY slug, commit"
    ).format(each)
    with psycopg.connect(conninfo, options="-c default_transaction_read_only=on") as conn:
        rows = conn.execute(query).fetchall()
    return [Pin(slug, commit, tuple(places)) for slug, commit, places in rows]


def main(argv: Sequence[str]) -> int:
    from scadbuddy.core.settings import Settings

    parser = argparse.ArgumentParser(prog="python -m scadbuddy.library.models_next")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, text in (
        ("build", "make the second models repository"),
        ("check", "compare it with the first"),
    ):
        command = commands.add_parser(name, help=text)
        command.add_argument("--old", type=Path, help="default: the data directory's models")
        command.add_argument("--dest", type=Path, help="default: models-next beside it")
    trial = commands.add_parser("push", help="push it to a trial branch of the git remote")
    trial.add_argument("--dest", type=Path, help="default: models-next beside the models")
    trial.add_argument("--branch", required=True, help=f"{TRIAL_BRANCH}, or {TRIAL_BRANCH}-<name>")
    options = parser.parse_args(argv)

    settings = Settings()
    live = DataPaths(settings.data_dir).models
    dest: Path = options.dest or live.with_name("models-next")
    if options.command == "push":
        return _push(settings, dest, options.branch, live)
    old: Path = options.old or live
    pins = read_pins(settings.database_url)
    try:
        if options.command == "build":
            return _say_built(build(old, dest, pins, live=live), dest)
        return _say_checked(check(old, dest, pins))
    except RefusedError as error:
        print(f"refused: {error}", file=sys.stderr)
    except (BuildError, GitError) as error:
        print(f"failed: {error}", file=sys.stderr)
    return 2


def _push(settings: Settings, dest: Path, branch: str, live: Path) -> int:
    from scadbuddy.library.settings_store import SettingsStore

    store = SettingsStore(settings)
    # Not `store.open`, which migrates: this only reads. The URL and its token are of one
    # snapshot, as the app reads them.
    store._pool.open(wait=True, timeout=store.connect_timeout)
    try:
        remote = store.snapshot().runtime
    finally:
        store.close()
    if not remote.git_remote_url:
        print("refused: no git remote is set", file=sys.stderr)
        return 2
    target = RemoteTarget(
        remote.git_remote_url, remote.git_remote_username, remote.git_remote_token
    )
    try:
        head = push_trial(dest, branch, target, live=live)
    except RefusedError as error:
        print(f"refused: {error}", file=sys.stderr)
    except GitError as error:
        print(f"failed: {scrub(str(error), target.token)}", file=sys.stderr)
    else:
        print(f"pushed {head} to {branch}")
        return 0
    return 2


def _say_built(report: Report, dest: Path) -> int:
    for pin in report.unresolved:
        print(f"not in the old repository: {pin}")
    for line in report.unmapped:
        print(f"no new id, left as it was: {line}")
    for name in report.copied:
        print(f"copied: {name}")
    commits = len(set(report.ids.values()))
    print(f"built {dest} at {report.head}: {commits} versions for {len(report.ids)} old ones")
    return 0


def _say_checked(result: Result) -> int:
    for note in result.notes:
        print(f"note: {note}")
    for line in result.differences:
        print(f"DIFFERENT: {line}")
    print(f"{len(result.differences)} differences")
    return 1 if result.differences else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
