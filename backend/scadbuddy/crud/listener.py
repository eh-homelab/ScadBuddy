"""The CRUD listener (#5298, plan D3): a second port, ``SCADBUDDY_CRUD_PORT``, served by
the API process on the same app, that answers only the CRUD routes, the service-token
exchange (which stays on the main port too) and ``/healthz``. Every other path is a
plain 404 there, so a pod admitted only to this port (the render worker, whose pod runs
template Python until #5296) reaches nothing else.

The lifespan starts it (`start`) once the app is ready and stops it (`stop`) first on
the way out, so the image's one ``uvicorn`` command serves both ports.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import posixpath
from collections.abc import Iterator
from typing import Final

import uvicorn
from fastapi import FastAPI
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from scadbuddy.crud.wire import PREFIX

logger = logging.getLogger(__name__)

CRUD_PATH_PREFIX: Final = f"/api/v1{PREFIX}/"
EXACT_PATHS: Final = frozenset({"/api/v1/auth/service-token", "/healthz"})
#: Where the lifespan keeps the running listener, on ``app.state``.
STATE_ATTR: Final = "crud_listener"
#: The interface it binds, as the image's main port.
HOST: Final = "0.0.0.0"  # the pod's port; the NetworkPolicy admits callers
#: How long `stop` lets in-flight CRUD requests finish. The API pod sets no
#: terminationGracePeriodSeconds (clusters `applications/scadbuddy/scadbuddy.yaml`), so
#: Kubernetes' default 30 s applies; 20 s leaves the rest of the lifespan's shutdown room.
GRACEFUL_SHUTDOWN: Final = 20


def serves(path: str) -> bool:
    """Whether the CRUD listener passes ``path`` to the app. A path that is not already
    normal (``..``, ``//``, a trailing ``/``) is refused, whatever it would resolve to."""
    if posixpath.normpath(path) != path:
        return False
    return path in EXACT_PATHS or path.startswith(CRUD_PATH_PREFIX)


class CrudOnly:
    """The ASGI path filter in front of the app on the CRUD listener."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and serves(scope["path"]):
            await self.app(scope, receive, send)
            return
        await PlainTextResponse("Not Found", status_code=404)(scope, receive, send)


class _Server(uvicorn.Server):
    """A server that leaves the process's signals to the main one: it is stopped by the
    lifespan, which the main server's own shutdown runs."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


class CrudListenerError(RuntimeError):
    """The CRUD listener could not start (its port is taken, say)."""


async def start(app: FastAPI, port: int) -> None:
    """Serve ``app`` behind :class:`CrudOnly` on ``port`` (0: any free port), until
    `stop`. Returns once it listens; raises :class:`CrudListenerError` if it cannot."""
    config = uvicorn.Config(
        CrudOnly(app),
        host=HOST,
        port=port,
        lifespan="off",
        # As the image's main port (Dockerfile CMD): `core/proxies.py` decides who is
        # believed about a forwarded client, never uvicorn.
        proxy_headers=False,
        # No CRUD route is a WebSocket; uvicorn answers an upgrade without the app.
        ws="none",
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN,
        # The app's logging stays as `configure_logging` set it.
        log_config=None,
    )
    server = _Server(config)

    async def serve() -> None:
        try:
            await server.serve()
        except SystemExit as exc:  # uvicorn exits when it cannot bind
            raise CrudListenerError(f"the CRUD listener could not listen on {port}") from exc

    task = asyncio.create_task(serve(), name="crud listener")
    while not server.started:
        if task.done():
            task.result()
            raise CrudListenerError("the CRUD listener stopped before it listened")
        await asyncio.sleep(0.01)

    def ended(done: asyncio.Task[None]) -> None:
        if getattr(app.state, STATE_ATTR, None) is None:
            return  # `stop` asked; it reports what the task raised
        error = None if done.cancelled() else done.exception()
        logger.error(
            "the CRUD listener on %s stopped while the API runs",
            port,
            exc_info=error,
        )

    task.add_done_callback(ended)
    setattr(app.state, STATE_ATTR, (server, task))


async def stop(app: FastAPI) -> None:
    """Stop the listener `start` started, if any, waiting for its requests to end. Never
    raises: a listener that failed is logged, so the rest of the shutdown still runs."""
    running: tuple[uvicorn.Server, asyncio.Task[None]] | None = getattr(app.state, STATE_ATTR, None)
    if running is None:
        return
    server, task = running
    setattr(app.state, STATE_ATTR, None)
    server.should_exit = True
    try:
        await task
    except Exception:
        logger.exception("the CRUD listener failed")


def crud_listener_port(app: FastAPI) -> int | None:
    """The port the running listener is bound to, or None."""
    running = getattr(app.state, STATE_ATTR, None)
    if running is None:
        return None
    server: uvicorn.Server = running[0]
    port: int = server.servers[0].sockets[0].getsockname()[1]
    return port
