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
import json
import logging
import re
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AbstractContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import date
from types import TracebackType
from typing import IO, Any, Literal, Self
from urllib.parse import quote

import httpx
from fastapi import status
from opentelemetry.trace import Span, SpanKind
from pydantic import BaseModel, ValidationError
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException

from scadbuddy.bambuddy.errors import (
    BAD_BODY_PROBLEM,
    REJECTED_PROBLEM,
    Scope,
    TagAlreadyLinkedError,
    UpstreamAnswer,
    map_response,
    map_transport,
    not_configured,
    scrubbed_detail,
    upstream_detail,
)
from scadbuddy.bambuddy.models import (
    Archive,
    ArchiveDetail,
    ArchivePhotoUpload,
    ArchiveRunList,
    ArchiveStats,
    AvailableFilament,
    BbLocation,
    BbSupplier,
    CatalogFilament,
    ExternalLink,
    FilamentRequirements,
    FileUpdate,
    Folder,
    FolderCreate,
    FolderUpdate,
    HaEntity,
    InventoryRemain,
    KProfile,
    KProfileTable,
    LibraryFile,
    LibraryListRow,
    LibraryPlates,
    LocalPreset,
    LocalPresetCatalogue,
    LocalPresetDetail,
    MaintenanceHistoryEntry,
    MoveResult,
    PresetCatalogue,
    PresetImport,
    PresetRef,
    PresetValues,
    Printer,
    PrinterFiles,
    PrinterMaintenanceOverview,
    PrinterMedia,
    PrinterStatus,
    Project,
    ProjectCreate,
    QueueItem,
    QueueItemCreate,
    SliceJob,
    SliceJobAccepted,
    SliceRequest,
    SlotConfigure,
    Spool,
    SpoolAssignment,
    SpoolBuddyDevice,
    SpoolCatalogEntry,
    SpoolFilamentPreset,
    SpoolUsage,
    TagScan,
    TimelapseInfo,
    TimelapseThumbnails,
    TrashedLibraryFile,
    VirtualPrinter,
    VirtualPrinterDiagnostic,
    VirtualPrinterList,
)
from scadbuddy.core.problems import ApiError
from scadbuddy.core.settings import split_urls
from scadbuddy.core.tracing import detached_span, span
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.makerworld.models import (
    BambuddyMakerWorldImported,
    BambuddyMakerWorldResolved,
    BambuddyMakerWorldStatus,
)

logger = logging.getLogger(__name__)

API_PREFIX = "/api/v1"


THREE_MF_MEDIA_TYPE = "model/3mf"

DEFAULT_TIMEOUT = 30.0
DEFAULT_UPLOAD_TIMEOUT = 180.0
#: The largest camera frame read into memory: a JPEG of a printer's camera is well
#: under a megabyte, so past this Bambuddy is answering something else (#1006).
MAX_CAMERA_FRAME_BYTES = 16 * 1024 * 1024
DEFAULT_SLICE_TIMEOUT = 600.0
#: A MakerWorld import (#6119): Bambuddy downloads the 3MF from MakerWorld inside the one
#: request, which ran past 120 s on prod on 2026-11-13 (the session #6119 cites).
MAKERWORLD_IMPORT_TIMEOUT = 600.0
#: Read by `BambuddyClient._parsed`, not mapped by `_send`: a link or id Bambuddy refuses.
_REFUSED_LINK = frozenset({400})
DEFAULT_SLICE_POLL = 2.0
#: The largest page ``GET /library/trash`` serves (``limit`` is ``le=500``).
TRASH_PAGE = 500
#: The Bambuddy permission that shows a caller every user's trash (``Permission``,
#: ``backend/app/core/permissions.py`` line 62 at v1.2.5.5).
LIBRARY_DELETE_ALL = "library:delete_all"
#: A live camera stream never ends, so it has no total timeout; a camera that sends no
#: frame for ``read`` seconds has stalled, and the stream ends.
CAMERA_STREAM_TIMEOUT = httpx.Timeout(10.0, read=30.0)
#: What Bambuddy's ``/camera/stream`` answers with.
MJPEG_TYPE = "multipart/x-mixed-replace; boundary=frame"
#: A multipart MJPEG type with a plain boundary token: the only type a stream is
#: served as, since ScadBuddy's origin would otherwise serve whatever Bambuddy (or an
#: external camera behind it) labels the bytes, ``text/html`` included.
_MJPEG = re.compile(
    r"multipart/x-mixed-replace;\s*boundary=\"?([A-Za-z0-9'()+_,.\-]{1,70})\"?", re.I
)


def mjpeg_type(upstream: str | None) -> str:
    """The content type to serve a camera stream as: ``multipart/x-mixed-replace`` with
    the upstream's boundary, since the parts are split on it; :data:`MJPEG_TYPE` when the
    upstream names no usable boundary. Never the upstream's type itself."""
    match = _MJPEG.fullmatch((upstream or "").strip())
    return f"multipart/x-mixed-replace; boundary={match.group(1)}" if match else MJPEG_TYPE


#: What a Bambuddy call does, which names its span (``bambuddy.<operation>``, spec
#: 2026-10-01 §4). A closed set, so a span name never carries an id or free text.
Operation = Literal[
    "media.attachment",
    "archives.list",
    "archives.photo.delete",
    "archives.photo.upload",
    "archives.printer_media",
    "archives.read",
    "archives.runs",
    "archives.stats",
    "archives.timelapse.info",
    "archives.timelapse.select",
    "archives.timelapse.thumbnails",
    "auth.me",
    "auth.status",
    "auth.ws_token",
    "external_links.create",
    "external_links.list",
    "external_links.update",
    "catalog.create",
    "catalog.update",
    "filament_catalog.create",
    "filament_catalog.update",
    "filaments.catalog",
    "library.annotate",
    "library.bulk_delete",
    "library.delete",
    "library.download",
    "library.filament_requirements",
    "library.files.list",
    "library.files.read",
    "library.files.update",
    "library.folders.by_project",
    "library.folders.create",
    "library.folders.delete",
    "library.folders.list",
    "library.folders.read",
    "library.folders.update",
    "library.listing",
    "library.move",
    "library.plate_thumbnail",
    "library.plates",
    "library.rename",
    "library.thumbnail",
    "library.trash.list",
    "library.trash.restore",
    "library.upload",
    "locations.create",
    "locations.list",
    "locations.update",
    "media.download",
    "media.photo",
    "media.plate_thumbnail",
    "media.source",
    "media.thumbnail",
    "media.timelapse",
    "maintenance.history",
    "maintenance.printer",
    "makerworld.import",
    "makerworld.resolve",
    "makerworld.status",
    "printers.available_filaments",
    "printers.camera.snapshot",
    "printers.camera.stream",
    "printers.camera.token",
    "printers.cover",
    "printers.files",
    "printers.files.download",
    "printers.inventory_remain",
    "printers.kprofiles",
    "printers.list",
    "printers.read",
    "printers.slot_configure",
    "printers.status",
    "projects.add_archives",
    "projects.add_queue",
    "projects.create",
    "projects.list",
    "projects.read",
    "queue.add",
    "queue.list",
    "queue.read",
    "settings.read",
    "settings.update",
    "slice.job.status",
    "slice.start",
    "slicer.local_preset",
    "slicer.local_preset.create",
    "slicer.local_presets",
    "slicer.local_presets.import",
    "slicer.preset_values",
    "slicer.presets",
    "spools.assign",
    "spools.assignments",
    "spools.by_tag",
    "spools.clear_tag",
    "spools.link_tag",
    "spools.read",
    "spools.catalog",
    "spools.filament_presets",
    "spools.list",
    "spools.usage",
    "spools.create",
    "spools.update",
    "spools.archive",
    "spools.restore",
    "spools.k_profiles",
    "spools.presets_put",
    "spools.suppliers_put",
    "suppliers.create",
    "suppliers.list",
    "suppliers.update",
    "ha.entities",
    "spoolbuddy.cancel_write",
    "spoolbuddy.devices",
    "spoolbuddy.scan",
    "spoolbuddy.write_tag",
    "version",
    "virtual_printers.create",
    "virtual_printers.delete",
    "virtual_printers.diagnostic",
    "virtual_printers.list",
    "virtual_printers.read",
    "virtual_printers.update",
]


#: The close code Bambuddy's WebSocket gives a join with no valid token
#: (``_WS_CLOSE_UNAUTHORIZED`` in ``routes/websocket.py``, v1.3.1).
_WS_UNAUTHORISED = 4401
#: Seconds websockets waits for the opening handshake.
WS_OPEN_TIMEOUT = 10.0
#: websockets logs its request line, ``?token=`` included, at DEBUG; this logger drops it.
_WS_LOGGER = logging.getLogger("scadbuddy.bambuddy.ws")
_WS_LOGGER.setLevel(logging.WARNING)
_SCAN_FRAMES = frozenset({"spoolbuddy_tag_matched", "spoolbuddy_unknown_tag"})


def _tag_scan(raw: str | bytes, device_id: str | None) -> TagScan | None:
    """The scan a WebSocket frame carries, or ``None`` for any other frame, one of
    another device or one that is not JSON."""
    try:
        frame = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(frame, dict) or frame.get("type") not in _SCAN_FRAMES:
        return None
    if device_id is not None and frame.get("device_id") != device_id:
        return None
    spool = frame.get("spool")
    matched = frame["type"] == "spoolbuddy_tag_matched" and isinstance(spool, dict)
    bb_spool_id = spool.get("id") if isinstance(spool, dict) and matched else None
    try:
        return TagScan.model_validate({**frame, "bb_spool_id": bb_spool_id})
    except ValidationError:
        return None


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


