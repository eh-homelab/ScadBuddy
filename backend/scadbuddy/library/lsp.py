"""The editor's language server: openscad-lsp on stdio, bridged to a WebSocket.

The browser speaks JSON-RPC over the socket, one message per frame; openscad-lsp
speaks the same messages on stdio behind LSP's ``Content-Length`` headers. The
bridge reframes them and does one other thing: it translates file URIs.

The editor names a model by the URI it opened it under (``file:///models/<slug>/``),
which is not a path on this machine. openscad-lsp resolves ``include``/``use`` beside
the file and reports definitions by real path, so every string that starts with the
client's root is rewritten to the model's real directory on the way in, and every
string under that directory is rewritten back on the way out. The client's root is
whatever ``rootUri`` its ``initialize`` names, so the bridge carries no copy of the
frontend's URI scheme. Until that arrives, and for a client that names none, the root
is ``DEFAULT_CLIENT_ROOT``, so no message ever shows the browser a path on this
machine.

The libraries the model pins (#93) are on the server's ``OPENSCADPATH``, and each one
is given a client URI of its own the same way: ``LIBRARY_CLIENT_ROOT`` +
``<name>@<commit>/`` stands for the library's directory in the checkout the model pins,
so a definition in BOSL2 reaches the editor as
``file:///libraries/BOSL2@<commit>/shapes3d.scad`` (#185), which the editor reads back
through ``GET /models/{slug}/libraries/{name}/files/{path}?commit=<commit>``. The
commit is in the URI because a checkout never changes under its commit, so a file the
editor already holds under that URI is still the right one, and one from before a
re-pin never stands in for the new pin's (a library name alone does not say which
checkout; two models can pin one at two commits). Anything outside the
model's directory and those libraries (openscad-lsp's own default library locations)
passes through as the server named it.

A server that stops answering (alive, but wedged) would hold its session's permit for
as long as the editor stays open, so a request left unanswered for
``REQUEST_TIMEOUT`` seconds ends the session: the server is killed and the socket
closed with 1011, and the editor carries on without one as it does on any failure.
This assumes the server answers every request, which openscad-lsp 2.0.1 does only for
the capabilities it advertises, on documents the editor has opened: an unknown method,
params it cannot read, or a URI it never saw gets no reply at all. The editor
(``frontend/src/lib/languageClient.ts``) keeps to that; a request outside it would
end a healthy session.

A killed server is given ``KILL_WAIT`` seconds to be reaped, then its permit is let go
whether or not it has died. That is deliberate: a process stuck in the kernel (a hung
filesystem) cannot be killed, and holding the permit for it wedges the editor all the
same. The cost is that such processes are not counted against ``SCADBUDDY_LSP_SESSIONS``,
so each one is logged with how many are still unreaped.
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
from starlette import status
from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

logger = logging.getLogger(__name__)

_CONTENT_LENGTH = b"content-length"

#: The client root the server's paths are shown under before, or without, one named
#: by the client's ``initialize``.
DEFAULT_CLIENT_ROOT = "file:///workspace/"
#: Where the model's pinned libraries are shown: ``<this><name>/...``.
LIBRARY_CLIENT_ROOT = "file:///libraries/"
#: Seconds a client's request may go unanswered before the server is taken as wedged.
#: The watchdog checks every tenth of it, so detection takes up to 1.1x this. Only
#: requests openscad-lsp answers may be sent (see the module docstring).
REQUEST_TIMEOUT = 60.0
#: Seconds to wait for a killed server to be reaped before letting the permit go;
#: asyncio's child watcher reaps it whenever it does die.
KILL_WAIT = 5.0

#: Killed servers that outlived ``KILL_WAIT``, each with the task still waiting on it;
#: the task drops its server out once asyncio reaps it.
_unreaped: dict[asyncio.subprocess.Process, asyncio.Task[int]] = {}


def frame(body: bytes) -> bytes:
    """One LSP message on the wire. The length is in bytes, not characters."""
    return b"Content-Length: %d\r\n\r\n" % len(body) + body


async def read_message(reader: asyncio.StreamReader) -> bytes | None:
    """The next message body, or None once the server has closed its stdout."""
    length = 0
    while True:
        line = await reader.readline()
        if not line:
            return None
        if not line.strip():
            break
        name, _, value = line.partition(b":")
        if name.strip().lower() == _CONTENT_LENGTH:
            length = int(value)
    return await reader.readexactly(length)


@dataclass(frozen=True)
class _Roots:
    """(client, server) directory URIs, each ending in a slash; the first root a
    string is under wins. The libraries come first: on disk they are apart from the
    model's directory, and on the client their roots are the more specific, so a
    client root as broad as ``file:///`` still leaves them alone."""

    pairs: tuple[tuple[str, str], ...]

    def inbound(self, value: Any) -> Any:
        return _rewrite(value, self.pairs)

    def outbound(self, value: Any) -> Any:
        return _rewrite(value, tuple((server, client) for client, server in self.pairs))


