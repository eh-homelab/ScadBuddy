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
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from psycopg_pool import ConnectionPool
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from scadbuddy.core.config import DEFAULT_LIBRARY_MAX_BYTES
from scadbuddy.core.paths import MODEL_META_NAME, DataPaths
from scadbuddy.library.history import (
    GIT,
    git_env,
)
from scadbuddy.library.url_import import (
    ImportRefusedError,
    ResolverUnavailableError,
    public_addresses,
)

logger = logging.getLogger(__name__)

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


class LibraryResolverUnavailableError(LibraryError):
    """The URL's host could not be looked up just now -- the resolver busy or slow --
    which says nothing about whether it is public (#205)."""


class LibraryTooLargeError(LibraryError):
    """The clone is larger than the store allows (``SCADBUDDY_LIBRARY_MAX_BYTES``)."""


class LibraryNotFoundError(KeyError):
    """Not in the catalogue, and no URL was given to pin it from."""


class LibraryCheckoutNotFoundError(KeyError):
    """No checkout of that library (at that commit) is on the volume."""


def _require_name(name: str) -> None:
    if not re.fullmatch(NAME_PATTERN, name):
        raise LibraryError(f"{name!r} is not a usable library name")


class LibraryNotInstalledError(LookupError):
    """A model pins a library whose checkout is not on the volume."""


class LibraryCheckoutMissingError(LibraryNotInstalledError):
    """A declared pin whose checkout is not on the volume: one that the pin itself
    says how to fetch again (:class:`CheckoutFetcher`)."""

    def __init__(self, message: str, pin: ModelLibrary) -> None:
        super().__init__(message)
        self.pin = pin


class LibraryDeclarationError(RuntimeError):
    """A model's ``libraries`` is not what ScadBuddy writes: a hand edit, most
    likely, or an entry that names a library without pinning it."""


def _problems(error: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(part) for part in detail['loc']) or 'entry'}: {detail['msg']}"
        for detail in error.errors()
    )


# ── a model's declaration ─────────────────────────────────────────────────────


def entry_name(entry: Any) -> str | None:
    """The library an entry of ``libraries`` names, valid or not: a pin's ``name``,
    or a bare string. A bare string is not a pin (:func:`parse_declaration` refuses
    it), but it still names the library, so pinning that name again replaces it and
    removing that name removes it."""
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict) and isinstance(entry.get("name"), str):
        return str(entry["name"])
    return None


def parse_declaration(meta: Any) -> list[ModelLibrary]:
    """``libraries`` from a parsed ``model.json``.

    Strict, unlike the rest of the metadata: an entry that cannot be read is an
    error rather than left out, because OpenSCAD only WARNs on a missing ``use``
    and a render without it would come out with half its geometry missing.
    """
    raw = meta.get("libraries") if isinstance(meta, dict) else None
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise LibraryDeclarationError(NOT_A_LIST)
    declared: list[ModelLibrary] = []
    for entry in raw:
        checked = _check_entry(entry)
        if isinstance(checked, str):
            raise LibraryDeclarationError(checked)
        declared.append(checked)
    return declared


NOT_A_LIST = f"`libraries` in {MODEL_META_NAME} is not a list"


def _check_entry(entry: Any) -> ModelLibrary | str:
    """One entry of ``libraries`` as a pin, or why :func:`parse_declaration`
    refuses it."""
    if isinstance(entry, str):
        return f"{MODEL_META_NAME} names library {entry!r} without a pin; pin it again"
    try:
        return ModelLibrary.model_validate(entry)
    except ValidationError as error:
        name = entry_name(entry)
        label = repr(name) if name is not None else "an entry"
        return f"{MODEL_META_NAME} library {label} is not valid: {_problems(error)}; pin it again"


def entry_problem(entry: Any) -> str | None:
    """Why :func:`parse_declaration` refuses one entry of ``libraries``, or None
    for a pin."""
    checked = _check_entry(entry)
    return checked if isinstance(checked, str) else None


class InvalidLibraryEntry(BaseModel):
    """An entry of a model's ``libraries`` that is not a pin (#217): a hand edit.
    The model lists without it, but cannot render until it is removed or pinned
    again."""

    name: str | None = Field(
        description="The name `DELETE /models/{slug}/libraries/{name}` removes it by; "
        "null when it has none that route takes"
    )
    index: int | None = Field(
        description="Its position in model.json's `libraries`: the DELETE's `index`, which "
        "removes this entry alone; null when `libraries` is not a list"
    )
    problem: str = Field(description="Why it is not a pin: the render's 409 detail")


