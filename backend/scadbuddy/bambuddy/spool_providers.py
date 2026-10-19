"""Bambuddy's inventory as a spool provider (#3818 tier A, plan 2026-10-18 D1, Task 6b).

Read only by the spool refresh on its own Schedule (`workflows/spool_refresh.py`), never
by the print dialog, so the hot path never waits on Bambuddy. Each unarchived spool of
``GET /inventory/spools`` (`BambuddyClient.spools`) is one record: its id as the record
id, ``rgba`` normalised to ``#RRGGBB`` as the picker does (`filaments.normalise_colour`),
``color_name`` as its name, what is left (`Spool.remaining_g`), the label weight, the
RFID keys Bambuddy read from the AMS, ``slicer_filament`` as its preset, and the tray
``GET /inventory/assignments`` puts it in (`BambuddyClient.spool_assignments`) as its
location, with what Bambuddy saw in the tray then. Assignments name Bambuddy's own
printer ids (``bambuddy:<id>``); the registry's ``printers.bambuddy_printer_id`` maps
them to a printer (plan 2026-10-16 Task 2).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from scadbuddy.bambuddy.client import client_for
from scadbuddy.bambuddy.filaments import normalise_colour
from scadbuddy.bambuddy.models import Spool, SpoolAssignment
from scadbuddy.core.components import Components, Core
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.spools.models import (
    SpoolLocation,
    SpoolSnapshot,
    clean_tag_uid,
    clean_tray_uuid,
    tray_fingerprint,
)
from scadbuddy.spools.providers import SpoolProvider, SpoolProviderBuild

logger = logging.getLogger(__name__)


def location(assignment: SpoolAssignment) -> SpoolLocation:
    seen = assignment.fingerprint_type is not None or assignment.fingerprint_color is not None
    return SpoolLocation(
        printer=f"bambuddy:{assignment.printer_id}",
        ams_id=assignment.ams_id,
        tray_id=assignment.tray_id,
        fingerprint=(
            tray_fingerprint(assignment.fingerprint_type, assignment.fingerprint_color)
            if seen
            else None
        ),
    )


def snapshot(spool: Spool, where: SpoolLocation | None = None) -> SpoolSnapshot:
    return SpoolSnapshot(
        record_id=str(spool.id),
        material=spool.material,
        colour=normalise_colour(spool.rgba),
        brand=spool.brand,
        name=spool.color_name,
        remaining_g=spool.remaining_g,
        label_weight_g=spool.label_weight or None,
        tray_uuid=clean_tray_uuid(spool.tray_uuid),
        tag_uid=clean_tag_uid(spool.tag_uid),
        preset=spool.slicer_filament,
        location=where,
    )


class BambuddyProvider:
    name = "bambuddy"
    label = "Bambuddy"
    #: Behind ScadBuddy's own inventory (tier B) when it comes, ahead of others.
    priority = 100

    def __init__(self, load: Callable[[], StoredSettings]) -> None:
        #: The stored settings, read at each call: the URL is a live setting.
        self._load = load

    async def configured(self) -> bool:
        return bool((await asyncio.to_thread(self._load)).bambuddy_url)

    async def records(self) -> list[SpoolSnapshot]:
        stored = await asyncio.to_thread(self._load)
        async with client_for(stored) as client:
            spools = await client.spools()
            try:
                assignments = await client.spool_assignments()
            except Exception as error:
                # The locations are a hint: the records still refresh without them.
                logger.warning(
                    "could not read Bambuddy's spool assignments; refreshing without them",
                    extra={"error": type(error).__name__},
                )
                assignments = []
        where = {a.spool_id: location(a) for a in assignments}
        return [snapshot(spool, where.get(spool.id)) for spool in spools]


def _build(core: Core, components: Components) -> SpoolProvider:
    return BambuddyProvider(core.settings_store.load)


SPOOL_PROVIDERS: list[SpoolProviderBuild] = [_build]
