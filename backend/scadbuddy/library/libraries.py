"""Third-party OpenSCAD libraries: pinned git checkouts, pinned per model.

OpenSCAD has no package manager, but it has ``OPENSCADPATH``, and every widely
used library is a git repository with tags. So a library here is exactly the
upstream repository at a commit (#93) -- no package format, no registry:

- **Checkouts** live at ``<data>/libraries/<name>/<commit>/<name>/``. The parent
  of the last ``<name>`` is what goes on ``OPENSCADPATH``, so ``use
  <BOSL2/std.scad>`` resolves exactly as it does on a desktop install. Keyed by
  commit, so a new pin never disturbs a render reading the old one, and an old
  model revision can still be rendered against the pin it was written with. A
  checkout is a cache shared by every model pinned to that commit; nothing about
  it is a setting.
- **The pins** are ``libraries`` in each model's ``model.json``: name -> url, ref,
  commit, for that model alone. A render puts only those on ``OPENSCADPATH``.
  Two models can pin the same library at different refs, or at different
  upstreams (a fork under the same ``use <NAME/...>``), and moving one never
  moves the other. Being in ``model.json``, the pins are versioned with the
  model: a revision, a restore, a duplicate all carry the pins that go with it.

Before per-model pins, ``libraries`` was a list of names and the pins lived in one
shared ``libraries.lock`` at the root of the models repository.
:func:`migrate_lockfile` moves the live pins into each model once, at boot; an
old revision that still declares by name renders against the lockfile as it was
at that revision (:func:`revision_search_path`), and restoring one writes those
pins into the model (:func:`pin_restored_declaration`).

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
import contextlib
import ipaddress
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from scadbuddy.core.config import DEFAULT_LIBRARY_MAX_BYTES
from scadbuddy.core.paths import MODEL_META_NAME, DataPaths, is_builtin, model_path
from scadbuddy.library.history import (
    GIT,
    GitError,
    ModelHistory,
    RevisionNotFoundError,
    git_env,
)
from scadbuddy.library.url_import import ImportRefusedError, public_addresses

logger = logging.getLogger(__name__)

#: The shared lockfile pins lived in before they moved into each model. Only read:
#: once at boot to migrate it, and at old revisions that still declare by name.
LOCKFILE_NAME = "libraries.lock"
#: A directory name OpenSCAD can `use <NAME/...>`: no separators, no dot-files.
NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
#: A branch or tag, never with a leading dash (it would read as an option) and
#: never with `..` (git refuses it in a ref anyway). The look-ahead needs Python's
#: `re`: a pydantic model using this sets ``regex_engine="python-re"``.
REF_PATTERN = r"^(?!.*\.\.)[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$"
#: A full SHA-1 or SHA-256 object name, as `git rev-parse HEAD` prints it.
COMMIT_PATTERN = r"^[0-9a-f]{40}([0-9a-f]{24})?$"
# A clone crosses the network, unlike every other git call here, and NopSCADlib
# is large; a stalled one still has to give its executor slot back.
CLONE_TIMEOUT = 300.0
STAGING_PREFIX = ".staging-"
#: Seconds past the clone timeout before the boot sweep treats a staging clone as
#: abandoned: room for the ``rev-parse`` and move after the clone, and for clocks
#: that differ between replicas sharing ``/data``.
STAGING_MAX_AGE_MARGIN = 600.0
DEFAULT_PORTS = {"http": 80, "https": 443}


class CatalogueLibrary(BaseModel):
    """One library ScadBuddy knows how to fetch, and the ref it suggests."""

    name: str
    url: str
    ref: str
    licence: str
    homepage: str


class LibraryPin(BaseModel):
    """Where a library came from, what was asked for, what that resolved to."""

    # REF_PATTERN refuses `..` with a look-ahead, which pydantic's default engine lacks.
    model_config = ConfigDict(regex_engine="python-re")

    url: str
    ref: str = Field(pattern=REF_PATTERN)
    commit: str = Field(pattern=COMMIT_PATTERN)


class ModelLibrary(LibraryPin):
    """One entry of a model's ``libraries``: a library pinned for that model."""

    name: str = Field(pattern=NAME_PATTERN, description="The directory `use <NAME/...>` names")


