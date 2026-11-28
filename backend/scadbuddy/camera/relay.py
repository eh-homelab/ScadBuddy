"""The camera relay (printer camera recording, phase 1): ``python -m scadbuddy.camera.relay``.

It runs beside MediaMTX in one pod and is the only process that holds printer access codes.
It reads the qualifying printers from the API's ``camera-relay`` CRUD routes
(`api/crud_camera_relay.py`) and gives MediaMTX one RTSPS pull per printer, through its control
API only (`mediamtx.MediaMtx`). The path name is the printer id. MediaMTX calls the local hook
server (`HOOK_HOST`:`HOOK_PORT`) when a pull becomes ready or not ready, and the relay reports
the feed state to the API, on change only. A printer can keep its RTSPS session open and send
no media: MediaMTX then keeps the path ready, and its ``readTimeout`` does not close it
(#8081). So `Relay.check_silence` restarts a ready pull whose ``bytesReceived`` has not grown
for `SILENT_SECONDS`. A report that does not land is sent again in the
background with back-off until it does; the newest state wins. Each `Relay.reconcile_all`
also reads MediaMTX's runtime list and sets each path's state from its ``ready`` flag, so a
lost hook is corrected at the next resync.

An access code never reaches a log or the disk. The relay parses the API's sources itself and
raises `SourceError`, which names the fields only, never their values; a MediaMTX failure
raises `MediaMtxError`, which names the path and the status only. `ChangeFollower` logs these.

The relay has no database secret and refuses to start with one
(`settings.RelaySettings.check_environment`). ``/healthz``, ``/readyz`` (`Relay.ready`) and
``/metrics`` on `process.HEALTH_PORT`.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from typing import Any, Final, Literal
from urllib.parse import quote

import httpx
from prometheus_client import Counter, Gauge
from pydantic import TypeAdapter, ValidationError

from scadbuddy.auth.service_auth import ServiceAuth, exchange_source
from scadbuddy.camera.feed import RETRY_MAX, RETRY_MIN, FeedSender
from scadbuddy.camera.follow import ChangeFollower
from scadbuddy.camera.mediamtx import MediaMtx, MediaMtxError
from scadbuddy.camera.mediamtx import why as _why
from scadbuddy.camera.models import CameraSourceWithCode, FeedState
from scadbuddy.camera.process import (
    HEALTH_PORT,
    HOOK_HOST,
    HOOK_PORT,
    healthy,
    main_with,
    serve_health,
    serve_local,
)
from scadbuddy.camera.settings import RelaySettings
from scadbuddy.crud.client import CrudClient, CrudRefusedError, wait_for_areas
from scadbuddy.printers.address import UnsafeAddressError, lan_address

logger = logging.getLogger(__name__)

BASE: Final = "/api/v1/internal/crud/camera-relay"
AREAS: Final = frozenset({"printers", "changes"})
HOOK_URL: Final = f"http://{HOOK_HOST}:{HOOK_PORT}/hook"
#: The user name of a Bambu printer's camera.
RELAY_USER: Final = "bblp"

HookEvent = Literal["ready", "not_ready"]

RECONNECTS = Counter(
    "scadbuddy_camera_relay_reconnects_total",
    "Times the relay's pull of a printer became ready again after it was not ready.",
    ["printer"],
)
PULLING = Gauge(
    "scadbuddy_camera_relay_pulling",
    "1 while the relay's pull of the printer is ready, 0 while it is being tried.",
    ["printer"],
)
SILENT_RESTARTS = Counter(
    "scadbuddy_camera_relay_silent_restarts_total",
    "Times the relay restarted a ready pull of a printer because no bytes came for a while.",
    ["printer"],
)
#: A ready pull that gets no bytes for this long is restarted (#8081). The printer's camera
#: sends about 3.5 MB in 10 s when it works (measured on prod's H2C, 2026-11-27 UTC), so
#: 30 s of nothing is a stuck source, not a slow one.
SILENT_SECONDS: Final = 30.0
#: After each restart that brings no bytes, the next wait doubles, up to this.
SILENT_MAX: Final = 300.0
#: How often `run` calls `Relay.check_silence`.
SILENT_TICK: Final = 5.0
_SOURCES = TypeAdapter(list[dict[str, Any]])


class SourceError(Exception):
    """The API sent a source the relay cannot read. It names fields, never values."""


class SourceRefusedError(SourceError):
    """The source itself is refused: its fields do not validate, or its host is not on the
    local network. Trying it again gives the same answer until the API's source changes."""


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
        raise SourceRefusedError(
            f"the API sent a camera source for {name} the relay cannot read: {', '.join(fields)}"
        ) from None


