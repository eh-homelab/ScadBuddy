"""Bambuddy's preset catalogue: every printer/process/filament preset it offers,
normalised into one shape.

Split out of ``print_run.py`` so that ``resolver.py`` can
depend on it without ``print_run.py`` depending on ``resolver.py`` — the catalogue is
the only piece of ``print_run.py`` the resolver needs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass

from pydantic import BaseModel, Field

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.concurrent import settled
from scadbuddy.bambuddy.models import LocalPreset, Preset, PresetRef

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
    catalogue, local = settled(presets), settled(local_presets)
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
#: seconds to answer, so every dialog open paid seconds for the same answer.
CATALOGUE_TTL = 120.0

_cached: dict[tuple[str, str | None], tuple[float, _Catalogue]] = {}


async def cached_catalogue(client: BambuddyClient) -> _Catalogue:
    """:func:`_catalogue`, kept for :data:`CATALOGUE_TTL` per Bambuddy (#2361).

    Only the print dialog's choices read it: a profile imported in Bambuddy shows
    there within the TTL, while the check and the run, which slice with what they
    resolve, still read the catalogue fresh. A failed read is not kept."""
    key = (client.config.base_url, client.config.api_key)
    held = _cached.get(key)
    if held is not None and time.monotonic() - held[0] < CATALOGUE_TTL:
        return held[1]
    catalogue = await _catalogue(client)
    _cached[key] = (time.monotonic(), catalogue)
    return catalogue


def forget_catalogues() -> None:
    """Drop every kept catalogue: each test starts with none."""
    _cached.clear()
