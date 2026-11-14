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
from scadbuddy.bambuddy.costs import archive_cost
from scadbuddy.bambuddy.models import (
    FINISH_PHOTO_PREFIX,
    ArchiveDetail,
    LibraryFile,
    PrinterMaintenanceOverview,
    SliceJob,
    Spool,
)
from scadbuddy.bambuddy.preview import PreviewStarted, preview_job, start_preview_plates
from scadbuddy.bambuddy.print_run import PrintRunRequest
from scadbuddy.bambuddy.print_source import LibrarySource, PrintSource
from scadbuddy.bambuddy.projects import FolderlessProjects, ProjectRequest, ensure_project
from scadbuddy.bambuddy.uploads import BambuddyUploadStore
from scadbuddy.core.problems import ApiError
from scadbuddy.library.settings_store import SettingsStore, StoredSettings
from scadbuddy.orders.integrations import (
    ArchiveFacts,
    ArchiveMedia,
    ArchiveSlot,
    CopiedMedia,
    MaintenanceTask,
    MediaTooLargeError,
    ShelfSpool,
    SpoolGroup,
)
from scadbuddy.print_library.ids import is_native_id
from scadbuddy.print_library.reads import NativeCopies, NativeReads, reads_for, served_reads


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
                slicer_filament=next(
                    (f for s in reversed(rows) if (f := (s.slicer_filament or "").strip())),
                    None,
                ),
            )
        )
    return out


def archive_facts(archive: ArchiveDetail) -> ArchiveFacts:
    """What an attempt copies of one archive (§8.2), and what a past print's item is
    priced from (#4777): its plate, estimate, file name and slots."""
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
        timelapse_name=_timelapse_name(archive) if archive.timelapse_path else None,
        plate_id=archive.plate_id,
        print_seconds=archive.print_time_seconds,
        filename=_file_name(archive),
        slots=[
            ArchiveSlot(slot_id=slot.slot_id, used_g=slot.used_g, color=slot.color)
            for slot in archive.extra_data.filament_slots
        ],
    )


def _file_name(archive: ArchiveDetail) -> str:
    """The printed file's own name, without any folder Bambuddy archived it under."""
    name = PurePosixPath((archive.filename or "").replace("\\", "/")).name
    return name or f"archive-{archive.id}.3mf"


