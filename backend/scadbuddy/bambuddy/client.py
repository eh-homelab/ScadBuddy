"""httpx client for Bambuddy's REST API.

Two path details are load-bearing and were measured against a live 1.2.5.5 rather
than read off the design spec:

* ``/api/v1/printers`` **404s** — only ``/api/v1/printers/`` exists, and it answers
  with a bare list whose ``id`` is an integer.
* ``/api/v1/library/folders`` and ``/api/v1/external-links/`` also answer with bare
  lists.
* **Reading** folders is that slashless path; **creating** one is
  ``/api/v1/library/folders/`` **with** the slash. Both routes are real here.
* ``/api/v1/printers/available-filaments`` takes a *required* ``model`` query
  parameter — without it the answer is a 422, not every printer.

Requests bodies are built from the models in ``models.py``, which mirror Bambuddy's own
``openapi.json`` field for field, and are serialised with ``exclude_none`` so an unset
optional is omitted rather than sent as an explicit ``null``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Mapping
from contextlib import AbstractContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import date
from types import TracebackType
from typing import Any, Literal, Self

import httpx
from fastapi import status
from opentelemetry.trace import Span, SpanKind

from scadbuddy.bambuddy.errors import Scope, map_response, map_transport, not_configured
from scadbuddy.bambuddy.models import (
    Archive,
    ArchiveDetail,
    ArchivePhotoUpload,
    ArchiveRunList,
    AvailableFilament,
    ExternalLink,
    FilamentRequirements,
    Folder,
    FolderCreate,
    InventoryRemain,
    LibraryFile,
    LibraryListRow,
    LibraryPlates,
    LocalPresetCatalogue,
    PresetCatalogue,
    Printer,
    PrinterMedia,
    PrinterStatus,
    Project,
    ProjectCreate,
    QueueItem,
    QueueItemCreate,
    SliceJob,
    SliceJobAccepted,
    SliceRequest,
    Spool,
    SpoolAssignment,
    SpoolFilamentPreset,
    TimelapseInfo,
    TimelapseThumbnails,
)
from scadbuddy.core.problems import ApiError
from scadbuddy.core.settings import split_urls
from scadbuddy.core.tracing import detached_span, span
from scadbuddy.library.settings_store import StoredSettings

logger = logging.getLogger(__name__)

API_PREFIX = "/api/v1"


THREE_MF_MEDIA_TYPE = "model/3mf"

DEFAULT_TIMEOUT = 30.0
DEFAULT_UPLOAD_TIMEOUT = 180.0
DEFAULT_SLICE_TIMEOUT = 600.0
DEFAULT_SLICE_POLL = 2.0


#: What a Bambuddy call does, which names its span (``bambuddy.<operation>``, spec
#: 2026-10-01 §4). A closed set, so a span name never carries an id or free text.
Operation = Literal[
    "archives.list",
    "archives.photo.delete",
    "archives.photo.upload",
    "archives.printer_media",
    "archives.read",
    "archives.runs",
    "archives.timelapse.info",
    "archives.timelapse.select",
    "archives.timelapse.thumbnails",
    "external_links.create",
    "external_links.list",
    "external_links.update",
    "library.annotate",
    "library.delete",
    "library.download",
    "library.filament_requirements",
    "library.files.list",
    "library.files.read",
    "library.folders.by_project",
    "library.folders.create",
    "library.folders.list",
    "library.listing",
    "library.plate_thumbnail",
    "library.plates",
    "library.thumbnail",
    "library.upload",
    "media.download",
    "media.photo",
    "media.plate_thumbnail",
    "media.source",
    "media.thumbnail",
    "media.timelapse",
    "printers.available_filaments",
    "printers.camera.snapshot",
    "printers.camera.token",
    "printers.inventory_remain",
    "printers.list",
    "printers.read",
    "printers.status",
    "projects.add_archives",
    "projects.add_queue",
    "projects.create",
    "projects.list",
    "projects.read",
    "queue.add",
    "queue.read",
    "settings.read",
    "slice.job.status",
    "slice.start",
    "slicer.local_presets",
    "slicer.presets",
    "spools.assignments",
    "spools.filament_presets",
    "spools.list",
    "version",
]


def _call_span(
    operation: Operation, method: str, scope: Scope, *, detached: bool = False
) -> AbstractContextManager[Span]:
    """Our side of a Bambuddy call (spec 2026-10-01 §4): no headers are injected; the
    span is named by the operation and holds the method, the status code and the scope,
    never a path or a body. ``detached`` is for a call held open across a ``yield`` (a
    stream), which may be closed from another task: that span is never made current."""
    name = f"bambuddy.{operation}"
    attributes = {"http.request.method": method, "scadbuddy.bambuddy.scope": str(scope)}
    if detached:
        return detached_span(name, kind=SpanKind.CLIENT, attributes=attributes)
    return span(name, kind=SpanKind.CLIENT, attributes=attributes)


@dataclass(frozen=True)
class BambuddyConfig:
    base_url: str
    api_key: str | None = None
    timeout: float = DEFAULT_TIMEOUT
    upload_timeout: float = DEFAULT_UPLOAD_TIMEOUT
    slice_timeout: float = DEFAULT_SLICE_TIMEOUT
    slice_poll_interval: float = DEFAULT_SLICE_POLL
    #: Where a browser reaches Bambuddy (#775); ``base_url`` when unset.
    web_base_url: str | None = None

    @classmethod
    def from_settings(cls, settings: StoredSettings) -> BambuddyConfig:
        if not settings.bambuddy_url:
            raise not_configured("no Bambuddy URL is configured; set one in Settings")
        web = split_urls(settings.bambuddy_web_urls)
        return cls(
            base_url=settings.bambuddy_url.rstrip("/"),
            api_key=settings.bambuddy_api_key,
            web_base_url=web[0] if web else None,
        )

    def url(self, path: str) -> str:
        return f"{self.base_url}{API_PREFIX}{path}"

    def web_url(self, path: str) -> str:
        return f"{self.web_base_url or self.base_url}{path}"


class BambuddyClient:
    """Every method raises :class:`ApiError` — the problem document the browser sees."""

    def __init__(self, config: BambuddyConfig, http: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(timeout=config.timeout)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    @property
    def _headers(self) -> dict[str, str]:
        return {"X-API-Key": self.config.api_key} if self.config.api_key else {}

    async def _send(
        self,
        method: str,
        path: str,
        *,
        scope: Scope,
        operation: Operation,
        what: str,
        params: Mapping[str, Any] | None = None,
        json: Any | None = None,
        files: Any | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        with _call_span(operation, method, scope) as current:
            try:
                response = await self._http.request(
                    method,
                    self.config.url(path),
                    headers=self._headers,
                    params=dict(params) if params else None,
                    json=json,
                    files=files,
                    timeout=timeout or self.config.timeout,
                )
            except httpx.HTTPError as error:
                logger.warning("bambuddy request failed", extra={"method": method, "path": path})
                raise map_transport(error, what=what) from error
            current.set_attribute("http.response.status_code", response.status_code)
            if response.is_success:
                return response
            raise map_response(response, scope=scope, what=what)

    @staticmethod
    def _rows(response: httpx.Response, *, what: str) -> list[Any]:
        """Bambuddy answers these routes with a bare JSON array."""
        body = response.json()
        if not isinstance(body, list):
            raise ApiError(
                status.HTTP_502_BAD_GATEWAY,
                f"Bambuddy answered {type(body).__name__}, not a list, when asked to {what}",
            )
        return body

    # --- read ----------------------------------------------------------------

    async def printers(self) -> list[Printer]:
        what = "list the printers"
        # The trailing slash is required: /api/v1/printers is a 404 on 1.2.5.5.
        response = await self._send(
            "GET", "/printers/", scope=Scope.READ_STATUS, operation="printers.list", what=what
        )
        return [Printer.model_validate(row) for row in self._rows(response, what=what)]

    async def printer(self, printer_id: int) -> Printer:
        """``GET /api/v1/printers/{id}`` — the same shape the list route returns."""
        response = await self._send(
            "GET",
            f"/printers/{printer_id}",
            scope=Scope.READ_STATUS,
            operation="printers.read",
            what=f"read printer {printer_id}",
        )
        return Printer.model_validate(response.json())

    async def printer_status(self, printer_id: int) -> PrinterStatus:
        """Live MQTT state: nozzles, AMS trays and the inlet each AMS is switched to."""
        response = await self._send(
            "GET",
            f"/printers/{printer_id}/status",
            scope=Scope.READ_STATUS,
            operation="printers.status",
            what=f"read the status of printer {printer_id}",
        )
        return PrinterStatus.model_validate(response.json())

    async def camera_snapshot(self, printer_id: int) -> bytes:
        """One JPEG frame from the printer's camera (#796).

        The snapshot route takes no API key: when Bambuddy's auth is on it wants a
        camera stream token as ``?token=`` (``camera.py`` at v1.2.5.6), minted under
        ``camera:view``, which an API key holds with ``can_read_status``. A capture can
        take Bambuddy up to 15 s; a camera that gives no frame is its 503.
        """
        what = f"capture the camera of printer {printer_id}"
        minted = await self._send(
            "POST",
            "/printers/camera/stream-token",
            scope=Scope.READ_STATUS,
            operation="printers.camera.token",
            what=what,
        )
        response = await self._send(
            "GET",
            f"/printers/{printer_id}/camera/snapshot",
            scope=Scope.READ_STATUS,
            operation="printers.camera.snapshot",
            what=what,
            params={"token": minted.json()["token"]},
        )
        return response.content

    async def available_filaments(
        self, model: str, *, location: str | None = None
    ) -> list[AvailableFilament]:
        """Filaments loaded across every active printer of ``model``, deduplicated.

        ``model`` is required by Bambuddy — omitting it is a 422, not "all printers".
        """
        what = f"list the filaments available on {model} printers"
        params: dict[str, Any] = {"model": model}
        if location is not None:
            params["location"] = location
        response = await self._send(
            "GET",
            "/printers/available-filaments",
            scope=Scope.READ_STATUS,
            operation="printers.available_filaments",
            what=what,
            params=params,
        )
        return [AvailableFilament.model_validate(row) for row in self._rows(response, what=what)]

    async def folders(self) -> list[Folder]:
        what = "list the library folders"
        response = await self._send(
            "GET",
            "/library/folders",
            scope=Scope.MANAGE_LIBRARY,
            operation="library.folders.list",
            what=what,
        )
        return [Folder.model_validate(row) for row in self._rows(response, what=what)]

    async def presets(self) -> PresetCatalogue:
        response = await self._send(
            "GET",
            "/slicer/presets",
            scope=Scope.MANAGE_LIBRARY,
            operation="slicer.presets",
            what="list the slicer presets",
        )
        return PresetCatalogue.model_validate(response.json())

    async def local_presets(self) -> LocalPresetCatalogue:
        """``GET /api/v1/local-presets/`` — OrcaSlicer profiles imported into Bambuddy.

        These do not appear in :meth:`presets`' ``local`` tier unless Bambuddy has
        classified them, so the two calls are not interchangeable.
        """
        response = await self._send(
            "GET",
            "/local-presets/",
            scope=Scope.MANAGE_LIBRARY,
            operation="slicer.local_presets",
            what="list the local presets",
        )
        return LocalPresetCatalogue.model_validate(response.json())

    # --- inventory -----------------------------------------------------------

    async def spools(self, *, include_archived: bool = False) -> list[Spool]:
        """``GET /api/v1/inventory/spools`` — every spool, loaded or on the shelf.

        Archived spools are excluded by default because the picker offers what can be
        printed with today; ``include_archived`` is Bambuddy's own query parameter.
        """
        what = "list the filament spools"
        response = await self._send(
            "GET",
            "/inventory/spools",
            scope=Scope.READ_STATUS,
            operation="spools.list",
            what=what,
            params={"include_archived": include_archived},
        )
        return [Spool.model_validate(row) for row in self._rows(response, what=what)]

    async def spool_filament_presets(self, spool_id: int) -> list[SpoolFilamentPreset]:
        """``GET /api/v1/inventory/spools/{id}/filament-presets`` — per-nozzle presets.

        The spool row's ``slicer_filament`` is one preset; this is every printer model
        and nozzle size the spool has its own preset for (#161).
        """
        what = "list a spool's per-nozzle filament presets"
        response = await self._send(
            "GET",
            f"/inventory/spools/{spool_id}/filament-presets",
            scope=Scope.READ_STATUS,
            operation="spools.filament_presets",
            what=what,
        )
        return [SpoolFilamentPreset.model_validate(row) for row in self._rows(response, what=what)]

    async def spool_assignments(self, *, printer_id: int | None = None) -> list[SpoolAssignment]:
        """``GET /api/v1/inventory/assignments`` — spool to printer/AMS/tray.

        Unfiltered it covers every printer, which is what the picker wants: a spool
        loaded in *another* printer is still offered, marked with where it is.
        """
        what = "list the spool assignments"
        response = await self._send(
            "GET",
            "/inventory/assignments",
            scope=Scope.READ_STATUS,
            operation="spools.assignments",
            what=what,
            params={"printer_id": printer_id} if printer_id is not None else None,
        )
        return [SpoolAssignment.model_validate(row) for row in self._rows(response, what=what)]

    async def inventory_remain(self, printer_id: int) -> InventoryRemain:
        """Per-loaded-slot remaining grams, flat tray id and feeding extruder."""
        response = await self._send(
            "GET",
            f"/printers/{printer_id}/inventory-remain",
            scope=Scope.READ_STATUS,
            operation="printers.inventory_remain",
            what=f"read the loaded filament of printer {printer_id}",
        )
        return InventoryRemain.model_validate(response.json())

    async def filament_requirements(
        self, file_id: int, *, plate_id: int | None = None
    ) -> FilamentRequirements:
        """What each plate slot of a library file needs.

        ``used_grams`` comes back ``0`` for a 3MF that carries no slice info — which is
        every 3MF ScadBuddy uploads before it has been sliced. That is *unknown*, and
        callers must not read it as "this print needs no filament".
        """
        response = await self._send(
            "GET",
            f"/library/files/{file_id}/filament-requirements",
            scope=Scope.MANAGE_LIBRARY,
            operation="library.filament_requirements",
            what=f"read the filament requirements of library file {file_id}",
            params={"plate_id": plate_id} if plate_id is not None else None,
        )
        return FilamentRequirements.model_validate(response.json())

    async def archives(
        self,
        *,
        printer_id: int | None = None,
        limit: int = 20,
        offset: int = 0,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> list[Archive]:
        """Archives, of one printer or all, optionally within a date window (on
        ``created_at``, which is when the print was dispatched). Bambuddy's order is
        not documented, so callers sort; ``limit`` keeps the read small. There is no
        filter by hash: a caller matching ``content_hash`` scans a window."""
        what = "list the archives"
        params: dict[str, Any] = {"limit": limit}
        if printer_id is not None:
            params["printer_id"] = printer_id
        if offset:
            params["offset"] = offset
        if date_from is not None:
            params["date_from"] = date_from.isoformat()
        if date_to is not None:
            params["date_to"] = date_to.isoformat()
        response = await self._send(
            "GET",
            "/archives/",
            scope=Scope.READ_STATUS,
            operation="archives.list",
            what=what,
            params=params,
        )
        return [Archive.model_validate(row) for row in self._rows(response, what=what)]

    async def archive(self, archive_id: int) -> ArchiveDetail:
        response = await self._send(
            "GET",
            f"/archives/{archive_id}",
            scope=Scope.READ_STATUS,
            operation="archives.read",
            what=f"read archive {archive_id}",
        )
        return ArchiveDetail.model_validate(response.json())

    async def archive_runs(self, archive_id: int) -> ArchiveRunList:
        """Every run of the archive; a reprint inside Bambuddy is a run, not an archive."""
        response = await self._send(
            "GET",
            f"/archives/{archive_id}/runs",
            scope=Scope.READ_STATUS,
            operation="archives.runs",
            what=f"list the runs of archive {archive_id}",
        )
        return ArchiveRunList.model_validate(response.json())

    async def timelapse_info(self, archive_id: int) -> TimelapseInfo:
        response = await self._send(
            "GET",
            f"/archives/{archive_id}/timelapse/info",
            scope=Scope.READ_STATUS,
            operation="archives.timelapse.info",
            what=f"read the timelapse of archive {archive_id}",
        )
        return TimelapseInfo.model_validate(response.json())

    async def timelapse_thumbnails(self, archive_id: int) -> TimelapseThumbnails:
        response = await self._send(
            "GET",
            f"/archives/{archive_id}/timelapse/thumbnails",
            scope=Scope.READ_STATUS,
            operation="archives.timelapse.thumbnails",
            what=f"read the timelapse frames of archive {archive_id}",
        )
        return TimelapseThumbnails.model_validate(response.json())

    async def printer_media(self, archive_id: int) -> PrinterMedia:
        """Timelapses and camera recordings for this print, including any still on the
        printer. Listing the printer needs ``can_control_printer`` as well; without it
        Bambuddy answers with the local timelapse only and a warning."""
        response = await self._send(
            "GET",
            f"/archives/{archive_id}/printer-media",
            scope=Scope.READ_STATUS,
            operation="archives.printer_media",
            what=f"list the printer media of archive {archive_id}",
        )
        return PrinterMedia.model_validate(response.json())

    async def select_timelapse(self, archive_id: int, filename: str) -> None:
        """Pull ``filename`` off the printer and attach it to the archive. Only on an
        explicit request: it downloads over FTP, as Bambuddy's own UI does."""
        await self._send(
            "POST",
            f"/archives/{archive_id}/timelapse/select",
            scope=Scope.MANAGE_ARCHIVES,
            operation="archives.timelapse.select",
            what=f"attach a timelapse to archive {archive_id}",
            params={"filename": filename},
            timeout=self.config.upload_timeout,
        )

    async def upload_archive_photo(
        self, archive_id: int, filename: str, content: bytes
    ) -> ArchivePhotoUpload:
        """Bambuddy takes only ``.jpg``, ``.jpeg``, ``.png`` and ``.webp`` here, and
        renames the file; the answer's ``filename`` is the name to keep."""
        response = await self._send(
            "POST",
            f"/archives/{archive_id}/photos",
            scope=Scope.MANAGE_ARCHIVES,
            operation="archives.photo.upload",
            what=f"add a photo to archive {archive_id}",
            files={"file": (filename, content)},
            timeout=self.config.upload_timeout,
        )
        return ArchivePhotoUpload.model_validate(response.json())

    async def delete_archive_photo(self, archive_id: int, filename: str) -> None:
        await self._send(
            "DELETE",
            f"/archives/{archive_id}/photos/{filename}",
            scope=Scope.MANAGE_ARCHIVES,
            operation="archives.photo.delete",
            what=f"delete a photo of archive {archive_id}",
        )

    @asynccontextmanager
    async def stream(
        self,
        path: str,
        *,
        operation: Operation,
        what: str,
        range_header: str | None = None,
        if_range: str | None = None,
    ) -> AsyncIterator[httpx.Response]:
        """``GET path`` without reading the body, for the media proxy (#307).

        ``Range`` and ``If-Range`` pass through, so Bambuddy's ``FileResponse`` answers
        a seek with a ``206`` and only those bytes. The response is open for the
        duration of the ``async with``; read it with ``aiter_raw``. A failure is mapped
        as every other call's is, once its (small) body has been read, except a ``416``:
        that is the answer to a seek past the end, and its ``Content-Range`` carries the
        length the media element needs, so it is passed through.
        """
        headers = dict(self._headers)
        if range_header is not None:
            headers["Range"] = range_header
        if if_range is not None:
            headers["If-Range"] = if_range
        request = self._http.build_request("GET", self.config.url(path), headers=headers)
        # The span fails only on what Bambuddy did: an error the consumer raises inside
        # the ``async with`` (a browser that went away) leaves it, and is raised after.
        # A transport error there is the body's read failing, which is Bambuddy's.
        consumer_error: Exception | None = None
        with _call_span(operation, "GET", Scope.READ_STATUS, detached=True) as current:
            try:
                response = await self._http.send(request, stream=True)
            except httpx.HTTPError as error:
                logger.warning("bambuddy request failed", extra={"method": "GET", "path": path})
                raise map_transport(error, what=what) from error
            current.set_attribute("http.response.status_code", response.status_code)
            try:
                if not response.is_success and response.status_code != 416:
                    await response.aread()
                    raise map_response(response, scope=Scope.READ_STATUS, what=what)
                try:
                    yield response
                except httpx.HTTPError as error:
                    logger.warning("bambuddy request failed", extra={"method": "GET", "path": path})
                    raise map_transport(error, what=what) from error
                except Exception as error:
                    consumer_error = error
            finally:
                await response.aclose()
        if consumer_error is not None:
            raise consumer_error

    # --- library -------------------------------------------------------------

    async def folders_by_project(self, project_id: int) -> list[Folder]:
        what = f"list the library folders of project {project_id}"
        response = await self._send(
            "GET",
            f"/library/folders/by-project/{project_id}",
            scope=Scope.MANAGE_LIBRARY,
            operation="library.folders.by_project",
            what=what,
        )
        return [Folder.model_validate(row) for row in self._rows(response, what=what)]

    async def create_folder(self, folder: FolderCreate) -> Folder:
        """``POST /api/v1/library/folders/`` — the trailing slash is the write route."""
        response = await self._send(
            "POST",
            "/library/folders/",
            scope=Scope.MANAGE_LIBRARY,
            operation="library.folders.create",
            what=f"create the {folder.name!r} library folder",
            json=folder.model_dump(mode="json", exclude_none=True),
        )
        return Folder.model_validate(response.json())

    async def upload_library_file(
        self,
        filename: str,
        content: bytes,
        *,
        folder_id: int | None = None,
        media_type: str = THREE_MF_MEDIA_TYPE,
    ) -> LibraryFile:
        response = await self._send(
            "POST",
            "/library/files",
            scope=Scope.MANAGE_LIBRARY,
            operation="library.upload",
            what=f"upload {filename}",
            params={"folder_id": folder_id} if folder_id is not None else None,
            files={"file": (filename, content, media_type)},
            timeout=self.config.upload_timeout,
        )
        return LibraryFile.model_validate(response.json())

    async def library_files(self, folder_id: int) -> list[LibraryFile]:
        """``GET /library/files?folder_id=`` — the files directly in one folder (#317)."""
        what = f"list the files in library folder {folder_id}"
        response = await self._send(
            "GET",
            "/library/files",
            scope=Scope.MANAGE_LIBRARY,
            operation="library.files.list",
            what=what,
            params={"folder_id": folder_id},
        )
        return [LibraryFile.model_validate(row) for row in self._rows(response, what=what)]

    async def library_file(self, file_id: int) -> LibraryFile:
        """``GET /library/files/{id}`` (``openapi/routes.txt``) — one file, notes and all."""
        response = await self._send(
            "GET",
            f"/library/files/{file_id}",
            scope=Scope.MANAGE_LIBRARY,
            operation="library.files.read",
            what=f"read library file {file_id}",
        )
        return LibraryFile.model_validate(response.json())

    async def library_listing(self, *, folder_id: int | None) -> list[LibraryListRow]:
        """``GET /library/files/`` — one folder's files, or the root's without one
        (``include_root`` defaults to true). One read, however many files: Bambuddy
        does not paginate it."""
        what = (
            "list the library files"
            if folder_id is None
            else f"list the files of library folder {folder_id}"
        )
        response = await self._send(
            "GET",
            "/library/files/",
            scope=Scope.MANAGE_LIBRARY,
            operation="library.listing",
            what=what,
            params={"folder_id": folder_id} if folder_id is not None else None,
        )
        return [LibraryListRow.model_validate(row) for row in self._rows(response, what=what)]

    async def library_plates(self, file_id: int) -> LibraryPlates:
        """``GET /library/files/{id}/plates`` — the plates Bambuddy reads out of the
        file, with whether each has a cover image."""
        response = await self._send(
            "GET",
            f"/library/files/{file_id}/plates",
            scope=Scope.MANAGE_LIBRARY,
            operation="library.plates",
            what=f"read the plates of library file {file_id}",
        )
        return LibraryPlates.model_validate(response.json())

    async def download_library_file(self, file_id: int) -> AsyncGenerator[bytes]:
        """``GET /library/files/{id}/download`` (``openapi/routes.txt``): by id, so the
        blob store never scans a folder to find a file (spec 2026-09-27 §6.3). The
        stream stays open until the generator ends: a caller that may stop early wraps
        it in ``contextlib.aclosing``."""
        what = f"download library file {file_id}"
        path = f"/library/files/{file_id}/download"
        with _call_span("library.download", "GET", Scope.MANAGE_LIBRARY, detached=True) as current:
            try:
                async with self._http.stream(
                    "GET",
                    self.config.url(path),
                    headers=self._headers,
                    timeout=self.config.upload_timeout,
                ) as response:
                    current.set_attribute("http.response.status_code", response.status_code)
                    if not response.is_success:
                        await response.aread()
                        raise map_response(response, scope=Scope.MANAGE_LIBRARY, what=what)
                    async for chunk in response.aiter_bytes():
                        yield chunk
            except httpx.HTTPError as error:
                logger.warning("bambuddy request failed", extra={"method": "GET", "path": path})
                raise map_transport(error, what=what) from error

    async def annotate_library_file(self, file_id: int, notes: str) -> LibraryFile:
        """``PUT /library/files/{id}`` — ``notes`` is the only free-text field a
        library file has; there is no ``url`` on one (``external_url`` lives on an
        archive, which a send never produces).

        **Sending ``notes`` alone is a partial update, not a full replace**, so it
        cannot clear the file's folder or project. Read off Bambuddy's own source
        rather than assumed — `update_file` guards every assignment with
        ``if data.<field> is not None``, and `FileUpdate` defaults each field to
        ``None``:
        https://github.com/maziggy/bambuddy/blob/9e9c08ba2cc08bf1e746ed98bef2b46b7bedea02/backend/app/api/routes/library.py#L5103-L5136

        Two details from the same lines, for whoever writes the next field: the
        sentinel that *clears* ``folder_id``/``project_id`` is ``0``, not ``null``,
        and an empty ``notes`` string is stored as ``NULL``.
        """
        response = await self._send(
            "PUT",
            f"/library/files/{file_id}",
            scope=Scope.MANAGE_LIBRARY,
            operation="library.annotate",
            what=f"annotate library file {file_id}",
            json={"notes": notes},
        )
        return LibraryFile.model_validate(response.json())

    async def delete_library_file(self, file_id: int) -> None:
        await self._send(
            "DELETE",
            f"/library/files/{file_id}",
            scope=Scope.MANAGE_LIBRARY,
            operation="library.delete",
            what=f"delete library file {file_id}",
        )

    # --- slice ---------------------------------------------------------------

    async def slice(self, file_id: int, request: SliceRequest) -> SliceJobAccepted:
        response = await self._send(
            "POST",
            f"/library/files/{file_id}/slice",
            scope=Scope.MANAGE_LIBRARY,
            operation="slice.start",
            what=f"slice library file {file_id}",
            json=request.model_dump(mode="json", exclude_none=True),
        )
        return SliceJobAccepted.model_validate(response.json())

    async def slice_job(self, job_id: int) -> SliceJob:
        response = await self._send(
            "GET",
            f"/slice-jobs/{job_id}",
            scope=Scope.MANAGE_LIBRARY,
            operation="slice.job.status",
            what=f"read slice job {job_id}",
        )
        return SliceJob.model_validate(response.json())

    async def await_slice(self, job_id: int) -> SliceJob:
        """Poll ``slice_job`` until it finishes, the deadline passes, or it fails."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.config.slice_timeout
        while True:
            job = await self.slice_job(job_id)
            if job.finished:
                return job
            if loop.time() >= deadline:
                raise ApiError(
                    status.HTTP_504_GATEWAY_TIMEOUT,
                    f"Bambuddy slice job {job_id} was still {job.status} after "
                    f"{self.config.slice_timeout:.0f}s",
                    slice_job_id=job_id,
                )
            await asyncio.sleep(self.config.slice_poll_interval)

    # --- queue ---------------------------------------------------------------

    async def queue_item(self, item_id: int) -> QueueItem:
        response = await self._send(
            "GET",
            f"/queue/{item_id}",
            scope=Scope.MANAGE_QUEUE,
            operation="queue.read",
            what=f"read queue item {item_id}",
        )
        return QueueItem.model_validate(response.json())

    async def enqueue(self, item: QueueItemCreate) -> QueueItem:
        """``POST /api/v1/queue/`` with the whole ``PrintQueueItemCreate``.

        ``exclude_none`` keeps an unset optional out of the body rather than sending
        an explicit ``null``; every remaining default is Bambuddy's own.
        """
        response = await self._send(
            "POST",
            "/queue/",
            scope=Scope.MANAGE_QUEUE,
            operation="queue.add",
            what=(
                f"queue archive {item.archive_id}"
                if item.library_file_id is None
                else f"queue library file {item.library_file_id}"
            ),
            json=item.model_dump(mode="json", exclude_none=True),
        )
        return QueueItem.model_validate(response.json())

    # --- the connection test (#322) -------------------------------------------

    async def version(self) -> str:
        """``GET /api/v1/updates/version``, which Bambuddy serves without a key."""
        response = await self._send(
            "GET",
            "/updates/version",
            scope=Scope.READ_STATUS,
            operation="version",
            what="read Bambuddy's version",
        )
        return str(response.json().get("version") or "unknown")

    async def capture_finish_photo(self) -> bool:
        """Bambuddy's ``capture_finish_photo`` setting (``GET /api/v1/settings/``, which
        needs ``SETTINGS_READ``: Read Status for a key)."""
        what = "read Bambuddy's settings"
        response = await self._send(
            "GET", "/settings/", scope=Scope.READ_STATUS, operation="settings.read", what=what
        )
        value = response.json().get("capture_finish_photo")
        if not isinstance(value, bool):
            raise ApiError(
                status.HTTP_502_BAD_GATEWAY,
                "Bambuddy's settings carry no capture_finish_photo, so this Bambuddy may be"
                " older than the setting",
            )
        return value

    # --- projects ------------------------------------------------------------

    async def projects(self, *, status_filter: str | None = None) -> list[Project]:
        what = "list the projects"
        response = await self._send(
            "GET",
            "/projects/",
            scope=Scope.MANAGE_PROJECTS,
            operation="projects.list",
            what=what,
            params={"status": status_filter} if status_filter is not None else None,
        )
        return [Project.model_validate(row) for row in self._rows(response, what=what)]

    async def project(self, project_id: int) -> Project:
        """``GET /api/v1/projects/{id}`` — the same model the list route returns, minus
        the roll-up counters, which is why they default rather than being required."""
        response = await self._send(
            "GET",
            f"/projects/{project_id}",
            scope=Scope.MANAGE_PROJECTS,
            operation="projects.read",
            what=f"read project {project_id}",
        )
        return Project.model_validate(response.json())

    async def create_project(self, project: ProjectCreate) -> Project:
        response = await self._send(
            "POST",
            "/projects/",
            scope=Scope.MANAGE_PROJECTS,
            operation="projects.create",
            what=f"create the {project.name!r} project",
            json=project.model_dump(mode="json", exclude_none=True),
        )
        return Project.model_validate(response.json())

    async def add_archives_to_project(self, project_id: int, archive_ids: list[int]) -> None:
        """Bambuddy documents no response body for this route, so none is parsed."""
        await self._send(
            "POST",
            f"/projects/{project_id}/add-archives",
            scope=Scope.MANAGE_PROJECTS,
            operation="projects.add_archives",
            what=f"add archives to project {project_id}",
            json={"archive_ids": archive_ids},
        )

    async def add_queue_items_to_project(self, project_id: int, queue_item_ids: list[int]) -> None:
        await self._send(
            "POST",
            f"/projects/{project_id}/add-queue",
            scope=Scope.MANAGE_PROJECTS,
            operation="projects.add_queue",
            what=f"add queue items to project {project_id}",
            json={"queue_item_ids": queue_item_ids},
        )

    # --- external links ------------------------------------------------------

    async def external_links(self) -> list[ExternalLink]:
        what = "list the external links"
        response = await self._send(
            "GET",
            "/external-links/",
            scope=Scope.MANAGE_LIBRARY,
            operation="external_links.list",
            what=what,
        )
        return [ExternalLink.model_validate(row) for row in self._rows(response, what=what)]

    async def create_external_link(
        self, *, name: str, url: str, icon: str = "link", open_in_new_tab: bool = False
    ) -> ExternalLink:
        response = await self._send(
            "POST",
            "/external-links/",
            scope=Scope.MANAGE_LIBRARY,
            operation="external_links.create",
            what=f"create the {name!r} external link",
            json={"name": name, "url": url, "icon": icon, "open_in_new_tab": open_in_new_tab},
        )
        return ExternalLink.model_validate(response.json())

    async def update_external_link(
        self,
        link_id: int,
        *,
        name: str | None = None,
        url: str | None = None,
        icon: str | None = None,
        open_in_new_tab: bool | None = None,
    ) -> ExternalLink:
        patch = {
            "name": name,
            "url": url,
            "icon": icon,
            "open_in_new_tab": open_in_new_tab,
        }
        response = await self._send(
            "PATCH",
            f"/external-links/{link_id}",
            scope=Scope.MANAGE_LIBRARY,
            operation="external_links.update",
            what=f"update external link {link_id}",
            json={key: value for key, value in patch.items() if value is not None},
        )
        return ExternalLink.model_validate(response.json())


@asynccontextmanager
async def client_for(settings: StoredSettings) -> AsyncIterator[BambuddyClient]:
    """A client built from the stored settings, or a 409 saying what is missing."""
    async with BambuddyClient(BambuddyConfig.from_settings(settings)) as client:
        yield client
