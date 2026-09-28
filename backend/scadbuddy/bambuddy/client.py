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
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date
from types import TracebackType
from typing import Any, Self

import httpx
from fastapi import status

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
from scadbuddy.library.settings_store import StoredSettings

logger = logging.getLogger(__name__)

API_PREFIX = "/api/v1"
THREE_MF_MEDIA_TYPE = "model/3mf"

DEFAULT_TIMEOUT = 30.0
DEFAULT_UPLOAD_TIMEOUT = 180.0
DEFAULT_SLICE_TIMEOUT = 600.0
DEFAULT_SLICE_POLL = 2.0


@dataclass(frozen=True)
class BambuddyConfig:
    base_url: str
    api_key: str | None = None
    timeout: float = DEFAULT_TIMEOUT
    upload_timeout: float = DEFAULT_UPLOAD_TIMEOUT
    slice_timeout: float = DEFAULT_SLICE_TIMEOUT
    slice_poll_interval: float = DEFAULT_SLICE_POLL

    @classmethod
    def from_settings(cls, settings: StoredSettings) -> BambuddyConfig:
        if not settings.bambuddy_url:
            raise not_configured("no Bambuddy URL is configured; set one in Settings")
        return cls(base_url=settings.bambuddy_url.rstrip("/"), api_key=settings.bambuddy_api_key)

    def url(self, path: str) -> str:
        return f"{self.base_url}{API_PREFIX}{path}"

    def web_url(self, path: str) -> str:
        return f"{self.base_url}{path}"


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
        what: str,
        params: Mapping[str, Any] | None = None,
        json: Any | None = None,
        files: Any | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
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
        response = await self._send("GET", "/printers/", scope=Scope.READ_STATUS, what=what)
        return [Printer.model_validate(row) for row in self._rows(response, what=what)]

    async def printer(self, printer_id: int) -> Printer:
        """``GET /api/v1/printers/{id}`` — the same shape the list route returns."""
        response = await self._send(
            "GET",
            f"/printers/{printer_id}",
            scope=Scope.READ_STATUS,
            what=f"read printer {printer_id}",
        )
        return Printer.model_validate(response.json())

    async def printer_status(self, printer_id: int) -> PrinterStatus:
        """Live MQTT state: nozzles, AMS trays and the inlet each AMS is switched to."""
        response = await self._send(
            "GET",
            f"/printers/{printer_id}/status",
            scope=Scope.READ_STATUS,
            what=f"read the status of printer {printer_id}",
        )
        return PrinterStatus.model_validate(response.json())

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
            what=what,
            params=params,
        )
        return [AvailableFilament.model_validate(row) for row in self._rows(response, what=what)]

    async def folders(self) -> list[Folder]:
        what = "list the library folders"
        response = await self._send(
            "GET", "/library/folders", scope=Scope.MANAGE_LIBRARY, what=what
        )
        return [Folder.model_validate(row) for row in self._rows(response, what=what)]

    async def presets(self) -> PresetCatalogue:
        response = await self._send(
            "GET", "/slicer/presets", scope=Scope.MANAGE_LIBRARY, what="list the slicer presets"
        )
        return PresetCatalogue.model_validate(response.json())

    async def local_presets(self) -> LocalPresetCatalogue:
        """``GET /api/v1/local-presets/`` — OrcaSlicer profiles imported into Bambuddy.

        These do not appear in :meth:`presets`' ``local`` tier unless Bambuddy has
        classified them, so the two calls are not interchangeable.
        """
        response = await self._send(
            "GET", "/local-presets/", scope=Scope.MANAGE_LIBRARY, what="list the local presets"
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
            "GET", "/archives/", scope=Scope.READ_STATUS, what=what, params=params
        )
        return [Archive.model_validate(row) for row in self._rows(response, what=what)]

    async def archive(self, archive_id: int) -> ArchiveDetail:
        response = await self._send(
            "GET",
            f"/archives/{archive_id}",
            scope=Scope.READ_STATUS,
            what=f"read archive {archive_id}",
        )
        return ArchiveDetail.model_validate(response.json())

    async def archive_runs(self, archive_id: int) -> ArchiveRunList:
        """Every run of the archive; a reprint inside Bambuddy is a run, not an archive."""
        response = await self._send(
            "GET",
            f"/archives/{archive_id}/runs",
            scope=Scope.READ_STATUS,
            what=f"list the runs of archive {archive_id}",
        )
        return ArchiveRunList.model_validate(response.json())

    async def timelapse_info(self, archive_id: int) -> TimelapseInfo:
        response = await self._send(
            "GET",
            f"/archives/{archive_id}/timelapse/info",
            scope=Scope.READ_STATUS,
            what=f"read the timelapse of archive {archive_id}",
        )
        return TimelapseInfo.model_validate(response.json())

    async def timelapse_thumbnails(self, archive_id: int) -> TimelapseThumbnails:
        response = await self._send(
            "GET",
            f"/archives/{archive_id}/timelapse/thumbnails",
            scope=Scope.READ_STATUS,
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
            what=f"delete a photo of archive {archive_id}",
        )

    @asynccontextmanager
    async def stream(
        self,
        path: str,
        *,
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
        try:
            response = await self._http.send(request, stream=True)
        except httpx.HTTPError as error:
            logger.warning("bambuddy request failed", extra={"method": "GET", "path": path})
            raise map_transport(error, what=what) from error
        try:
            if not response.is_success and response.status_code != 416:
                await response.aread()
                raise map_response(response, scope=Scope.READ_STATUS, what=what)
            yield response
        finally:
            await response.aclose()

    # --- library -------------------------------------------------------------

    async def folders_by_project(self, project_id: int) -> list[Folder]:
        what = f"list the library folders of project {project_id}"
        response = await self._send(
            "GET",
            f"/library/folders/by-project/{project_id}",
            scope=Scope.MANAGE_LIBRARY,
            what=what,
        )
        return [Folder.model_validate(row) for row in self._rows(response, what=what)]

    async def create_folder(self, folder: FolderCreate) -> Folder:
        """``POST /api/v1/library/folders/`` — the trailing slash is the write route."""
        response = await self._send(
            "POST",
            "/library/folders/",
            scope=Scope.MANAGE_LIBRARY,
            what=f"create the {folder.name!r} library folder",
            json=folder.model_dump(mode="json", exclude_none=True),
        )
        return Folder.model_validate(response.json())

    async def upload_library_file(
        self, filename: str, content: bytes, *, folder_id: int | None = None
    ) -> LibraryFile:
        response = await self._send(
            "POST",
            "/library/files",
            scope=Scope.MANAGE_LIBRARY,
            what=f"upload {filename}",
            params={"folder_id": folder_id} if folder_id is not None else None,
            files={"file": (filename, content, THREE_MF_MEDIA_TYPE)},
            timeout=self.config.upload_timeout,
        )
        return LibraryFile.model_validate(response.json())

    async def library_file(self, file_id: int) -> LibraryFile:
        """``GET /library/files/{id}`` (``openapi/routes.txt``) — one file, notes and all."""
        response = await self._send(
            "GET",
            f"/library/files/{file_id}",
            scope=Scope.MANAGE_LIBRARY,
            what=f"read library file {file_id}",
        )
        return LibraryFile.model_validate(response.json())

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
            what=f"annotate library file {file_id}",
            json={"notes": notes},
        )
        return LibraryFile.model_validate(response.json())

    async def delete_library_file(self, file_id: int) -> None:
        await self._send(
            "DELETE",
            f"/library/files/{file_id}",
            scope=Scope.MANAGE_LIBRARY,
            what=f"delete library file {file_id}",
        )

    # --- slice ---------------------------------------------------------------

    async def slice(self, file_id: int, request: SliceRequest) -> SliceJobAccepted:
        response = await self._send(
            "POST",
            f"/library/files/{file_id}/slice",
            scope=Scope.MANAGE_LIBRARY,
            what=f"slice library file {file_id}",
            json=request.model_dump(mode="json", exclude_none=True),
        )
        return SliceJobAccepted.model_validate(response.json())

    async def slice_job(self, job_id: int) -> SliceJob:
        response = await self._send(
            "GET",
            f"/slice-jobs/{job_id}",
            scope=Scope.MANAGE_LIBRARY,
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
            what=f"queue library file {item.library_file_id}",
            json=item.model_dump(mode="json", exclude_none=True),
        )
        return QueueItem.model_validate(response.json())

    # --- projects ------------------------------------------------------------

    async def projects(self, *, status_filter: str | None = None) -> list[Project]:
        what = "list the projects"
        response = await self._send(
            "GET",
            "/projects/",
            scope=Scope.MANAGE_PROJECTS,
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
            what=f"read project {project_id}",
        )
        return Project.model_validate(response.json())

    async def create_project(self, project: ProjectCreate) -> Project:
        response = await self._send(
            "POST",
            "/projects/",
            scope=Scope.MANAGE_PROJECTS,
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
            what=f"add archives to project {project_id}",
            json={"archive_ids": archive_ids},
        )

    async def add_queue_items_to_project(self, project_id: int, queue_item_ids: list[int]) -> None:
        await self._send(
            "POST",
            f"/projects/{project_id}/add-queue",
            scope=Scope.MANAGE_PROJECTS,
            what=f"add queue items to project {project_id}",
            json={"queue_item_ids": queue_item_ids},
        )

    # --- external links ------------------------------------------------------

    async def external_links(self) -> list[ExternalLink]:
        what = "list the external links"
        response = await self._send(
            "GET", "/external-links/", scope=Scope.MANAGE_LIBRARY, what=what
        )
        return [ExternalLink.model_validate(row) for row in self._rows(response, what=what)]

    async def create_external_link(
        self, *, name: str, url: str, icon: str = "link", open_in_new_tab: bool = False
    ) -> ExternalLink:
        response = await self._send(
            "POST",
            "/external-links/",
            scope=Scope.MANAGE_LIBRARY,
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
            what=f"update external link {link_id}",
            json={key: value for key, value in patch.items() if value is not None},
        )
        return ExternalLink.model_validate(response.json())


@asynccontextmanager
async def client_for(settings: StoredSettings) -> AsyncIterator[BambuddyClient]:
    """A client built from the stored settings, or a 409 saying what is missing."""
    async with BambuddyClient(BambuddyConfig.from_settings(settings)) as client:
        yield client
