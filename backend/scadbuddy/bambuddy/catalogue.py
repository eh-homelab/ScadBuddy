"""Bambuddy's preset catalogue: every printer/process/filament preset it offers,
normalised into one shape.

Split out of ``print_run.py`` so that ``resolver.py`` can
depend on it without ``print_run.py`` depending on ``resolver.py`` — the catalogue is
the only piece of ``print_run.py`` the resolver needs.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field, TypeAdapter

from scadbuddy.bambuddy.client import BambuddyClient, BambuddyConfig
from scadbuddy.bambuddy.concurrent import settled
from scadbuddy.bambuddy.models import (
    LocalPreset,
    LocalPresetCatalogue,
    Preset,
    PresetCatalogue,
    PresetRef,
)

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


def _local_choice(preset: LocalPreset) -> PresetChoice:
    colours = _local_list(preset.default_filament_colour)
    return PresetChoice(
        ref=preset.ref(),
        name=preset.name,
        filament_type=preset.filament_type,
        filament_colour=colours[0] if colours else None,
        compatible_printers=_local_list(preset.compatible_printers),
    )


@dataclass(frozen=True)
class _Catalogue:
    """Every preset Bambuddy offers, normalised into :class:`PresetChoice` rows.

    Both catalogues go in: ``/local-presets/`` (OrcaSlicer imports) is *not* the
    ``local`` tier of ``/slicer/presets``, and a user's own filament profile lives only
    there.
    """

    printer: list[PresetChoice]
    process: list[PresetChoice]
    filament: list[PresetChoice]

    def to_json(self) -> dict[str, object]:
        return {
            kind: _CHOICES.dump_python(getattr(self, kind), mode="json")
            for kind in ("printer", "process", "filament")
        }

    @classmethod
    def from_json(cls, raw: dict[str, object]) -> _Catalogue:
        return cls(
            **{
                kind: _CHOICES.validate_python(raw[kind])
                for kind in ("printer", "process", "filament")
            }
        )

    def names(self) -> dict[tuple[str, str], str]:
        """``(source, id) -> name``, for resolving the refs a pipeline carries."""
        return {
            (choice.ref.source, choice.ref.id): choice.name
            for choice in (*self.printer, *self.process, *self.filament)
        }


_CHOICES = TypeAdapter(list[PresetChoice])


async def _catalogue(client: BambuddyClient) -> _Catalogue:
    presets, local_presets = await asyncio.gather(
        client.presets(), client.local_presets(), return_exceptions=True
    )
    catalogue, local = settled(presets), settled(local_presets)
    # Thousands of rows: built off the event loop, which every other request waits on (#2978).
    return await asyncio.to_thread(_build, catalogue, local)


def _build(catalogue: PresetCatalogue, local: LocalPresetCatalogue) -> _Catalogue:
    printers: list[PresetChoice] = []
    processes: list[PresetChoice] = []
    filaments: list[PresetChoice] = []
    for tier in (catalogue.cloud, catalogue.standard, catalogue.local, catalogue.orca_cloud):
        printers.extend(_choice(preset) for preset in tier.printer)
        processes.extend(_choice(preset) for preset in tier.process)
        filaments.extend(_choice(preset) for preset in tier.filament)
    printers.extend(_local_choice(row) for row in local.printer)
    processes.extend(_local_choice(row) for row in local.process)
    filaments.extend(_local_choice(row) for row in local.filament)
    return _Catalogue(printer=printers, process=processes, filament=filaments)


#: How long the dialog trusts a catalogue it read (#2361). Bambuddy's presets change
#: only when someone imports or edits a profile there, and ``/slicer/presets`` takes
#: seconds to answer, so every dialog open paid seconds for the same answer. Past it,
#: the kept one is still served and a fresh read runs behind it (#2978).
CATALOGUE_TTL = 120.0


@dataclass(frozen=True)
class _Kept:
    read_at: float  # wall clock: every replica compares it
    catalogue: _Catalogue


class CatalogueStore:
    """``bambuddy_preset_catalogues``: the last catalogue read, for every replica (#2978).

    The API runs several processes, so one kept in memory alone was almost never the
    one a reopen landed on, and every open read Bambuddy's 1 MB ``/slicer/presets``.
    Rows are keyed by a hash of the Bambuddy URL and key, never the key itself."""

    def __init__(self, pool: ConnectionPool[Connection[DictRow]]) -> None:
        self._pool = pool

    async def load(self, key: str) -> _Kept | None:
        return await asyncio.to_thread(self._load, key)

    async def save(self, key: str, kept: _Kept) -> None:
        await asyncio.to_thread(self._save, key, kept)

    def _load(self, key: str) -> _Kept | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT catalogue, extract(epoch FROM read_at) AS read_at"
                " FROM bambuddy_preset_catalogues WHERE key = %s",
                (key,),
            ).fetchone()
        if row is None:
            return None
        try:
            return _Kept(float(row["read_at"]), _Catalogue.from_json(row["catalogue"]))
        except (ValueError, KeyError, TypeError):
            logger.warning("a kept preset catalogue could not be read; reading Bambuddy's")
            return None

    def _save(self, key: str, kept: _Kept) -> None:
        with self._pool.connection() as conn:
            # A slower refresh never replaces a newer one another replica wrote.
            conn.execute(
                "INSERT INTO bambuddy_preset_catalogues (key, catalogue, read_at)"
                " VALUES (%s, %s, to_timestamp(%s))"
                " ON CONFLICT (key) DO UPDATE SET catalogue = EXCLUDED.catalogue,"
                " read_at = EXCLUDED.read_at"
                " WHERE bambuddy_preset_catalogues.read_at < EXCLUDED.read_at",
                (key, Jsonb(kept.catalogue.to_json()), kept.read_at),
            )


_cached: dict[str, _Kept] = {}
_refreshing: dict[str, asyncio.Task[None]] = {}


def _key(config: BambuddyConfig) -> str:
    return hashlib.sha256(f"{config.base_url}\0{config.api_key or ''}".encode()).hexdigest()


async def _read(client: BambuddyClient, store: CatalogueStore | None, key: str) -> _Catalogue:
    kept = _Kept(time.time(), await _catalogue(client))
    _cached[key] = kept
    if store is not None:
        try:
            await store.save(key, kept)
        except Exception:  # the dialog still has it; the next open reads Bambuddy again
            logger.warning(
                "the preset catalogue could not be kept for other replicas", exc_info=True
            )
    return kept.catalogue


def _refresh(config: BambuddyConfig, store: CatalogueStore | None, key: str) -> None:
    """Read the catalogue again behind the dialog, once at a time per Bambuddy. Its own
    client: the request's closes when the request answers."""
    if key in _refreshing:
        return

    async def run() -> None:
        try:
            async with BambuddyClient(config) as fresh:
                await _read(fresh, store, key)
        except Exception:  # the kept one stays; the next stale open tries again
            logger.warning("refreshing the preset catalogue failed", exc_info=True)
        finally:
            _refreshing.pop(key, None)

    _refreshing[key] = asyncio.get_running_loop().create_task(run())


async def cached_catalogue(
    client: BambuddyClient, store: CatalogueStore | None = None
) -> _Catalogue:
    """:func:`_catalogue`, kept per Bambuddy (#2361) in this process and, with a
    ``store``, for every replica (#2978).

    Only the very first open waits on Bambuddy. A kept catalogue older than
    :data:`CATALOGUE_TTL` is served as it is while a fresh read runs behind it, so a
    profile imported in Bambuddy shows on the open after that read, not within a fixed
    time. Only the print dialog's choices read it: the check and the run, which slice
    with what they resolve, still read the catalogue fresh. A failed read is not kept."""
    key = _key(client.config)
    kept = _cached.get(key)
    if store is not None and (kept is None or time.time() - kept.read_at >= CATALOGUE_TTL):
        # Another replica may have read it since.
        shared = await store.load(key)
        if shared is not None and (kept is None or shared.read_at > kept.read_at):
            kept = _cached[key] = shared
    if kept is None:
        return await _read(client, store, key)
    if time.time() - kept.read_at >= CATALOGUE_TTL:
        _refresh(client.config, store, key)
    return kept.catalogue


async def settle_refreshes() -> None:
    """Wait for every background read: tests and shutdown."""
    while _refreshing:
        await asyncio.gather(*list(_refreshing.values()), return_exceptions=True)


def forget_catalogues() -> None:
    """Drop every kept catalogue, and stop every refresh: each test starts with none."""
    for task in _refreshing.values():
        task.cancel()
    _refreshing.clear()
    _cached.clear()