async def _dialable(source: CameraSourceWithCode) -> CameraSourceWithCode:
    """``source`` with its host replaced by the address `lan_address` checked, the same
    check the LAN client runs at every connect: MediaMTX dials that address, never a name
    that could resolve differently later. The error names the printer, never the host."""
    try:
        address = await lan_address(source.host)
    except UnsafeAddressError:
        raise SourceRefusedError(
            f"the camera source for {source.printer_id} is not on the local network"
        ) from None
    except OSError:
        raise SourceError(
            f"the camera source host for {source.printer_id} did not resolve"
        ) from None
    return source.model_copy(update={"host": address})


class Relay:
    def __init__(
        self,
        crud: CrudClient,
        mtx: MediaMtx,
        *,
        source_for: Callable[[CameraSourceWithCode], str] = rtsps_source,
        retry_min: float = RETRY_MIN,
        retry_max: float = RETRY_MAX,
        hook_url: str = HOOK_URL,
    ) -> None:
        self._crud = crud
        self._mtx = mtx
        self._source_for = source_for
        self._hook_url = hook_url
        #: The paths the relay holds in MediaMTX; `None` until it first lists them.
        self._held: set[str] | None = None
        #: The conf the relay last put for each path. MediaMTX lists a source with its
        #: user and password redacted (``rtsps://<redacted>:<redacted>@…``, measured
        #: 2026-11-24 UTC on bluenviron/mediamtx:1, v1.21.4, with ``GET
        #: /v3/config/paths/list``), so the relay compares with its own copy. After a
        #: restart it has none, and puts each path once again.
        self._confs: dict[str, dict[str, Any]] = {}
        self._feed = FeedSender(crud, BASE, "relay", retry_min=retry_min, retry_max=retry_max)
        self._lock = asyncio.Lock()
        #: Each held path's last pull state, and the paths that were ready once, for the
        #: metrics.
        self._pull: dict[str, FeedState] = {}
        self._was_ready: set[str] = set()
        #: A full reconcile has put the API's paths in MediaMTX (`ready`).
        self._synced = False
        #: Each ready path's last ``bytesReceived`` and when it last changed, and how long
        #: a silence must last before `check_silence` restarts the pull.
        self._heard: dict[str, tuple[int, float]] = {}
        self._silent_after: dict[str, float] = {}
        #: The paths a failed silent restart deleted and did not put back: each
        #: `check_silence` reconciles them until that succeeds.
        self._lost: set[str] = set()

    def wanted(self) -> set[str]:
        """The paths the relay has put in MediaMTX and not removed: each one must be in
        MediaMTX's runtime list (`process.healthy`)."""
        return set(self._confs)

    def ready(self) -> bool:
        """``/readyz``: a full reconcile has put the API's paths in MediaMTX. A pull need
        not run: a printer that is off must not take the relay out of its Service."""
        return self._synced

    def _conf(self, source: CameraSourceWithCode) -> dict[str, Any]:
        return {
            "source": self._source_for(source),
            "sourceFingerprint": source.cert_sha256.lower(),
            "sourceOnDemand": False,
            # MediaMTX expands $MTX_PATH itself, with no shell (measured 2026-11-24 UTC on
            # v1.21.4). The older runOnReady/runOnNotReady are deprecated in favour of these.
            "runOnAvailable": f"curl -fsS -X POST {self._hook_url}/$MTX_PATH/ready",
            "runOnUnavailable": f"curl -fsS -X POST {self._hook_url}/$MTX_PATH/not_ready",
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

    async def _remove(self, name: str, present: bool, report: FeedState = "stopped") -> None:
        """Delete the path when MediaMTX has it, and report ``stopped``: also when a
        delete went through earlier but its report did not. A restart that puts the path
        back at once reports ``retrying`` instead (``report``): the printer is still
        recorded."""
        if present:
            try:
                await self._mtx.delete_path(name)
            except httpx.HTTPError as error:
                raise MediaMtxError(f"MediaMTX did not delete path {name}: {_why(error)}") from None
            self._confs.pop(name, None)
            if self._held is not None:
                self._held.discard(name)
        if report != "stopped":
            if present:
                self._want(name, report)
        elif present or self._feed.desired(name) not in (None, "stopped"):
            self._want(name, "stopped")

    def _want(self, printer_id: str, state: FeedState) -> None:
        self._feed.want(printer_id, state)
        if state == "stopped":
            self._pull.pop(printer_id, None)
            self._was_ready.discard(printer_id)
            for metric in (PULLING, RECONNECTS, SILENT_RESTARTS):
                with suppress(KeyError):
                    metric.remove(printer_id)
            return
        if state == "pulling":
            if self._pull.get(printer_id) == "retrying" and printer_id in self._was_ready:
                RECONNECTS.labels(printer_id).inc()
            self._was_ready.add(printer_id)
        self._pull[printer_id] = state
        PULLING.labels(printer_id).set(1 if state == "pulling" else 0)

    async def flushed(self) -> None:
        """Wait until every desired feed state has landed."""
        await self._feed.flushed()

    async def aclose(self) -> None:
        """Stop the feed senders."""
        await self._feed.aclose()

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
            refused: set[str] = set()
            for item in items:
                try:
                    parsed = _parse(item)
                except SourceError as error:
                    unreadable.append(error)
                    continue
                try:
                    sources[parsed.printer_id] = await _dialable(parsed)
                except SourceError as error:
                    unreadable.append(error)
                    refused.add(parsed.printer_id)
            existing = await self._existing()
            self._held = set(existing)
            kept = {str(item.get("printer_id")) for item in items} - refused
            for name in sorted((set(existing) | self._feed.printers()) - kept):
                await self._remove(name, name in existing)
            for printer_id, parsed in sources.items():
                await self._put(printer_id, self._conf(parsed), printer_id in existing)
            self._synced = True
            await self._runtime()
            if unreadable:
                raise SourceError("; ".join(str(error) for error in unreadable))

    async def reconcile(self, printer_id: str) -> None:
        """Bring one printer's path up to date: add or replace it, or remove it when the API
        answers 404 ``not_recorded``."""
        async with self._lock:
            await self._reconcile_locked(printer_id)

    async def restart(self, printer_id: str, *, report: FeedState = "stopped") -> None:
        """Drop one printer's MediaMTX path, then bring it up again, so MediaMTX dials the
        printer afresh (``camera.reconnect``). One hold of the lock, as `reconcile`.
        ``report`` is the state reported between the two (`_remove`)."""
        async with self._lock:
            existing = await self._existing()
            self._held = set(existing)
            await self._remove(printer_id, printer_id in existing, report)
            await self._reconcile_locked(printer_id)

    async def _reconcile_locked(self, printer_id: str) -> None:
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
        existing = await self._existing()
        self._held = set(existing)
        try:
            parsed = _parse(item)
        except SourceRefusedError:
            # With no path in MediaMTX (a silent restart deleted it), the feed reports
            # `stopped`, as for a host off the LAN. A path MediaMTX still has is left.
            if printer_id not in existing:
                await self._remove(printer_id, False)
            raise
        try:
            parsed = await _dialable(parsed)
        except SourceError:
            await self._remove(printer_id, printer_id in existing)
            raise
        await self._put(printer_id, self._conf(parsed), printer_id in existing)

    async def check_silence(self, now: float) -> None:
        """Restart each ready pull whose ``bytesReceived`` has not changed for the path's
        wait (`SILENT_SECONDS` at first, doubled after each restart up to `SILENT_MAX`, and
        back to `SILENT_SECONDS` once bytes come). ``now`` is a monotonic time.

        The read runs outside the lock: a `reconcile_all` that replaces a path between the
        read and the restart makes the relay restart a fresh pull once, which costs a few
        seconds. A restart that fails after its delete (the CRUD read or the put) leaves the
        path out of MediaMTX: the name goes in `_lost`, and each later call reconciles it
        until that succeeds or the source is refused (`SourceRefusedError`). A refused name
        leaves `_lost`; only a resync, or a change to that printer, puts it back. The
        change follower knows nothing of these restarts, so it would not resync."""
        for name in sorted(self._lost):
            try:
                await self.reconcile(name)
            except SourceRefusedError as error:
                # The source does not validate or is off the LAN, so each try gives the
                # same answer. A host that does not resolve, or a body that does not
                # decode, can pass on a later try: those stay in `_lost`.
                self._lost.discard(name)
                logger.warning("camera relay: %s is not put back: %s", name, error)
            except Exception as error:
                logger.warning(
                    "camera relay: %s is still not back after a failed restart (%s: %s)",
                    name,
                    type(error).__name__,
                    error,
                )
            else:
                self._lost.discard(name)
        try:
            received = await self._mtx.received()
        except httpx.HTTPError as error:
            raise MediaMtxError(f"MediaMTX did not list its running paths: {_why(error)}") from None
        held = set(self._held or ())
        for name in set(self._heard) - (held & set(received)):
            del self._heard[name]
        # A lost path keeps its wait: it is silent still when it comes back.
        for name in set(self._silent_after) - held - self._lost:
            del self._silent_after[name]
        silent: list[str] = []
        for name in sorted(held & set(received)):
            last = self._heard.get(name)
            if last is None or received[name] != last[0]:
                if last is not None:
                    self._silent_after.pop(name, None)
                self._heard[name] = (received[name], now)
            elif now - last[1] >= self._silent_after.get(name, SILENT_SECONDS):
                silent.append(name)
        for name in silent:
            wait = self._silent_after.get(name, SILENT_SECONDS)
            logger.warning(
                "camera relay: the pull of %s is ready but got no bytes for %.0f s; restarting it",
                name,
                now - self._heard[name][1],
            )
            SILENT_RESTARTS.labels(name).inc()
            self._silent_after[name] = min(wait * 2, SILENT_MAX)
            del self._heard[name]
            try:
                await self.restart(name, report="retrying")
            except Exception as error:
                # The other printers still restart; the next call puts this one back.
                self._lost.add(name)
                logger.warning(
                    "camera relay: restart of %s failed (%s: %s)", name, type(error).__name__, error
                )

    async def on_hook(self, printer_id: str, event: HookEvent) -> None:
        """MediaMTX's pull of ``printer_id`` became ready or not ready. A hook for a path the
        relay no longer holds (a late one after a removal) reports nothing."""
        async with self._lock:
            if self._held is None or printer_id not in self._held:
                return
            self._want(printer_id, "pulling" if event == "ready" else "retrying")


async def serve_hooks(relay: Relay, host: str, port: int) -> asyncio.Server:
    """``POST /hook/<printer_id>/ready`` (or ``not_ready``), from MediaMTX's ``runOnAvailable``
    and ``runOnUnavailable`` commands: 204, or 404 for any other request."""

    def route(method: str, target: str) -> Callable[[], Awaitable[None]] | None:
        parts = target.split("/")
        if method != "POST" or len(parts) != 4 or parts[1] != "hook" or not parts[2]:
            return None
        event = parts[3]
        if event not in ("ready", "not_ready"):
            return None
        return lambda: relay.on_hook(parts[2], event)  # type: ignore[arg-type]

    return await serve_local(route, host, port)


async def _watch_silence(relay: Relay, stop: asyncio.Event) -> None:
    """Call `Relay.check_silence` every `SILENT_TICK` until ``stop``. A failed check is
    logged, and the next tick tries again."""
    while not stop.is_set():
        try:
            await relay.check_silence(time.monotonic())
        except Exception as error:
            logger.warning(
                "camera relay: silence check failed (%s: %s)", type(error).__name__, error
            )
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), SILENT_TICK)


