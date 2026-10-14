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
import uuid
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field, TypeAdapter, ValidationError

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

    def names(self) -> dict[tuple[str, str], str]:
        """``(source, id) -> name``, for resolving the refs a pipeline carries."""
        return {
            (choice.ref.source, choice.ref.id): choice.name
            for choice in (*self.printer, *self.process, *self.filament)
        }


async def _catalogue(client: BambuddyClient) -> _Catalogue:
    presets, local_presets = await asyncio.gather(
        client.presets(), client.local_presets(), return_exceptions=True
    )
    # Thousands of rows: built off the event loop, which every other request shares.
    return await asyncio.to_thread(_build, settled(presets), settled(local_presets))


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


#: How old a kept catalogue may be before the dialog reads a fresh one behind it
#: (#2361, #2978). Bambuddy's presets change only when someone imports or edits a
#: profile there, and ``/slicer/presets`` (1 MB) takes seconds to answer.
CATALOGUE_TTL = 120.0

#: Under the data volume's ``cache/``, which every replica shares (#2978): a catalogue
#: kept in one process missed almost every open, the API running several.
CATALOGUE_DIRNAME = "catalogues"
#: Bumped when :class:`_Catalogue`'s shape changes, so an older file is never read.
CATALOGUE_VERSION = 1

_ADAPTER = TypeAdapter(_Catalogue)


async def read_catalogue(client: BambuddyClient) -> _Catalogue:
    """The preset catalogue, read fresh from Bambuddy, as the check and the run read it;
    :func:`cached_catalogue` is the print dialog's kept copy."""
    return await _catalogue(client)


#: The catalogue this process last parsed, by its file and that file's mtime: one
#: entry, so a changed Bambuddy URL or key does not keep the old one alive (#3447).
_parsed: dict[Path, tuple[int, _Catalogue]] = {}
#: The background reads running in this process, one per file.
_refreshing: dict[Path, asyncio.Task[None]] = {}


async def cached_catalogue(client: BambuddyClient, cache: Path) -> _Catalogue:
    """:func:`_catalogue`, kept in a file under ``cache`` per Bambuddy (#2361, #2978).

    A kept catalogue answers at once, however old; one older than
    :data:`CATALOGUE_TTL` is read again in the background for the next open. So a
    profile imported in Bambuddy shows from the open after the one that started that
    read. Only the print dialog's choices read it: the check and the run, which slice
    with what they resolve, still read the catalogue fresh. A failed read is not kept;
    with nothing kept, the open waits for its own read and fails with it."""
    path = _path(cache, client.config)
    kept = await asyncio.to_thread(_load, path)
    if kept is None:
        catalogue = await _catalogue(client)
        try:
            await asyncio.to_thread(_keep, path, catalogue)
        except OSError as error:
            # Kept or not, this open has its catalogue.
            logger.warning(
                "could not keep the preset catalogue", extra={"error": type(error).__name__}
            )
        return catalogue
    age, catalogue = kept
    if age >= CATALOGUE_TTL and path not in _refreshing:
        task = asyncio.create_task(_refresh(client.config, path))
        _refreshing[path] = task
        task.add_done_callback(lambda _: _refreshing.pop(path, None))
    return catalogue


def _path(cache: Path, config: BambuddyConfig) -> Path:
    """One file per Bambuddy and key, named by a digest so the key never reaches a name."""
    digest = hashlib.sha256(f"{config.base_url}\0{config.api_key or ''}".encode()).hexdigest()
    return cache / CATALOGUE_DIRNAME / f"v{CATALOGUE_VERSION}-{digest[:32]}.json"


def _load(path: Path) -> tuple[float, _Catalogue] | None:
    """The kept catalogue and its age in seconds; ``None`` when there is none to read."""
    try:
        stat = path.stat()
        held = _parsed.get(path)
        if held is None or held[0] != stat.st_mtime_ns:
            held = (stat.st_mtime_ns, _ADAPTER.validate_json(path.read_bytes()))
            _parsed.clear()
            _parsed[path] = held
    except (OSError, ValidationError):
        return None
    return time.time() - stat.st_mtime, held[1]


def _keep(path: Path, catalogue: _Catalogue) -> None:
    """Write ``path`` aside and rename it in, so another replica never reads half."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
    try:
        partial.write_bytes(_ADAPTER.dump_json(catalogue))
        partial.replace(path)
    except OSError:
        # Nothing sweeps this directory, so a write that failed part-way goes (#3447).
        partial.unlink(missing_ok=True)
        raise


async def _refresh(config: BambuddyConfig, path: Path) -> None:
    """Read the catalogue again with a client of its own, since the request's closes
    when it answers. A failure keeps the old one for the next open to retry."""
    try:
        async with BambuddyClient(config) as client:
            catalogue = await _catalogue(client)
        await asyncio.to_thread(_keep, path, catalogue)
    except Exception as error:
        # Nothing awaits this task, so whatever fails ends here, and only by its type:
        # a pydantic error quotes Bambuddy's body (#3446).
        logger.warning(
            "could not refresh the kept preset catalogue", extra={"error": type(error).__name__}
        )


async def stop_refreshes() -> None:
    """Cancel this process's background reads and wait for them, at shutdown (#3446)."""
    tasks = list(_refreshing.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def forget_catalogues() -> None:
    """Forget what this process parsed and stop its refreshes: each test starts with
    none. The files are each test's own, under its own ``cache``."""
    for task in _refreshing.values():
        task.cancel()
    _refreshing.clear()
    _parsed.clear()