#: The curated catalogue. Refs are the latest release tag when this was written
#: (MCAD has none since 2019, so it follows its branch); a model can pin any other.
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
    """A library could not be pinned: a bad name, ref or URL, or the clone failed."""


class LibraryFetchError(LibraryError):
    """git could not fetch it: an unknown ref, an unreachable URL, a timeout."""


class LibraryTooLargeError(LibraryError):
    """The clone is larger than the store allows (``SCADBUDDY_LIBRARY_MAX_BYTES``)."""


class LibraryNotFoundError(KeyError):
    """Not in the catalogue, and no URL was given to pin it from."""


class LibraryNotInstalledError(LookupError):
    """A model declares a library that has no pin, or whose checkout is gone."""


class LibraryDeclarationError(RuntimeError):
    """A model's ``libraries`` is not what ScadBuddy writes -- a hand edit, most
    likely -- or names a pin in a legacy ``libraries.lock`` that cannot be read."""


# ── the legacy lockfile ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class Lock:
    """``libraries.lock``, parsed entry by entry.

    A hand-edited entry that is not a valid pin is kept out of ``pins`` and held
    in ``broken`` by name, so it fails only the models that declare it. A file
    that is not a JSON object at all has no names to hold apart: ``unreadable``
    says why, and it fails every name looked up in it.
    """

    pins: dict[str, LibraryPin] = field(default_factory=dict)
    #: name -> why it is not valid.
    broken: dict[str, str] = field(default_factory=dict)
    unreadable: str | None = None

    def pin(self, name: str) -> LibraryPin | None:
        """``name``'s pin, ``None`` when it has none, or
        :class:`LibraryDeclarationError` when its entry -- or the whole file --
        cannot be read."""
        if self.unreadable is not None:
            raise LibraryDeclarationError(self.unreadable)
        if name in self.broken:
            raise LibraryDeclarationError(self.broken[name])
        return self.pins.get(name)


def _problems(error: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(part) for part in detail['loc']) or 'entry'}: {detail['msg']}"
        for detail in error.errors()
    )


def _parse_lock(raw: str) -> Lock:
    try:
        loaded: Any = json.loads(raw)
    except json.JSONDecodeError as error:
        return Lock(unreadable=f"{LOCKFILE_NAME} is not valid JSON: {error}")
    if not isinstance(loaded, dict):
        return Lock(unreadable=f"{LOCKFILE_NAME} is not a JSON object")
    pins: dict[str, LibraryPin] = {}
    broken: dict[str, str] = {}
    for name, pin in loaded.items():
        try:
            pins[name] = LibraryPin.model_validate(pin)
        except ValidationError as error:
            broken[name] = f"{LOCKFILE_NAME} entry {name!r} is not valid: {_problems(error)}"
    return Lock(pins=pins, broken=broken)


def read_lock(paths: DataPaths) -> Lock | None:
    """The legacy lockfile as it is on the volume, or ``None`` once migrated."""
    lock = paths.models / LOCKFILE_NAME
    if not lock.is_file():
        return None
    return _parse_lock(lock.read_text(encoding="utf-8"))


def lock_at(history: ModelHistory, commit: str) -> Lock:
    """The legacy lockfile as it was at ``commit``: what that revision rendered with."""
    try:
        raw = history.show(commit, LOCKFILE_NAME)
    except RevisionNotFoundError:
        # Older than the first pin, or newer than the migration.
        return Lock()
    return _parse_lock(raw.decode("utf-8"))


# ── a model's declaration ─────────────────────────────────────────────────────

#: One entry of ``libraries``: a pin, or -- written before pins moved into the
#: model -- a bare name that the legacy lockfile pins.
Declared = ModelLibrary | str


def entry_name(entry: Any) -> str | None:
    """The library an entry of ``libraries`` names, however it is written."""
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict) and isinstance(entry.get("name"), str):
        return str(entry["name"])
    return None


