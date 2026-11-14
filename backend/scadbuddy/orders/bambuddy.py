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
import logging
from collections import defaultdict
from collections.abc import Awaitable
from contextlib import aclosing
from pathlib import Path, PurePosixPath
from typing import Any, cast, get_args

from pydantic import ValidationError

from scadbuddy.bambuddy.client import BambuddyClient, Operation, client_for, maybe_client
from scadbuddy.bambuddy.costs import archive_cost
from scadbuddy.bambuddy.models import (
    FINISH_PHOTO_PREFIX,
    ArchiveDetail,
    CatalogFilament,
    LibraryFile,
    LocalPreset,
    PrinterMaintenanceOverview,
    SliceJob,
    Spool,
)
from scadbuddy.bambuddy.preview import PreviewStarted, preview_job, start_preview_plates
from scadbuddy.bambuddy.print_run import PrintRunRequest
from scadbuddy.bambuddy.print_source import LibrarySource, PrintSource
from scadbuddy.bambuddy.projects import FolderlessProjects, ProjectRequest, ensure_project
from scadbuddy.bambuddy.trays import tray_at
from scadbuddy.bambuddy.uploads import BambuddyUploadStore
from scadbuddy.core.problems import ApiError
from scadbuddy.library.settings_store import SettingsStore, StoredSettings
from scadbuddy.orders.density import density_text
from scadbuddy.orders.integrations import (
    ArchiveFacts,
    ArchiveMedia,
    ArchiveSlot,
    CopiedMedia,
    LocalDensity,
    MaintenanceTask,
    MediaTooLargeError,
    ShelfSpool,
    SpoolGroup,
)
from scadbuddy.orders.models import FilamentEffect
from scadbuddy.print_library.ids import is_native_id
from scadbuddy.print_library.reads import NativeCopies, NativeReads, reads_for, served_reads

logger = logging.getLogger(__name__)


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


def _hex(text: str) -> bool:
    try:
        int(text, 16)
    except ValueError:
        return False
    return True


def _alpha(rgba: str | None) -> int:
    """A spool's ``RRGGBBAA`` alpha byte; 255 (opaque) for an ``RRGGBB`` or none."""
    text = (rgba or "").strip()
    return int(text[6:], 16) if len(text) == 8 and _hex(text) else 255


def _stops(extra_colors: str | None) -> list[str]:
    """Bambuddy's ``extra_colors`` (comma-separated 6- or 8-digit hex, ``#`` optional;
    ``normalize_extra_colors`` in backend/app/schemas/spool.py at
    https://github.com/maziggy/bambuddy/blob/1aa7bce166682fde929fec0d535bdaaa22685aad/backend/app/schemas/spool.py)
    as ``#RRGGBB``/``#RRGGBBAA`` stops: a token it would refuse is dropped, and only
    its ``MAX_EXTRA_COLOR_STOPS`` (8) first are kept."""
    tokens = (t.strip().lstrip("#") for t in (extra_colors or "").split(","))
    return ["#" + t.upper() for t in tokens if len(t) in (6, 8) and _hex(t)][:8]


def _effect(effect_type: str | None) -> FilamentEffect | None:
    """Bambuddy's ``effect_type`` as its ``normalize_effect_type`` spells it ("Dual
    Color" is ``dual-color``); ``None`` for one outside ``ALLOWED_EFFECT_TYPES``."""
    text = (effect_type or "").strip().lower().replace("_", "-").replace(" ", "-")
    return cast(FilamentEffect, text) if text in get_args(FilamentEffect) else None


def _identity(spool: Spool) -> tuple[str, str, str, str]:
    colour = _colour(spool.rgba)
    return (
        spool.material.strip().upper(),
        (spool.subtype or "").strip().upper(),
        (spool.brand or "").strip().upper(),
        colour if colour is not None else "name:" + (spool.color_name or "").strip().upper(),
    )


