"""The camera live edge (printer camera live view, phase 3): ``python -m scadbuddy.camera.edge``.

It runs beside MediaMTX in one pod, which serves WebRTC (WHEP, :8889) and LL-HLS (:8888) to
the API, the only client its NetworkPolicy admits, and only with the edge read user's
password (`deploy/camera/mediamtx-edge.yml`). It
reads the recorded printers from the API's ``camera`` CRUD routes (`api/crud_camera.py`), as
the recorders do, and gives MediaMTX one path per printer, named by the printer id, through
its control API only (`mediamtx.MediaMtx`). Each path reads the relay's path of that printer
with the relay read secret (`mediamtx.relay_source`), on demand: MediaMTX pulls from the relay
only while someone watches. It closes the pull 10 s after the path's last reader goes
(`sourceOnDemandCloseAfter`). A WebRTC viewer stops reading when it leaves. An HLS viewer's
session stays a reader for about 40 s more, so after the last HLS viewer the pull closes about
50 s later (measured 2026-11-25, MediaMTX v1.21.4, the edge e2e).

A path that has not changed is never put again, so a resync cuts no viewer; the edge deletes
a path only when its printer stops being recorded. After a restart the controller has no copy
of what it put (MediaMTX lists a source with its password redacted), and puts each path once.

The read password never reaches a log: a MediaMTX failure raises `MediaMtxError`, which names
the path and the status only. The edge has no database secret and refuses to start with one
(`settings.EdgeSettings.check_environment`). ``/healthz``, ``/readyz`` (`Edge.ready`) and
``/metrics`` on `process.HEALTH_PORT`; ``scadbuddy_camera_viewers`` comes from MediaMTX's
runtime list at each scrape (`ViewersCollector`).
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Iterator, Sequence
from typing import Any, Final
from urllib.parse import quote

import httpx
from prometheus_client import REGISTRY
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector
from pydantic import TypeAdapter, ValidationError

from scadbuddy.auth.service_auth import ServiceAuth, exchange_source
from scadbuddy.camera.follow import ChangeFollower
from scadbuddy.camera.mediamtx import PAGE, MediaMtx, MediaMtxError, relay_source, why
from scadbuddy.camera.models import CameraSource
from scadbuddy.camera.process import HEALTH_PORT, healthy, main_with, serve_health
from scadbuddy.camera.settings import EdgeSettings
from scadbuddy.crud.client import CrudClient, CrudRefusedError, wait_for_areas

logger = logging.getLogger(__name__)

BASE: Final = "/api/v1/internal/crud/camera"
AREAS: Final = frozenset({"printers", "changes"})
#: How long MediaMTX keeps the relay pull after the path's last reader leaves.
CLOSE_AFTER: Final = "10s"
VIEWERS: Final = "scadbuddy_camera_viewers"
VIEWERS_HELP: Final = (
    "MediaMTX readers of the printer's edge path: one per WebRTC session; HLS counts one muxer."
)
#: How long one scrape may wait for MediaMTX's runtime list.
SCRAPE_TIMEOUT: Final = 2.0
_SOURCES = TypeAdapter(list[CameraSource])


class SourceError(Exception):
    """The API sent a source list the edge cannot read."""


class Edge:
    def __init__(self, crud: CrudClient, mtx: MediaMtx, settings: EdgeSettings) -> None:
        self._crud = crud
        self._mtx = mtx
        self._relay_url = settings.camera_relay_rtsp_url
        self._read_user = settings.camera_relay_read_user
        self._read_pass = settings.camera_relay_read_pass
        #: The conf the edge last put for each path: MediaMTX lists a source with its
        #: password redacted, so the edge compares with its own copy (as the relay does).
        self._confs: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        #: A full reconcile has put the API's paths in MediaMTX (`ready`).
        self._synced = False

    def __repr__(self) -> str:
        return f"Edge(printers={sorted(self._confs)!r})"

    def wanted(self) -> set[str]:
        """The paths the edge has put in MediaMTX and not removed: each one must be in
        MediaMTX's runtime list (`process.healthy`)."""
        return set(self._confs)

    def ready(self) -> bool:
        """``/readyz``: a full reconcile has put the API's paths in MediaMTX."""
        return self._synced

    def _conf(self, printer_id: str) -> dict[str, Any]:
        return {
            "source": relay_source(self._relay_url, self._read_user, self._read_pass, printer_id),
            "sourceOnDemand": True,
            "sourceOnDemandCloseAfter": CLOSE_AFTER,
            "rtspTransport": "tcp",
        }

    async def _existing(self) -> set[str]:
        try:
            return set(await self._mtx.paths())
        except httpx.HTTPError as error:
            raise MediaMtxError(f"MediaMTX did not list its paths: {why(error)}") from None

    async def _put(self, name: str, present: bool) -> None:
        conf = self._conf(name)
        if present and self._confs.get(name) == conf:
            return
        self._confs.pop(name, None)
        try:
            await self._mtx.put_path(name, conf)
        except httpx.HTTPError as error:
            raise MediaMtxError(f"MediaMTX did not take path {name}: {why(error)}") from None
        self._confs[name] = conf

    async def _remove(self, name: str, present: bool) -> None:
        if present:
            try:
                await self._mtx.delete_path(name)
            except httpx.HTTPError as error:
                raise MediaMtxError(f"MediaMTX did not delete path {name}: {why(error)}") from None
        self._confs.pop(name, None)

    async def reconcile_all(self) -> None:
        """Make MediaMTX's paths match the API's list of recorded printers."""
        async with self._lock:
            response = await self._crud.request("GET", f"{BASE}/printers")
            try:
                wanted = {s.printer_id for s in _SOURCES.validate_python(response.json())}
            except (ValueError, ValidationError):
                raise SourceError(
                    "the API sent a camera source list the edge cannot read"
                ) from None
            existing = await self._existing()
            for name in sorted((existing | set(self._confs)) - wanted):
                await self._remove(name, name in existing)
            for name in sorted(wanted):
                await self._put(name, name in existing)
            self._synced = True

    async def reconcile(self, printer_id: str) -> None:
        """Add or replace one printer's path, or remove it when the API answers 404."""
        async with self._lock:
            try:
                await self._crud.request("GET", f"{BASE}/printers/{quote(printer_id, safe='')}")
            except CrudRefusedError as refused:
                if refused.status != 404:
                    raise
                await self._remove(printer_id, printer_id in await self._existing())
                return
            await self._put(printer_id, printer_id in await self._existing())