def invalid_entries(raw: Any) -> list[InvalidLibraryEntry]:
    """The entries of a ``model.json``'s ``libraries`` (``raw``) that
    :func:`parse_declaration` refuses, each with the reason it gives."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        return [InvalidLibraryEntry(name=None, index=None, problem=NOT_A_LIST)]
    invalid: list[InvalidLibraryEntry] = []
    for index, entry in enumerate(raw):
        problem = entry_problem(entry)
        if problem is None:
            continue
        name = entry_name(entry)
        if name is not None and not re.fullmatch(NAME_PATTERN, name):
            name = None
        invalid.append(InvalidLibraryEntry(name=name, index=index, problem=problem))
    return invalid


def declared_libraries(model_dir: Path) -> list[ModelLibrary]:
    """``libraries`` from a model directory's ``model.json`` -- the live one or an
    exported revision, which is an ordinary model directory too."""
    meta = model_dir / MODEL_META_NAME
    if not meta.is_file():
        return []
    return parse_declaration(json.loads(meta.read_text(encoding="utf-8")))


def search_path(paths: DataPaths, declared: Sequence[ModelLibrary]) -> tuple[Path, ...]:
    """The ``OPENSCADPATH`` for a model: one checkout per library it pins.

    A pin whose checkout is not on the volume is an error rather than a silent
    omission: OpenSCAD only WARNs on a missing ``use``, so leaving it off would
    render a model with half its geometry missing. It is
    :class:`LibraryCheckoutMissingError`, which :class:`CheckoutFetcher` answers by
    cloning it again.
    """
    directories: list[Path] = []
    for pin in declared:
        directory = paths.libraries / pin.name / pin.commit
        if not (directory / pin.name).is_dir():
            raise LibraryCheckoutMissingError(
                f"{pin.name!r} is pinned to {pin.commit[:7]}, which is not on this volume; "
                f"pin it to this model again at {pin.ref!r}",
                pin,
            )
        directories.append(directory)
    return tuple(directories)


def model_search_path(paths: DataPaths, slug: str) -> tuple[Path, ...]:
    """:func:`search_path` for a live model: its own pins."""
    return search_path(paths, declared_libraries(paths.model_dir(slug)))


def revision_search_path(paths: DataPaths, directory: Path) -> tuple[Path, ...]:
    """:func:`search_path` for an exported revision: the pins it was written with."""
    return search_path(paths, declared_libraries(directory))


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


def same_repository(first: str, second: str) -> bool:
    """``https://host/o/r``, ``.../r.git`` and ``.../r/`` all name one repository,
    whatever the case of the scheme and host (the path's case is significant)."""

    def bare(url: str) -> str:
        parts = urlsplit(url.rstrip("/").removesuffix(".git").rstrip("/"))
        return urlunsplit(parts._replace(scheme=parts.scheme.lower(), netloc=parts.netloc.lower()))

    return bare(first) == bare(second)


#: `pg_advisory_lock` key a removal holds and a lease's insert shares ("SCADLEAS").
REMOVAL_LOCK = 0x5343_4144_4C45_4153
#: Seconds a lease row lives unless its holder renews it; a holder renews every third.
LEASE_TTL = 60.0


class CheckoutLeases:
    """The render leases in Postgres (#872), so a removal in one process sees a render
    in another: the render worker and the API each build their own gate.

    A row lives ``ttl`` seconds by the database's clock unless renewed, so a holder
    that crashed blocks a removal for one TTL at most. A removal holds
    :data:`REMOVAL_LOCK` exclusively while it checks and deletes, and a lease is
    inserted holding it shared: a lease is either seen by the removal's check or taken
    after the removal ends (and then finds the checkout gone, :func:`require_checkouts`).
    """

    def __init__(self, pool: ConnectionPool[Any], root: Path, *, ttl: float = LEASE_TTL) -> None:
        self.pool = pool
        self.root = root
        self.ttl = ttl

    def take(self, holder: str, checkouts: Sequence[Path]) -> uuid.UUID:
        """Record a lease for ``holder`` on ``checkouts``; waits out a removal."""
        token = uuid.uuid4()
        rows = [(token, holder, path.parent.name, path.name, self.ttl) for path in checkouts]
        with self.pool.connection() as conn, conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock_shared(%s)", (REMOVAL_LOCK,))
            conn.execute("DELETE FROM library_leases WHERE expires_at <= now()")
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO library_leases (token, holder, library, commit, expires_at)"
                    " VALUES (%s, %s, %s, %s, now() + make_interval(secs => %s))",
                    rows,
                )
        return token

    def renew(self, token: uuid.UUID) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                "UPDATE library_leases SET expires_at = now() + make_interval(secs => %s)"
                " WHERE token = %s",
                (self.ttl, token),
            )

    def drop(self, token: uuid.UUID) -> None:
        with self.pool.connection() as conn:
            conn.execute("DELETE FROM library_leases WHERE token = %s", (token,))

    def holders(self, directory: Path) -> list[str]:
        """The live holders of ``directory`` -- one checkout, or a library's directory
        of them -- in the order they took their leases."""
        try:
            parts = directory.relative_to(self.root).parts
        except ValueError:
            return []
        if len(parts) not in (1, 2):
            return []
        # A library's directory matches every commit of it.
        commit = parts[1] if len(parts) == 2 else None
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT holder FROM library_leases WHERE library = %s"
                " AND (%s::text IS NULL OR commit = %s) AND expires_at > now()"
                " GROUP BY holder ORDER BY min(taken_at), holder",
                (parts[0], commit, commit),
            )
            return [_first_column(row) for row in cur.fetchall()]

    @contextlib.contextmanager
    def removing(self) -> Iterator[None]:
        """Hold :data:`REMOVAL_LOCK` exclusively: no lease is inserted meanwhile. A
        transaction's lock, so however the block ends it goes with the transaction and
        never back to the pool on its connection."""
        with self.pool.connection() as conn, conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (REMOVAL_LOCK,))
            yield


