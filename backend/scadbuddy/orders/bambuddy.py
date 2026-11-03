"""Orders' Bambuddy (#4253, spec 2026-10-18 §8, Ruling 7): the one file in ``orders/``
that reaches ``bambuddy.client`` (``tests/test_orders_bambuddy_seam.py`` checks it).

:class:`BambuddyIntegrations` is the ``OrderIntegrations`` seam with ``bambuddy_url`` set
(``orders/integrations.py``): the spool import's read (§8.3), the order's nested
projects (§8.1) and an attempt's archives (§8.2). The functions below it are
the quote's slice through Bambuddy's slicer (#4250 Ruling 2), the print dialog's own
background slice, which the quote takes on whichever slicer the stack has; they are
here so that ``orders/`` has one door to the client.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections import defaultdict
from contextlib import aclosing
from pathlib import Path, PurePosixPath
from typing import Any

from scadbuddy.bambuddy.client import Operation, client_for, maybe_client
from scadbuddy.bambuddy.farm import inventory_view
from scadbuddy.bambuddy.models import FINISH_PHOTO_PREFIX, ArchiveDetail, SliceJob, Spool
from scadbuddy.bambuddy.preview import PreviewStarted, preview_job, start_preview_plates
from scadbuddy.bambuddy.print_run import PrintRunRequest
from scadbuddy.bambuddy.print_source import PrintSource
from scadbuddy.bambuddy.projects import FolderlessProjects, ProjectRequest, ensure_project
from scadbuddy.bambuddy.uploads import BambuddyUploadStore
from scadbuddy.core.problems import ApiError
from scadbuddy.library.settings_store import SettingsStore, StoredSettings
from scadbuddy.orders.integrations import (
    ArchiveFacts,
    ArchiveMedia,
    CopiedMedia,
    MediaTooLargeError,
    ShelfSpool,
    SpoolGroup,
)


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


def archive_facts(archive: ArchiveDetail) -> ArchiveFacts:
    """What an attempt copies of one archive (§8.2)."""
    grams = archive.total_filament_actual_grams
    if grams is None:
        grams = archive.filament_used_grams
    return ArchiveFacts(
        archive_id=archive.id,
        status=archive.status,
        started_at=archive.started_at,
        completed_at=archive.completed_at,
        actual_seconds=archive.actual_time_seconds,
        grams=grams,
        finish_photos=[p for p in archive.photos if p.startswith(FINISH_PHOTO_PREFIX)],
        timelapse=bool(archive.timelapse_path),
    )


def _timelapse_name(archive: ArchiveDetail) -> str:
    suffix = PurePosixPath(archive.timelapse_path or "").suffix.lower()
    return f"timelapse-{archive.id}{suffix if suffix in ('.mp4', '.avi', '.mov') else '.mp4'}"


class BambuddyIntegrations:
    """The seam with a Bambuddy linked."""

    def __init__(
        self, settings: SettingsStore, folderless: FolderlessProjects | None = None
    ) -> None:
        self.settings = settings
        self.folderless = folderless

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

    async def spool_inventory(self) -> list[ShelfSpool]:
        """``inventory_view``'s spools (archived ones included, so a named one is
        known), each with the printer it is loaded in."""
        stored = await asyncio.to_thread(self.settings.load)
        async with client_for(stored) as client:
            view = await inventory_view(client, include_archived=True)
        return [
            ShelfSpool(
                id=spool.id,
                material=spool.material,
                subtype=spool.subtype,
                brand=spool.brand,
                colour=_colour(spool.rgba),
                remaining_g=spool.remaining_g,
                archived=spool.archived,
                loaded_printer_id=None if spool.loaded is None else spool.loaded.printer_id,
            )
            for spool in view.spools
        ]

    async def archive(self, archive_id: int) -> ArchiveFacts | None:
        stored = await asyncio.to_thread(self.settings.load)
        async with client_for(stored) as client:
            try:
                return archive_facts(await client.archive(archive_id))
            except ApiError as error:
                if error.status == 404 and await client.archive_deleted(archive_id):
                    return None
                raise

    async def download_archive_media(
        self, archive_id: int, what: ArchiveMedia, name: str | None, target: Path, limit: int
    ) -> CopiedMedia:
        """Streamed to ``target``, hashed as it is written, never held in memory: a
        timelapse runs to hundreds of megabytes."""
        stored = await asyncio.to_thread(self.settings.load)
        async with client_for(stored) as client:
            operation: Operation
            if what == "finished_photo":
                assert name is not None
                path, filename = f"/archives/{archive_id}/photos/{name}", name
                operation = "media.photo"
            else:
                detail = await client.archive(archive_id)
                path, filename = f"/archives/{archive_id}/timelapse", _timelapse_name(detail)
                operation = "media.timelapse"
            digest = hashlib.sha256()
            size = 0
            try:
                async with client.stream(
                    path, operation=operation, what="copy the print's file", pass_416=False
                ) as upstream:
                    with target.open("wb") as out:
                        async for chunk in upstream.aiter_bytes():
                            size += len(chunk)
                            if size > limit:
                                raise MediaTooLargeError(f"{filename} is over {limit} bytes")
                            digest.update(chunk)
                            await asyncio.to_thread(out.write, chunk)
            except BaseException:
                target.unlink(missing_ok=True)
                raise
        return CopiedMedia(filename=filename, sha256=digest.hexdigest(), size=size)

    async def ensure_project(self, name: str, parent_id: int | None) -> int:
        """``ensure_project``'s create, as the project picker's create makes one: the
        project and its library folder, nested under ``parent_id``'s (#930)."""
        stored = await asyncio.to_thread(self.settings.load)
        async with client_for(stored) as client:
            view = await ensure_project(
                client,
                ProjectRequest(name=name, parent_id=parent_id),
                folderless=self.folderless,
            )
        return view.id


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
