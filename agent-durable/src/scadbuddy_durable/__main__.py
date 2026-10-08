"""python -m scadbuddy_durable: the agent-durable sidecar.

Phase 5a serves /healthz only. 5c starts the `agent` queue's worker here.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal

from scadbuddy_durable.health import serve_health

PORT = int(os.environ.get("SCADBUDDY_DURABLE_HEALTH_PORT", "8082"))


async def run() -> None:
    server = await serve_health("0.0.0.0", PORT, lambda: {"status": "ok", "worker": "not started"})
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    server.close()
    await server.wait_closed()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