def _first_column(row: Any) -> str:
    """A row's one column, whatever the pool's row factory."""
    return str(next(iter(row.values())) if isinstance(row, dict) else row[0])


@contextlib.asynccontextmanager
async def _in_thread(manager: contextlib.AbstractContextManager[None]) -> AsyncIterator[None]:
    """A blocking context manager entered and exited off the event loop."""
    await asyncio.to_thread(manager.__enter__)
    try:
        yield
    except BaseException as error:
        if not await asyncio.to_thread(manager.__exit__, type(error), error, error.__traceback__):
            raise
    else:
        await asyncio.to_thread(manager.__exit__, None, None, None)


@contextlib.asynccontextmanager
async def _shared_lease(
    leases: CheckoutLeases, holder: str, checkouts: Sequence[Path]
) -> AsyncIterator[None]:
    """A lease row held, and renewed every third of its TTL, until the block exits."""
    token = await asyncio.to_thread(leases.take, holder, checkouts)

    async def renew() -> None:
        while True:
            await asyncio.sleep(leases.ttl / 3)
            try:
                await asyncio.to_thread(leases.renew, token)
            except Exception:
                logger.exception("could not renew a checkout lease", extra={"holder": holder})

    renewing = asyncio.create_task(renew())
    try:
        yield
    finally:
        renewing.cancel()
        try:
            await asyncio.to_thread(leases.drop, token)
        except Exception:
            # It lapses at its expiry instead.
            logger.exception("could not release a checkout lease", extra={"holder": holder})


