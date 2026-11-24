"""The parts the camera relay and the camera recorder share as processes: the local hook
server that MediaMTX's commands call, ``/healthz`` and ``/metrics``, and ``main``."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from collections.abc import Awaitable, Callable, Collection, Mapping
from contextlib import suppress
from typing import Final

import httpx
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import ValidationError

from scadbuddy.camera.mediamtx import MediaMtx
from scadbuddy.camera.settings import CameraRefusal
from scadbuddy.core.logging import configure_logging
from scadbuddy.intake_worker.hardening import forbid_dumping

logger = logging.getLogger(__name__)

HOOK_HOST: Final = "127.0.0.1"
HOOK_PORT: Final = 9091
HEALTH_PORT: Final = 9090

#: A hook request's method and target (``/path?query``) to the action that handles it, or
#: ``None`` when no route takes the request.
Route = Callable[[str, str], Callable[[], Awaitable[None]] | None]


async def serve_local(route: Route, host: str, port: int) -> asyncio.Server:
    """A small HTTP server for MediaMTX's hook commands: 204 when ``route`` takes the
    request, also when its action fails (MediaMTX does not retry; the error's type is
    logged), else 404. Headers and body are read and ignored."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        status = b"404 Not Found"
        with suppress(Exception):
            line = await asyncio.wait_for(reader.readline(), 5)
            end = (b"\r\n", b"\n", b"")
            while await asyncio.wait_for(reader.readline(), 5) not in end:
                continue
            method, target, *_ = line.decode("ascii").split(" ")
            action = route(method, target)
            if action is not None:
                status = b"204 No Content"
                try:
                    await action()
                except Exception as error:
                    logger.warning("a camera hook failed (%s)", type(error).__name__)
        with suppress(Exception):
            writer.write(
                b"HTTP/1.1 " + status + b"\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
        writer.close()

    return await asyncio.start_server(handle, host, port)


#: How long one liveness check may take; a slower one answers 503.
CHECK_BUDGET: Final = 3.0

#: The liveness check behind ``/healthz``.
Check = Callable[[], Awaitable[bool]]


async def healthy(
    mtx: MediaMtx, follower: asyncio.Task[None] | None, wanted: Collection[str] = ()
) -> bool:
    """Alive when MediaMTX's control API answers, its runtime list holds every path the
    controller has put (``wanted``), and the change follower runs. A path is missing when
    MediaMTX's container restarted alone and lost them: the controller's restart puts them
    again. Before the follower starts (``None``: the process waits for the CRUD service, or
    reads its first list), the follower does not count: a restart cannot bring the API back."""
    try:
        running = await mtx.ready()
    except httpx.HTTPError:
        return False
    missing = set(wanted) - running.keys()
    if missing:
        logger.warning("MediaMTX lacks %d of the controller's paths", len(missing))
        return False
    return follower is None or not follower.done()


async def _alive(check: Check) -> bool:
    try:
        return await asyncio.wait_for(check(), CHECK_BUDGET)
    except Exception:
        return False


async def serve_health(port: int, alive: Check) -> asyncio.Server:
    """``GET /metrics``: the process's metrics. Any other request: 200 when ``alive``
    answers true within `CHECK_BUDGET`, else 503, so a liveness probe restarts a stuck
    pod. On every address."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        with suppress(Exception):
            line = await asyncio.wait_for(reader.readline(), 5)
            status = b"200 OK"
            if line.split(b" ")[1:2] == [b"/metrics"]:
                body, kind = generate_latest(), CONTENT_TYPE_LATEST.encode()
            elif await _alive(alive):
                body, kind = b"ok", b"text/plain"
            else:
                status, body, kind = b"503 Service Unavailable", b"unhealthy", b"text/plain"
            writer.write(
                b"HTTP/1.1 "
                + status
                + b"\r\nContent-Type: "
                + kind
                + b"\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + body
            )
            await writer.drain()
        writer.close()

    return await asyncio.start_server(handle, "0.0.0.0", port)


def main_with[S](
    check_environment: Callable[[Mapping[str, str]], None],
    load: Callable[[], S],
    log_level: Callable[[S], str],
    run: Callable[[S, asyncio.Event], Awaitable[None]],
) -> int:
    """Refuse a database secret, forbid dumps, read the settings without echoing a value,
    then ``run`` until SIGTERM or SIGINT. A `CameraRefusal` from ``run`` exits 2."""
    try:
        check_environment(os.environ)
    except CameraRefusal as refusal:
        configure_logging()
        for reason in refusal.reasons:
            logger.error(reason)
        return 2
    # Before the proof is read: no process of this user may read this one's memory, where
    # the service credential and the process's secrets live.
    forbid_dumping()
    try:
        settings = load()
    except ValidationError as invalid:
        configure_logging()
        for error in invalid.errors(include_input=False, include_url=False):
            name = ".".join(str(part) for part in error["loc"]) or "settings"
            logger.error("setting %s: %s", name, error["msg"])
        return 2
    configure_logging(log_level(settings))

    async def _main() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, stop.set)
        await run(settings, stop)

    try:
        asyncio.run(_main())
    except CameraRefusal as refusal:
        # A refusal found after the start, such as the recorder's clock check.
        for reason in refusal.reasons:
            logger.error(reason)
        return 2
    return 0
