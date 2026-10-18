"""Bambuddy's inventory as a spool provider (#3818 tier A, plan 2026-10-18 D1, Task 6b).

Read only by the spool refresh on its own Schedule (`workflows/spool_refresh.py`), never
by the print dialog, so the hot path never waits on Bambuddy. Each unarchived spool of
``GET /inventory/spools`` (`BambuddyClient.spools`) is one record: its id as the record
id, ``rgba`` normalised to ``#RRGGBB`` as the picker does (`filaments.normalise_colour`),
``color_name`` as its name, what is left (`Spool.remaining_g`), the label weight, the
RFID keys Bambuddy read from the AMS, and ``slicer_filament`` as its preset.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from scadbuddy.bambuddy.client import client_for
from scadbuddy.bambuddy.filaments import normalise_colour
from scadbuddy.bambuddy.models import Spool
from scadbuddy.core.components import Components, Core
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.spools.models import SpoolSnapshot, clean_key
from scadbuddy.spools.providers import SpoolProvider, SpoolProviderBuild


def snapshot(spool: Spool) -> SpoolSnapshot:
    return SpoolSnapshot(
        record_id=str(spool.id),
        material=spool.material,
        colour=normalise_colour(spool.rgba),
        brand=spool.brand,
        name=spool.color_name,
        remaining_g=spool.remaining_g,
        label_weight_g=spool.label_weight or None,
        tray_uuid=clean_key(spool.tray_uuid),
        tag_uid=clean_key(spool.tag_uid),
        preset=spool.slicer_filament,
    )


class BambuddyProvider:
    name = "bambuddy"
    label = "Bambuddy"

    def __init__(self, load: Callable[[], StoredSettings]) -> None:
        #: The stored settings, read at each call: the URL is a live setting.
        self._load = load

    async def configured(self) -> bool:
        return bool((await asyncio.to_thread(self._load)).bambuddy_url)

    async def records(self) -> list[SpoolSnapshot]:
        stored = await asyncio.to_thread(self._load)
        async with client_for(stored) as client:
            return [snapshot(spool) for spool in await client.spools()]


def _build(core: Core, components: Components) -> SpoolProvider:
    return BambuddyProvider(core.settings_store.load)


SPOOL_PROVIDERS: list[SpoolProviderBuild] = [_build]