class CheckoutGate:
    """Keeps removing a library checkout apart from pinning or rendering one (#253).

    A pin clones (or finds) a checkout and THEN records it in a model; a removal
    checks that no model records it and THEN deletes it. Interleaved, a removal
    could delete the checkout a pin has just found but not yet recorded, leaving a
    model pinned to nothing. So any number of pins may run together, and a removal
    waits for them all and runs alone.

    A render resolves its ``OPENSCADPATH`` once and then reads those checkouts for
    as long as OpenSCAD runs, which can outlast the model's own pin. So it holds a
    lease on them: taken only while no removal runs, and a removal refuses -- it
    does not wait out a render that may take the whole render timeout -- while one
    is held on anything it would delete (:meth:`leased`).

    With ``leases`` the render leases are also kept in Postgres, and a removal also
    holds their removal lock, so a render in another process (the render worker,
    #872) is seen by a removal here and the other way round. Pins stay in-process
    (#1131).
    """

    def __init__(self, leases: CheckoutLeases | None = None) -> None:
        self.shared = leases
        self._condition = asyncio.Condition()
        self._pins = 0
        self._removing = False
        #: lease token -> (holder, the checkout directories it reads). Keyed by a
        #: token, not the holder: two attempts at one job -- the first still running
        #: after its store lease lapsed and the job was retried -- each hold their
        #: own, and the first ending must not release the second's.
        self._leases: dict[object, tuple[str, tuple[Path, ...]]] = {}

    @contextlib.asynccontextmanager
    async def pinning(self) -> AsyncIterator[None]:
        async with self._condition:
            await self._condition.wait_for(lambda: not self._removing)
            self._pins += 1
        try:
            yield
        finally:
            async with self._condition:
                self._pins -= 1
                self._condition.notify_all()

    @contextlib.asynccontextmanager
    async def removing(self) -> AsyncIterator[None]:
        async with self._condition:
            await self._condition.wait_for(lambda: not self._removing and self._pins == 0)
            self._removing = True
        try:
            if self.shared is None:
                yield
            else:
                async with _in_thread(self.shared.removing()):
                    yield
        finally:
            async with self._condition:
                self._removing = False
                self._condition.notify_all()

    @contextlib.asynccontextmanager
    async def rendering(self, holder: str, checkouts: Sequence[Path]) -> AsyncIterator[None]:
        """Hold ``checkouts`` for ``holder`` until the block exits. Waits out a
        removal in progress, so the caller must check afterwards that what it
        resolved is still there (:func:`require_checkouts`)."""
        async with self._condition:
            await self._condition.wait_for(lambda: not self._removing)
            token = self.hold(holder, checkouts)
        try:
            if self.shared is None:
                yield
            else:
                async with _shared_lease(self.shared, holder, checkouts):
                    yield
        finally:
            # Whatever ends the attempt -- done, failed, cancelled by shutdown.
            self.release(token)

    def hold(self, holder: str, checkouts: Sequence[Path]) -> object:
        """The synchronous half of :meth:`rendering`: record a lease as it stands,
        and return the token that releases it."""
        token = object()
        self._leases[token] = (holder, tuple(checkouts))
        return token

    def release(self, token: object) -> None:
        self._leases.pop(token, None)

    def leased(self, directory: Path) -> list[str]:
        """The holders reading ``directory`` -- one checkout, or a library's
        directory of them -- each once, in the order they took their leases: this
        process's, then other processes'. Queries Postgres when the leases are shared,
        so an ``async`` caller hands it to :func:`asyncio.to_thread`."""
        local = [
            holder
            for holder, checkouts in self._leases.values()
            if any(path == directory or path.parent == directory for path in checkouts)
        ]
        shared = self.shared.holders(directory) if self.shared is not None else []
        return list(dict.fromkeys([*local, *shared]))


