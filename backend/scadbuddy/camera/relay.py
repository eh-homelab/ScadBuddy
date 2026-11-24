"""The camera relay (printer camera recording, phase 1): ``python -m scadbuddy.camera.relay``.

It runs beside MediaMTX in one pod and is the only process that holds printer access codes.
It reads the qualifying printers from the API's ``camera-relay`` CRUD routes
(`api/crud_camera_relay.py`) and gives MediaMTX one RTSPS pull per printer, through its control
API only (`mediamtx.MediaMtx`). The path name is the printer id. MediaMTX calls the local hook
server (`HOOK_HOST`:`HOOK_PORT`) when a pull becomes ready or not ready, and the relay reports
the feed state to the API, on change only. A report that does not land is sent again in the
background with back-off until it does; the newest state wins. Each `Relay.reconcile_all`
also reads MediaMTX's runtime list and sets each path's state from its ``ready`` flag, so a
lost hook is corrected at the next resync.

An access code never reaches a log or the disk. The relay parses the API's sources itself and
raises `SourceError`, which names the fields only, never their values; a MediaMTX failure
raises `MediaMtxError`, which names the path and the status only. `ChangeFollower` logs these.

The relay has no database secret and refuses to start with one
(`settings.RelaySettings.check_environment`). ``/healthz`` and ``/metrics`` on `HEALTH_PORT`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from collections.abc import Callable, Sequence
from contextlib import suppress
from typing import Any, Final, Literal
from urllib.parse import quote

import httpx
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import TypeAdapter, ValidationError

from scadbuddy.auth.service_auth import ServiceAuth, exchange_source
from scadbuddy.camera.follow import ChangeFollower
from scadbuddy.camera.mediamtx import MediaMtx
from scadbuddy.camera.models import CameraSourceWithCode, FeedReport, FeedState
from scadbuddy.camera.settings import CameraRefusal, RelaySettings
from scadbuddy.core.logging import configure_logging
from scadbuddy.crud.client import CrudClient, CrudRefusedError, wait_for_areas
from scadbuddy.intake_worker.hardening import forbid_dumping

logger = logging.getLogger(__name__)

BASE: Final = "/api/v1/internal/crud/camera-relay"
AREAS: Final = frozenset({"printers", "changes"})
HOOK_HOST: Final = "127.0.0.1"
HOOK_PORT: Final = 9091
HEALTH_PORT: Final = 9090
HOOK_URL: Final = f"http://{HOOK_HOST}:{HOOK_PORT}/hook"
#: The back-off of a feed report that did not land.
RETRY_MIN: Final = 0.5
RETRY_MAX: Final = 30.0
#: The user name of a Bambu printer's camera.
RELAY_USER: Final = "bblp"

HookEvent = Literal["ready", "not_ready"]
_SOURCES = TypeAdapter(list[dict[str, Any]])


class SourceError(Exception):
    """The API sent a source the relay cannot read. It names fields, never values."""


class MediaMtxError(Exception):
    """MediaMTX refused or missed a path change. It names the path and the status only."""


def rtsps_source(source: CameraSourceWithCode) -> str:
    """The printer's RTSPS URL, with its access code. Only MediaMTX gets it."""
    code = quote(source.access_code.get_secret_value(), safe="")
    host = f"[{source.host}]" if ":" in source.host else source.host
    return f"rtsps://{RELAY_USER}:{code}@{host}:{source.port}{source.path}"


def _parse(item: object) -> CameraSourceWithCode:
    try:
        return CameraSourceWithCode.model_validate(item)
    except ValidationError as invalid:
        fields = sorted({".".join(str(p) for p in e["loc"]) or "source" for e in invalid.errors()})
        printer_id = item.get("printer_id") if isinstance(item, dict) else None
        name = printer_id if isinstance(printer_id, str) else "unknown"
        raise SourceError(
            f"the API sent a camera source for {name} the relay cannot read: {', '.join(fields)}"
        ) from None


