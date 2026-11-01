"""Bambuddy's preset catalogue: every printer/process/filament preset it offers,
normalised into one shape.

Split out of ``print_run.py`` so that ``resolver.py`` can
depend on it without ``print_run.py`` depending on ``resolver.py`` — the catalogue is
the only piece of ``print_run.py`` the resolver needs.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import hmac
import json
import logging
import math
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from functools import cached_property, partial
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi import status
from psycopg import AsyncConnection
from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from scadbuddy.bambuddy.client import BambuddyClient, BambuddyConfig
from scadbuddy.bambuddy.concurrent import settled
from scadbuddy.bambuddy.errors import UNAVAILABLE_PROBLEM
from scadbuddy.bambuddy.models import (
    LocalPreset,
    LocalPresetCatalogue,
    Preset,
    PresetCatalogue,
    PresetRef,
    PresetTier,
)
from scadbuddy.bambuddy.slot_presets import is_slot_preset
from scadbuddy.core.problems import ApiError
from scadbuddy.core.secrets import Kek

logger = logging.getLogger(__name__)


class PresetChoice(BaseModel):
    """One slicer preset in Bambuddy's catalogue, as the resolver reads it.

    ``compatible_printers`` is normalised to a list here: ``/slicer/presets`` returns
    one, while ``/local-presets/`` stores the same thing as a JSON-encoded *string*.
    An empty list means the preset declares no restriction, not that it fits nothing.
    """

    ref: PresetRef
    name: str
    filament_type: str | None = None
    filament_colour: str | None = None
    compatible_printers: list[str] = Field(default_factory=list)
    filament_id: str | None = None


def _local_list(raw: str | None) -> list[str]:
    """``compatible_printers`` on a local preset is a JSON-encoded string."""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except ValueError:
        # Bambuddy stores the OrcaSlicer column verbatim; a non-JSON one is not fatal.
        logger.info("a local preset's compatible_printers was not JSON")
        return []
    return [str(entry) for entry in parsed] if isinstance(parsed, list) else []


def _choice(preset: Preset) -> PresetChoice:
    return PresetChoice(
        ref=PresetRef(source=preset.source, id=preset.id),
        name=preset.name or f"{preset.source}:{preset.id}",
        filament_type=preset.filament_type,
        filament_colour=preset.filament_colour,
        compatible_printers=list(preset.compatible_printers or []),
    )


def local_choice(preset: LocalPreset) -> PresetChoice:
    """A ``/local-presets/`` row as a catalogue row: Bambuddy's, and the kept copy's on a
    native run (``slicing/catalogue.py``, #4645)."""
    colours = _local_list(preset.default_filament_colour)
    return PresetChoice(
        ref=preset.ref(),
        name=preset.name,
        filament_type=preset.filament_type,
        filament_colour=colours[0] if colours else None,
        compatible_printers=_local_list(preset.compatible_printers),
    )


@dataclass(frozen=True)
class Catalogue:
    """Every preset Bambuddy offers, normalised into :class:`PresetChoice` rows.

    Both catalogues go in: ``/local-presets/`` (OrcaSlicer imports) is *not* the
    ``local`` tier of ``/slicer/presets``, and a user's own filament profile lives only
    there.
    """

    printer: list[PresetChoice]
    process: list[PresetChoice]
    filament: list[PresetChoice]
    #: Set only on the image's own catalogue (``slicing/catalogue.py``
    #: ``native_catalogue``, #3818): each field's ``setting_id -> name``, which is what
    #: a ``cloud`` ref resolves by there. ``None`` on Bambuddy's, whose refs are its own.
    native_setting_ids: dict[str, dict[str, str]] | None = None

    @property
    def is_native(self) -> bool:
        return self.native_setting_ids is not None

    @property
    def owner(self) -> str:
        """Whose presets these are, as the resolver's messages name them."""
        return "Bambu Studio" if self.is_native else "Bambuddy"

    @property
    def slicer(self) -> str:
        """What slices with them, as the resolver's messages name it."""
        return "The native slicer" if self.is_native else "Bambuddy"

    @property
    def native_note(self) -> str:
        """Said after a preset the image's catalogue lacks; empty on Bambuddy's."""
        if not self.is_native:
            return ""
        return " A native print uses Bambu Studio's own presets: pick one under Advanced."

    @cached_property
    def _offered(self) -> dict[str, frozenset[str]]:
        """Each field's preset names, built once per catalogue."""
        return {
            field: frozenset(row.name for row in getattr(self, field))
            for field in ("printer", "process", "filament")
        }

    @cached_property
    def _held(self) -> dict[str, frozenset[str]]:
        """Each field's ``local`` ids: on the image's own catalogue, the kept copy's
        Bambuddy local presets it offers (:meth:`with_held`)."""
        return {
            field: frozenset(
                row.ref.id for row in getattr(self, field) if row.ref.source == "local"
            )
            for field in ("printer", "process", "filament")
        }

    def native_ref(self, ref: PresetRef, field: str | None = None) -> PresetRef | None:
        """On the image's own catalogue, the ref a native slice reads for the preset
        ``ref`` names in ``field`` (``printer``, ``process``, ``filament``) or any of
        them: a ``standard`` ref as it is when offered, a ``cloud`` ref (a
        ``setting_id``) as the standard ref of the preset it names, a ``local`` ref as it
        is when the kept copy's row is offered (#4645); ``orca_cloud`` and anything not
        offered is ``None``. On Bambuddy's catalogue every ref is its own, so ``None``."""
        ids = self.native_setting_ids
        if ids is None:
            return None
        for name in [field] if field is not None else list(self._offered):
            if ref.source == "standard" and ref.id in self._offered[name]:
                return ref
            if ref.source == "cloud" and ref.id in ids.get(name, {}):
                return PresetRef(source="standard", id=ids[name][ref.id])
            if ref.source == "local" and ref.id in self._held[name]:
                return ref
        return None

    def with_held(
        self,
        printer: list[PresetChoice],
        process: list[PresetChoice],
        filament: list[PresetChoice],
    ) -> Catalogue:
        """This catalogue with the kept copy's local presets' rows after its own
        (``slicing/catalogue.py`` ``catalogue_for``, #4645)."""
        return dataclasses.replace(
            self,
            printer=[*self.printer, *printer],
            process=[*self.process, *process],
            filament=[*self.filament, *filament],
        )

    def names(self) -> dict[tuple[str, str], str]:
        """``(source, id) -> name``, for resolving the refs a pipeline carries."""
        return {
            (choice.ref.source, choice.ref.id): choice.name
            for choice in (*self.printer, *self.process, *self.filament)
        }


async def _read(client: BambuddyClient) -> tuple[PresetCatalogue, LocalPresetCatalogue]:
    presets, local_presets = await asyncio.gather(
        client.presets(), client.local_presets(), return_exceptions=True
    )
    return settled(presets), settled(local_presets)


async def _catalogue(client: BambuddyClient) -> Catalogue:
    """Bambuddy's catalogue, its rows saying which printers they fit
    (:func:`_check_standard_tier`)."""
    read, local = await _read(client)
    _check_standard_tier(read)
    # Thousands of rows: built off the event loop, which every other request shares.
    return await asyncio.to_thread(_build, read, local)


def _is_empty(tier: PresetTier) -> bool:
    return not (tier.printer or tier.process or tier.filament)


def _check_standard_tier(catalogue: PresetCatalogue) -> None:
    """Refuse a read whose cloud tier lists presets but whose standard tier lists none
    (#4717). Bambuddy's ``_fetch_bundled_presets`` answers empty slots, with no error or
    status, when its slicer sidecar's ``/profiles/bundled`` fails (a ReadTimeout while
    the sidecar sliced, on prod 2026-10-25), and its ``_enrich_cloud_metadata`` then has
    no standard row to copy ``compatible_printers`` from, so every cloud row declares no
    printer and the resolver refuses every process as one Bambuddy lacks. That read is
    Bambuddy unavailable, never a catalogue to resolve with or keep. With no cloud
    presets either, an empty standard tier is no such sign.
    The check is of the whole tier: ``_fetch_bundled_presets`` fills every slot from one
    ``/profiles/bundled`` answer or returns them all empty (``backend/app/api/routes/
    slicer_presets.py`` at maziggy/bambuddy@036f0a6, read 2026-10-25), so a tier with
    printers but no processes is not a shape it sends.
    It answers the same with no sidecar URL at all, which it cannot slice without either
    (its slice route needs one, ``library.py`` ``_run_slicer_with_fallback``), so the
    message names that cause too. Only a run or dialog that Bambuddy slices meets it: a
    native one resolves against the image's catalogue (``slicing/catalogue.py``
    ``catalogue_for`` given ``native``) and reads Bambuddy's only for names
    (:func:`read_preset_names`, unchecked), so a native print with ``local`` presets
    is not refused for want of a sidecar."""
    if _is_empty(catalogue.standard) and not _is_empty(catalogue.cloud):
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Bambuddy listed its cloud presets but none of its standard (bundled) ones, "
            "most likely because its slicer sidecar did not answer it (busy, down, or not "
            "configured), so it could not say which printers each preset fits. Try again "
            "once Bambuddy's slicer answers.",
            type_=UNAVAILABLE_PROBLEM,
        )