def require_checkouts(checkouts: Sequence[Path]) -> None:
    """:class:`LibraryNotInstalledError` for any of ``checkouts`` removed since it
    was resolved -- the message :func:`search_path` gives for one never there."""
    for directory in checkouts:
        name = directory.parent.name
        if not (directory / name).is_dir():
            raise LibraryNotInstalledError(
                f"{name!r} is pinned to {directory.name[:7]}, which is not on this volume; "
                "pin it to this model again"
            )


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

    def installed(self, name: str | None = None) -> list[tuple[str, str]]:
        """``(name, commit)`` for every checkout on the volume, or ``name``'s alone.
        Only complete ones: a staging clone is not a checkout yet."""
        root = self.paths.libraries
        if not root.is_dir():
            return []
        if name is not None:
            _require_name(name)
        found: list[tuple[str, str]] = []
        for library in sorted(root.iterdir()):
            if library.name.startswith(STAGING_PREFIX) or not library.is_dir():
                continue
            if name is not None and library.name != name:
                continue
            if not re.fullmatch(NAME_PATTERN, library.name):
                continue
            found.extend(
                (library.name, checkout.name)
                for checkout in sorted(library.iterdir())
                if re.fullmatch(COMMIT_PATTERN, checkout.name)
                and (checkout / library.name).is_dir()
            )
        return found

    def remove(self, name: str, commit: str | None = None) -> list[str]:
        """Delete ``name``'s checkout at ``commit``, or every checkout of it, from the
        volume. Returns the commits removed; :class:`LibraryCheckoutNotFoundError`
        when there was none.

        Knows nothing of which models pin what: the caller checks that first. Each
        checkout is moved aside before it is deleted, so a render never reads one
        half gone -- it finds it whole, or finds it missing and says so.
        """
        _require_name(name)
        if commit is not None and not re.fullmatch(COMMIT_PATTERN, commit):
            raise LibraryError(f"{commit!r} is not a full commit id")
        commits = [c for _, c in self.installed(name) if commit is None or c == commit]
        if not commits:
            raise LibraryCheckoutNotFoundError(name if commit is None else f"{name}@{commit}")
        library = self.paths.libraries / name
        for found in commits:
            doomed = self.paths.libraries / f"{STAGING_PREFIX}{uuid.uuid4().hex}"
            os.replace(library / found, doomed)
            shutil.rmtree(doomed, ignore_errors=True)
        with contextlib.suppress(OSError):
            library.rmdir()  # only when it emptied
        return commits

    def sweep_checkouts(self, keep: Callable[[str, str], bool]) -> list[str]:
        """Remove every checkout ``keep(name, commit)`` is false for. Returns
        ``name@commit`` for each one removed.

        Runs at boot, under the :class:`CheckoutGate` alone. Another replica sharing
        ``/data`` may have just cloned a checkout it has not recorded in a model yet,
        so -- as :meth:`sweep_staging` -- only a checkout older than the clone
        timeout plus ``STAGING_MAX_AGE_MARGIN`` goes. One that cannot be read or
        removed, or that ``keep`` cannot answer for, is logged and kept.
        """
        cutoff = time.time() - self.timeout - STAGING_MAX_AGE_MARGIN
        removed: list[str] = []
        for name, commit in self.installed():
            try:
                if (self.paths.libraries / name / commit).stat().st_mtime > cutoff:
                    continue
                if keep(name, commit):
                    continue
                self.remove(name, commit)
            except LibraryCheckoutNotFoundError:
                continue  # gone already
            except OSError:
                logger.exception(
                    "could not sweep a library checkout", extra={"library": name, "commit": commit}
                )
                continue
            removed.append(f"{name}@{commit}")
        return removed

    def resolve(self, name: str, *, url: str | None = None, ref: str | None = None) -> ModelLibrary:
        """Clone ``name`` at ``ref`` and return the pin: the commit that resolved to.

        ``url`` and ``ref`` default to the catalogue's. A curated name may be pinned
        from another repository -- a fork is still ``use <BOSL2/...>`` -- but only
        the catalogue's own URL skips the vetting, and another one needs a ``ref``.
        """
        url, ref, pinned = self._prepare(name, url, ref)
        commit = self._clone(name, url, ref, pinned)
        return ModelLibrary(name=name, url=url, ref=ref, commit=commit)

    def fetch(self, pin: ModelLibrary) -> None:
        """Clone ``pin`` again, at the commit it records, into its checkout: one
        that was pinned and has since gone from the volume (#169).

        The same checks as :meth:`resolve` -- the URL vetted unless it is the
        catalogue's, the transport allowed, the size capped -- so a ``model.json``
        brought from elsewhere cannot fetch what a pin could not. The ref is cloned
        first (cheap, and a tag still names the commit); if it has moved on, the
        commit itself is fetched, which the upstream must allow (GitHub does).
        """
        url, ref, pinned = self._prepare(pin.name, pin.url, pin.ref)
        self._clone(pin.name, url, ref, pinned, commit=pin.commit)

    def _prepare(
        self, name: str, url: str | None, ref: str | None
    ) -> tuple[str, str, tuple[str, ...]]:
        """Check what :meth:`resolve` was asked for, and fill in the catalogue's
        defaults. Returns the URL and ref to clone, and the git config that holds
        the clone to the vetted addresses."""
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
        trusted = known is not None and same_repository(url, known.url)
        if trusted:
            assert known is not None
            url = known.url
        if ref is None:
            if not trusted:
                raise LibraryError(f"a library from {url} needs a ref to pin")
            assert known is not None
            ref = known.ref
        # REF_PATTERN already refuses `..`; the explicit test is deliberate defence in
        # depth, so a later edit to the pattern cannot let one through (#217).
        if not re.fullmatch(REF_PATTERN, ref) or ".." in ref:
            raise LibraryError(f"{ref!r} is not a usable branch or tag name")
        scheme = urlsplit(url).scheme.lower()
        if scheme not in self.protocols:
            raise LibraryError(f"{url!r} is not a {' or '.join(self.protocols)} URL")
        # The catalogue's own URLs are trusted as they are; anything a client named
        # is vetted on every clone.
        return url, ref, (() if trusted else self._vet(url))

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
            addresses = asyncio.run(public_addresses(host, port, tell_unavailable=True))
        except ResolverUnavailableError:
            raise LibraryResolverUnavailableError(
                f"could not resolve {host} just now; try again"
            ) from None
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

    def _clone(
        self,
        name: str,
        url: str,
        ref: str,
        pinned: Sequence[str] = (),
        *,
        commit: str | None = None,
    ) -> str:
        """Clone into a staging directory beside the checkouts, then move it into
        place under the commit it resolved to. Nothing half-cloned is ever at a
        path a render could read. With ``commit``, the checkout is at that commit
        whatever ``ref`` names now."""
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
            head = self._git("-C", str(staging / name), "rev-parse", "HEAD")
            if commit is not None and head != commit:
                self._check_out(staging, name, url, commit, pinned)
                head = self._git("-C", str(staging / name), "rev-parse", "HEAD")
                if head != commit:
                    raise LibraryFetchError(f"{url} did not give {commit[:7]}")
            # The last poll can land before the clone's final writes.
            size = _tree_size(staging, self.max_bytes)
            if size > self.max_bytes:
                raise LibraryTooLargeError(f"{url} at {ref!r} {self._over(size)}")
            destination = self.paths.libraries / name / head
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(staging, destination)
            except OSError:
                # Already cloned at this commit, by an earlier install or a
                # concurrent one; either copy is the same tree.
                if not (destination / name).is_dir():
                    raise
            return head
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def _check_out(
        self, staging: Path, name: str, url: str, commit: str, pinned: Sequence[str]
    ) -> None:
        """Fetch ``commit`` into the shallow clone at ``staging`` and check it out:
        the ref it was pinned at has moved on since."""
        clone = str(staging / name)
        try:
            # The clone's own config does not carry `pinned`; the fetch is held to
            # the vetted addresses the same way.
            self._git(
                *pinned,
                "-C",
                clone,
                "fetch",
                "--quiet",
                "--depth",
                "1",
                "origin",
                commit,
                watch=staging,
            )
            self._git("-C", clone, "checkout", "--quiet", "--detach", commit, watch=staging)
        except LibraryFetchError as error:
            raise LibraryFetchError(f"could not fetch {commit[:7]} from {url}: {error}") from error
        except LibraryTooLargeError as error:
            raise LibraryTooLargeError(f"{url} at {commit[:7]} {error}") from None

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
            logger.warning(
                "killed git (pid %d) was not reaped within %gs; a reaper thread waits for it",
                process.pid,
                KILL_WAIT,
                extra={"pid": process.pid},
            )
            threading.Thread(target=process.wait, name="git-reaper", daemon=True).start()