def maintenance_tasks(overview: PrinterMaintenanceOverview) -> list[MaintenanceTask]:
    """A printer's maintenance tasks in orders' own shape (#4625)."""
    return [
        MaintenanceTask(
            task=item.maintenance_type_name,
            interval_hours=item.interval_hours,
            interval_type=item.interval_type,
            enabled=item.enabled,
        )
        for item in overview.maintenance_items
    ]


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
        """``GET /inventory/spools`` (archived ones included, so a named one is known)
        and ``GET /inventory/assignments`` for where each is loaded: two reads, none per
        printer, so one printer Bambuddy cannot reach refuses no print (#5269 review)."""
        stored = await asyncio.to_thread(self.settings.load)
        async with client_for(stored) as client:
            spools, assignments = await asyncio.gather(
                client.spools(include_archived=True), client.spool_assignments()
            )
        loaded = {a.spool_id: a.printer_id for a in assignments}
        return [
            ShelfSpool(
                id=spool.id,
                material=spool.material,
                subtype=spool.subtype,
                brand=spool.brand,
                colour=_colour(spool.rgba),
                remaining_g=spool.remaining_g,
                archived=spool.archived_at is not None,
                loaded_printer_id=loaded.get(spool.id),
            )
            for spool in spools
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

    async def archive_cost(self, archive_id: int) -> float | None:
        stored = await asyncio.to_thread(self.settings.load)
        async with client_for(stored) as client:
            try:
                archive = await client.archive(archive_id)
            except ApiError as error:
                if error.status == 404 and await client.archive_deleted(archive_id):
                    return None
                raise
            runs = await client.archive_runs(archive_id)
        return archive_cost(archive.cost, archive.filament_used_grams, runs.items)

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
            elif what == "file":
                if name is None:
                    name = _file_name(await client.archive(archive_id))
                path, filename = f"/archives/{archive_id}/download", name
                operation = "media.download"
            else:
                if name is None:
                    name = _timelapse_name(await client.archive(archive_id))
                path, filename = f"/archives/{archive_id}/timelapse", name
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

    async def maintenance_tasks(self, printer_id: int) -> list[MaintenanceTask]:
        """``GET /maintenance/printers/{id}``: one printer's, never the overview of
        every printer, as a machine rate prices one printer."""
        stored = await asyncio.to_thread(self.settings.load)
        async with client_for(stored) as client:
            return maintenance_tasks(await client.printer_maintenance(printer_id))

    async def project_exists(self, project_id: int) -> bool:
        """``GET /projects/{id}``: false only on a 404, a project deleted in Bambuddy."""
        stored = await asyncio.to_thread(self.settings.load)
        async with client_for(stored) as client:
            try:
                await client.project(project_id)
            except ApiError as error:
                if error.status == 404:
                    return False
                raise
        return True

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


async def start_library_quote_preview(
    file_id: int,
    settings: StoredSettings,
    uploads: BambuddyUploadStore,
    request: PrintRunRequest,
    *,
    lan: bool,
    native_reads: NativeCopies | None,
    **kwargs: Any,
) -> list[PreviewStarted]:
    """A library file item's slice (#5544): :func:`start_quote_preview` of the file, read
    as the dialog's background slice of a library file reads it (``bambuddy/operations.py``
    ``preview_run``): its slots from the download it uploads (#2550), and a native run's
    copy into the native library. A sliced file is refused there (422): it has nothing to
    slice."""
    async with maybe_client(settings, native=lan) as client:
        source = await LibrarySource.load(
            client,
            file_id,
            uploads=uploads,
            settings=settings,
            read_own=True,
            files=reads_for(file_id, client, native_reads),
            native=native_reads if lan else None,
        )
        return await start_preview_plates(
            client, source, settings, uploads, request, native_reads=native_reads, **kwargs
        )


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


# -- a library file item's file (#5544) --------------------------------------------------


class ServedLibrary:
    """``orders/quote.py`` ``LibraryFiles`` over the library that serves (#5544), read as
    ``POST /print/library/{file_id}/run`` reads it (``check_served``, ``served_reads``):
    ScadBuddy's own on ``library_backend=native``, else Bambuddy's. A file of the other
    library is not there, as that route's 404 says."""

    def __init__(self, settings: SettingsStore, native: NativeReads) -> None:
        self.settings = settings
        self.native = native

    async def file(self, file_id: int) -> LibraryFile | None:
        """The file, or None when the library that serves does not have it (deleted,
        trashed, or the other library's). Any other failure raises."""
        native = await self.native()
        if is_native_id(file_id) != (native is not None):
            return None
        stored = await asyncio.to_thread(self.settings.load)
        async with maybe_client(stored, native=native is not None) as client:
            try:
                return await served_reads(file_id, client, native).library_file(file_id)
            except ApiError as error:
                if error.status == 404:
                    return None
                raise

    async def exists(self, file_id: int) -> bool:
        return await self.file(file_id) is not None

    async def colours(self, file_id: int) -> dict[int, str]:
        """The file's slot colours by slot id (from 1), as its filament requirements name
        them; empty when they cannot be read (the slots are then grey)."""
        native = await self.native()
        stored = await asyncio.to_thread(self.settings.load)
        try:
            async with maybe_client(stored, native=native is not None) as client:
                needs = await served_reads(file_id, client, native).filament_requirements(file_id)
        except ApiError:
            return {}
        return {slot.slot_id: slot.color for slot in needs.filaments if slot.color}