class Relay:
    def __init__(
        self,
        crud: CrudClient,
        mtx: MediaMtx,
        *,
        source_for: Callable[[CameraSourceWithCode], str] = rtsps_source,
        retry_min: float = RETRY_MIN,
        retry_max: float = RETRY_MAX,
    ) -> None:
        self._crud = crud
        self._mtx = mtx
        self._source_for = source_for
        #: The paths the relay holds in MediaMTX; `None` until it first lists them.
        self._held: set[str] | None = None
        #: The conf the relay last put for each path. MediaMTX lists a source with its
        #: user and password redacted (``rtsps://<redacted>:<redacted>@…``, measured
        #: 2026-11-24 UTC on bluenviron/mediamtx:1, v1.21.4, with ``GET
        #: /v3/config/paths/list``), so the relay compares with its own copy. After a
        #: restart it has none, and puts each path once again.
        self._confs: dict[str, dict[str, Any]] = {}
        #: The feed state the relay wants the API to hold, per printer.
        self._desired: dict[str, FeedState] = {}
        #: The last feed state the API took, per printer.
        self._reported: dict[str, FeedState] = {}
        #: One sender per printer whose desired state has not landed yet.
        self._senders: dict[str, asyncio.Task[None]] = {}
        self._retry_min = retry_min
        self._retry_max = retry_max
        self._lock = asyncio.Lock()

    def _conf(self, source: CameraSourceWithCode) -> dict[str, Any]:
        return {
            "source": self._source_for(source),
            "sourceFingerprint": source.cert_sha256.lower(),
            "sourceOnDemand": False,
            # MediaMTX expands $MTX_PATH itself, with no shell (measured 2026-11-24 UTC on
            # v1.21.4). The older runOnReady/runOnNotReady are deprecated in favour of these.
            "runOnAvailable": f"curl -fsS -X POST {HOOK_URL}/$MTX_PATH/ready",
            "runOnUnavailable": f"curl -fsS -X POST {HOOK_URL}/$MTX_PATH/not_ready",
        }

    async def _existing(self) -> dict[str, dict[str, Any]]:
        try:
            return await self._mtx.paths()
        except httpx.HTTPError as error:
            raise MediaMtxError(f"MediaMTX did not list its paths: {_why(error)}") from None

    async def _put(self, name: str, conf: dict[str, Any], present: bool) -> None:
        if present and self._confs.get(name) == conf:
            return
        self._confs.pop(name, None)
        try:
            await self._mtx.put_path(name, conf)
        except httpx.HTTPError as error:
            raise MediaMtxError(f"MediaMTX did not take path {name}: {_why(error)}") from None
        self._confs[name] = conf
        assert self._held is not None
        self._held.add(name)
        # Until MediaMTX calls the ready hook (``runOnAvailable``), the pull is being tried.
        self._want(name, "retrying")

    async def _remove(self, name: str, present: bool) -> None:
        """Delete the path when MediaMTX has it, and report ``stopped``: also when a
        delete went through earlier but its report did not."""
        if present:
            try:
                await self._mtx.delete_path(name)
            except httpx.HTTPError as error:
                raise MediaMtxError(f"MediaMTX did not delete path {name}: {_why(error)}") from None
            self._confs.pop(name, None)
            if self._held is not None:
                self._held.discard(name)
        if present or self._desired.get(name, "stopped") != "stopped":
            self._want(name, "stopped")

    def _want(self, printer_id: str, state: FeedState) -> None:
        """Record the state the API should hold. A sender posts it in the background,
        again with back-off until it lands; a newer state replaces one still waiting."""
        self._desired[printer_id] = state
        sender = self._senders.get(printer_id)
        if self._reported.get(printer_id) != state and (sender is None or sender.done()):
            self._senders[printer_id] = asyncio.create_task(self._send(printer_id))

    async def _send(self, printer_id: str) -> None:
        delay = self._retry_min
        path = f"{BASE}/printers/{quote(printer_id, safe='')}/feed"
        while (state := self._desired[printer_id]) != self._reported.get(printer_id):
            body = FeedReport(part="relay", state=state).model_dump(mode="json", exclude_none=True)
            try:
                await self._crud.request("POST", path, json=body)
            except CrudRefusedError as refused:
                # A refusal does not pass with time: stop. A later state or resync tries
                # again, since the API's state is no longer known.
                self._reported.pop(printer_id, None)
                logger.warning(
                    "camera relay: the API refused feed state %s for %s (%s)",
                    state,
                    printer_id,
                    refused.status,
                )
                return
            except Exception as error:
                # The post may have reached the API before it failed (a lost answer): its
                # state is no longer known, so the next desired state is always posted.
                self._reported.pop(printer_id, None)
                logger.warning(
                    "camera relay: feed state %s for %s did not land (%s); retrying",
                    state,
                    printer_id,
                    type(error).__name__,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._retry_max)
                continue
            self._reported[printer_id] = state
            delay = self._retry_min

    async def flushed(self) -> None:
        """Wait until every desired feed state has landed."""
        while pending := [t for t in self._senders.values() if not t.done()]:
            await asyncio.gather(*pending)

    async def aclose(self) -> None:
        """Stop the senders."""
        for sender in self._senders.values():
            sender.cancel()
        await asyncio.gather(*self._senders.values(), return_exceptions=True)

    async def _runtime(self) -> None:
        """Set each held path's desired state from MediaMTX's runtime ``ready`` flag: a
        lost hook, or a hook that came before a replace, is corrected here."""
        try:
            ready = await self._mtx.ready()
        except httpx.HTTPError as error:
            raise MediaMtxError(f"MediaMTX did not list its running paths: {_why(error)}") from None
        for name in sorted(self._held or ()):
            self._want(name, "pulling" if ready.get(name) else "retrying")

    async def reconcile_all(self) -> None:
        """Make MediaMTX's paths match the API's list. A path that has not changed is left
        alone, so a resync after a quiet run costs one list on each side and MediaMTX's
        runtime list (`_runtime`), which sets each held path's feed state."""
        async with self._lock:
            response = await self._crud.request("GET", f"{BASE}/printers")
            try:
                items = _SOURCES.validate_python(response.json())
            except (ValueError, ValidationError):
                raise SourceError(
                    "the API sent a camera source list the relay cannot read"
                ) from None
            sources: dict[str, CameraSourceWithCode] = {}
            unreadable: list[SourceError] = []
            for item in items:
                try:
                    parsed = _parse(item)
                except SourceError as error:
                    unreadable.append(error)
                    continue
                sources[parsed.printer_id] = parsed
            existing = await self._existing()
            self._held = set(existing)
            kept = {str(item.get("printer_id")) for item in items}
            for name in sorted((set(existing) | set(self._desired)) - kept):
                await self._remove(name, name in existing)
            for printer_id, parsed in sources.items():
                await self._put(printer_id, self._conf(parsed), printer_id in existing)
            await self._runtime()
            if unreadable:
                raise SourceError("; ".join(str(error) for error in unreadable))

    async def reconcile(self, printer_id: str) -> None:
        """Bring one printer's path up to date: add or replace it, or remove it when the API
        answers 404 ``not_recorded``."""
        async with self._lock:
            try:
                response = await self._crud.request(
                    "GET", f"{BASE}/printers/{quote(printer_id, safe='')}"
                )
            except CrudRefusedError as refused:
                if refused.status != 404:
                    raise
                existing = await self._existing()
                self._held = set(existing)
                await self._remove(printer_id, printer_id in existing)
                return
            try:
                item = response.json()
            except ValueError:
                raise SourceError(f"the API sent an unreadable source for {printer_id}") from None
            parsed = _parse(item)
            existing = await self._existing()
            self._held = set(existing)
            await self._put(printer_id, self._conf(parsed), printer_id in existing)

    async def on_hook(self, printer_id: str, event: HookEvent) -> None:
        """MediaMTX's pull of ``printer_id`` became ready or not ready. A hook for a path the
        relay no longer holds (a late one after a removal) reports nothing."""
        async with self._lock:
            if self._held is None or printer_id not in self._held:
                return
            self._want(printer_id, "pulling" if event == "ready" else "retrying")