def _build(catalogue: PresetCatalogue, local: LocalPresetCatalogue) -> Catalogue:
    printers: list[PresetChoice] = []
    processes: list[PresetChoice] = []
    filaments: list[PresetChoice] = []
    for tier in (catalogue.cloud, catalogue.standard, catalogue.local, catalogue.orca_cloud):
        printers.extend(_choice(preset) for preset in tier.printer)
        processes.extend(_choice(preset) for preset in tier.process)
        filaments.extend(_choice(preset) for preset in tier.filament)
    printers.extend(local_choice(row) for row in local.printer)
    processes.extend(local_choice(row) for row in local.process)
    # A slot's own preset is one already offered, under ScadBuddy's name (#3962): it
    # is never offered in a picker or matched to a spool beside the preset it copies.
    filaments.extend(local_choice(row) for row in local.filament if not is_slot_preset(row.name))
    return Catalogue(printer=printers, process=processes, filament=filaments)


#: How old a kept catalogue may be before the dialog reads a fresh one behind it
#: (#2361, #2978). Bambuddy's presets change only when someone imports or edits a
#: profile there, and ``/slicer/presets`` (1 MB) takes seconds to answer.
CATALOGUE_TTL = 120.0

#: Under the data volume's ``cache/``, which every replica shares (#2978): a catalogue
#: kept in one process missed almost every open, the API running several.
CATALOGUE_DIRNAME = "catalogues"
#: Bumped when :class:`Catalogue`'s shape changes, so an older file is never read.
CATALOGUE_VERSION = 1
#: How long a file in the directory may go unwritten before a keep removes it (#3423):
#: one named for an old Bambuddy URL or key, or an older version, is never read again.
#: A live one is rewritten every :data:`CATALOGUE_TTL` it is opened, so a removed live
#: file costs only its next open a read.
CATALOGUE_ORPHAN_AGE = 7 * 24 * 3600.0
#: The HKDF label of :func:`catalogue_name_key`.
NAME_KEY_INFO = b"scadbuddy:bambuddy:catalogue_name:v1"
#: Prefixes the key of :func:`catalogue_refresh_claim`'s advisory lock.
REFRESH_LOCK_PREFIX = "scadbuddy-catalogue-refresh:"
#: How long a refresh waits to connect for its claim, in seconds, before it refreshes
#: unclaimed.
REFRESH_CLAIM_CONNECT_TIMEOUT = 5

