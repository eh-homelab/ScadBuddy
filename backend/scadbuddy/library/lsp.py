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
frontend's URI scheme. Anything outside the directory — a library on
``OPENSCADPATH`` — passes through as the server named it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
from starlette import status
from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

logger = logging.getLogger(__name__)

_CONTENT_LENGTH = b"content-length"


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
    client: str
    server: str

    def inbound(self, value: Any) -> Any:
        return _rewrite(value, self.client, self.server)

    def outbound(self, value: Any) -> Any:
        return _rewrite(value, self.server, self.client)


def _rewrite(value: Any, old: str, new: str) -> Any:
    """Every string under ``old`` moved under ``new``; ``old`` ends in a slash, so a
    sibling directory that merely shares a prefix is left alone."""
    if isinstance(value, str):
        if value.startswith(old):
            return new + value[len(old) :]
        if value == old.rstrip("/"):
            return new.rstrip("/")
        return value
    if isinstance(value, list):
        return [_rewrite(item, old, new) for item in value]
    if isinstance(value, dict):
        return {key: _rewrite(item, old, new) for key, item in value.items()}
    return value


def _directory_uri(uri: str) -> str:
    return uri if uri.endswith("/") else uri + "/"


async def serve(websocket: WebSocket, binary: str, root: Path, env: Mapping[str, str]) -> None:
    """Run one openscad-lsp in ``root`` for an accepted socket, until either side ends.

    The server lives exactly as long as the socket: closing the editor kills it, and a
    server that exits closes the editor's socket.
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
    roots: _Roots | None = None

    async def to_server() -> None:
        nonlocal roots
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
            if roots is None and message.get("method") == "initialize":
                params = message.setdefault("params", {})
                client_root = params.get("rootUri")
                if client_root:
                    roots = _Roots(_directory_uri(client_root), server_root)
                else:
                    params["rootUri"] = server_root
            if roots is not None:
                message = roots.inbound(message)
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
            if roots is not None:
                message = roots.outbound(message)
            await websocket.send_text(json.dumps(message))

    try:
        async with anyio.create_task_group() as pumps:

            async def pump(direction: Callable[[], Awaitable[None]]) -> None:
                await direction()
                # Whichever side ends first ends the session.
                pumps.cancel_scope.cancel()

            pumps.start_soon(pump, to_server)
            pumps.start_soon(pump, to_client)
    finally:
        # Shielded: the handler can be cancelled while this runs (a server shutting
        # down, the test client), and an interrupted cleanup leaves the process behind.
        with anyio.CancelScope(shield=True):
            if process.returncode is None:
                process.kill()
            await process.wait()
            # Anything but our own kill means it went on its own: say so, or a server
            # that crashes on every session is invisible.
            if process.returncode not in (0, -signal.SIGKILL):
                logger.warning("openscad-lsp exited with status %d", process.returncode)
            if (
                websocket.client_state == WebSocketState.CONNECTED
                and websocket.application_state == WebSocketState.CONNECTED
            ):
                await websocket.close()
