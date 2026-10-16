import asyncio
import json

from scadbuddy_durable.health import serve_health


async def _get(port: int, path: str) -> tuple[str, str]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    await writer.drain()
    raw = (await reader.read()).decode()
    writer.close()
    head, _, body = raw.partition("\r\n\r\n")
    return head.splitlines()[0], body


async def test_healthz_answers_the_status() -> None:
    server = await serve_health("127.0.0.1", 0, lambda: {"status": "ok", "worker": "not started"})
    port = server.sockets[0].getsockname()[1]
    try:
        line, body = await _get(port, "/healthz")
        assert line == "HTTP/1.1 200 OK"
        assert json.loads(body) == {"status": "ok", "worker": "not started"}
        line, _ = await _get(port, "/other")
        assert line == "HTTP/1.1 404 Not Found"
    finally:
        server.close()
        await server.wait_closed()