_ADAPTER = TypeAdapter(Catalogue)


async def read_preset_names(client: BambuddyClient) -> dict[tuple[str, str], str]:
    """Every preset's name by its ref (:meth:`Catalogue.names`), read fresh from Bambuddy:
    what the native slicer finds a ``cloud`` ref by when its id is not one of the
    image's. Names need no standard tier, so a read without one (#4717) still answers.
    The run's catalogue is ``slicing/catalogue.py`` ``catalogue_for``; the print
    dialog's kept copy is :func:`cached_catalogue`."""
    read, local = await _read(client)
    return (await asyncio.to_thread(_build, read, local)).names()


#: The catalogue this process last parsed, by its file and that file's mtime: one
#: entry, so a changed Bambuddy URL or key does not keep the old one alive (#3447).
_parsed: dict[Path, tuple[int, Catalogue]] = {}
#: How many opens in a row could not read each file, so a miss that persists (a volume
#: that cannot be read, say) shows as one in its log line, not as one miss after another
#: each answered from memory (#4623 review).
_misses: dict[Path, int] = {}
#: The background reads running in this process, one per file.
_refreshing: dict[Path, asyncio.Task[None]] = {}
#: The first reads running in this process, one per file with nothing kept yet, which
#: every open of that file waits on (#2906).
_reading: dict[Path, asyncio.Task[Catalogue]] = {}
#: When this process last found each file's refresh claimed by another replica
#: (``time.monotonic``), so its opens do not each ask again until that one is done.
_refused: dict[Path, float] = {}
#: How long a refused claim is not asked again, in seconds: about as long as a
#: refresh takes on prod (1.4 to 12 s, #4528).
REFUSED_CLAIM_BACKOFF = 15.0