class ViewersCollector(Collector):
    """``scadbuddy_camera_viewers{printer}``: each path's ``readers`` in MediaMTX's runtime
    list (``GET /v3/paths/list``), read at scrape time. Nothing when MediaMTX does not
    answer. `process.serve_health` scrapes in a thread, so this blocking call holds up no
    probe."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:9997",
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._transport = transport

    def describe(self) -> Iterator[GaugeMetricFamily]:
        yield GaugeMetricFamily(VIEWERS, VIEWERS_HELP, labels=["printer"])

    def collect(self) -> Iterator[GaugeMetricFamily]:
        family = GaugeMetricFamily(VIEWERS, VIEWERS_HELP, labels=["printer"])
        try:
            counts = self._readers()
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
            logger.warning(
                "camera edge: MediaMTX did not list its readers (%s)", type(error).__name__
            )
            counts = {}
        for name, count in sorted(counts.items()):
            family.add_metric([name], count)
        yield family

    def _readers(self) -> dict[str, int]:
        found: dict[str, int] = {}
        with httpx.Client(
            base_url=self._base_url, timeout=SCRAPE_TIMEOUT, transport=self._transport
        ) as http:
            page = 0
            while True:
                response = http.get("/v3/paths/list", params={"page": page, "itemsPerPage": PAGE})
                response.raise_for_status()
                body = response.json()
                for item in body.get("items") or []:
                    found[item["name"]] = len(item.get("readers") or [])
                page += 1
                if page >= body.get("pageCount", 1):
                    return found


async def run(settings: EdgeSettings, stop: asyncio.Event | None = None) -> None:
    stop = stop or asyncio.Event()
    auth = ServiceAuth(exchange_source("camera", settings))
    crud = CrudClient(settings.api_internal_url, "camera", auth)
    mtx = MediaMtx(settings.camera_mediamtx_api_url)
    edge = Edge(crud, mtx, settings)
    viewers = ViewersCollector(settings.camera_mediamtx_api_url)
    REGISTRY.register(viewers)
    follower: asyncio.Task[None] | None = None

    async def alive() -> bool:
        return await healthy(mtx, follower, edge.wanted())

    async def ready() -> bool:
        return follower is not None and edge.ready() and await alive()

    health = await serve_health(HEALTH_PORT, alive, ready)
    try:
        if not await wait_for_areas(crud, "camera", AREAS, stop):
            logger.info("stopped before the CRUD service served the camera areas")
            return
        try:
            await edge.reconcile_all()
        except Exception as error:
            # MediaMTX may still be starting: the follower's first ``ready`` reads again.
            logger.warning(
                "camera edge: first reconcile failed (%s: %s)", type(error).__name__, error
            )
        follow = ChangeFollower(
            crud, f"{BASE}/changes", edge.reconcile, edge.reconcile_all, resync_first=True
        )
        follower = asyncio.create_task(follow.run())
        logger.info("camera edge running")
        await stop.wait()
    finally:
        if follower is not None:
            follower.cancel()
            await asyncio.gather(follower, return_exceptions=True)
        health.close()
        REGISTRY.unregister(viewers)
        await mtx.aclose()
        await crud.aclose()


def main(argv: Sequence[str] | None = None) -> int:
    return main_with(
        EdgeSettings.check_environment,
        lambda: EdgeSettings(),  # type: ignore[call-arg]  # read from the environment
        lambda settings: settings.log_level,
        run,
    )


if __name__ == "__main__":
    sys.exit(main())