@dataclass
class CheckoutFetcher:
    """Clones a pinned checkout that has gone from the volume back into place
    (#169), rather than failing the render or create that found it missing.

    Through the same path a pin takes: the store's checks and size cap, one of the
    ``installs`` permits, and the :class:`CheckoutGate` as a pin holds it, so no
    removal runs while it fetches.
    """

    store: LibraryStore
    installs: asyncio.Semaphore
    checkouts: CheckoutGate

    async def search_path(self, resolve: Callable[[], tuple[Path, ...]]) -> tuple[Path, ...]:
        """``resolve()`` -- one of the ``*search_path`` functions, off the loop --
        fetching each missing checkout it names and trying again. A checkout that
        cannot be fetched is :class:`LibraryNotInstalledError`, saying why."""
        fetched: set[tuple[str, str]] = set()
        while True:
            try:
                return await asyncio.to_thread(resolve)
            except LibraryCheckoutMissingError as missing:
                pin = missing.pin
                if (pin.name, pin.commit) in fetched:
                    raise  # fetched, and gone again: a removal won the race
                fetched.add((pin.name, pin.commit))
                await self.fetch(pin)

    async def fetch(self, pin: ModelLibrary) -> None:
        try:
            async with self.checkouts.pinning(), self.installs:
                await asyncio.to_thread(self.store.fetch, pin)
        except LibraryError as error:
            logger.warning(
                "could not fetch a missing library checkout again",
                extra={"library": pin.name, "commit": pin.commit, "reason": str(error)},
            )
            raise LibraryNotInstalledError(
                f"{pin.name!r} is pinned to {pin.commit[:7]}, which is not on this volume, "
                f"and fetching it again failed ({error}); pin it to this model again"
            ) from None
        logger.info(
            "fetched a missing library checkout again",
            extra={"library": pin.name, "commit": pin.commit},
        )


async def resolve_search_path(
    fetcher: CheckoutFetcher | None, resolve: Callable[[], tuple[Path, ...]]
) -> tuple[Path, ...]:
    """``resolve()`` off the loop, re-fetching missing checkouts when there is a
    ``fetcher`` to do it."""
    if fetcher is None:
        return await asyncio.to_thread(resolve)
    return await fetcher.search_path(resolve)
