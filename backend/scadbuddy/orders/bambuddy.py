"""Orders' Bambuddy (#4253, spec 2026-10-18 §8, Ruling 7): the one file in ``orders/``
that reaches ``bambuddy.client`` (``tests/test_orders_bambuddy_seam.py`` checks it).

:class:`BambuddyIntegrations` is the ``OrderIntegrations`` seam with ``bambuddy_url`` set
(``orders/integrations.py``): the spool import's read (§8.3). The functions below it are
the quote's slice through Bambuddy's slicer (#4250 Ruling 2), the print dialog's own
background slice, which the quote takes on whichever slicer the stack has; they are
here so that ``orders/`` has one door to the client.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from contextlib import aclosing
from typing import Any

from scadbuddy.bambuddy.client import client_for, maybe_client
from scadbuddy.bambuddy.models import SliceJob, Spool
from scadbuddy.bambuddy.preview import PreviewStarted, preview_job, start_preview_plates
from scadbuddy.bambuddy.print_run import PrintRunRequest
from scadbuddy.bambuddy.print_source import PrintSource
from scadbuddy.bambuddy.uploads import BambuddyUploadStore
from scadbuddy.library.settings_store import SettingsStore, StoredSettings
from scadbuddy.orders.integrations import SpoolGroup


def _colour(rgba: str | None) -> str | None:
    """A spool's ``RRGGBBAA`` (no ``#``, ``models.Spool``) as a record's ``#RRGGBB``."""
    text = (rgba or "").strip()
    if len(text) not in (6, 8):
        return None
    try:
        int(text, 16)
    except ValueError:
        return None
    return "#" + text[:6].upper()


def _identity(spool: Spool) -> tuple[str, str, str, str]:
    colour = _colour(spool.rgba)
    return (
        spool.material.strip().upper(),
        (spool.subtype or "").strip().upper(),
        (spool.brand or "").strip().upper(),
        colour if colour is not None else "name:" + (spool.color_name or "").strip().upper(),
    )


def spool_groups(spools: list[Spool]) -> list[SpoolGroup]:
    """Every unarchived spool by the record identity it would price (material, subtype,
    brand and ``#RRGGBB``, upper-cased), in that order. That is ``bambuddy/farm.py``
    ``_filament_key``'s grouping with the colour as a record holds it: the alpha byte and
    a 6- or 8-digit spelling are dropped, so two spools that would land on one record are
    one group, priced once (#5159 review). A spool with no ``rgba`` keeps its colour's
    name as its key, never matched to an rgba one (#2918). A group's names are its most
    recent spool's (the highest id)."""
    groups: dict[tuple[str, str, str, str], list[Spool]] = defaultdict(list)
    for spool in spools:
        if spool.archived_at is None:
            groups[_identity(spool)].append(spool)
    out: list[SpoolGroup] = []
    for key in sorted(groups):
        rows = sorted(groups[key], key=lambda s: s.id)
        latest = rows[-1]
        out.append(
            SpoolGroup(
                material=latest.material.strip(),
                subtype=(latest.subtype or "").strip() or None,
                brand=(latest.brand or "").strip() or None,
                colour_name=(latest.color_name or "").strip() or None,
                colour=_colour(latest.rgba),
                spools=[(s.id, s.cost_per_kg) for s in rows],
            )
        )
    return out


class BambuddyIntegrations:
    """The seam with a Bambuddy linked."""

    def __init__(self, settings: SettingsStore) -> None:
        self.settings = settings

    @property
    def linked(self) -> bool:
        return True

    async def spool_groups(self) -> list[SpoolGroup]:
        """The inventory's spools (``inventory_view``'s, ``GET /inventory/spools``,
        archived ones left out) by filament. Only the spools: the loaded slots that
        ``inventory_view`` adds say nothing about a price."""
        stored = await asyncio.to_thread(self.settings.load)
        async with client_for(stored) as client:
            return spool_groups(await client.spools())


# -- the quote's slice through Bambuddy's slicer (#4250) ---------------------------------


async def start_quote_preview(
    source: PrintSource,
    settings: StoredSettings,
    uploads: BambuddyUploadStore,
    request: PrintRunRequest,
    *,
    lan: bool,
    **kwargs: Any,
) -> list[PreviewStarted]:
    """``start_preview_plates`` with the client a run on Bambuddy's path needs, or none
    for a printer the LAN reaches (plan 2026-10-18 D3)."""
    async with maybe_client(settings, native=lan) as client:
        return await start_preview_plates(client, source, settings, uploads, request, **kwargs)


async def quote_preview_job(
    settings: StoredSettings, uploads: BambuddyUploadStore, job_id: int, subject: str
) -> SliceJob:
    """A Bambuddy background slice's state."""
    async with client_for(settings) as client:
        return await preview_job(client, uploads, job_id, subject)


async def download_sliced(settings: StoredSettings, file_id: int, limit: int) -> bytes | None:
    """A Bambuddy library file's bytes; ``None`` once they pass ``limit``."""
    data = bytearray()
    async with (
        client_for(settings) as client,
        aclosing(client.download_library_file(file_id)) as chunks,
    ):
        async for chunk in chunks:
            data += chunk
            if len(data) > limit:
                return None
    return bytes(data)