def parse_declaration(meta: Any) -> list[Declared]:
    """``libraries`` from a parsed ``model.json``.

    Strict, unlike the rest of the metadata: an entry that cannot be read is an
    error rather than left out, because OpenSCAD only WARNs on a missing ``use``
    and a render without it would come out with half its geometry missing.
    """
    raw = meta.get("libraries") if isinstance(meta, dict) else None
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise LibraryDeclarationError(f"`libraries` in {MODEL_META_NAME} is not a list")
    declared: list[Declared] = []
    for entry in raw:
        if isinstance(entry, str):
            declared.append(entry)
            continue
        try:
            declared.append(ModelLibrary.model_validate(entry))
        except ValidationError as error:
            name = entry_name(entry)
            label = repr(name) if name is not None else "an entry"
            raise LibraryDeclarationError(
                f"{MODEL_META_NAME} library {label} is not valid: {_problems(error)}; pin it again"
            ) from None
    return declared


def declared_libraries(model_dir: Path) -> list[Declared]:
    """``libraries`` from a model directory's ``model.json`` -- the live one or an
    exported revision, which is an ordinary model directory too."""
    meta = model_dir / MODEL_META_NAME
    if not meta.is_file():
        return []
    return parse_declaration(json.loads(meta.read_text(encoding="utf-8")))


def _by_name(declared: Iterable[Declared]) -> bool:
    return any(isinstance(entry, str) for entry in declared)


def search_path(
    paths: DataPaths, declared: Sequence[Declared], legacy: Lock | None = None
) -> tuple[Path, ...]:
    """The ``OPENSCADPATH`` for a model: one checkout per library it declares.

    A declared library with no pin, or whose checkout is not on the volume, is an
    error rather than a silent omission: OpenSCAD only WARNs on a missing ``use``,
    so leaving it off would render a model with half its geometry missing. A bare
    name is looked up in ``legacy``, the lockfile of its time.
    """
    directories: list[Path] = []
    for entry in declared:
        if isinstance(entry, str):
            name = entry
            pin = legacy.pin(name) if legacy is not None else None
            if pin is None:
                raise LibraryNotInstalledError(
                    f"{name!r} is declared but has no pin; pin it to this model again"
                )
        else:
            name, pin = entry.name, entry
        directory = paths.libraries / name / pin.commit
        if not (directory / name).is_dir():
            raise LibraryNotInstalledError(
                f"{name!r} is pinned to {pin.commit[:7]}, which is not on this volume; "
                f"pin it to this model again at {pin.ref!r}"
            )
        directories.append(directory)
    return tuple(directories)


def model_search_path(paths: DataPaths, slug: str) -> tuple[Path, ...]:
    """:func:`search_path` for a live model: its own pins."""
    declared = declared_libraries(paths.model_dir(slug))
    # Only a model the migration could not pin still declares by name, and the
    # lockfile is gone by then; read it anyway in case the migration never ran.
    return search_path(paths, declared, read_lock(paths) if _by_name(declared) else None)


def revision_search_path(
    history: ModelHistory, paths: DataPaths, directory: Path, commit: str
) -> tuple[Path, ...]:
    """:func:`search_path` for an exported revision: its own pins, or -- written
    before pins moved into the model -- the lockfile as it was at ``commit``."""
    declared = declared_libraries(directory)
    return search_path(paths, declared, lock_at(history, commit) if _by_name(declared) else None)


def _pin_names(meta: dict[str, Any], lock: Lock) -> bool:
    """Replace every bare name in ``meta``'s ``libraries`` that ``lock`` pins with
    that pin. One it does not pin stays a name, and a render of it says so."""
    libraries = meta.get("libraries")
    if not isinstance(libraries, list) or not any(isinstance(e, str) for e in libraries):
        return False
    rewritten: list[Any] = []
    for entry in libraries:
        pin = lock.pins.get(entry) if isinstance(entry, str) else None
        rewritten.append(
            ModelLibrary(name=entry, **pin.model_dump()).model_dump()
            if isinstance(entry, str) and pin is not None
            else entry
        )
    if rewritten == libraries:
        return False
    meta["libraries"] = rewritten
    return True


def _rewrite_meta(meta_path: Path, lock: Lock) -> bool:
    try:
        meta: Any = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(meta, dict) or not _pin_names(meta, lock):
        return False
    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return True


