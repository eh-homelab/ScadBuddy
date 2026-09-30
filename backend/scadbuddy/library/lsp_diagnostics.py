"""openscad-lsp's diagnostics for a source, as data (#252).

The editor gets these over the LSP socket (``library/lsp.py``). An agent has no
editor, so this runs the same server once over stdio, gives it the source and
returns what it publishes as a list.

Measured on the pinned openscad-lsp 2.0.1 (Dockerfile ``OPENSCAD_LSP_VERSION``;
``src/server/handler/notification.rs`` at
https://github.com/Leathong/openscad-LSP/blob/v2.0.1/src/server/handler/notification.rs):

- ``textDocument/publishDiagnostics`` is sent on ``didChange`` only. ``didOpen`` stores
  the text and publishes nothing. So the document is opened empty and the source
  arrives as one change inserted at 0:0.
- What it reports is tree-sitter's parse: ``syntax error`` for an error node and
  ``missing <kind>`` for a missing one, always severity 1 (error), with a range. It
  also reports ``file not found!`` for an ``include`` whose file is missing, but only
  when the change starts at that statement, which here means only when it is the
  first one. An unknown module or function is not reported: that is OpenSCAD's own
  check (``POST /models/check``).

So this is a fast parse check that needs no OpenSCAD run and gives columns as
well as lines, not a replacement for the check.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import anyio
from pydantic import BaseModel, Field

from scadbuddy.library.lsp import KILL_WAIT, frame, read_message, reap_later

#: How long the whole exchange may take. openscad-lsp answers in milliseconds; this
#: only bounds a server that has wedged.
DIAGNOSTICS_TIMEOUT = 10.0
#: The file name the document is opened as, in ``root``, so relative includes resolve
#: beside it as they do on render.
DOCUMENT_NAME = "model.scad"

Severity = Literal["error", "warning", "information", "hint"]
_SEVERITIES: dict[int, Severity] = {1: "error", 2: "warning", 3: "information", 4: "hint"}


class LspDiagnostic(BaseModel):
    """One LSP ``Diagnostic``, with 1-based lines and columns as an editor shows them
    (LSP's own are 0-based, https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#position)."""

    line: int = Field(ge=1)
    column: int = Field(ge=1)
    end_line: int = Field(ge=1)
    end_column: int = Field(ge=1)
    severity: Severity
    message: str


class LspDiagnosticsError(RuntimeError):
    """The server exited, sent something unreadable, or published nothing in time."""


def _diagnostic(raw: Any) -> LspDiagnostic | None:
    try:
        start, end = raw["range"]["start"], raw["range"]["end"]
        return LspDiagnostic(
            line=int(start["line"]) + 1,
            column=int(start["character"]) + 1,
            end_line=int(end["line"]) + 1,
            end_column=int(end["character"]) + 1,
            severity=_SEVERITIES.get(int(raw.get("severity", 1)), "error"),
            message=str(raw.get("message", "")),
        )
    except (KeyError, TypeError, ValueError):
        return None


async def lsp_diagnostics(
    binary: str,
    root: Path,
    source: str,
    *,
    env: Mapping[str, str],
    timeout: float | None = None,
) -> list[LspDiagnostic]:
    """What openscad-lsp publishes for ``source`` opened as ``root/model.scad``.

    The file on disk is never read or written: the server works from the text it is
    sent, and ``root`` only decides where includes resolve.
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
    uri = (root / DOCUMENT_NAME).as_uri()

    async def send(message: dict[str, Any]) -> None:
        stdin.write(frame(json.dumps({"jsonrpc": "2.0", **message}).encode()))
        await stdin.drain()

    async def exchange() -> list[LspDiagnostic]:
        await send(
            {
                "id": 1,
                "method": "initialize",
                "params": {"processId": None, "rootUri": root.as_uri(), "capabilities": {}},
            }
        )
        while True:
            body = await read_message(stdout)
            if body is None:
                raise EOFError("openscad-lsp exited before it answered initialize")
            if json.loads(body).get("id") == 1:
                break
        await send({"method": "initialized", "params": {}})
        await send(
            {
                "method": "textDocument/didOpen",
                "params": {
                    "textDocument": {"uri": uri, "languageId": "openscad", "version": 1, "text": ""}
                },
            }
        )
        origin = {"line": 0, "character": 0}
        await send(
            {
                "method": "textDocument/didChange",
                "params": {
                    "textDocument": {"uri": uri, "version": 2},
                    "contentChanges": [{"range": {"start": origin, "end": origin}, "text": source}],
                },
            }
        )
        while True:
            body = await read_message(stdout)
            if body is None:
                raise EOFError("openscad-lsp exited before it published diagnostics")
            message = json.loads(body)
            params = message.get("params") or {}
            if (
                message.get("method") == "textDocument/publishDiagnostics"
                and params.get("uri") == uri
            ):
                found = (_diagnostic(raw) for raw in params.get("diagnostics") or [])
                return sorted((d for d in found if d is not None), key=lambda d: (d.line, d.column))

    try:
        return await asyncio.wait_for(exchange(), timeout=timeout or DIAGNOSTICS_TIMEOUT)
    except TimeoutError:
        raise LspDiagnosticsError(
            f"openscad-lsp published nothing within {timeout or DIAGNOSTICS_TIMEOUT:g}s"
        ) from None
    except (EOFError, OSError, ValueError, asyncio.IncompleteReadError) as error:
        # ValueError covers a bad Content-Length and a body that is not JSON.
        raise LspDiagnosticsError(f"openscad-lsp failed: {error}") from None
    finally:
        # Shielded, as lsp.py `_serve` is: a request cancelled while this runs (a
        # client gone, a server shutting down) would otherwise leave the process
        # unreaped (#750 review).
        with anyio.CancelScope(shield=True):
            if process.returncode is None:
                process.kill()
            with anyio.move_on_after(KILL_WAIT) as waiting:
                await process.wait()
            if waiting.cancelled_caught:
                reap_later(process)