def _rewrite(value: Any, pairs: Sequence[tuple[str, str]]) -> Any:
    """Every string under one of the ``old`` roots moved under its ``new``; each
    ``old`` ends in a slash, so a sibling directory that merely shares a prefix is
    left alone."""
    if isinstance(value, str):
        for old, new in pairs:
            if value.startswith(old):
                return new + value[len(old) :]
            if value == old.rstrip("/"):
                return new.rstrip("/")
        return value
    if isinstance(value, list):
        return [_rewrite(item, pairs) for item in value]
    if isinstance(value, dict):
        return {key: _rewrite(item, pairs) for key, item in value.items()}
    return value


def library_roots(libraries: Mapping[str, Path]) -> tuple[tuple[str, str], ...]:
    """(client, server) for each library: ``name`` -> the directory ``use
    <name/...>`` resolves into, ``<libraries>/<name>/<commit>/<name>``, shown as
    ``LIBRARY_CLIENT_ROOT`` + ``<name>@<commit>/``."""
    return tuple(
        (f"{LIBRARY_CLIENT_ROOT}{name}@{directory.parent.name}/", directory.as_uri() + "/")
        for name, directory in sorted(libraries.items())
    )


def _directory_uri(uri: str) -> str:
    return uri if uri.endswith("/") else uri + "/"


