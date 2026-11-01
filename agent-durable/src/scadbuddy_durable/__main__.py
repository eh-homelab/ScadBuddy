"""python -m scadbuddy_durable: the agent-durable sidecar.

Serves /healthz and runs no worker (``worker.py``: the Python turn is retired), and says
so, so the pod stays up for its operator to read until it is scaled to zero.

Keep this module's top level to imports and constants: the image build imports it to
check every start-up module ships its bytecode (Dockerfile, #4316), so anything else
here would run in the build.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal

from scadbuddy_durable.health import serve_health
from scadbuddy_durable.worker import NO_WORKER

PORT = int(os.environ.get("SCADBUDDY_DURABLE_HEALTH_PORT", "8082"))
log = logging.getLogger("scadbuddy_durable")


async def run() -> None:
    status = {"status": "ok", "worker": "none"}
    log.info("%s", NO_WORKER)
    server = await serve_health("0.0.0.0", PORT, lambda: dict(status))
    # The port it got: a test starts it on port 0 and reads this line (#3655).
    log.info("health on port %d", server.sockets[0].getsockname()[1])
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
