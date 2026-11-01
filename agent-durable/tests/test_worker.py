"""The sidecar runs no worker (plan 2026-10-13 Step 8.1, spec §14.4): the Python turn is
retired, so with every variable the old worker needed (and no SCADBUDDY_DURABLE_TOOLS_JSON)
it connects to nothing, registers no workflow, and says why on /healthz and in its log."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path

from scadbuddy_durable.worker import NO_WORKER


async def _get(port: int, path: str) -> str:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    await writer.drain()
    raw = (await reader.read()).decode()
    writer.close()
    return raw.partition("\r\n\r\n")[2]


async def test_the_sidecar_runs_no_worker_and_says_why(tmp_path: Path) -> None:
    kek = tmp_path / "kek"
    kek.write_text("WlpaWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlo=")
    env = {k: v for k, v in os.environ.items() if not k.startswith("SCADBUDDY_")}
    env.update(
        {
            "SCADBUDDY_DURABLE_HEALTH_PORT": "0",
            "SCADBUDDY_DATABASE_URL": "postgresql://u:pw@127.0.0.1:1/db",
            # Nothing listens here: a worker that tried to connect would fail to start.
            "SCADBUDDY_TEMPORAL_ADDRESS": "127.0.0.1:1",
            "SCADBUDDY_SECRET_KEY_FILE": str(kek),
        }
    )
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "scadbuddy_durable", env=env, stderr=asyncio.subprocess.PIPE
    )
    try:
        assert proc.stderr is not None
        stderr = proc.stderr
        lines: list[str] = []

        async def named_port() -> int:
            while True:
                line = (await stderr.readline()).decode()
                assert line, "the sidecar exited before it named its port"
                lines.append(line)
                if match := re.search(r"health on port (\d+)", line):
                    return int(match[1])

        port = await asyncio.wait_for(named_port(), timeout=30)
        assert json.loads(await _get(port, "/healthz")) == {"status": "ok", "worker": "none"}
        assert any(NO_WORKER in line for line in lines)
    finally:
        proc.terminate()
        await proc.wait()
    # SIGTERM stops it cleanly.
    assert proc.returncode == 0