async def serve(
    websocket: WebSocket,
    binary: str,
    root: Path,
    env: Mapping[str, str],
    libraries: Mapping[str, Path] | None = None,
) -> None:
    """Run one openscad-lsp in ``root`` for an accepted socket, until either side ends.

    ``libraries`` names each library on ``env``'s ``OPENSCADPATH`` and the directory
    its files are in (``<libraries>/<name>/<commit>/<name>``), for the client URIs they
    are shown under.

    The server lives exactly as long as the socket: closing the editor kills it, and a
    server that exits, or leaves a request unanswered for ``REQUEST_TIMEOUT``, closes
    the editor's socket.
    """
    process = await asyncio.create_subprocess_exec(
        binary,
        "--stdio",
        cwd=root,
        env=dict(env),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    assert process.stdin is not None and process.stdout is not None
    stdin, stdout = process.stdin, process.stdout
    server_root = root.as_uri() + "/"
    libraries_at = library_roots(libraries or {})
    roots = _Roots((*libraries_at, (DEFAULT_CLIENT_ROOT, server_root)))
    initialized = False
    # The client's requests the server has yet to answer, by id: when each was sent,
    # oldest first. A list, so a client that reuses an id still has each one watched.
    unanswered: dict[int | str, list[float]] = {}
    close_code = status.WS_1000_NORMAL_CLOSURE

    async def to_server() -> None:
        nonlocal roots, initialized
        while True:
            try:
                text = await websocket.receive_text()
            except WebSocketDisconnect:
                return
            try:
                message = json.loads(text)
            except json.JSONDecodeError:
                message = None
            # Every JSON-RPC message is an object; the socket has no auth, so anything
            # else is refused by code rather than raised out of the bridge.
            if not isinstance(message, dict):
                await websocket.close(code=status.WS_1007_INVALID_FRAME_PAYLOAD_DATA)
                return
            if not initialized and message.get("method") == "initialize":
                initialized = True
                params = message.setdefault("params", {})
                client_root = params.get("rootUri")
                if client_root:
                    roots = _Roots((*libraries_at, (_directory_uri(client_root), server_root)))
                else:
                    # Rewritten to the server root below, like any client path.
                    params["rootUri"] = DEFAULT_CLIENT_ROOT
            message = roots.inbound(message)
            request_id = message.get("id")
            if "method" in message and isinstance(request_id, int | str):
                unanswered.setdefault(request_id, []).append(time.monotonic())
            try:
                stdin.write(frame(json.dumps(message).encode()))
                await stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                # The server went away mid-session; ending here closes the socket.
                return

    async def to_client() -> None:
        while True:
            try:
                body = await read_message(stdout)
                if body is None:
                    return
                message = json.loads(body)
            except (ValueError, asyncio.IncompleteReadError) as error:
                # A bad Content-Length, a truncated body or one that is not JSON: the
                # stream cannot be resynchronised, so the session ends like any other
                # server failure rather than raising out of the bridge.
                logger.warning("openscad-lsp sent an unreadable message: %s", error)
                return
            if not isinstance(message, dict):
                logger.warning("openscad-lsp sent an unreadable message: not a JSON object")
                return
            request_id = message.get("id")
            if "method" not in message and isinstance(request_id, int | str):
                sent = unanswered.get(request_id)
                if sent:
                    sent.pop(0)
                    if not sent:
                        del unanswered[request_id]
            await websocket.send_text(json.dumps(roots.outbound(message)))

    async def watchdog() -> None:
        nonlocal close_code
        while True:
            await anyio.sleep(REQUEST_TIMEOUT / 10)
            if (
                unanswered
                and time.monotonic() - min(sent[0] for sent in unanswered.values())
                > REQUEST_TIMEOUT
            ):
                logger.warning(
                    "openscad-lsp left a request unanswered for %gs; ending the session",
                    REQUEST_TIMEOUT,
                )
                close_code = status.WS_1011_INTERNAL_ERROR
                return

    try:
        async with anyio.create_task_group() as pumps:

            async def pump(direction: Callable[[], Awaitable[None]]) -> None:
                await direction()
                # Whichever side ends first ends the session.
                pumps.cancel_scope.cancel()

            pumps.start_soon(pump, to_server)
            pumps.start_soon(pump, to_client)
            pumps.start_soon(pump, watchdog)
    finally:
        # Shielded: the handler can be cancelled while this runs (a server shutting
        # down, the test client), and an interrupted cleanup leaves the process behind.
        with anyio.CancelScope(shield=True):
            if process.returncode is None:
                process.kill()
            with anyio.move_on_after(KILL_WAIT) as waiting:
                await process.wait()
            if waiting.cancelled_caught:
                # Stuck in the kernel: holding the permit for it would be the wedge
                # all over again, so it is let go and counted instead.
                reaper = asyncio.ensure_future(process.wait())
                reaper.add_done_callback(lambda _: _unreaped.pop(process, None))
                _unreaped[process] = reaper
                logger.warning(
                    "killed openscad-lsp (pid %d) was not reaped within %gs; "
                    "%d killed server(s) not yet reaped",
                    process.pid,
                    KILL_WAIT,
                    len(_unreaped),
                )
            # Anything but our own kill means it went on its own: say so, or a server
            # that crashes on every session is invisible.
            if process.returncode not in (None, 0, -signal.SIGKILL):
                logger.warning("openscad-lsp exited with status %d", process.returncode)
            if (
                websocket.client_state == WebSocketState.CONNECTED
                and websocket.application_state == WebSocketState.CONNECTED
            ):
                await websocket.close(code=close_code)
