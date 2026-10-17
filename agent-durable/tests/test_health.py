import asyncio
import json
import os
import re
import sys

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


async def test_the_sidecar_on_port_0_says_which_port_it_bound() -> None:
    """#3655: a test starts it on port 0 and reads the port from this line, instead of
    probing for a free one that something else can take before the sidecar binds it."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("SCADBUDDY_")}
    env["SCADBUDDY_DURABLE_HEALTH_PORT"] = "0"
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "scadbuddy_durable", env=env, stderr=asyncio.subprocess.PIPE
    )
    try:
        assert proc.stderr is not None
        stderr = proc.stderr

        async def named_port() -> int:
            while True:
                line = (await stderr.readline()).decode()
                assert line, "the sidecar exited before it named its port"
                if match := re.search(r"health on port (\d+)", line):
                    return int(match[1])

        port = await asyncio.wait_for(named_port(), timeout=30)
        status, body = await _get(port, "/healthz")
        assert status == "HTTP/1.1 200 OK"
        assert json.loads(body)["status"] == "ok"
    finally:
        proc.terminate()
        await proc.wait()