def _why(error: httpx.HTTPError) -> str:
    """An httpx error as its type and status: never its request, whose body holds the code."""
    if isinstance(error, httpx.HTTPStatusError):
        return f"{type(error).__name__} {error.response.status_code}"
    return type(error).__name__


async def serve_hooks(relay: Relay, host: str, port: int) -> asyncio.Server:
    """``POST /hook/<printer_id>/ready`` (or ``not_ready``), from MediaMTX's ``runOnAvailable``
    and ``runOnUnavailable`` commands: 204, or 404 for any other request."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        status = b"404 Not Found"
        with suppress(Exception):
            line = await asyncio.wait_for(reader.readline(), 5)
            end = (b"\r\n", b"\n", b"")
            while await asyncio.wait_for(reader.readline(), 5) not in end:
                continue
            method, target, *_ = line.decode("ascii").split(" ")
            parts = target.split("/")
            if method == "POST" and len(parts) == 4 and parts[1] == "hook" and parts[2]:
                event = parts[3]
                if event in ("ready", "not_ready"):
                    status = b"204 No Content"
                    await relay.on_hook(parts[2], event)  # type: ignore[arg-type]
        with suppress(Exception):
            writer.write(
                b"HTTP/1.1 " + status + b"\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
        writer.close()

    return await asyncio.start_server(handle, host, port)


async def _healthz(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """``GET /metrics``: the process's metrics; any other request 200."""
    with suppress(Exception):
        line = await asyncio.wait_for(reader.readline(), 5)
        if line.split(b" ")[1:2] == [b"/metrics"]:
            body, kind = generate_latest(), CONTENT_TYPE_LATEST.encode()
        else:
            body, kind = b"ok", b"text/plain"
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: "
            + kind
            + b"\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\nConnection: close\r\n\r\n"
            + body
        )
        await writer.drain()
    writer.close()