class BambuddyAnswerError(ApiError):
    """What Bambuddy's answer did wrong, found while reading it inside
    :meth:`BambuddyClient.stream`'s ``async with`` (a frame over its cap): it fails the
    call's span, as a refusal does. Any other error the reader raises leaves it alone."""


#: Bambuddy's ``detail`` for an archive it does not have (``routes/archives.py``).
ARCHIVE_NOT_FOUND = "Archive not found"


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
        passthrough: frozenset[int] = frozenset(),
    ) -> httpx.Response:
        """One call to Bambuddy; a status in ``passthrough`` is returned to the caller
        to read, not mapped to an error."""
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
            if response.is_success or response.status_code in passthrough:
                return response
            raise map_response(response, scope=scope, what=what)

    @staticmethod
    def _rows(response: httpx.Response, *, what: str) -> list[Any]:
        """Bambuddy answers these routes with a bare JSON array. A body that is not JSON,
        or not an array, is a 502 `BAD_BODY_PROBLEM` (R-T7-8, R-T7-10). As for the other
        answers of the wrong shape, no ``bambuddy_status`` is set: Bambuddy's status was a
        success."""
        try:
            body = response.json()
        except ValueError:
            raise ApiError(
                status.HTTP_502_BAD_GATEWAY,
                f"Bambuddy answered something that is not JSON when asked to {what}",
                type_=BAD_BODY_PROBLEM,
            ) from None
        if not isinstance(body, list):
            raise ApiError(
                status.HTTP_502_BAD_GATEWAY,
                f"Bambuddy answered {type(body).__name__}, not a list, when asked to {what}",
                type_=BAD_BODY_PROBLEM,
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

    async def printer_maintenance(self, printer_id: int) -> PrinterMaintenanceOverview:
        """``GET /api/v1/maintenance/printers/{id}``: the printer's maintenance tasks and
        their intervals (#4625). Bambuddy gates it on ``MAINTENANCE_READ``, which an API
        key holds with ``can_read_status`` (``backend/app/core/auth.py``
        ``_APIKEY_SCOPE_BY_PERMISSION`` at maziggy/bambuddy@2f871923)."""
        response = await self._send(
            "GET",
            f"/maintenance/printers/{printer_id}",
            scope=Scope.READ_STATUS,
            operation="maintenance.printer",
            what=f"read printer {printer_id}'s maintenance tasks",
        )
        return PrinterMaintenanceOverview.model_validate(response.json())

    async def maintenance_history(self, item_id: int) -> list[MaintenanceHistoryEntry]:
        """``GET /api/v1/maintenance/items/{item_id}/history``: every time one task of a
        printer was marked done, newest first, with the print-hour counter then (#5774,
        spec 2026-11-17-machine-rate-actuals §2.1, §3.7). Gated on ``MAINTENANCE_READ``,
        as :meth:`printer_maintenance` is. An item Bambuddy does not have answers an empty
        list, not a 404 (measured on prod's Bambuddy 1.3.1, 2026-11-18)."""
        what = f"read maintenance item {item_id}'s log"
        response = await self._send(
            "GET",
            f"/maintenance/items/{item_id}/history",
            scope=Scope.READ_STATUS,
            operation="maintenance.history",
            what=what,
        )
        return [
            MaintenanceHistoryEntry.model_validate(row) for row in self._rows(response, what=what)
        ]

    async def printer_access_code(self, printer_id: int) -> str | None:
        """The printer's LAN access code as Bambuddy holds it, or ``None`` when it is
        withheld: ``GET /api/v1/printers/{id}`` carries ``access_code`` only for an
        Admin or Operator session, or when Bambuddy's auth is off; "Viewers and API keys
        never receive it" (``backend/app/api/routes/printers.py``
        ``_caller_can_view_printer_secrets`` and ``get_printer``, lines 126-150 and
        393-410, maziggy/bambuddy v1.3.0b1-daily.20261025). Read only by the Bambuddy
        sync (`printers/bambuddy_sync.py`), which hands it straight to the LAN probe:
        never logged, never in a span or a model (`Printer` excludes it)."""
        response = await self._send(
            "GET",
            f"/printers/{printer_id}",
            scope=Scope.READ_STATUS,
            operation="printers.read",
            what=f"read printer {printer_id}",
        )
        body = response.json()
        code = body.get("access_code") if isinstance(body, dict) else None
        if not isinstance(code, str) or not code.strip():
            return None
        return code.strip()

    async def virtual_printers(self) -> VirtualPrinterList:
        """``GET /api/v1/virtual-printers`` (#4747): needs ``settings:read``, which an
        API key holds through Read Status (``backend/app/core/auth.py``
        ``_APIKEY_SCOPE_BY_PERMISSION``, line 130, v1.3.0b1-daily.20261025)."""
        response = await self._send(
            "GET",
            "/virtual-printers",
            scope=Scope.READ_STATUS,
            operation="virtual_printers.list",
            what="list the virtual printers",
        )
        return VirtualPrinterList.model_validate(response.json())

    async def _vp_call(
        self,
        method: str,
        path: str,
        *,
        operation: Operation,
        what: str,
        json: Any | None = None,
    ) -> httpx.Response:
        """A virtual-printer call (#4747): a read under Read Status, a write under
        `Scope.ADMIN_SETTINGS`. Bambuddy's 400 (an invalid mode, a bind IP in use, no
        target in proxy mode; ``routes/virtual_printers.py``) is a 422 with its reason, and
        so is its own 422: both with `scrubbed_detail`, since a validation error echoes the
        request's values and a write may carry an access code (never its body)."""
        write = method != "GET"
        response = await self._send(
            method,
            path,
            scope=Scope.ADMIN_SETTINGS if write else Scope.READ_STATUS,
            operation=operation,
            what=what,
            json=json,
            passthrough=frozenset(
                {status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_CONTENT}
            ),
        )
        if response.status_code in (
            status.HTTP_400_BAD_REQUEST,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
        ):
            detail = scrubbed_detail(response)
            error = ApiError(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"Bambuddy refused to {what}" + (f": {detail}" if detail else ""),
                type_=REJECTED_PROBLEM,
                bambuddy_status=response.status_code,
            )
            error.upstream = UpstreamAnswer(status=response.status_code, detail=detail)
            raise error
        return response

    async def virtual_printer(self, vp_id: int) -> VirtualPrinter:
        response = await self._vp_call(
            "GET",
            f"/virtual-printers/{vp_id}",
            operation="virtual_printers.read",
            what=f"read virtual printer {vp_id}",
        )
        return VirtualPrinter.model_validate(response.json())

    async def virtual_printer_diagnostic(self, vp_id: int) -> VirtualPrinterDiagnostic:
        response = await self._vp_call(
            "GET",
            f"/virtual-printers/{vp_id}/diagnostic",
            operation="virtual_printers.diagnostic",
            what=f"diagnose virtual printer {vp_id}",
        )
        return VirtualPrinterDiagnostic.model_validate(response.json())

    async def create_virtual_printer(self, body: dict[str, Any]) -> VirtualPrinter:
        """``body`` may carry an ``access_code``: never logged, never in a span."""
        response = await self._vp_call(
            "POST",
            "/virtual-printers",
            operation="virtual_printers.create",
            what="create the virtual printer",
            json=body,
        )
        return VirtualPrinter.model_validate(response.json())

    async def update_virtual_printer(self, vp_id: int, body: dict[str, Any]) -> VirtualPrinter:
        response = await self._vp_call(
            "PUT",
            f"/virtual-printers/{vp_id}",
            operation="virtual_printers.update",
            what=f"change virtual printer {vp_id}",
            json=body,
        )
        return VirtualPrinter.model_validate(response.json())

    async def delete_virtual_printer(self, vp_id: int) -> None:
        await self._vp_call(
            "DELETE",
            f"/virtual-printers/{vp_id}",
            operation="virtual_printers.delete",
            what=f"delete virtual printer {vp_id}",
        )

    async def auth_enabled(self) -> bool | None:
        """``GET /api/v1/auth/status`` (public, ``routes/auth.py`` line 395): whether
        Bambuddy's auth is on; ``None`` when it does not say."""
        try:
            response = await self._send(
                "GET",
                "/auth/status",
                scope=Scope.READ_STATUS,
                operation="auth.status",
                what="read whether Bambuddy's auth is on",
            )
        except ApiError as error:
            logger.debug("could not read Bambuddy's auth status", extra={"type": error.type})
            return None
        try:
            body = response.json()
        except ValueError:
            logger.debug("Bambuddy's auth status is not JSON")
            return None
        enabled = body.get("auth_enabled") if isinstance(body, dict) else None
        return enabled if isinstance(enabled, bool) else None

    async def ws_token(self) -> str | None:
        """A token for Bambuddy's WebSocket, or ``None`` when its auth is off (the socket
        then takes no token: ``routes/websocket.py`` lines 101-135, v1.3.1).
        ``POST /api/v1/auth/ws-token`` (``WEBSOCKET_CONNECT``, which ``can_read_status``
        covers: Read Status; ``routes/auth.py`` line 670, v1.3.1) answers ``{"token": ...}``,
        valid for 60 minutes. An unreadable auth status counts as on, and a refusal then
        says so."""
        enabled = await self.auth_enabled()
        if enabled is False:
            return None
        what = "mint a WebSocket token"
        try:
            response = await self._send(
                "POST",
                "/auth/ws-token",
                scope=Scope.READ_STATUS,
                operation="auth.ws_token",
                what=what,
            )
        except ApiError as error:
            if enabled is not None:
                raise
            unread = ApiError(
                error.status,
                f"{error.detail}. Bambuddy's auth status could not be read, so a token was"
                " tried in case its auth is on",
                title=error.title,
                type_=error.type,
                headers=error.headers,
                **error.extensions,
            )
            unread.upstream = error.upstream
            raise unread from error
        body = response.json()
        token = body.get("token") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token:
            raise ApiError(
                status.HTTP_502_BAD_GATEWAY,
                f"Bambuddy answered no token when asked to {what}",
            )
        return token

    def _ws_url(self, token: str | None) -> str:
        scheme, _, rest = self.config.base_url.partition("://")
        url = f"{'wss' if scheme == 'https' else 'ws'}://{rest}{API_PREFIX}/ws"
        return url if token is None else f"{url}?token={quote(token, safe='')}"

    async def next_tag_scan(
        self, *, timeout: float, device_id: str | None = None
    ) -> TagScan | None:
        """Wait up to ``timeout`` seconds, token mint and connect included, for a SpoolBuddy
        reader to read a tag, on ``WS /api/v1/ws`` (``?token=`` from :meth:`ws_token` with
        Bambuddy's auth on). The socket is sent nothing; every frame is read and all but
        ``spoolbuddy_tag_matched`` and ``spoolbuddy_unknown_tag`` (of ``device_id``, when
        given) are ignored. ``None`` only when that deadline passes. A refused join is the
        scope 409 of ``map_response`` (Read Status); a connect that times out, or a close
        before any tag, is the 502 or 504 of ``map_transport``."""
        what = "listen for a SpoolBuddy tag scan"
        deadline = asyncio.timeout(timeout)
        with _call_span("spoolbuddy.scan", "GET", Scope.READ_STATUS):
            try:
                async with deadline:
                    token = await self.ws_token()
                    async with ws_connect(
                        self._ws_url(token),
                        logger=_WS_LOGGER,
                        open_timeout=WS_OPEN_TIMEOUT,
                        # Like httpx, the socket follows the proxy environment
                        # (HTTP_PROXY, NO_PROXY); websockets 15+ takes it from here.
                        proxy=True,
                    ) as socket:
                        async for raw in socket:
                            scan = _tag_scan(raw, device_id)
                            if scan is not None:
                                return scan
                    raise map_transport(httpx.ReadError("the socket closed"), what=what)
            except TimeoutError as error:
                if deadline.expired():
                    return None
                raise map_transport(httpx.ConnectTimeout("connect timed out"), what=what) from error
            except InvalidStatus as error:
                refused = httpx.Response(error.response.status_code)
                raise map_response(refused, scope=Scope.READ_STATUS, what=what) from error
            except ConnectionClosed as error:
                if error.rcvd is not None and error.rcvd.code == _WS_UNAUTHORISED:
                    unauthorised = httpx.Response(status.HTTP_401_UNAUTHORIZED)
                    raise map_response(unauthorised, scope=Scope.READ_STATUS, what=what) from error
                raise map_transport(httpx.ReadError(type(error).__name__), what=what) from error
            except (OSError, WebSocketException) as error:
                raise map_transport(httpx.ConnectError(type(error).__name__), what=what) from error

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

    async def _camera_token(self, *, what: str) -> str:
        """A camera stream token, minted under ``camera:view``. An answer without one is
        a 502, as any unexpected body is (#2752 review)."""
        minted = await self._send(
            "POST",
            "/printers/camera/stream-token",
            scope=Scope.READ_STATUS,
            operation="printers.camera.token",
            what=what,
        )
        try:
            body = minted.json()
        except ValueError:
            body = None
        token = body.get("token") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token:
            raise ApiError(
                status.HTTP_502_BAD_GATEWAY,
                f"Bambuddy answered no camera token when asked to {what}",
            )
        return token

    async def camera_snapshot(self, printer_id: int) -> bytes:
        """One JPEG frame from the printer's camera (#796).

        The snapshot route takes no API key: when Bambuddy's auth is on it wants a
        camera stream token as ``?token=`` (``camera.py`` at v1.2.5.6), minted under
        ``camera:view``, which an API key holds with ``can_read_status``. A capture can
        take Bambuddy up to 15 s; a camera that gives no frame is its 503.
        """
        what = f"capture the camera of printer {printer_id}"
        token = await self._camera_token(what=what)
        frame = bytearray()
        async with self.stream(
            f"/printers/{printer_id}/camera/snapshot",
            operation="printers.camera.snapshot",
            what=what,
            params={"token": token},
            # Nothing sends a Range here, so a 416 is no seek: map it, never a "frame".
            pass_416=False,
        ) as response:
            async for chunk in response.aiter_bytes():
                frame += chunk
                if len(frame) > MAX_CAMERA_FRAME_BYTES:
                    raise BambuddyAnswerError(
                        status.HTTP_502_BAD_GATEWAY,
                        f"Bambuddy's frame was over {MAX_CAMERA_FRAME_BYTES} bytes when asked"
                        f" to {what}",
                    )
        return bytes(frame)

    @asynccontextmanager
    async def camera_stream(
        self, printer_id: int, *, fps: int
    ) -> AsyncIterator[tuple[str, AsyncIterator[bytes]]]:
        """The printer's live camera as Bambuddy's MJPEG stream (#981): its content type
        (``multipart/x-mixed-replace; boundary=frame``) and its bytes, open until the
        context exits. A refusal (no printer, no camera) raises before anything is
        yielded, so a caller can still answer it as an error.

        Leaving the context closes the connection, and that is the whole release:
        Bambuddy fans one camera out to every viewer and stops it 5 s after the last
        one disconnects (``camera.py``'s broadcaster). ``/camera/stop`` is never
        called: once no subscriber is counted it force-stops every stream of the
        printer, an external camera's other viewers included. The token is minted
        here, as the snapshot's is, and never leaves the server."""
        what = f"stream the camera of printer {printer_id}"
        token = await self._camera_token(what=what)
        path = f"/printers/{printer_id}/camera/stream"
        with _call_span(
            "printers.camera.stream", "GET", Scope.READ_STATUS, detached=True
        ) as current:
            try:
                async with self._http.stream(
                    "GET",
                    self.config.url(path),
                    headers=self._headers,
                    params={"token": token, "fps": fps},
                    timeout=CAMERA_STREAM_TIMEOUT,
                ) as response:
                    current.set_attribute("http.response.status_code", response.status_code)
                    if not response.is_success:
                        await response.aread()
                        raise map_response(response, scope=Scope.READ_STATUS, what=what)
                    yield mjpeg_type(response.headers.get("content-type")), response.aiter_bytes()
            except httpx.HTTPError as error:
                logger.warning("bambuddy request failed", extra={"method": "GET", "path": path})
                raise map_transport(error, what=what) from error

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
        # 1 MB of JSON: parsed off the event loop, which every other request shares (#2978).
        return await asyncio.to_thread(PresetCatalogue.model_validate_json, response.content)

    async def preset_values(self, ref: PresetRef) -> PresetValues:
        """``GET /slicer/preset-values`` — a process preset's effective values, flattened
        by Bambuddy's slicer sidecar, so a change can be shown against the value it
        replaces and written in the shape the preset stores it (#717)."""
        response = await self._send(
            "GET",
            "/slicer/preset-values",
            scope=Scope.MANAGE_LIBRARY,
            operation="slicer.preset_values",
            what=f"read the values of process preset {ref.id}",
            params={"source": ref.source, "id": ref.id, "slot": "process"},
        )
        return PresetValues.model_validate(response.json())

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

    async def local_preset(self, preset_id: int) -> LocalPresetDetail:
        """``GET /api/v1/local-presets/{id}`` — one local preset with its ``setting``, the
        document Bambuddy's slicer slices with: what the native slicer builds a ``local``
        filament or printer preset's file from (``slicing/presets.py``, #4233). Bambuddy
        gates it as it gates the list (``Permission.SETTINGS_READ`` on both, read in its
        source on 2026-10-22), so the same scope is named here. A preset deleted since the
        plan named it is a 404.
        """
        response = await self._send(
            "GET",
            f"/local-presets/{preset_id}",
            scope=Scope.MANAGE_LIBRARY,
            operation="slicer.local_preset",
            what=f"read local preset {preset_id}",
        )
        return LocalPresetDetail.model_validate(response.json())

    async def create_local_preset(
        self, name: str, preset_type: str, setting: Mapping[str, Any]
    ) -> LocalPreset:
        """``POST /api/v1/local-presets/``: a preset made from ``setting``, stored as it
        is sent (``routes/local_presets.py`` ``create_local_preset``: no ``inherits``
        resolution, unlike ``PUT``, which merges over OrcaSlicer's base), and sliced with
        verbatim (:class:`LocalPresetDetail`). Bambuddy gates it on
        ``Permission.SETTINGS_UPDATE``, which it never grants an API key while its auth
        is on (``core/auth.py`` ``_APIKEY_DENIED_PERMISSIONS``, read on 2026-10-24): then
        this is a 403, and the per-slot presets that call it go without (#3962)."""
        response = await self._send(
            "POST",
            "/local-presets/",
            scope=Scope.MANAGE_LIBRARY,
            operation="slicer.local_preset.create",
            what=f"create local preset {name}",
            json={"name": name, "preset_type": preset_type, "setting": dict(setting)},
        )
        return LocalPreset.model_validate(response.json())

    async def import_local_presets(self, zip_bytes: bytes) -> PresetImport:
        """``POST /api/v1/local-presets/import``: one zip of presets, sent as the multipart
        part ``file`` named ``profiles.zip``. Bambuddy gates it like the create call
        (:meth:`create_local_preset`), so a refusal names the same scope (#6625)."""
        response = await self._send(
            "POST",
            "/local-presets/import",
            scope=Scope.MANAGE_LIBRARY,
            operation="slicer.local_presets.import",
            what="import local presets",
            files={"file": ("profiles.zip", zip_bytes, "application/zip")},
        )
        return PresetImport.model_validate(response.json())

    async def k_profiles(self, printer_id: int, nozzle_diameter: str) -> list[KProfile]:
        """``GET /api/v1/printers/{id}/kprofiles/?nozzle_diameter=``: the K table the
        printer holds for one nozzle. The trailing slash is Bambuddy's route (#6625). A
        body that is not JSON is a 502 `BAD_BODY_PROBLEM`, as `_rows` answers (#6856)."""
        what = f"read printer {printer_id}'s K profiles"
        response = await self._send(
            "GET",
            f"/printers/{printer_id}/kprofiles/",
            scope=Scope.READ_STATUS,
            operation="printers.kprofiles",
            what=what,
            params={"nozzle_diameter": nozzle_diameter},
        )
        try:
            body = response.json()
        except ValueError:
            raise ApiError(
                status.HTTP_502_BAD_GATEWAY,
                f"Bambuddy answered something that is not JSON when asked to {what}",
                type_=BAD_BODY_PROBLEM,
            ) from None
        return KProfileTable.model_validate(body).profiles

    # --- inventory -----------------------------------------------------------

    async def spools(self, *, include_archived: bool = False) -> list[Spool]:
        """``GET /api/v1/inventory/spools`` — every spool, loaded or on the shelf.

        Archived spools are excluded by default because the picker offers what can be
        printed with today; ``include_archived`` is Bambuddy's own query parameter.
        """
        rows = await self.spool_rows(include_archived=include_archived)
        return [Spool.model_validate(row) for row in rows]

    async def spool_rows(self, *, include_archived: bool = False) -> list[Any]:
        """`spools`' rows as Bambuddy sent them, not yet validated: a caller that takes
        each row on its own (`filament/bambuddy_pull.py`) skips a malformed one rather than
        losing them all."""
        what = "list the filament spools"
        response = await self._send(
            "GET",
            "/inventory/spools",
            scope=Scope.READ_STATUS,
            operation="spools.list",
            what=what,
            params={"include_archived": include_archived},
        )
        return self._rows(response, what=what)

    async def ha_entities(self, search: str | None) -> list[HaEntity]:
        """``GET /api/v1/location-ha-sensors/entities`` (#6268): Home Assistant's entities
        and their states as Bambuddy reads them, matched by ``search`` (none: all). Each
        row is validated on its own: one that is not an entity is logged and left out.
        Scope: Bambuddy has no key scope for storage locations or their sensors, and no
        read-only inventory scope; every other read of its inventory here uses
        ``READ_STATUS``, the one read scope, so this does too (not measured live)."""
        what = "list the Home Assistant entities"
        response = await self._send(
            "GET",
            "/location-ha-sensors/entities",
            scope=Scope.READ_STATUS,
            operation="ha.entities",
            what=what,
            params={"search": search} if search else None,
        )
        entities: list[HaEntity] = []
        for row in self._rows(response, what=what):
            try:
                entities.append(HaEntity.model_validate(row))
            except ValidationError:
                logger.warning("a Bambuddy Home Assistant entity is not an entity; skipped")
        return entities

    async def spool_usage(self, *, limit: int = 200) -> list[SpoolUsage]:
        """``GET /api/v1/inventory/usage`` (#6263): BB's usage rows, one per spool per
        print. The prod key's scopes answered it with 200 on 2026-11-15 (the probe in
        ``docs/superpowers/plans/2026-11-15-filament-capture.md``). Each row is validated
        on its own: one that is not a usage row is logged and left out."""
        what = "list the spool usage history"
        response = await self._send(
            "GET",
            "/inventory/usage",
            scope=Scope.READ_STATUS,
            operation="spools.usage",
            what=what,
            params={"limit": limit},
        )
        rows: list[SpoolUsage] = []
        for row in self._rows(response, what=what):
            try:
                rows.append(SpoolUsage.model_validate(row))
            except ValidationError:
                row_id = row.get("id") if isinstance(row, dict) else None
                logger.warning(
                    "a Bambuddy usage row is not a usage row; skipped",
                    extra={"bb_usage_id": row_id if isinstance(row_id, int) else None},
                    exc_info=True,
                )
        return rows

    async def filament_catalog(self) -> list[CatalogFilament]:
        """``GET /api/v1/filament-catalog/`` — Bambuddy's filament catalogue, each entry
        with its ``density`` (#6136). Bambuddy gates it with ``FILAMENTS_READ``
        (``backend/app/api/routes/filaments.py``), which an API key holds through
        ``can_read_status`` (``_APIKEY_SCOPE_BY_PERMISSION``,
        ``backend/app/core/auth.py``), both at maziggy/bambuddy v1.3.2b1-daily.20261113.
        """
        what = "list the filament catalogue"
        response = await self._send(
            "GET",
            "/filament-catalog/",
            scope=Scope.READ_STATUS,
            operation="filaments.catalog",
            what=what,
        )
        return [CatalogFilament.model_validate(row) for row in self._rows(response, what=what)]

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

    async def spool_catalog(self) -> list[SpoolCatalogEntry]:
        """``GET /api/v1/inventory/catalog`` — Bambuddy's empty-spool catalog, which a
        spool's ``core_weight_catalog_id`` names (``CatalogEntry`` in the 1.2.6b1 OpenAPI,
        spec 2026-11-14 §3). The pull maps each entry to an insert type."""
        what = "list the spool catalog"
        response = await self._send(
            "GET",
            "/inventory/catalog",
            scope=Scope.READ_STATUS,
            operation="spools.catalog",
            what=what,
        )
        return [SpoolCatalogEntry.model_validate(row) for row in self._rows(response, what=what)]

    async def assign_spool(
        self, *, spool_id: int, printer_id: int, ams_id: int, tray_id: int
    ) -> SpoolAssignment:
        """``POST /api/v1/inventory/assignments``: record that ``spool_id`` is in the
        tray (#2164). Only on a person's confirmation in the print dialog. Bambuddy also
        configures the tray over MQTT from the spool, as its own Assign does."""
        what = f"assign spool {spool_id} to printer {printer_id}'s tray"
        response = await self._send(
            "POST",
            "/inventory/assignments",
            scope=Scope.MANAGE_INVENTORY,
            operation="spools.assign",
            what=what,
            json={
                "spool_id": spool_id,
                "printer_id": printer_id,
                "ams_id": ams_id,
                "tray_id": tray_id,
            },
        )
        return SpoolAssignment.model_validate(response.json())

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
        response = await self._list_archives(
            what,
            printer_id=printer_id,
            project_id=None,
            limit=limit,
            offset=offset,
            date_from=date_from,
            date_to=date_to,
        )
        return [Archive.model_validate(row) for row in self._rows(response, what=what)]

    async def archive_outcomes(
        self,
        *,
        printer_id: int | None = None,
        project_id: int | None = None,
        limit: int = 20,
        offset: int = 0,
        date_from: date | None = None,
        date_to: date | None = None,
        skip_invalid: bool = False,
    ) -> list[ArchiveDetail]:
        """:meth:`archives` with each row's outcome (#1912): the list route answers
        the whole ``ArchiveResponse``, the same schema as :meth:`archive`.

        With ``skip_invalid``, a row that does not parse is logged and left out rather
        than failing the read, for a caller that reads the rest singly anyway."""
        what = "list the archives"
        response = await self._list_archives(
            what,
            printer_id=printer_id,
            project_id=project_id,
            limit=limit,
            offset=offset,
            date_from=date_from,
            date_to=date_to,
        )
        rows = self._rows(response, what=what)
        if not skip_invalid:
            return [ArchiveDetail.model_validate(row) for row in rows]
        parsed: list[ArchiveDetail] = []
        unparsed = 0
        for row in rows:
            try:
                parsed.append(ArchiveDetail.model_validate(row))
            except ValidationError:
                unparsed += 1
        if unparsed:
            # One line per read, not per row (#3477 review): each such archive falls
            # back to its own single read.
            logger.info(
                "archive rows did not parse; left out of the list read",
                extra={"rows": unparsed},
            )
        return parsed

    async def _list_archives(
        self,
        what: str,
        *,
        printer_id: int | None,
        project_id: int | None,
        limit: int,
        offset: int,
        date_from: date | None,
        date_to: date | None,
    ) -> httpx.Response:
        params: dict[str, Any] = {"limit": limit}
        if printer_id is not None:
            params["printer_id"] = printer_id
        if project_id is not None:
            params["project_id"] = project_id
        if offset:
            params["offset"] = offset
        if date_from is not None:
            params["date_from"] = date_from.isoformat()
        if date_to is not None:
            params["date_to"] = date_to.isoformat()
        return await self._send(
            "GET",
            "/archives/",
            scope=Scope.READ_STATUS,
            operation="archives.list",
            what=what,
            params=params,
        )

    async def archive_stats(
        self, *, date_from: date | None = None, date_to: date | None = None
    ) -> ArchiveStats:
        """``GET /api/v1/archives/stats``: totals over every archive, or over those
        created within the window (both ends inclusive)."""
        params: dict[str, Any] = {}
        if date_from is not None:
            params["date_from"] = date_from.isoformat()
        if date_to is not None:
            params["date_to"] = date_to.isoformat()
        response = await self._send(
            "GET",
            "/archives/stats",
            scope=Scope.READ_STATUS,
            operation="archives.stats",
            what="read the print statistics",
            params=params,
        )
        return ArchiveStats.model_validate(response.json())

    async def archive(self, archive_id: int) -> ArchiveDetail:
        response = await self._send(
            "GET",
            f"/archives/{archive_id}",
            scope=Scope.READ_STATUS,
            operation="archives.read",
            what=f"read archive {archive_id}",
        )
        return ArchiveDetail.model_validate(response.json())

    async def archive_deleted(self, archive_id: int) -> bool:
        """Whether Bambuddy says the archive is gone: its own 404,
        ``{"detail": "Archive not found"}`` (``routes/archives.py``). A 404 with any
        other body (a proxy's, a route that moved) raises as any refusal does, so a
        caller that deletes on the answer never deletes on a guess (#3047)."""
        what = f"read archive {archive_id}"
        response = await self._send(
            "GET",
            f"/archives/{archive_id}",
            scope=Scope.READ_STATUS,
            operation="archives.read",
            what=what,
            passthrough=frozenset({status.HTTP_404_NOT_FOUND}),
        )
        if response.is_success:
            return False
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict) and body.get("detail") == ARCHIVE_NOT_FOUND:
            return True
        raise map_response(response, scope=Scope.READ_STATUS, what=what)

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

    async def printer_files(self, printer_id: int, path: str) -> PrinterFiles:
        """``GET /printers/{id}/files?path=`` (#4698): one directory of the printer's
        storage, read over FTP. Only on an explicit request: it reaches the printer. The key
        needs Control Printer (``PRINTERS_FILES``)."""
        response = await self._send(
            "GET",
            f"/printers/{printer_id}/files",
            scope=Scope.CONTROL_PRINTER,
            operation="printers.files",
            what=f"list {path} on printer {printer_id}",
            params={"path": path},
        )
        return PrinterFiles.model_validate(response.json())

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
        self, archive_id: int, filename: str, content: bytes | IO[bytes]
    ) -> ArchivePhotoUpload:
        """Bambuddy takes only ``.jpg``, ``.jpeg``, ``.png`` and ``.webp`` here, and
        renames the file; the answer's ``filename`` is the name to keep. A file object
        is streamed from where it is, never read whole (#309)."""
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
        params: Mapping[str, str] | None = None,
        pass_416: bool = True,
        scope: Scope = Scope.READ_STATUS,
    ) -> AsyncIterator[httpx.Response]:
        """``GET path`` without reading the body, for the media proxy (#307).

        ``Range`` and ``If-Range`` pass through, so Bambuddy's ``FileResponse`` answers
        a seek with a ``206`` and only those bytes. The response is open for the
        duration of the ``async with``. Read it with ``aiter_raw`` to pass the bytes on
        as Bambuddy sent them (the media proxy keeps its ``Content-Encoding``), or with
        ``aiter_bytes`` where the caller counts or uses the decoded bytes, as the camera
        frame's cap does. A failure is mapped
        as every other call's is, once its (small) body has been read, except a ``416``:
        that is the answer to a seek past the end, and its ``Content-Range`` carries the
        length the media element needs, so it is passed through. ``pass_416=False`` maps
        it as any refusal, for a caller that sends no ``Range`` (#3397).
        """
        headers = dict(self._headers)
        if range_header is not None:
            headers["Range"] = range_header
        if if_range is not None:
            headers["If-Range"] = if_range
        request = self._http.build_request(
            "GET", self.config.url(path), headers=headers, params=params
        )
        # The span fails only on what Bambuddy did: an error the consumer raises inside
        # the ``async with`` (a browser that went away) leaves it, and is raised after.
        # A transport error there is the body's read failing, which is Bambuddy's.
        consumer_error: Exception | None = None
        with _call_span(operation, "GET", scope, detached=True) as current:
            try:
                response = await self._http.send(request, stream=True)
            except httpx.HTTPError as error:
                logger.warning("bambuddy request failed", extra={"method": "GET", "path": path})
                raise map_transport(error, what=what) from error
            current.set_attribute("http.response.status_code", response.status_code)
            try:
                if not response.is_success and (response.status_code != 416 or not pass_416):
                    await response.aread()
                    raise map_response(response, scope=scope, what=what)
                try:
                    yield response
                except httpx.HTTPError as error:
                    logger.warning("bambuddy request failed", extra={"method": "GET", "path": path})
                    raise map_transport(error, what=what) from error
                except BambuddyAnswerError:
                    raise
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

    async def folder(self, folder_id: int) -> Folder:
        """``GET /api/v1/library/folders/{id}`` — one folder, 404 "Folder not found" when
        Bambuddy cannot see it (#2263)."""
        response = await self._send(
            "GET",
            f"/library/folders/{folder_id}",
            scope=Scope.MANAGE_LIBRARY,
            operation="library.folders.read",
            what=f"read library folder {folder_id}",
        )
        return Folder.model_validate(response.json())

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
        content: bytes | IO[bytes],
        *,
        folder_id: int | None = None,
        media_type: str = THREE_MF_MEDIA_TYPE,
    ) -> LibraryFile:
        """``content`` as bytes, or a seekable file, which is sent from its start a chunk
        at a time rather than read whole (#503); a retry sends it from its start again."""
        if not isinstance(content, bytes):
            content.seek(0)
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

    async def library_listing(
        self, *, folder_id: int | None, every_folder: bool = False
    ) -> list[LibraryListRow]:
        """``GET /library/files/`` — one folder's files, or the root's without one
        (``include_root`` defaults to true). One read, however many files: Bambuddy
        does not paginate it. ``every_folder`` (without ``folder_id``) is every file in
        the library, ``include_root=false`` (#3630)."""
        what = (
            "list every library file"
            if every_folder
            else "list the library files"
            if folder_id is None
            else f"list the files of library folder {folder_id}"
        )
        params: dict[str, Any] | None = None
        if folder_id is not None:
            params = {"folder_id": folder_id}
        elif every_folder:
            params = {"include_root": "false"}
        response = await self._send(
            "GET",
            "/library/files/",
            scope=Scope.MANAGE_LIBRARY,
            operation="library.listing",
            what=what,
            params=params,
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

    async def update_library_file(self, file_id: int, update: FileUpdate) -> LibraryFile:
        """``PUT /library/files/{id}``: rename and/or move. Partial, as
        :meth:`annotate_library_file` explains."""
        response = await self._send(
            "PUT",
            f"/library/files/{file_id}",
            scope=Scope.MANAGE_LIBRARY,
            operation="library.files.update",
            what=f"update library file {file_id}",
            json=update.model_dump(mode="json", exclude_none=True),
        )
        return LibraryFile.model_validate(response.json())

    async def update_folder(self, folder_id: int, update: FolderUpdate) -> Folder:
        """``PUT /library/folders/{id}``: rename and/or move; partial, ``parent_id=0``
        is the top level."""
        response = await self._send(
            "PUT",
            f"/library/folders/{folder_id}",
            scope=Scope.MANAGE_LIBRARY,
            operation="library.folders.update",
            what=f"update library folder {folder_id}",
            json=update.model_dump(mode="json", exclude_none=True),
        )
        return Folder.model_validate(response.json())

    async def delete_folder(self, folder_id: int) -> None:
        """``DELETE /library/folders/{id}``. The caller empties it first (the folder
        delete kind), so what this drops is the folder's own record."""
        await self._send(
            "DELETE",
            f"/library/folders/{folder_id}",
            scope=Scope.MANAGE_LIBRARY,
            operation="library.folders.delete",
            what=f"delete library folder {folder_id}",
        )

    async def rename_library_file(self, file_id: int, filename: str) -> LibraryFile:
        """``PUT /library/files/{id}`` with ``filename`` alone (#2670): the same partial
        update as :meth:`annotate_library_file`, under the same id. Bambuddy refuses a
        name a printer's SD card would (``validate_print_filename``) with a 400."""
        response = await self._send(
            "PUT",
            f"/library/files/{file_id}",
            scope=Scope.MANAGE_LIBRARY,
            operation="library.rename",
            what=f"rename library file {file_id} to {filename!r}",
            json={"filename": filename},
        )
        return LibraryFile.model_validate(response.json())

    async def move_library_files(self, file_ids: list[int], folder_id: int | None) -> MoveResult:
        """``POST /library/files/move`` (``FileMoveRequest``, #2670): each file into
        ``folder_id``, the root when ``None``, under its own id. Bambuddy skips a file its
        key's user did not add, or one it cannot relocate, each with a reason, and passes
        over an id it does not have without a word; it 404s a folder it does not have and
        403s a read-only external one."""
        target = "the library root" if folder_id is None else f"library folder {folder_id}"
        response = await self._send(
            "POST",
            "/library/files/move",
            scope=Scope.MANAGE_LIBRARY,
            operation="library.move",
            what=f"move {len(file_ids)} library files to {target}",
            json={"file_ids": file_ids, "folder_id": folder_id},
        )
        return MoveResult.model_validate(response.json())

    async def delete_library_file(self, file_id: int) -> bool:
        """``DELETE /library/files/{id}``: moves the file to Bambuddy's trash, and says
        so as ``trashed``. An external file skips the trash (``trashed: false``): its
        record is dropped for good (Bambuddy's ``openapi.json``, read 2026-10-10, #2167)."""
        response = await self._send(
            "DELETE",
            f"/library/files/{file_id}",
            scope=Scope.MANAGE_LIBRARY,
            operation="library.delete",
            what=f"delete library file {file_id}",
        )
        try:
            body = response.json()
        except ValueError:
            return True
        return not (isinstance(body, dict) and body.get("trashed") is False)

    async def bulk_delete_library_files(self, file_ids: list[int]) -> int:
        """``POST /library/bulk-delete`` (``BulkDeleteRequest``): each file to the trash,
        as :meth:`delete_library_file` does. Bambuddy skips a file the key's user does
        not own (without ``*_all``) and answers only how many it deleted
        (``BulkDeleteResponse.deleted_files``), so the caller reads which it skipped."""
        what = f"delete {len(file_ids)} library files"
        response = await self._send(
            "POST",
            "/library/bulk-delete",
            scope=Scope.MANAGE_LIBRARY,
            operation="library.bulk_delete",
            what=what,
            json={"file_ids": file_ids, "folder_ids": []},
        )
        body = response.json()
        deleted = body.get("deleted_files") if isinstance(body, dict) else None
        if not isinstance(deleted, int):
            raise ApiError(
                status.HTTP_502_BAD_GATEWAY,
                f"Bambuddy did not say how many files it deleted when asked to {what}",
            )
        return deleted

    async def trashed_library_files(self) -> list[TrashedLibraryFile]:
        """``GET /library/trash``: every file in Bambuddy's trash that its API key's user
        may see, page by page (``limit`` at most 500). Bambuddy v1.2.5.5
        ``backend/app/api/routes/library_trash.py`` ``list_trash`` (lines 88-150):
        ``{items, total, retention_days}``, each item with its ``folder_id``; a caller
        without ``library:delete_all`` sees only its user's files
        (:meth:`sees_every_users_trash` says which)."""
        found: list[TrashedLibraryFile] = []
        while True:
            response = await self._send(
                "GET",
                "/library/trash",
                scope=Scope.MANAGE_LIBRARY,
                operation="library.trash.list",
                what="list Bambuddy's library trash",
                params={"limit": TRASH_PAGE, "offset": len(found)},
            )
            body = response.json()
            items = [TrashedLibraryFile.model_validate(row) for row in body["items"]]
            found.extend(items)
            if not items or len(found) >= body["total"]:
                return found

    async def sees_every_users_trash(self) -> bool:
        """Whether :meth:`trashed_library_files` lists every user's trashed files rather
        than only the key's user's. Bambuddy v1.2.5.5 narrows ``list_trash`` to the
        caller's own files unless ``require_ownership_permission`` grants
        ``library:delete_all`` (``backend/app/core/auth.py`` lines 2106-2220), which it
        grants to every caller while auth is off. So: ``GET /auth/status``
        (``routes/auth.py`` line 395, public) for ``auth_enabled``, then ``GET /auth/me``
        (line 662), whose ``permissions`` for an API key are those the key can exercise
        (``apikey_effective_permissions``, ``core/auth.py`` line 340). Any other shape
        is a 502, never read as seeing everything."""
        what = "read whether Bambuddy's API key sees every user's trash"
        response = await self._send(
            "GET", "/auth/status", scope=Scope.MANAGE_LIBRARY, operation="auth.status", what=what
        )
        body = response.json()
        enabled = body.get("auth_enabled") if isinstance(body, dict) else None
        if not isinstance(enabled, bool):
            raise ApiError(
                status.HTTP_502_BAD_GATEWAY,
                f"Bambuddy did not say whether auth is on, asked to {what}",
            )
        if not enabled:
            return True
        response = await self._send(
            "GET", "/auth/me", scope=Scope.MANAGE_LIBRARY, operation="auth.me", what=what
        )
        body = response.json()
        permissions = body.get("permissions") if isinstance(body, dict) else None
        if not isinstance(permissions, list) or not all(isinstance(p, str) for p in permissions):
            raise ApiError(
                status.HTTP_502_BAD_GATEWAY,
                f"Bambuddy did not list the API key's permissions, asked to {what}",
            )
        return LIBRARY_DELETE_ALL in permissions

    async def restore_library_file(self, file_id: int) -> None:
        """``POST /library/trash/{id}/restore``: a trashed file back where it was. A file
        not in the trash (an external one, or one already purged) is the 404."""
        await self._send(
            "POST",
            f"/library/trash/{file_id}/restore",
            scope=Scope.MANAGE_LIBRARY,
            operation="library.trash.restore",
            what=f"restore library file {file_id} from Bambuddy's trash",
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

    async def queue(
        self, *, printer_id: int | None = None, status: str | None = None
    ) -> list[QueueItem]:
        """``GET /api/v1/queue/``: every item, finished ones too, unless ``status``
        narrows it. Reading the queue needs only ``can_read_status`` (``QUEUE_READ``
        in Bambuddy's ``auth.py``); ``can_queue`` is for changing it."""
        what = "list the print queue"
        params: dict[str, Any] = {}
        if printer_id is not None:
            params["printer_id"] = printer_id
        if status is not None:
            params["status"] = status
        response = await self._send(
            "GET",
            "/queue/",
            scope=Scope.READ_STATUS,
            operation="queue.list",
            what=what,
            params=params,
        )
        return [QueueItem.model_validate(row) for row in self._rows(response, what=what)]

    async def queue_item(self, item_id: int) -> QueueItem:
        """``GET /api/v1/queue/{id}``. It needs ``queue:read_all`` or ``queue:read_own``
        (``backend/app/api/routes/print_queue.py`` lines 1854-1863 at maziggy/bambuddy
        f9080d1), which an API key holds with ``can_read_status`` (``auth.py`` lines
        89-91), as the list does; 404 "Queue item not found" for an item it lacks."""
        response = await self._send(
            "GET",
            f"/queue/{item_id}",
            scope=Scope.READ_STATUS,
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

    # --- MakerWorld (#6119) ----------------------------------------------------
    # Bambuddy's ``backend/app/api/routes/makerworld.py`` (maziggy/bambuddy at
    # v1.3.2b1-daily.20261112): status and resolve need ``makerworld:view`` (Read Status
    # for a key), import ``makerworld:import`` (Manage Library), by ``core/auth.py``
    # ``_APIKEY_SCOPE_BY_PERMISSION``. The download also needs a Bambu Cloud token: for an
    # API key its owner's, and only when the key has cloud access.

    @staticmethod
    def _parsed[M: BaseModel](response: httpx.Response, model: type[M], *, what: str) -> M:
        """``response`` read as ``model``. A 400 is Bambuddy refusing the link or id
        (``_map_service_error``'s ``ProviderUrlError``, an unknown source type): the
        caller's to fix, so a 422 with Bambuddy's own words."""
        if response.status_code == status.HTTP_400_BAD_REQUEST:
            detail = upstream_detail(response)
            raise ApiError(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"Bambuddy rejected the request to {what}" + (f": {detail}" if detail else ""),
                type_=REJECTED_PROBLEM,
                bambuddy_status=response.status_code,
            )
        try:
            return model.model_validate(response.json())
        except (ValueError, ValidationError) as error:
            raise ApiError(
                status.HTTP_502_BAD_GATEWAY,
                f"Bambuddy's answer was not the expected shape when asked to {what}",
                type_=REJECTED_PROBLEM,
            ) from error

    async def makerworld_status(self) -> BambuddyMakerWorldStatus:
        what = "read its MakerWorld status"
        response = await self._send(
            "GET",
            "/makerworld/status",
            scope=Scope.READ_STATUS,
            operation="makerworld.status",
            passthrough=_REFUSED_LINK,
            what=what,
        )
        return self._parsed(response, BambuddyMakerWorldStatus, what=what)

    async def makerworld_resolve(self, url: str) -> BambuddyMakerWorldResolved:
        what = "resolve a MakerWorld link"
        response = await self._send(
            "POST",
            "/makerworld/resolve",
            scope=Scope.READ_STATUS,
            operation="makerworld.resolve",
            passthrough=_REFUSED_LINK,
            what=what,
            json={"url": url},
        )
        return self._parsed(response, BambuddyMakerWorldResolved, what=what)

    async def makerworld_import(
        self, model_id: int, *, profile_id: int | None, folder_id: int | None
    ) -> BambuddyMakerWorldImported:
        """Bambuddy downloads the profile's 3MF into its library, in ``folder_id`` (its own
        ``MakerWorld`` folder without one). It dedupes by the model and profile's source
        URL: a repeat answers the first file with ``was_existing`` and downloads nothing."""
        what = f"import MakerWorld model {model_id}"
        response = await self._send(
            "POST",
            "/makerworld/import",
            scope=Scope.MANAGE_LIBRARY,
            operation="makerworld.import",
            passthrough=_REFUSED_LINK,
            what=what,
            json={
                "model_id": model_id,
                "source_type": "makerworld",
                "profile_id": profile_id,
                "folder_id": folder_id,
            },
            timeout=MAKERWORLD_IMPORT_TIMEOUT,
        )
        return self._parsed(response, BambuddyMakerWorldImported, what=what)

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
        try:
            body = response.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            # #5840: an answer that is not an object names no version.
            raise ApiError(
                status.HTTP_502_BAD_GATEWAY,
                f"Bambuddy answered {type(body).__name__}, not an object, "
                "when asked to read Bambuddy's version",
            )
        return str(body.get("version") or "unknown")

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

    # --- tags, auto-add, slots and SpoolBuddy (#6264) -------------------------

    async def link_tag(
        self,
        spool_id: int,
        *,
        tag_uid: str | None,
        tray_uuid: str | None,
        tag_type: str | None = None,
        data_origin: str = "nfc_link",
    ) -> Spool:
        """``PATCH /api/v1/inventory/spools/{id}/link-tag`` (``INVENTORY_UPDATE``: Manage
        Inventory; ``routes/inventory.py`` line 2771, maziggy/bambuddy v1.3.1). A 409
        ``tag_already_linked`` (``services/tag_conflict.py`` lines 24-40) raises
        `TagAlreadyLinkedError` naming the holder; any other 409 maps as any refusal."""
        what = f"link a tag to spool {spool_id}"
        response = await self._send(
            "PATCH",
            f"/inventory/spools/{spool_id}/link-tag",
            scope=Scope.MANAGE_INVENTORY,
            operation="spools.link_tag",
            what=what,
            json={
                "tag_uid": tag_uid,
                "tray_uuid": tray_uuid,
                "tag_type": tag_type,
                "data_origin": data_origin,
            },
            passthrough=frozenset({status.HTTP_409_CONFLICT}),
        )
        if response.status_code == status.HTTP_409_CONFLICT:
            raise self._tag_conflict(response, what=what)
        return Spool.model_validate(response.json())

    @staticmethod
    def _tag_conflict(response: httpx.Response, *, what: str) -> ApiError:
        try:
            body = response.json()
        except ValueError:
            body = None
        detail = body.get("detail") if isinstance(body, dict) else None
        if isinstance(detail, dict) and detail.get("code") == "tag_already_linked":
            holder = detail.get("spool_id")
            field = detail.get("field")
            if isinstance(holder, int) and field in ("tag_uid", "tray_uuid"):
                message = detail.get("message")
                return TagAlreadyLinkedError(
                    message if isinstance(message, str) else "the tag is already linked",
                    holder_id=holder,
                    field=field,
                )
        return map_response(response, scope=Scope.MANAGE_INVENTORY, what=what)

    async def clear_spool_tag(self, spool_id: int) -> Spool:
        """``PATCH /api/v1/inventory/spools/{id}`` with both identifiers null
        (``SpoolUpdate``; ``routes/inventory.py`` line 1693, v1.3.1): Manage Inventory."""
        response = await self._send(
            "PATCH",
            f"/inventory/spools/{spool_id}",
            scope=Scope.MANAGE_INVENTORY,
            operation="spools.clear_tag",
            what=f"clear the tag of spool {spool_id}",
            json={"tag_uid": None, "tray_uuid": None},
        )
        return Spool.model_validate(response.json())

    async def spool_by_tag(self, *, tray_uuid: str | None, tag_uid: str | None) -> Spool | None:
        """``GET /api/v1/inventory/spools/by-tag``: the spool holding the tag, or ``None``
        on Bambuddy's 404 (``routes/inventory.py`` line 1560, v1.3.1). Bambuddy accepts
        ``inventory:read`` or ``inventory:update`` there, so a Read Status or a Manage
        Inventory key opens it; the scope names Manage Inventory, which the push needs for
        its writes anyway."""
        response = await self._send(
            "GET",
            "/inventory/spools/by-tag",
            scope=Scope.MANAGE_INVENTORY,
            operation="spools.by_tag",
            what="find the spool of a tag",
            params={
                key: value
                for key, value in (("tray_uuid", tray_uuid), ("tag_uid", tag_uid))
                if value is not None
            },
            passthrough=frozenset({status.HTTP_404_NOT_FOUND}),
        )
        if response.status_code == status.HTTP_404_NOT_FOUND:
            return None
        return Spool.model_validate(response.json())

    async def spool_row(self, spool_id: int) -> Spool:
        """``GET /api/v1/inventory/spools/{id}``: one spool, for a write that reads state
        first (``INVENTORY_READ``: Read Status, ``can_read_status``;
        ``routes/inventory.py`` line 1602, maziggy/bambuddy v1.3.1)."""
        response = await self._send(
            "GET",
            f"/inventory/spools/{spool_id}",
            scope=Scope.READ_STATUS,
            operation="spools.read",
            what=f"read spool {spool_id}",
        )
        return Spool.model_validate(response.json())

    # --- the filament sync's writes (#6265) -----------------------------------
    # Routes, bodies and permissions: ``tests/bambuddy/recordings/openapi/sync-routes.json``
    # (Bambuddy 1.3.1, the source at tag v1.3.1). The ``INVENTORY_UPDATE`` routes take
    # Manage Inventory (``can_manage_inventory``); the lists take Read Status.

    async def create_spool(self, body: dict[str, Any]) -> Spool:
        """``POST /api/v1/inventory/spools`` with a ``SpoolCreate`` (``routes/inventory.py``
        line 1625)."""
        response = await self._send(
            "POST",
            "/inventory/spools",
            scope=Scope.MANAGE_INVENTORY,
            operation="spools.create",
            what="create a spool",
            json=body,
        )
        return Spool.model_validate(response.json())

    async def update_spool(self, spool_id: int, body: dict[str, Any]) -> Spool:
        """``PATCH /api/v1/inventory/spools/{id}`` with a ``SpoolUpdate``: only the fields
        sent change (``model_dump(exclude_unset=True)``, ``routes/inventory.py`` lines
        1693-1716). A sent ``weight_used`` locks the weight unless ``weight_locked`` is
        sent too."""
        response = await self._send(
            "PATCH",
            f"/inventory/spools/{spool_id}",
            scope=Scope.MANAGE_INVENTORY,
            operation="spools.update",
            what=f"update spool {spool_id}",
            json=body,
        )
        return Spool.model_validate(response.json())

    async def archive_spool(self, spool_id: int) -> Spool:
        """``POST /api/v1/inventory/spools/{id}/archive``, no body (``routes/inventory.py``
        line 1774). It also frees the trays the spool was assigned to."""
        response = await self._send(
            "POST",
            f"/inventory/spools/{spool_id}/archive",
            scope=Scope.MANAGE_INVENTORY,
            operation="spools.archive",
            what=f"archive spool {spool_id}",
        )
        return Spool.model_validate(response.json())

    async def restore_spool(self, spool_id: int) -> Spool:
        """``POST /api/v1/inventory/spools/{id}/restore``, no body (``routes/inventory.py``
        line 1796)."""
        response = await self._send(
            "POST",
            f"/inventory/spools/{spool_id}/restore",
            scope=Scope.MANAGE_INVENTORY,
            operation="spools.restore",
            what=f"restore spool {spool_id}",
        )
        return Spool.model_validate(response.json())

    async def put_spool_k_profiles(self, spool_id: int, rows: list[dict[str, Any]]) -> list[Any]:
        """``PUT /api/v1/inventory/spools/{id}/k-profiles`` with ``SpoolKProfileBase`` rows:
        the spool's whole list is replaced (``routes/inventory.py`` line 2020). Answers the
        list Bambuddy stored, as sent (``SpoolKProfileResponse`` rows, not validated)."""
        what = f"replace the K-profiles of spool {spool_id}"
        response = await self._send(
            "PUT",
            f"/inventory/spools/{spool_id}/k-profiles",
            scope=Scope.MANAGE_INVENTORY,
            operation="spools.k_profiles",
            what=what,
            json=rows,
        )
        return self._rows(response, what=what)

    async def put_spool_presets(self, spool_id: int, rows: list[dict[str, Any]]) -> list[Any]:
        """``PUT /api/v1/inventory/spools/{id}/filament-presets`` with
        ``SpoolFilamentPresetBase`` rows: the whole list is replaced
        (``routes/inventory.py`` line 2126). Answers the list Bambuddy stored, as sent
        (``SpoolFilamentPresetResponse`` rows, not validated)."""
        what = f"replace the per-nozzle presets of spool {spool_id}"
        response = await self._send(
            "PUT",
            f"/inventory/spools/{spool_id}/filament-presets",
            scope=Scope.MANAGE_INVENTORY,
            operation="spools.presets_put",
            what=what,
            json=rows,
        )
        return self._rows(response, what=what)

    async def put_spool_suppliers(self, spool_id: int, rows: list[dict[str, Any]]) -> list[Any]:
        """``PUT /api/v1/inventory/spools/{id}/suppliers`` with ``SpoolSupplierLinkInput``
        rows: the whole list is replaced; a duplicate supplier or two purchase sources is
        a 400 and an unknown supplier a 404 (``routes/inventory.py`` lines 2060-2081). Answers
        the list Bambuddy stored, as sent (``SpoolSupplierResponse`` rows, not validated)."""
        what = f"replace the suppliers of spool {spool_id}"
        response = await self._send(
            "PUT",
            f"/inventory/spools/{spool_id}/suppliers",
            scope=Scope.MANAGE_INVENTORY,
            operation="spools.suppliers_put",
            what=what,
            json=rows,
        )
        return self._rows(response, what=what)

    async def suppliers(self) -> list[BbSupplier]:
        """``GET /api/v1/inventory/suppliers`` (``INVENTORY_READ``: Read Status;
        ``routes/inventory.py`` line 896)."""
        what = "list the suppliers"
        response = await self._send(
            "GET",
            "/inventory/suppliers",
            scope=Scope.READ_STATUS,
            operation="suppliers.list",
            what=what,
        )
        return [BbSupplier.model_validate(row) for row in self._rows(response, what=what)]

    async def create_supplier(self, body: dict[str, Any]) -> BbSupplier:
        """``POST /api/v1/inventory/suppliers`` with a ``SupplierCreate``: 201, or 409 for
        a name another supplier has (``routes/inventory.py`` line 916)."""
        response = await self._send(
            "POST",
            "/inventory/suppliers",
            scope=Scope.MANAGE_INVENTORY,
            operation="suppliers.create",
            what="create a supplier",
            json=body,
        )
        return BbSupplier.model_validate(response.json())

    async def update_supplier(self, supplier_id: int, body: dict[str, Any]) -> BbSupplier:
        """``PATCH /api/v1/inventory/suppliers/{id}`` with a ``SupplierUpdate``: only the
        fields sent change (``routes/inventory.py`` line 939)."""
        response = await self._send(
            "PATCH",
            f"/inventory/suppliers/{supplier_id}",
            scope=Scope.MANAGE_INVENTORY,
            operation="suppliers.update",
            what=f"update supplier {supplier_id}",
            json=body,
        )
        return BbSupplier.model_validate(response.json())

    async def locations(self) -> list[BbLocation]:
        """``GET /api/v1/inventory/locations`` (``INVENTORY_READ``: Read Status;
        ``routes/inventory.py`` line 700)."""
        what = "list the storage locations"
        response = await self._send(
            "GET",
            "/inventory/locations",
            scope=Scope.READ_STATUS,
            operation="locations.list",
            what=what,
        )
        return [BbLocation.model_validate(row) for row in self._rows(response, what=what)]

    async def create_location(self, body: dict[str, Any]) -> BbLocation:
        """``POST /api/v1/inventory/locations`` with a ``LocationCreate``: 201, or 409 for
        a name another location has (``routes/inventory.py`` line 716)."""
        response = await self._send(
            "POST",
            "/inventory/locations",
            scope=Scope.MANAGE_INVENTORY,
            operation="locations.create",
            what="create a storage location",
            json=body,
        )
        return BbLocation.model_validate(response.json())

    async def update_location(self, location_id: int, body: dict[str, Any]) -> BbLocation:
        """``PATCH /api/v1/inventory/locations/{id}`` with a ``LocationUpdate``; a rename
        carries over to the spools in it (``routes/inventory.py`` line 740)."""
        response = await self._send(
            "PATCH",
            f"/inventory/locations/{location_id}",
            scope=Scope.MANAGE_INVENTORY,
            operation="locations.update",
            what=f"update storage location {location_id}",
            json=body,
        )
        return BbLocation.model_validate(response.json())

    async def create_catalog_entry(self, name: str, weight: float) -> SpoolCatalogEntry:
        """``POST /api/v1/inventory/catalog`` (``routes/inventory.py`` line 530).
        ``CatalogEntryCreate.weight`` is whole grams, so ``weight`` is rounded."""
        response = await self._send(
            "POST",
            "/inventory/catalog",
            scope=Scope.MANAGE_INVENTORY,
            operation="catalog.create",
            what="create a spool catalog entry",
            json={"name": name, "weight": round(weight)},
        )
        return SpoolCatalogEntry.model_validate(response.json())

    async def update_catalog_entry(
        self, entry_id: int, name: str, weight: float
    ) -> SpoolCatalogEntry:
        """``PUT /api/v1/inventory/catalog/{id}``: name and whole grams, both required
        (``routes/inventory.py`` line 545)."""
        response = await self._send(
            "PUT",
            f"/inventory/catalog/{entry_id}",
            scope=Scope.MANAGE_INVENTORY,
            operation="catalog.update",
            what=f"update spool catalog entry {entry_id}",
            json={"name": name, "weight": round(weight)},
        )
        return SpoolCatalogEntry.model_validate(response.json())

    async def create_filament(self, body: dict[str, Any]) -> CatalogFilament:
        """``POST /api/v1/filament-catalog/`` with a ``FilamentCreate``
        (``FILAMENTS_CREATE``, ``routes/filaments.py`` line 34). No API key can hold it, so
        a 403 is the admin-only scope 409 (`Scope.ADMIN_FILAMENTS`)."""
        response = await self._send(
            "POST",
            "/filament-catalog/",
            scope=Scope.ADMIN_FILAMENTS,
            operation="filament_catalog.create",
            what="add a filament to Bambuddy's catalog",
            json=body,
        )
        return CatalogFilament.model_validate(response.json())

    async def update_filament(self, filament_id: int, body: dict[str, Any]) -> CatalogFilament:
        """``PATCH /api/v1/filament-catalog/{id}`` with a ``FilamentUpdate``: only the
        fields sent change (``FILAMENTS_UPDATE``, ``routes/filaments.py`` line 63; no API
        key can hold it, as for `create_filament`)."""
        response = await self._send(
            "PATCH",
            f"/filament-catalog/{filament_id}",
            scope=Scope.ADMIN_FILAMENTS,
            operation="filament_catalog.update",
            what=f"update filament {filament_id} in Bambuddy's catalog",
            json=body,
        )
        return CatalogFilament.model_validate(response.json())

    async def auto_add_unknown_rfid(self) -> bool:
        """Bambuddy's ``auto_add_unknown_rfid`` setting (``AppSettings`` in
        ``rfid-routes.json``; ``GET /api/v1/settings/``: Read Status)."""
        response = await self._send(
            "GET",
            "/settings/",
            scope=Scope.READ_STATUS,
            operation="settings.read",
            what="read Bambuddy's settings",
        )
        body = response.json()
        value = body.get("auto_add_unknown_rfid") if isinstance(body, dict) else None
        if not isinstance(value, bool):
            raise ApiError(
                status.HTTP_502_BAD_GATEWAY,
                "Bambuddy's settings carry no auto_add_unknown_rfid, so this Bambuddy may be"
                " older than the setting",
            )
        return value

    async def set_auto_add_unknown_rfid(self, value: bool) -> None:
        """``PATCH /api/v1/settings/`` (``SETTINGS_UPDATE``, which no API key holds: a
        403 is a scope 409 and the write works only while Bambuddy's auth is off;
        ``routes/settings.py`` line 552, v1.3.1)."""
        await self._send(
            "PATCH",
            "/settings/",
            scope=Scope.ADMIN_SETTINGS,
            operation="settings.update",
            what="change Bambuddy's auto_add_unknown_rfid setting",
            json={"auto_add_unknown_rfid": value},
        )

    async def configure_slot(
        self, printer_id: int, ams_id: int, tray_id: int, params: SlotConfigure
    ) -> None:
        """``POST /api/v1/printers/{id}/slots/{ams}/{tray}/configure`` with the values in
        the query (``PRINTERS_CONTROL``: Control Printer; ``routes/printers.py`` line
        2963, v1.3.1)."""
        await self._send(
            "POST",
            f"/printers/{printer_id}/slots/{ams_id}/{tray_id}/configure",
            scope=Scope.CONTROL_PRINTER,
            operation="printers.slot_configure",
            what=f"configure slot {ams_id}/{tray_id} of printer {printer_id}",
            params=params.query(),
        )

    async def spoolbuddy_devices(self) -> list[SpoolBuddyDevice]:
        """``GET /api/v1/spoolbuddy/devices`` (``INVENTORY_READ``: Read Status;
        ``routes/spoolbuddy.py`` line 280, v1.3.1)."""
        what = "list the SpoolBuddy devices"
        response = await self._send(
            "GET",
            "/spoolbuddy/devices",
            scope=Scope.READ_STATUS,
            operation="spoolbuddy.devices",
            what=what,
        )
        return [SpoolBuddyDevice.model_validate(row) for row in self._rows(response, what=what)]

    async def queue_tag_write(self, device_id: str, spool_id: int) -> list[str]:
        """``POST /api/v1/spoolbuddy/nfc/write-tag`` (``INVENTORY_UPDATE``: Manage
        Inventory; ``routes/spoolbuddy.py`` line 830, v1.3.1): queues one write, once per call.
        Returns the answer's ``warnings``."""
        response = await self._send(
            "POST",
            "/spoolbuddy/nfc/write-tag",
            scope=Scope.MANAGE_INVENTORY,
            operation="spoolbuddy.write_tag",
            what=f"queue a tag write for spool {spool_id}",
            json={"device_id": device_id, "spool_id": spool_id},
        )
        body = response.json()
        warnings = body.get("warnings") if isinstance(body, dict) else None
        if not isinstance(warnings, list):
            return []
        return [w for w in warnings if isinstance(w, str)]

    async def cancel_tag_write(self, device_id: str) -> None:
        """``POST /api/v1/spoolbuddy/devices/{device_id}/cancel-write`` (Manage Inventory;
        ``routes/spoolbuddy.py`` line 1181, v1.3.1)."""
        await self._send(
            "POST",
            f"/spoolbuddy/devices/{quote(device_id, safe='')}/cancel-write",
            scope=Scope.MANAGE_INVENTORY,
            operation="spoolbuddy.cancel_write",
            what="cancel the pending tag write",
        )

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


@asynccontextmanager
async def maybe_client(
    settings: StoredSettings, *, native: bool
) -> AsyncIterator[BambuddyClient | None]:
    """``None`` on a native run (#3818 tier A, plan 2026-10-18 D3), else
    :func:`client_for`'s client: a native run never builds one, so "never waits on
    Bambuddy" is a type the print path's entry points carry. A run on Bambuddy's path
    with no URL configured is still :func:`client_for`'s 409 here, at its entry, rather
    than a ``None`` its every read would then trip on."""
    if native:
        yield None
        return
    async with client_for(settings) as client:
        yield client


#: Reads a process preset's values (#717); :func:`read_once` makes one that reads each
#: preset at most once.
ReadPresetValues = Callable[[PresetRef], Awaitable[PresetValues]]


def preset_reader(client: BambuddyClient | None, read: ReadPresetValues | None) -> ReadPresetValues:
    """``read`` when given, else ``client``'s own read: a native run passes a reader of
    the image's profiles and no client (#3818 tier A, ``slicing/values.py``)."""
    if read is not None:
        return read
    if client is None:
        raise ValueError("reading preset values needs a Bambuddy client or a reader")
    return client.preset_values


def read_once(client: BambuddyClient) -> ReadPresetValues:
    """``client.preset_values``, read at most once per preset by whoever shares it
    (#2804 review): a print's filament profiles and its supports change the same process
    preset. A failed read fails the same way for every one of them.

    It assumes its callers take turns, as ``check_print`` and ``plan_run`` do: two reads
    of the same preset gathered concurrently would both miss and both read."""
    read: dict[tuple[str, str], PresetValues | ApiError] = {}

    async def values(process: PresetRef) -> PresetValues:
        key = (process.source, process.id)
        if key not in read:
            try:
                read[key] = await client.preset_values(process)
            except ApiError as error:
                read[key] = error
        answer = read[key]
        if isinstance(answer, ApiError):
            raise answer
        return answer

    return values
