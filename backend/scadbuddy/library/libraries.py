"""Third-party OpenSCAD libraries: pinned git checkouts, a lockfile, per-model declaration.

OpenSCAD has no package manager, but it has ``OPENSCADPATH``, and every widely
used library is a git repository with tags. So a library here is exactly the
upstream repository at a commit (#93) -- no package format, no registry:

- **Checkouts** live at ``<data>/libraries/<name>/<commit>/<name>/``. The parent
  of the last ``<name>`` is what goes on ``OPENSCADPATH``, so ``use
  <BOSL2/std.scad>`` resolves exactly as it does on a desktop install. Keyed by
  commit, so a new pin never disturbs a render reading the old one, and an old
  model revision can still be rendered against the pin it was written with.
- **The lockfile** is ``libraries.lock`` at the root of the models repository
  (#90): name -> url, ref, commit. It is versioned with the models, so a model
  revision and the pins it rendered with are one history.
- **The declaration** is ``libraries`` in a model's ``model.json``. A render puts
  only those on ``OPENSCADPATH``, never every library ever installed.

Clones use the same hermetic git as the history (:func:`git_env`), with
``GIT_ALLOW_PROTOCOL`` narrowing the transports to the ones this store was built
with -- ``https`` in production, so a user-added URL cannot name a local path or
one of git's command-running transports.

A URL that is not the catalogue's is vetted like the URL import's
(:func:`~scadbuddy.library.url_import.public_addresses`): refused unless every
address its host resolves to is public, so a client cannot aim a clone at the
cluster's own network. The clone is then held to those addresses
(``http.curloptResolve``) and never follows a redirect, so neither a second
lookup nor a ``Location`` can move it somewhere that was not vetted.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel

from scadbuddy.core.paths import MODEL_META_NAME, DataPaths
from scadbuddy.library.history import (
    GIT,
    GitError,
    ModelHistory,
    RevisionNotFoundError,
    git_env,
)
from scadbuddy.library.url_import import ImportRefusedError, public_addresses

logger = logging.getLogger(__name__)

LOCKFILE_NAME = "libraries.lock"
#: A directory name OpenSCAD can `use <NAME/...>`: no separators, no dot-files.
NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
#: A branch or tag, never with a leading dash (it would read as an option). `..`
#: is refused on top of this: git refuses it in a ref anyway, and the pattern has
#: to stay free of look-arounds to double as the API's own validation.
REF_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$"
# A clone crosses the network, unlike every other git call here, and NopSCADlib
# is large; a stalled one still has to give its executor slot back.
CLONE_TIMEOUT = 300.0
STAGING_PREFIX = ".staging-"
DEFAULT_PORTS = {"http": 80, "https": 443}


class CatalogueLibrary(BaseModel):
    """One library ScadBuddy knows how to fetch."""

    name: str
    url: str
    ref: str
    licence: str
    homepage: str


class LibraryPin(BaseModel):
    """One entry of ``libraries.lock``: where it came from, what was asked for, what
    that resolved to."""

    url: str
    ref: str
    commit: str


class LibraryEntry(BaseModel):
    """The catalogue and the lockfile, joined: what can be added, and what is."""

    name: str
    url: str
    ref: str
    licence: str | None = None
    homepage: str | None = None
    curated: bool
    pin: LibraryPin | None = None


#: The curated catalogue. Refs are the latest release tag when this was written
#: (MCAD has none since 2019, so it follows its branch); a user can pin any other.
CURATED: tuple[CatalogueLibrary, ...] = (
    CatalogueLibrary(
        name="BOSL2",
        url="https://github.com/BelfrySCAD/BOSL2.git",
        ref="v2.0.761",
        licence="BSD-2-Clause",
        homepage="https://github.com/BelfrySCAD/BOSL2",
    ),
    CatalogueLibrary(
        name="dotSCAD",
        url="https://github.com/JustinSDK/dotSCAD.git",
        ref="v3.3",
        licence="LGPL-3.0",
        homepage="https://github.com/JustinSDK/dotSCAD",
    ),
    CatalogueLibrary(
        name="NopSCADlib",
        url="https://github.com/nophead/NopSCADlib.git",
        ref="v21.43.1",
        licence="GPL-3.0",
        homepage="https://github.com/nophead/NopSCADlib",
    ),
    CatalogueLibrary(
        name="Round-Anything",
        url="https://github.com/Irev-Dev/Round-Anything.git",
        ref="1.0.4",
        licence="MIT",
        homepage="https://github.com/Irev-Dev/Round-Anything",
    ),
    CatalogueLibrary(
        name="MCAD",
        url="https://github.com/openscad/MCAD.git",
        ref="master",
        licence="LGPL-2.1",
        homepage="https://github.com/openscad/MCAD",
    ),
)


class LibraryError(RuntimeError):
    """A library could not be added: a bad name, ref or URL, or the clone failed."""


class LibraryFetchError(LibraryError):
    """git could not fetch it: an unknown ref, an unreachable URL, a timeout."""


class LibraryNotFoundError(KeyError):
    """Not in the catalogue, and no URL was given to add it by."""


class LibraryNotInstalledError(LookupError):
    """A model declares a library that has no pin, or whose checkout is gone."""


# ── the lockfile ──────────────────────────────────────────────────────────────


def _parse_lock(raw: str) -> dict[str, LibraryPin]:
    loaded: Any = json.loads(raw)
    if not isinstance(loaded, dict):
        return {}
    return {name: LibraryPin.model_validate(pin) for name, pin in loaded.items()}


def _lock_body(pins: Mapping[str, LibraryPin]) -> str:
    # Sorted, so a pin change is a one-entry diff in the history.
    return json.dumps({name: pins[name].model_dump() for name in sorted(pins)}, indent=2) + "\n"


def read_pins(paths: DataPaths) -> dict[str, LibraryPin]:
    """The lockfile as it is now: what a render of a live model uses."""
    lock = paths.models / LOCKFILE_NAME
    if not lock.is_file():
        return {}
    return _parse_lock(lock.read_text(encoding="utf-8"))


def pins_at(history: ModelHistory, commit: str) -> dict[str, LibraryPin]:
    """The lockfile as it was at ``commit``: what that revision rendered with."""
    try:
        raw = history.show(commit, LOCKFILE_NAME)
    except RevisionNotFoundError:
        # Older than the first pin.
        return {}
    return _parse_lock(raw.decode("utf-8"))


def _write_pins(paths: DataPaths, pins: Mapping[str, LibraryPin]) -> None:
    (paths.models / LOCKFILE_NAME).write_text(_lock_body(pins), encoding="utf-8")


# ── a model's declaration ─────────────────────────────────────────────────────


def _libraries_of(meta: Any) -> list[str]:
    libraries = meta.get("libraries") if isinstance(meta, dict) else None
    return [str(name) for name in libraries] if isinstance(libraries, list) else []


def declared_libraries(model_dir: Path) -> list[str]:
    """``libraries`` from a model directory's ``model.json`` -- the live one or an
    exported revision, which is an ordinary model directory too."""
    meta = model_dir / MODEL_META_NAME
    if not meta.is_file():
        return []
    return _libraries_of(json.loads(meta.read_text(encoding="utf-8")))


def search_path(
    paths: DataPaths, declared: Sequence[str], pins: Mapping[str, LibraryPin]
) -> tuple[Path, ...]:
    """The ``OPENSCADPATH`` for a model: one checkout per library it declares.

    A declared library with no pin, or whose checkout is not on the volume, is an
    error rather than a silent omission: OpenSCAD only WARNs on a missing ``use``,
    so leaving it off would render a model with half its geometry missing.
    """
    directories: list[Path] = []
    for name in declared:
        pin = pins.get(name)
        if pin is None:
            raise LibraryNotInstalledError(f"{name!r} is declared but has no pin; add it first")
        directory = paths.libraries / name / pin.commit
        if not (directory / name).is_dir():
            raise LibraryNotInstalledError(
                f"{name!r} is pinned to {pin.commit[:7]}, which is not on this volume; "
                f"add it again at {pin.ref!r}"
            )
        directories.append(directory)
    return tuple(directories)


def model_search_path(paths: DataPaths, slug: str) -> tuple[Path, ...]:
    """:func:`search_path` for a live model: its declaration, the lockfile as it is."""
    return search_path(paths, declared_libraries(paths.model_dir(slug)), read_pins(paths))


def restore_pins(history: ModelHistory, paths: DataPaths, slug: str, commit: str) -> list[str]:
    """Put back the pins ``slug`` rendered with at ``commit``, for the libraries it
    declared then. Every other pin stays where it is.

    The ``also`` hook of :meth:`ModelHistory.restore`: it runs under the history's
    write lock, and what it returns is staged into the restore's own commit, so the
    model and its pins come back as one revision.
    """
    try:
        meta = json.loads(history.show(commit, f"{slug}/{MODEL_META_NAME}"))
    except RevisionNotFoundError:
        return []
    old = pins_at(history, commit)
    wanted = {name: old[name] for name in _libraries_of(meta) if name in old}
    if not wanted:
        return []
    _write_pins(paths, {**read_pins(paths), **wanted})
    return [LOCKFILE_NAME]


# ── installing ────────────────────────────────────────────────────────────────


def _same_repository(first: str, second: str) -> bool:
    """``https://host/o/r``, ``.../r.git`` and ``.../r/`` all name one repository,
    whatever the case of the scheme and host (the path's case is significant)."""

    def bare(url: str) -> str:
        parts = urlsplit(url.rstrip("/").removesuffix(".git").rstrip("/"))
        return urlunsplit(parts._replace(scheme=parts.scheme.lower(), netloc=parts.netloc.lower()))

    return bare(first) == bare(second)


@dataclass
class _NameLock:
    """One library name's install lock, and how many adds hold or wait for it."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    users: int = 0


class LibraryStore:
    """``<data>/libraries/`` and the lockfile that pins them.

    Sync on purpose, like :class:`ModelHistory`: a clone is a subprocess, so an
    ``async`` caller hands it to :func:`asyncio.to_thread`.
    """

    def __init__(
        self,
        paths: DataPaths,
        history: ModelHistory,
        *,
        catalogue: Sequence[CatalogueLibrary] = CURATED,
        protocols: Sequence[str] = ("https",),
        git: str = GIT,
        timeout: float = CLONE_TIMEOUT,
    ) -> None:
        self.paths = paths
        self.history = history
        self.catalogue = {entry.name: entry for entry in catalogue}
        self.protocols = tuple(protocols)
        self.git = git
        self.timeout = timeout
        # Serialises the lockfile's read-modify-write when there is no history
        # to do it under; with one, its own write lock does.
        self._lock = threading.Lock()
        # One lock per library name, held from reading the URL the name is bound
        # to until its pin is recorded: two adds of the same new name must not
        # both find it unbound. Per name rather than one lock for every add,
        # because a clone can take minutes (NopSCADlib) and adding BOSL2 should
        # not queue behind it (the API caps clones in flight separately, see
        # AppState.installs). Counted, so an entry goes once nobody holds or
        # waits for it and the table never outgrows the adds in flight.
        self._names: dict[str, _NameLock] = {}
        self._names_guard = threading.Lock()

    def entries(self) -> list[LibraryEntry]:
        pins = read_pins(self.paths)
        listed = [
            LibraryEntry(**entry.model_dump(), curated=True, pin=pins.get(entry.name))
            for entry in self.catalogue.values()
        ]
        listed += [
            LibraryEntry(name=name, url=pin.url, ref=pin.ref, curated=False, pin=pin)
            for name, pin in sorted(pins.items())
            if name not in self.catalogue
        ]
        return listed

    def install(self, name: str, *, url: str | None = None, ref: str | None = None) -> LibraryPin:
        """Clone ``name`` at ``ref`` and pin it to the commit that resolved to.

        ``url`` and ``ref`` default to the catalogue's. The pin is one revision of
        the models repository; re-pinning the commit already pinned is none.
        """
        if not re.fullmatch(NAME_PATTERN, name):
            raise LibraryError(f"{name!r} is not a usable library name")
        # A URL is recorded in the pin, logged and quoted back in errors, so one that
        # carries credentials is refused before any of that -- without quoting it.
        if url is not None and "@" in urlsplit(url).netloc:
            raise LibraryError("a library URL must not carry a user name or password")
        with self._names_guard:
            entry = self._names.setdefault(name, _NameLock())
            entry.users += 1
        try:
            with entry.lock:
                return self._install_locked(name, url, ref)
        finally:
            with self._names_guard:
                entry.users -= 1
                if entry.users == 0:
                    del self._names[name]

    def _install_locked(self, name: str, url: str | None, ref: str | None) -> LibraryPin:
        known = self.catalogue.get(name)
        # A name is bound to one upstream: the catalogue's for a curated library,
        # the one it was first added from otherwise. Every model declaring it
        # trusts that upstream, so a different URL under the same name would
        # silently swap the code they render with.
        recorded = read_pins(self.paths).get(name)
        bound = known.url if known is not None else recorded.url if recorded else None
        if url is None:
            if bound is None:
                raise LibraryNotFoundError(name)
            url = bound
        elif bound is not None:
            if _same_repository(url, bound):
                url = bound
            else:
                raise LibraryError(f"{name!r} comes from {bound}; add {url} under another name")
        if ref is None:
            if known is None:
                raise LibraryError(f"a library from {url} needs a ref to pin")
            ref = known.ref
        if not re.fullmatch(REF_PATTERN, ref) or ".." in ref:
            raise LibraryError(f"{ref!r} is not a usable branch or tag name")
        scheme = urlsplit(url).scheme.lower()
        if scheme not in self.protocols:
            raise LibraryError(f"{url!r} is not a {' or '.join(self.protocols)} URL")
        # The catalogue's own URLs are trusted as they are; anything a client named
        # -- now or when it first bound the name -- is vetted on every clone.
        pinned = self._vet(url) if known is None else ()

        commit = self._clone(name, url, ref, pinned)
        pin = LibraryPin(url=url, ref=ref, commit=commit)
        self._record(name, pin)
        return pin

    def _vet(self, url: str) -> tuple[str, ...]:
        """Refuse ``url`` unless its host resolves only to public addresses, and
        return the git config that holds the clone to exactly those addresses.

        ``file`` has no host to vet; it is only ever allowed in tests.
        """
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        if scheme == "file":
            return ()
        try:
            host, port = parts.hostname, parts.port
        except ValueError:
            host = port = None
        if not host:
            raise LibraryError(f"{url!r} does not name a host")
        port = port or DEFAULT_PORTS.get(scheme, 443)
        try:
            # `install` runs off the loop (the route hands it to a thread), so the
            # import's async lookup gets a loop of its own here.
            addresses = asyncio.run(public_addresses(host, port))
        except ImportRefusedError as error:
            raise LibraryError(str(error)) from None
        try:
            ipaddress.ip_address(host)
        except ValueError:
            resolved = ",".join(
                f"[{address}]" if ":" in address else address
                for address in dict.fromkeys(addresses)
            )
            return ("-c", f"http.curloptResolve={host}:{port}:{resolved}")
        # An address literal: there is no second lookup to pin.
        return ()

    def _clone(self, name: str, url: str, ref: str, pinned: Sequence[str] = ()) -> str:
        """Clone into a staging directory beside the checkouts, then move it into
        place under the commit it resolved to. Nothing half-cloned is ever at a
        path a render could read."""
        self.paths.libraries.mkdir(parents=True, exist_ok=True)
        staging = self.paths.libraries / f"{STAGING_PREFIX}{uuid.uuid4().hex}"
        try:
            try:
                self._git(
                    *pinned,
                    "clone",
                    "--quiet",
                    "--depth",
                    "1",
                    "--branch",
                    ref,
                    "--",
                    url,
                    str(staging / name),
                )
            except LibraryFetchError as error:
                raise LibraryFetchError(f"could not clone {ref!r} from {url}: {error}") from error
            commit = self._git("-C", str(staging / name), "rev-parse", "HEAD")
            destination = self.paths.libraries / name / commit
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(staging, destination)
            except OSError:
                # Already cloned at this commit, by an earlier install or a
                # concurrent one; either copy is the same tree.
                if not (destination / name).is_dir():
                    raise
            return commit
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def _git(self, *args: str) -> str:
        env = {**git_env(), "GIT_ALLOW_PROTOCOL": ":".join(self.protocols)}
        try:
            # A fixed argv with no shell; the URL and ref are validated above and
            # the URL follows `--`.
            # No redirects: a vetted host must not hand the clone on to one that
            # was not.
            completed = subprocess.run(
                [
                    self.git,
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-c",
                    "http.followRedirects=false",
                    *args,
                ],
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as error:
            raise LibraryFetchError(f"git timed out after {self.timeout:g}s") from error
        except OSError as error:
            raise LibraryFetchError(f"could not run {self.git!r}: {error}") from error
        if completed.returncode != 0:
            # git's stderr stays in the log: it describes what the fetch reached,
            # or failed to, which is no business of the client's.
            logger.warning(
                "git failed", extra={"git_args": args, "git_stderr": completed.stderr.strip()}
            )
            raise LibraryFetchError("no such ref, or the repository could not be reached")
        return completed.stdout.strip()

    def _record(self, name: str, pin: LibraryPin) -> None:
        def write() -> None:
            _write_pins(self.paths, {**read_pins(self.paths), name: pin})

        if not self.history.available:
            with self._lock:
                write()
            return
        message = f"Pin {name} to {pin.ref} ({pin.commit[:7]})"
        try:
            # The read-modify-write runs under the history's write lock, so a restore
            # putting an old pin back cannot interleave with it and lose one of the two.
            self.history.commit(message, LOCKFILE_NAME, prepare=write)
        except (GitError, OSError):
            # As `Catalogue._commit`: once `write` has run the pin is live, since
            # renders read the lockfile, not HEAD, and a lost revision is the smaller
            # harm than telling the client an add failed that did not. A failure
            # before `write` (the lock wait) left the old pin, and that one raises.
            if read_pins(self.paths).get(name) != pin:
                raise
            logger.exception("could not record a revision", extra={"revision_message": message})
