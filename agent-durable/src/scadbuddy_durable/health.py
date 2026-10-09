"""GET /healthz, the one port this container opens (spec 2026-10-01 §6.3a)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable


async def serve_health(
    host: str, port: int, status: Callable[[], dict[str, str]]
) -> asyncio.Server:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await asyncio.wait_for(reader.readline(), timeout=5)
            parts = request.decode("latin-1").split()
            if len(parts) >= 2 and parts[0] == "GET" and parts[1] == "/healthz":
                body = json.dumps(status()).encode()
                head = "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            else:
                body = b'{"error":"not found"}'
                head = "HTTP/1.1 404 Not Found\r\nContent-Type: application/json\r\n"
            writer.write(
                f"{head}Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body
            )
            await writer.drain()
        except (TimeoutError, ConnectionError):
            pass
        finally:
            writer.close()

    return await asyncio.start_server(handle, host, port)