def catalogue_name_key(kek: Kek | None) -> bytes | None:
    """The key a kept catalogue's file name is an HMAC under (#3313), derived from the
    KEK so a listing of the shared volume cannot be tried against guessed API keys.
    ``None`` without a KEK: then the API key is stored in the clear anyway."""
    if kek is None:
        return None
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=NAME_KEY_INFO).derive(kek.key)


#: A catalogue read deferred: what the dialog's check is given (#4196).
ReadCatalogue = Callable[[], Awaitable[Catalogue]]
#: Held while one replica refreshes a kept file (#4528): entered with the file, it
#: yields whether this replica may refresh it, False while another one does.
RefreshClaim = Callable[[Path], AbstractAsyncContextManager[bool]]


def catalogue_refresh_claim(conninfo: str) -> RefreshClaim:
    """A :data:`RefreshClaim` held across every replica on the database at ``conninfo``
    (#4528): a session-level ``pg_try_advisory_lock`` per file, on a connection of its
    own that is closed on the way out, so a replica that dies mid-refresh lets go with
    its connection. On prod two replicas refreshed the same expired copy within seconds,
    each reading Bambuddy's ~1 MB ``/slicer/presets``, since :data:`_refreshing` is per
    process. A claim that cannot connect is granted: a database that does not answer
    costs a second read, never a copy left unrefreshed."""

    @asynccontextmanager
    async def claim(path: Path) -> AsyncIterator[bool]:
        conn: AsyncConnection[tuple[object, ...]] | None = None
        try:
            conn = await AsyncConnection.connect(
                conninfo, autocommit=True, connect_timeout=REFRESH_CLAIM_CONNECT_TIMEOUT
            )
        except Exception as error:
            # Said by its type only: a connection error can quote the conninfo.
            logger.warning(
                "could not claim the preset catalogue's refresh",
                extra={"error": type(error).__name__},
            )
        if conn is None:
            # Outside the handler, so a refresh's own error never carries the connect
            # error, and its conninfo, as its context.
            yield True
            return
        try:
            # Keyed by the file's name alone, which holds while every replica shares
            # one cache volume (#2978). Replicas with caches of their own would skip a
            # refresh while another replica refreshed its own copy, so the key would
            # then need the volume too.
            cursor = await conn.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))",
                (f"{REFRESH_LOCK_PREFIX}{path.name}",),
            )
            row = await cursor.fetchone()
            yield bool(row and row[0])
        finally:
            await conn.close()

    return claim


def kept_catalogue(
    client: BambuddyClient, cache: Path, kek: Kek | None, refresh_claim: RefreshClaim | None
) -> ReadCatalogue:
    """:func:`cached_catalogue` for ``client`` as a reader, its file named under
    :func:`catalogue_name_key` as the dialog's choices name it: what the dialog's check
    reads (#4196)."""
    return partial(
        cached_catalogue,
        client,
        cache,
        name_key=catalogue_name_key(kek),
        refresh_claim=refresh_claim,
    )