async def run(settings: RelaySettings, stop: asyncio.Event | None = None) -> None:
    stop = stop or asyncio.Event()
    auth = ServiceAuth(exchange_source("camera-relay", settings))
    crud = CrudClient(settings.api_internal_url, "camera-relay", auth)
    mtx = MediaMtx(settings.camera_mediamtx_api_url)
    relay = Relay(crud, mtx, source_for=rtsps_source)
    hooks: asyncio.Server | None = None
    follower: asyncio.Task[None] | None = None
    watcher: asyncio.Task[None] | None = None

    async def alive() -> bool:
        return await healthy(mtx, follower, relay.wanted())

    async def ready() -> bool:
        return follower is not None and relay.ready() and await alive()

    health = await serve_health(HEALTH_PORT, alive, ready)
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
            crud,
            f"{BASE}/changes",
            relay.reconcile,
            relay.reconcile_all,
            resync_first=True,
            on_reconnect=relay.restart,
        )
        follower = asyncio.create_task(follow.run())
        watcher = asyncio.create_task(_watch_silence(relay, stop))
        logger.info("camera relay running")
        await stop.wait()
    finally:
        for task in (follower, watcher):
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        # The hooks first, so no hook starts a sender after the senders stop.
        if hooks is not None:
            hooks.close()
        await relay.aclose()
        health.close()
        await mtx.aclose()
        await crud.aclose()


def main(argv: Sequence[str] | None = None) -> int:
    return main_with(
        RelaySettings.check_environment,
        lambda: RelaySettings(),  # type: ignore[call-arg]  # read from the environment
        lambda settings: settings.log_level,
        run,
    )


if __name__ == "__main__":
    sys.exit(main())
