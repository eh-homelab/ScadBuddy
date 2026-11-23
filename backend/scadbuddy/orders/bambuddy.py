"""Orders' Bambuddy (#4253, spec 2026-10-18 §8, Ruling 7): the one file in ``orders/``
that reaches ``bambuddy.client`` (``tests/test_orders_bambuddy_seam.py`` checks it).

:class:`BambuddyIntegrations` is the ``OrderIntegrations`` seam with ``bambuddy_url`` set
(``orders/integrations.py``): the spool import's tray assignments (its spools are
ScadBuddy's own since #6262), the order's nested projects (§8.1) and an attempt's
archives (§8.2). The functions below it are
the quote's slice through Bambuddy's slicer (#4250 Ruling 2), the print dialog's own
background slice, which the quote takes on whichever slicer the stack has; they are
here so that ``orders/`` has one door to the client.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from contextlib import aclosing, asynccontextmanager
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from scadbuddy.bambuddy.client import BambuddyClient, Operation, client_for, maybe_client
from scadbuddy.bambuddy.costs import archive_cost
from scadbuddy.bambuddy.models import (
    FINISH_PHOTO_PREFIX,
    ArchiveDetail,
    FolderCreate,
    LibraryFile,
    PrinterMaintenanceOverview,
    SliceJob,
    SpoolAssignment,
)
from scadbuddy.bambuddy.preview import PreviewStarted, preview_job, start_preview_plates
from scadbuddy.bambuddy.print_run import PrintRunRequest
from scadbuddy.bambuddy.print_source import LibrarySource, PrintSource
from scadbuddy.bambuddy.projects import FolderlessProjects, ProjectRequest, ensure_project
from scadbuddy.bambuddy.send import Sink, upload_raw
from scadbuddy.bambuddy.uploads import BambuddyUploadStore
from scadbuddy.core.problems import ApiError
from scadbuddy.filament.bambuddy_pull import read_assignments
from scadbuddy.library.settings_store import SettingsStore, StoredSettings
from scadbuddy.orders.held_print import STOREFRONT_FOLDER, NativeShelf, Shelf, Shelved
from scadbuddy.orders.integrations import (
    ArchiveFacts,
    ArchiveMedia,
    ArchiveSlot,
    CopiedMedia,
    MaintenanceDone,
    MaintenanceTask,
    MediaTooLargeError,
)
from scadbuddy.print_library.ids import is_native_id
from scadbuddy.print_library.reads import NativeCopies, NativeReads, reads_for, served_reads

if TYPE_CHECKING:
    from scadbuddy.orders.storefront.held_file import HeldFile
    from scadbuddy.print_library.native import NativeLibrary

logger = logging.getLogger(__name__)

#: How a held copy is sent to Bambuddy, as the library page's upload sends a file.
UPLOAD_MEDIA_TYPE = "application/octet-stream"


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
            item_id=item.id,
            current_hours=item.current_hours,
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

    async def spool_assignments(self) -> list[SpoolAssignment]:
        """``GET /inventory/assignments``, as `filament/bambuddy_pull.py`
        `read_assignments` reads it."""
        return await read_assignments(await asyncio.to_thread(self.settings.load))

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

    async def maintenance_history(
        self, item_ids: Sequence[int]
    ) -> dict[int, list[MaintenanceDone]]:
        """``GET /maintenance/items/{id}/history`` of each task, in turn, on one client
        (#5774 §3.7)."""
        stored = await asyncio.to_thread(self.settings.load)
        logs: dict[int, list[MaintenanceDone]] = {}
        async with client_for(stored) as client:
            for item_id in item_ids:
                logs[item_id] = [
                    MaintenanceDone(
                        history_id=row.id,
                        at=row.performed_at,
                        hours_at=row.hours_at_maintenance,
                        notes=row.notes,
                    )
                    for row in await client.maintenance_history(item_id)
                ]
        return logs

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
    held: HeldFile | None = None,
    **kwargs: Any,
) -> list[PreviewStarted]:
    """A library file item's slice (#5544): :func:`start_quote_preview` of the file, read
    as the dialog's background slice of a library file reads it (``bambuddy/operations.py``
    ``preview_run``): its slots from the download it uploads (#2550), and a native run's
    copy into the native library. A sliced file is refused there (422): it has nothing to
    slice. With ``held`` (a file listing's price, #6710 §7.3.1) the file is read from its
    held copy in place of the library, and its copies name the copy's version; the rest of
    the slice is the same."""
    async with maybe_client(settings, native=lan) as client:
        source = await LibrarySource.load(
            client,
            file_id,
            uploads=uploads,
            settings=settings,
            read_own=True,
            files=held if held is not None else reads_for(file_id, client, native_reads),
            native=native_reads if lan else None,
            held=held is not None,
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

    async def download(self, file_id: int) -> AsyncGenerator[bytes]:
        """The file's bytes, from the library that serves (a storefront file listing's
        publish, #6710 §5.1)."""
        native = await self.native()
        stored = await asyncio.to_thread(self.settings.load)
        async with maybe_client(stored, native=native is not None) as client:
            reads = served_reads(file_id, client, native)
            async with aclosing(reads.download_library_file(file_id)) as chunks:
                async for chunk in chunks:
                    yield chunk

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


class BambuddyShelf:
    """``orders/held_print.py`` ``Shelf`` over Bambuddy's library (#6710 §10.2). Its
    listing states no hash, so a file of the copy's size has its ``file_hash`` read
    (``GET /library/files/{id}``)."""

    def __init__(self, client: BambuddyClient, uploads: BambuddyUploadStore) -> None:
        self.client = client
        self.uploads = uploads

    async def storefront_folder(self) -> int:
        tops = [f for f in await self.client.folders() if f.parent_id is None]
        named = sorted(f.id for f in tops if f.name == STOREFRONT_FOLDER)
        if named:
            return named[0]
        made = await self.client.create_folder(FolderCreate(name=STOREFRONT_FOLDER))
        return made.id

    async def find(self, folder_id: int, sha256: str, size: int) -> Shelved | None:
        for row in sorted(await self.client.library_files(folder_id), key=lambda r: r.id):
            if row.file_size is not None and row.file_size != size:
                continue
            try:
                found = await self.client.library_file(row.id)
            except ApiError as error:
                if error.status == 404:
                    continue
                raise
            if (found.file_hash or "").lower() == sha256:
                return Shelved(found.id, found.filename)
        return None

    async def upload(self, filename: str, chunks: AsyncIterator[bytes], folder_id: int) -> Shelved:
        # Streamed from the content store into the one spool `upload_raw` keeps.
        async def write(sink: Sink) -> None:
            async for chunk in chunks:
                await asyncio.to_thread(sink.write, chunk)

        made = await upload_raw(
            self.client,
            self.uploads,
            folder_id,
            filename=filename,
            media_type=UPLOAD_MEDIA_TYPE,
            write=write,
        )
        return Shelved(made.id, made.filename)


@asynccontextmanager
async def served_shelf(
    settings: SettingsStore, native: NativeLibrary | None, uploads: BambuddyUploadStore
) -> AsyncIterator[Shelf]:
    """``orders/held_print.py``'s shelf of the library that serves: ``native`` when it
    serves, else Bambuddy's."""
    if native is not None:
        yield NativeShelf(native)
        return
    stored = await asyncio.to_thread(settings.load)
    async with client_for(stored) as client:
        yield BambuddyShelf(client, uploads)