async def cached_catalogue(
    client: BambuddyClient,
    cache: Path,
    *,
    name_key: bytes | None = None,
    refresh_claim: RefreshClaim | None = None,
) -> Catalogue:
    """:func:`_catalogue`, kept in a file under ``cache`` per Bambuddy (#2361, #2978).

    A kept catalogue answers at once, however old; one older than
    :data:`CATALOGUE_TTL` is read again in the background for the next open. So a
    profile imported in Bambuddy shows from the open after the one that started that
    read. Only the print dialog reads it, its choices and its check (#4196): the run,
    which slices with what it resolves, still reads the catalogue fresh. A failed read
    is not kept.
    ``name_key`` is :func:`catalogue_name_key`'s. With ``refresh_claim``
    (:func:`catalogue_refresh_claim`) one replica at a time refreshes a file (#4528);
    without it, one task per process does.

    With nothing kept, the opens of that file wait on one read and fail with it
    (#2906), rather than each reading ``/slicer/presets`` (~6 s). The read is shielded
    and has a client of its own, so an open given up on (``choices_for`` cancels its
    catalogue read when another read fails first) does not take it from the others."""
    path = _path(cache, client.config, name_key)
    kept = await asyncio.to_thread(_load, path)
    if kept is None:
        reading = _reading.get(path)
        if reading is None:
            reading = asyncio.create_task(_first_read(client.config, path))
            _reading[path] = reading
            reading.add_done_callback(lambda done: _settle_first_read(path, done))
        return await asyncio.shield(reading)
    age, catalogue = kept
    refused = _refused.get(path)
    backing_off = refused is not None and time.monotonic() - refused < REFUSED_CLAIM_BACKOFF
    if age >= CATALOGUE_TTL and path not in _refreshing and not backing_off:
        task = asyncio.create_task(_refresh(client.config, path, refresh_claim))
        _refreshing[path] = task
        task.add_done_callback(lambda _: _refreshing.pop(path, None))
    return catalogue


def _path(cache: Path, config: BambuddyConfig, name_key: bytes | None = None) -> Path:
    """One file per Bambuddy and key, named by a digest so the key never reaches a name:
    an HMAC under ``name_key`` when there is one (#3313)."""
    message = f"{config.base_url}\0{config.api_key or ''}".encode()
    digest = (
        hmac.new(name_key, message, hashlib.sha256).hexdigest()
        if name_key is not None
        else hashlib.sha256(message).hexdigest()
    )
    return cache / CATALOGUE_DIRNAME / f"v{CATALOGUE_VERSION}-{digest[:32]}.json"


def _load(path: Path) -> tuple[float, Catalogue] | None:
    """The kept catalogue and its age in seconds; ``None`` when there is none to read.

    A file that cannot be read when this process holds what it last read of it (parsed
    here, or read fresh, :func:`_hold`) answers that, its age unknown and so
    past :data:`CATALOGUE_TTL`, so the open reads again behind it (#4528): a check on
    prod waited seconds on a fresh read with the file on the shared volume."""
    try:
        stat = path.stat()
        held = _parsed.get(path)
        if held is None or held[0] != stat.st_mtime_ns:
            held = (stat.st_mtime_ns, _ADAPTER.validate_json(path.read_bytes()))
            _parsed.clear()
            _parsed[path] = held
    except (OSError, ValidationError) as error:
        # Said by its type and errno only, never its text (#3446): a check on prod waited
        # on a fresh read with this file on the shared volume, and nothing said why (#4196).
        # The name is a digest with no key in it (#3313).
        last = _parsed.get(path)
        _misses[path] = _misses.get(path, 0) + 1
        logger.info(
            "no kept preset catalogue could be read",
            extra={
                "error": type(error).__name__,
                "errno": getattr(error, "errno", None),
                "file": path.name,
                "held": last is not None,
                "misses_in_a_row": _misses[path],
            },
        )
        return None if last is None else (math.inf, last[1])
    _misses.pop(path, None)
    return time.time() - stat.st_mtime, held[1]


def _hold(path: Path, catalogue: Catalogue) -> None:
    """Hold ``catalogue``, just read fresh, as this process's last read of ``path``
    (#4528), under the mtime its file has now: once kept, the file it wrote, so the next
    open does not parse it back; not kept, the older file left there (#4623 review), so
    the fresh read is what answers until a refresh replaces that file; no file (none
    kept yet, or the directory could not be made), none, and the fresh read then answers
    every later open, as a held copy, until a keep succeeds.
    Another replica that renames its own read in between is held under ours, which is
    the same catalogue read at the same time."""
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        mtime = -1
    _parsed.clear()
    _parsed[path] = (mtime, catalogue)


async def _keep_and_hold(path: Path, catalogue: Catalogue) -> None:
    """:func:`_keep`, then :func:`_hold` whether or not the keep failed."""
    try:
        await asyncio.to_thread(_keep, path, catalogue)
    finally:
        await asyncio.to_thread(_hold, path, catalogue)