def spool_groups(
    spools: list[Spool],
    *,
    local: dict[str, LocalDensity] | None = None,
    trays: dict[int, str] | None = None,
    catalogue: list[CatalogFilament] | None = None,
) -> list[SpoolGroup]:
    """Every unarchived spool by the record identity it would price (material, subtype,
    brand and ``#RRGGBB``, upper-cased), in that order. That is ``bambuddy/farm.py``
    ``_filament_key``'s grouping with the colour as a record holds it: the alpha byte and
    a 6- or 8-digit spelling are dropped, so two spools that would land on one record are
    one group, priced once (#5159 review). A spool with no ``rgba`` keeps its colour's
    name as its key, never matched to an rgba one (#2918). A group's names are its most
    recent spool's (the highest id).

    Each group also carries what gives its record a density (#6136,
    `orders/density.py`): its spools' preset ids (``slicer_filament``, newest first, and
    their K-profiles' ``setting_id``), the ``tray_info_idx`` in ``trays`` (by spool id)
    of a spool of it, the local preset in ``local`` (by id) that ``slicer_filament``
    names, and the one ``density`` of the ``catalogue`` entries of its material, brand
    and colour whose ``name`` holds each word of its subtype."""
    groups: dict[tuple[str, str, str, str], list[Spool]] = defaultdict(list)
    for spool in spools:
        if spool.archived_at is None:
            groups[_identity(spool)].append(spool)
    out: list[SpoolGroup] = []
    for key in sorted(groups):
        rows = sorted(groups[key], key=lambda s: s.id)
        latest = rows[-1]
        newest_first = list(reversed(rows))
        slicer_filament = next(
            (f for s in newest_first if (f := (s.slicer_filament or "").strip())), None
        )
        settings = list(
            dict.fromkeys(
                setting
                for s in newest_first
                for k in s.k_profiles
                if (setting := (k.setting_id or "").strip())
            )
        )
        colour = _colour(latest.rgba)
        out.append(
            SpoolGroup(
                material=latest.material.strip(),
                subtype=(latest.subtype or "").strip() or None,
                brand=(latest.brand or "").strip() or None,
                colour_name=(latest.color_name or "").strip() or None,
                colour=colour,
                alpha=_alpha(latest.rgba),
                extra_colours=_stops(latest.extra_colors),
                effect=_effect(latest.effect_type),
                spools=[(s.id, s.cost_per_kg) for s in rows],
                slicer_filament=slicer_filament,
                k_profile_settings=settings,
                tray_info_idx=next(
                    (t for s in newest_first if (t := (trays or {}).get(s.id))), None
                ),
                local_preset=(local or {}).get(slicer_filament or ""),
                catalogue_density=_catalogue_density(catalogue or [], latest, colour),
            )
        )
    return out


def _catalogue_density(
    catalogue: list[CatalogFilament], spool: Spool, colour: str | None
) -> str | None:
    """The one density of the catalogue's entries of the spool's material, brand and
    colour (#6136); ``None`` for none, entries that disagree, or a spool with no brand
    or colour. A spool with a subtype takes only an entry whose ``name`` holds each word
    of it, so a PLA Basic spool never takes a PLA Matte entry of its colour (#6148
    review). Bambuddy stores ``color_hex`` as ``#RRGGBB`` (``models/filament.py``,
    validated by ``schemas/filament.py``'s ``^#[0-9A-Fa-f]{6}$`` at v1.3.2b1-daily.20261113:
    https://github.com/maziggy/bambuddy/blob/v1.3.2b1-daily.20261113/backend/app/schemas/filament.py)."""
    if colour is None or not (spool.brand or "").strip():
        return None
    subtype = set((spool.subtype or "").upper().split())
    densities = {
        text
        for entry in catalogue
        if entry.type.strip().upper() == spool.material.strip().upper()
        and (entry.brand or "").strip().upper() == (spool.brand or "").strip().upper()
        and (entry.color_hex or "").upper() == colour
        and subtype <= set(entry.name.upper().split())
        and (text := density_text(entry.density)) is not None
    }
    return densities.pop() if len(densities) == 1 else None


def _local_densities(presets: list[LocalPreset]) -> dict[str, LocalDensity]:
    """Each local filament preset with a density, by its id as a spool names it."""
    return {
        str(preset.id): LocalDensity(name=preset.name, density_g_cm3=density)
        for preset in presets
        if preset.preset_type == "filament"
        and (density := density_text(preset.filament_density)) is not None
    }


