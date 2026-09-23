"""httpx client for Bambuddy's REST API.

Two path details are load-bearing and were measured against a live 1.2.5.5 rather
than read off the design spec:

* ``/api/v1/printers`` **404s** — only ``/api/v1/printers/`` exists, and it answers
  with a bare list whose ``id`` is an integer.
* ``/api/v1/library/folders`` and ``/api/v1/external-links/`` also answer with bare
  lists, while ``/api/v1/slicer-pipelines/`` wraps its rows in ``{"pipelines": [...]}``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Self

import httpx
from fastapi import status

from scadbuddy.bambuddy.errors import Scope, map_response, map_transport, not_configured
from scadbuddy.bambuddy.models import (
    ExternalLink,
    Folder,
    LibraryFile,
    Pipeline,
    PipelineList,
    PipelineRun,
    PresetCatalogue,
    Printer,
    QueueItem,
    SliceJob,
    SliceJobAccepted,
    SliceRequest,
)
from scadbuddy.core.problems import ApiError
from scadbuddy.library.settings_store import StoredSettings

logger = logging.getLogger(__name__)

API_PREFIX = "/api/v1"
THREE_MF_MEDIA_TYPE = "model/3mf"

# Queue defaults. bed_levelling and flow_cali are three-way enums on this API
# ("off" | "on" | "auto"), not the booleans the design spec assumed — sending a
# bool 422s.
QUEUE_BED_LEVELLING = "off"
QUEUE_FLOW_CALI = "off"

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

    async def folders(self) -> list[Folder]:
        what = "list the library folders"
        response = await self._send(
            "GET", "/library/folders", scope=Scope.MANAGE_LIBRARY, what=what
        )
        return [Folder.model_validate(row) for row in self._rows(response, what=what)]

    async def pipelines(self) -> list[Pipeline]:
        response = await self._send(
            "GET",
            "/slicer-pipelines/",
            scope=Scope.MANAGE_QUEUE,
            what="list the slicer pipelines",
        )
        return PipelineList.model_validate(response.json()).pipelines

    async def presets(self) -> PresetCatalogue:
        response = await self._send(
            "GET", "/slicer/presets", scope=Scope.MANAGE_LIBRARY, what="list the slicer presets"
        )
        return PresetCatalogue.model_validate(response.json())

    # --- library -------------------------------------------------------------

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

    # --- pipelines and queue -------------------------------------------------

    async def run_pipeline(
        self, pipeline_id: int, *, source_library_file_id: int, copies: int = 1, force: bool = False
    ) -> PipelineRun:
        response = await self._send(
            "POST",
            f"/slicer-pipelines/{pipeline_id}/run",
            scope=Scope.MANAGE_QUEUE,
            what=f"run slicer pipeline {pipeline_id}",
            json={
                "source_library_file_id": source_library_file_id,
                "copies": copies,
                "force": force,
            },
        )
        return PipelineRun.model_validate(response.json())

    async def enqueue(
        self,
        *,
        printer_id: int,
        library_file_id: int,
        quantity: int = 1,
        plate_id: int = 1,
        use_ams: bool = True,
        manual_start: bool = False,
    ) -> QueueItem:
        response = await self._send(
            "POST",
            "/queue/",
            scope=Scope.MANAGE_QUEUE,
            what=f"queue library file {library_file_id}",
            json={
                "printer_id": printer_id,
                "library_file_id": library_file_id,
                "quantity": quantity,
                "plate_id": plate_id,
                "use_ams": use_ams,
                "bed_levelling": QUEUE_BED_LEVELLING,
                "flow_cali": QUEUE_FLOW_CALI,
                "vibration_cali": True,
                "layer_inspect": True,
                "timelapse": True,
                "manual_start": manual_start,
            },
        )
        return QueueItem.model_validate(response.json())

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