def pin_restored_declaration(
    history: ModelHistory, paths: DataPaths, slug: str, commit: str
) -> list[str]:
    """The ``also`` hook of :meth:`ModelHistory.restore`: a revision that declares
    by name comes back with the pins the lockfile gave it at ``commit`` written
    into its ``model.json``. Nothing outside the model moves.

    A revision written with per-model pins already carries them; it is left as
    checked out.
    """
    _rewrite_meta(paths.model_meta(slug), lock_at(history, commit))
    # `model.json` is under the slug, which the restore commits anyway.
    return []


def _needs_lock(paths: DataPaths, slug: str, lock: Lock) -> bool:
    """Does ``slug`` declare by name a library ``lock`` pins?"""
    try:
        meta: Any = json.loads(paths.model_meta(slug).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    libraries = meta.get("libraries") if isinstance(meta, dict) else None
    return isinstance(libraries, list) and any(
        isinstance(entry, str) and entry in lock.pins for entry in libraries
    )


def migrate_lockfile(
    paths: DataPaths, history: ModelHistory | None, slugs: Sequence[str]
) -> list[str]:
    """Move the pins of a legacy ``libraries.lock`` into the models that declare
    them, and remove it, as one revision. Returns the slugs rewritten.

    Only a user's own models: a built-in's ``model.json`` is the image's, mirrored
    by the boot sync and written by nothing else, which would put a rewrite back on
    the next boot. A built-in still declaring by name reads the lockfile at render
    time instead (:func:`model_search_path`), so the lockfile stays while one needs
    it; it goes once the image pins its own libraries.

    A lockfile that cannot be read at all is left where it is, so nothing is lost;
    each model still declaring by name reads it until someone pins its libraries
    again. A single broken entry leaves that name unpinned in the models that use
    it, which then say so on their next render.
    """
    lock = read_lock(paths)
    if lock is None:
        return []
    if lock.unreadable is not None:
        logger.warning("legacy library lockfile not migrated", extra={"reason": lock.unreadable})
        return []
    mine = [slug for slug in slugs if not is_builtin(slug)]
    kept_for = [slug for slug in slugs if is_builtin(slug) and _needs_lock(paths, slug, lock)]
    changed: list[str] = []

    def migrate() -> None:
        for slug in mine:
            if _rewrite_meta(paths.model_meta(slug), lock):
                changed.append(slug)
        if kept_for:
            logger.info("legacy library lockfile kept for built-ins", extra={"slugs": kept_for})
        else:
            (paths.models / LOCKFILE_NAME).unlink(missing_ok=True)

    message = "Move library pins from libraries.lock into each model"
    if history is None or not history.available:
        migrate()
        return changed
    try:
        history.commit(
            message, LOCKFILE_NAME, *(model_path(slug) for slug in mine), prepare=migrate
        )
    except (GitError, OSError):
        # As `Catalogue._commit`: the files are what renders read; a lost revision
        # is the smaller harm.
        logger.exception("could not record a revision", extra={"revision_message": message})
    return changed


# ── installing ────────────────────────────────────────────────────────────────


# How often a running clone's staging directory is measured against the cap:
# every SIZE_POLL_INTERVAL, or nine times as long as the last walk took so that
# walking costs at most about a tenth of the clone's wall time, but never less
# often than SIZE_POLL_MAX_INTERVAL. A clone can overshoot the cap by what it
# transfers in one interval plus one walk before it is killed. A walk stops once it
# has counted past the cap, but its cost grows with the number of files, so for a
# repository of very many small files that walk can take seconds; CLONE_TIMEOUT
# still bounds the whole clone.
SIZE_POLL_INTERVAL = 0.2
SIZE_POLL_MAX_INTERVAL = 2.0
# How long to wait for a killed git to be reaped before giving up on it.
KILL_WAIT = 5.0


def _tree_size(root: Path, limit: int | None = None) -> int:
    """Bytes of every file under ``root``, ``.git`` included: what it takes on the
    volume. Symlinks count as themselves, never what they point at. A file git
    renames or removes mid-walk (a running clone's temporaries) counts as nothing.
    With ``limit``, it returns as soon as the count passes it, rather than walking
    the rest of a tree already known to be too large."""
    total = 0
    for directory, _, files in os.walk(root):
        for file in files:
            with contextlib.suppress(FileNotFoundError):
                total += (Path(directory) / file).lstat().st_size
            if limit is not None and total > limit:
                return total
    return total


def _size(n: int) -> str:
    return f"{n / 1e6:.0f} MB" if n >= 1_000_000 else f"{n} bytes"


def _same_repository(first: str, second: str) -> bool:
    """``https://host/o/r``, ``.../r.git`` and ``.../r/`` all name one repository,
    whatever the case of the scheme and host (the path's case is significant)."""

    def bare(url: str) -> str:
        parts = urlsplit(url.rstrip("/").removesuffix(".git").rstrip("/"))
        return urlunsplit(parts._replace(scheme=parts.scheme.lower(), netloc=parts.netloc.lower()))

    return bare(first) == bare(second)


class LibraryStore:
    """``<data>/libraries/``: the checkouts every model's pins point into.

    It records nothing: :meth:`resolve` fetches and returns a pin, and the model
    it is for stores it (:meth:`~scadbuddy.library.catalogue.Catalogue.pin_library`).

    Sync on purpose, like :class:`ModelHistory`: a clone is a subprocess, so an
    ``async`` caller hands it to :func:`asyncio.to_thread`.
    """

    def __init__(
        self,
        paths: DataPaths,
        *,
        catalogue: Sequence[CatalogueLibrary] = CURATED,
        protocols: Sequence[str] = ("https",),
        git: str = GIT,
        timeout: float = CLONE_TIMEOUT,
        max_bytes: int = DEFAULT_LIBRARY_MAX_BYTES,
    ) -> None:
        self.paths = paths
        self.catalogue = {entry.name: entry for entry in catalogue}
        self.protocols = tuple(protocols)
        self.git = git
        self.timeout = timeout
        self.max_bytes = max_bytes

    def sweep_staging(self) -> list[str]:
        """Remove the staging clones an install killed mid-clone left behind.

        Runs at boot. Another replica sharing ``/data`` may be mid-clone, so only
        staging older than the clone timeout plus ``STAGING_MAX_AGE_MARGIN`` goes.
        """
        root = self.paths.libraries
        if not root.is_dir():
            return []
        cutoff = time.time() - self.timeout - STAGING_MAX_AGE_MARGIN
        removed: list[str] = []
        for entry in sorted(root.iterdir()):
            if not entry.name.startswith(STAGING_PREFIX):
                continue
            try:
                if entry.stat().st_mtime > cutoff:
                    continue
            except FileNotFoundError:
                continue
            except OSError:
                logger.exception("could not read a staging clone", extra={"entry": entry.name})
                continue
            # One that cannot go must not keep the rest; as the catalogue's
            # sweeps, log it and move on.
            try:
                shutil.rmtree(entry)
            except OSError:
                logger.exception("could not remove a staging clone", extra={"entry": entry.name})
                continue
            removed.append(entry.name)
        return removed

    def entries(self) -> list[CatalogueLibrary]:
        return list(self.catalogue.values())

    def resolve(self, name: str, *, url: str | None = None, ref: str | None = None) -> ModelLibrary:
        """Clone ``name`` at ``ref`` and return the pin: the commit that resolved to.

        ``url`` and ``ref`` default to the catalogue's. A curated name may be pinned
        from another repository -- a fork is still ``use <BOSL2/...>`` -- but only
        the catalogue's own URL skips the vetting, and another one needs a ``ref``.
        """
        if not re.fullmatch(NAME_PATTERN, name):
            raise LibraryError(f"{name!r} is not a usable library name")
        # A URL is recorded in the pin, logged and quoted back in errors, so one that
        # carries credentials is refused before any of that -- without quoting it.
        if url is not None and "@" in urlsplit(url).netloc:
            raise LibraryError("a library URL must not carry a user name or password")
        known = self.catalogue.get(name)
        if url is None:
            if known is None:
                raise LibraryNotFoundError(name)
            url = known.url
        trusted = known is not None and _same_repository(url, known.url)
        if trusted:
            assert known is not None
            url = known.url
        if ref is None:
            if not trusted:
                raise LibraryError(f"a library from {url} needs a ref to pin")
            assert known is not None
            ref = known.ref
        if not re.fullmatch(REF_PATTERN, ref) or ".." in ref:
            raise LibraryError(f"{ref!r} is not a usable branch or tag name")
        scheme = urlsplit(url).scheme.lower()
        if scheme not in self.protocols:
            raise LibraryError(f"{url!r} is not a {' or '.join(self.protocols)} URL")
        # The catalogue's own URLs are trusted as they are; anything a client named
        # is vetted on every clone.
        pinned = () if trusted else self._vet(url)
        commit = self._clone(name, url, ref, pinned)
        return ModelLibrary(name=name, url=url, ref=ref, commit=commit)

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
                    watch=staging,
                )
            except LibraryFetchError as error:
                raise LibraryFetchError(f"could not clone {ref!r} from {url}: {error}") from error
            except LibraryTooLargeError as error:
                raise LibraryTooLargeError(f"{url} at {ref!r} {error}") from None
            # The last poll can land before the clone's final writes.
            size = _tree_size(staging, self.max_bytes)
            if size > self.max_bytes:
                raise LibraryTooLargeError(f"{url} at {ref!r} {self._over(size)}")
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

    def _git(self, *args: str, watch: Path | None = None) -> str:
        """Run git; with ``watch``, also kill it once that directory grows past
        ``max_bytes``, so an oversized clone is stopped rather than finished."""
        env = {**git_env(), "GIT_ALLOW_PROTOCOL": ":".join(self.protocols)}
        try:
            # A fixed argv with no shell; the URL and ref are validated above and
            # the URL follows `--`.
            # No redirects: a vetted host must not hand the clone on to one that
            # was not.
            # Its own process group: a clone runs git-remote-https as a child, and on
            # a timeout that helper must go too, or a tarpit host keeps it (and the
            # socket) alive after the request has given up.
            process = subprocess.Popen(
                [
                    self.git,
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-c",
                    "http.followRedirects=false",
                    *args,
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        except OSError as error:
            raise LibraryFetchError(f"could not run {self.git!r}: {error}") from error
        deadline = time.monotonic() + self.timeout
        next_walk = time.monotonic() + SIZE_POLL_INTERVAL
        while True:
            now = time.monotonic()
            wait = max(deadline - now, 0)
            if watch is not None:
                wait = min(wait, max(next_walk - now, 0))
            try:
                stdout, stderr = process.communicate(timeout=wait)
                break
            except subprocess.TimeoutExpired as error:
                if time.monotonic() >= deadline:
                    self._kill(process)
                    raise LibraryFetchError(f"git timed out after {self.timeout:g}s") from error
                if watch is not None:
                    started = time.monotonic()
                    size = _tree_size(watch, self.max_bytes)
                    if size > self.max_bytes:
                        self._kill(process)
                        raise LibraryTooLargeError(self._over(size)) from None
                    walked = time.monotonic() - started
                    next_walk = time.monotonic() + min(
                        max(SIZE_POLL_INTERVAL, 9 * walked), SIZE_POLL_MAX_INTERVAL
                    )
        completed = subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
        if completed.returncode != 0:
            # git's stderr stays in the log: it describes what the fetch reached,
            # or failed to, which is no business of the client's.
            logger.warning(
                "git failed", extra={"git_args": args, "git_stderr": completed.stderr.strip()}
            )
            raise LibraryFetchError("no such ref, or the repository could not be reached")
        return completed.stdout.strip()

    def _over(self, size: int) -> str:
        return f"reached {_size(size)}, over the {_size(self.max_bytes)} a library may take"

    @staticmethod
    def _kill(process: subprocess.Popen[str]) -> None:
        # The whole group: a clone's git-remote-https child goes too. A git that
        # is not reaped in time (stuck in the kernel) is reaped by a daemon thread
        # whenever it does die, rather than holding up the caller's error or
        # staying a zombie.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        try:
            process.communicate(timeout=KILL_WAIT)
        except subprocess.TimeoutExpired:
            threading.Thread(target=process.wait, name="git-reaper", daemon=True).start()