async def _density_read[T](read: Awaitable[T], what: str) -> T | None:
    """One of the density chain's reads (#6136); ``None`` when Bambuddy refuses it, so
    the spools are still imported, with no density from it."""
    try:
        return await read
    except ApiError as error:
        logger.warning("no densities from Bambuddy's %s: %s", what, error.detail)
        return None
    except ValidationError as error:
        logger.warning(
            "no densities from Bambuddy's %s: an answer of another shape: %s", what, error
        )
        return None


async def _tray_presets(client: BambuddyClient, spools: list[Spool]) -> dict[int, str]:
    """The ``tray_info_idx`` of each loaded spool that names no preset, by spool id:
    one read of the assignments, then the status reads of the printers that hold one,
    at once, so the slowest printer bounds them, not their sum. A printer whose read
    Bambuddy refuses gives no tray, logged; the other printers' trays are kept, and no
    read is left running when this returns."""
    unnamed = {
        s.id
        for s in spools
        if not (s.slicer_filament or "").strip()
        and not any((k.setting_id or "").strip() for k in s.k_profiles)
    }
    if not unnamed:
        return {}
    loaded = [a for a in await client.spool_assignments() if a.spool_id in unnamed]
    printer_ids = sorted({a.printer_id for a in loaded})
    statuses = await asyncio.gather(
        *(client.printer_status(p) for p in printer_ids), return_exceptions=True
    )
    found: dict[int, str] = {}
    for printer_id, status in zip(printer_ids, statuses, strict=True):
        if isinstance(status, ApiError | ValidationError):
            logger.warning("no AMS trays from Bambuddy's printer %s: %s", printer_id, status)
            continue
        if isinstance(status, BaseException):
            raise status
        for assignment in loaded:
            if assignment.printer_id != printer_id:
                continue
            tray = tray_at(status, assignment.ams_id, assignment.tray_id)
            idx = (tray.tray_info_idx or "").strip() if tray is not None else ""
            if idx:
                found[assignment.spool_id] = idx
    return found


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
        archived ones left out) by filament, with what gives each a density (#6136):
        the local presets (``GET /local-presets/``) only when a spool names one by its
        numeric id, the AMS trays (``GET /inventory/assignments`` and each such
        printer's ``GET /printers/{id}/status``) only for the spools that name no
        preset (a group uses a tray's id only after its own preset ids), and the
        filament catalogue (``GET /filament-catalog/``). The three density reads run at
        once after the spools' read, so the slowest bounds them, not their sum (#6148
        review). A density read Bambuddy refuses gives no density, logged; only the
        spools' read fails the call."""
        stored = await asyncio.to_thread(self.settings.load)
        async with client_for(stored) as client:
            spools = [s for s in await client.spools() if s.archived_at is None]
            failed: BaseException | None = None
            try:
                async with asyncio.TaskGroup() as reads:
                    presets = (
                        reads.create_task(_density_read(client.local_presets(), "local presets"))
                        if any((s.slicer_filament or "").strip().isdigit() for s in spools)
                        else None
                    )
                    trays = reads.create_task(
                        _density_read(_tray_presets(client, spools), "AMS trays")
                    )
                    catalogue = reads.create_task(
                        _density_read(client.filament_catalog(), "filament catalogue")
                    )
            except ExceptionGroup as group:
                # The first read's own error, not the group: the log and the trace show
                # what failed, as before the reads ran at once (#6186 review). It is
                # raised after this block, so its own cause stays; any other read that
                # failed is logged here (#6188 review).
                failed, *others = group.exceptions
                for other in others:
                    logger.warning("another density read failed too", exc_info=other)
            if failed is not None:
                raise failed
            local_read = None if presets is None else presets.result()
            local = _local_densities(local_read.filament) if local_read is not None else {}
            return spool_groups(
                spools,
                local=local,
                trays=trays.result() or {},
                catalogue=catalogue.result() or [],
            )

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