def _keep(path: Path, catalogue: Catalogue) -> None:
    """Write ``path`` aside and rename it in, so another replica never reads half."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
    try:
        partial.write_bytes(_ADAPTER.dump_json(catalogue))
        partial.replace(path)
    except OSError:
        # A write that failed part-way goes at once (#3447).
        partial.unlink(missing_ok=True)
        raise
    _sweep(path)


def _sweep(kept: Path) -> None:
    """Remove the files beside ``kept`` that have gone :data:`CATALOGUE_ORPHAN_AGE`
    unwritten (#3423): an old URL's, key's or version's, or a crashed write's. Another
    replica may remove the same file first, and a file that cannot go waits for the
    next keep. It lists the whole directory on every keep, refreshes included: that
    holds one file per Bambuddy, key and version, a handful, so it is not rate-limited
    (#3816 review). A directory that grows past that wants a limit here."""
    cutoff = time.time() - CATALOGUE_ORPHAN_AGE
    try:
        entries = list(kept.parent.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.name == kept.name:
            continue
        try:
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                entry.unlink()
        except OSError:
            continue


async def _first_read(config: BambuddyConfig, path: Path) -> Catalogue:
    """The read every open with nothing kept waits on, with a client of its own: the
    open that started it may answer, or be given up on, before it ends.

    An open's load can miss just before another read keeps the file and leaves
    ``_reading``, so this looks again first and takes what was kept (#3809 review)."""
    kept = await asyncio.to_thread(_load, path)
    if kept is not None:
        return kept[1]
    async with BambuddyClient(config) as client:
        catalogue = await _catalogue(client)
    try:
        await _keep_and_hold(path, catalogue)
    except OSError as error:
        # Kept or not, these opens have their catalogue.
        logger.warning("could not keep the preset catalogue", extra={"error": type(error).__name__})
    return catalogue


def _settle_first_read(path: Path, done: asyncio.Task[Catalogue]) -> None:
    """Let the next open start a read of its own, and take the failure, so a read whose
    every open was given up on is not logged as never retrieved."""
    if _reading.get(path) is done:
        del _reading[path]
    if not done.cancelled():
        done.exception()


async def _refresh(config: BambuddyConfig, path: Path, claim: RefreshClaim | None) -> None:
    """Read the catalogue again with a client of its own, since the request's closes
    when it answers. A failure keeps the old one for the next open to retry.

    Only under ``claim`` (#4528): a replica that another one is refreshing for reads
    nothing, and one that takes the claim just after the last holder renamed a fresh
    copy in finds the file no longer expired and reads nothing either."""
    try:
        async with _claimed(claim, path) as claimed:
            if not claimed:
                _refused[path] = time.monotonic()
                return
            _refused.pop(path, None)
            if not await asyncio.to_thread(_expired, path):
                return
            async with BambuddyClient(config) as client:
                catalogue = await _catalogue(client)
            await _keep_and_hold(path, catalogue)
    except Exception as error:
        # Nothing awaits this task, so whatever fails ends here, and only by its type:
        # a pydantic error quotes Bambuddy's body (#3446). A problem's type is ScadBuddy's
        # own constant URI, which tells a busy sidecar (#4717) from other failures.
        logger.warning(
            "could not refresh the kept preset catalogue",
            extra={
                "error": type(error).__name__,
                "problem": error.type if isinstance(error, ApiError) else None,
            },
        )


@asynccontextmanager
async def _claimed(claim: RefreshClaim | None, path: Path) -> AsyncIterator[bool]:
    """``claim`` on ``path``; with none, this process's own dedup is the only one."""
    if claim is None:
        yield True
        return
    async with claim(path) as claimed:
        yield claimed


def _expired(path: Path) -> bool:
    """Whether the kept file is past :data:`CATALOGUE_TTL`, or cannot be looked at."""
    try:
        return time.time() - path.stat().st_mtime >= CATALOGUE_TTL
    except OSError:
        return True


async def stop_refreshes() -> None:
    """Cancel this process's background reads and wait for them, at shutdown (#3446):
    the refreshes and any first read still running, so each closes its client."""
    tasks = [*_refreshing.values(), *_reading.values()]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def forget_catalogues() -> None:
    """Forget what this process parsed and stop its refreshes: each test starts with
    none. The files are each test's own, under its own ``cache``. A read left by a
    test whose event loop has closed is only dropped: cancelling it would raise (#3311)."""
    for task in [*_refreshing.values(), *_reading.values()]:
        if not task.get_loop().is_closed():
            task.cancel()
    _refreshing.clear()
    _reading.clear()
    _parsed.clear()
    _misses.clear()
    _refused.clear()