async def run(settings: RelaySettings, *, stop: asyncio.Event | None = None) -> None:
    stop = stop or asyncio.Event()
    auth = ServiceAuth(exchange_source("camera-relay", settings))
    crud = CrudClient(settings.api_internal_url, "camera-relay", auth)
    mtx = MediaMtx(settings.camera_mediamtx_api_url)
    relay = Relay(crud, mtx, source_for=rtsps_source)
    health = await asyncio.start_server(_healthz, "0.0.0.0", HEALTH_PORT)
    hooks: asyncio.Server | None = None
    follower: asyncio.Task[None] | None = None
    try:
        if not await wait_for_areas(crud, "camera-relay", AREAS, stop):
            logger.info("stopped before the CRUD service served the camera-relay areas")
            return
        hooks = await serve_hooks(relay, HOOK_HOST, HOOK_PORT)
        try:
            await relay.reconcile_all()
        except Exception as error:
            # MediaMTX may still be starting: the follower's first ``ready`` reads again.
            logger.warning(
                "camera relay: first reconcile failed (%s: %s)", type(error).__name__, error
            )
        follow = ChangeFollower(
            crud, f"{BASE}/changes", relay.reconcile, relay.reconcile_all, resync_first=True
        )
        follower = asyncio.create_task(follow.run())
        logger.info("camera relay running")
        await stop.wait()
    finally:
        if follower is not None:
            follower.cancel()
            await asyncio.gather(follower, return_exceptions=True)
        # The hooks first, so no hook starts a sender after the senders stop.
        if hooks is not None:
            hooks.close()
        await relay.aclose()
        health.close()
        await mtx.aclose()
        await crud.aclose()


def main(argv: Sequence[str] | None = None) -> int:
    try:
        RelaySettings.check_environment(os.environ)
    except CameraRefusal as refusal:
        configure_logging()
        for reason in refusal.reasons:
            logger.error(reason)
        return 2
    # Before the proof is read: no process of this user may read this one's memory, where
    # the service credential and the access codes live.
    forbid_dumping()
    try:
        settings = RelaySettings()  # type: ignore[call-arg]  # read from the environment
    except ValidationError as invalid:
        configure_logging()
        for error in invalid.errors(include_input=False, include_url=False):
            name = ".".join(str(part) for part in error["loc"]) or "settings"
            logger.error("setting %s: %s", name, error["msg"])
        return 2
    configure_logging(settings.log_level)

    async def _main() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, stop.set)
        await run(settings, stop=stop)

    asyncio.run(_main())
    return 0


if __name__ == "__main__":
    sys.exit(main())
