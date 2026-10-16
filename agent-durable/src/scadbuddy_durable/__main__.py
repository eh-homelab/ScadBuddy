"""python -m scadbuddy_durable: the agent-durable sidecar.

Serves /healthz and runs the ``agent`` queue's worker (``worker.py``). Without the
worker's configuration it serves /healthz alone and says why the worker is not
running, so the pod stays up for its operator to read.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal

from scadbuddy_durable.health import serve_health
from scadbuddy_durable.worker import config_from_env, start

PORT = int(os.environ.get("SCADBUDDY_DURABLE_HEALTH_PORT", "8082"))
log = logging.getLogger("scadbuddy_durable")


async def run() -> None:
    status = {"status": "ok", "worker": "starting"}
    server = await serve_health("0.0.0.0", PORT, lambda: dict(status))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    worker_task: asyncio.Task[None] | None = None
    worker = None
    try:
        cfg = config_from_env()
    except ValueError as err:
        log.error("worker not started: %s", err)
        status["worker"] = "not configured"
    else:
        try:
            worker = await start(cfg)
        except Exception as err:  # reported by type only: a message can name a path
            log.error("worker not started: %s", type(err).__name__)
            status["worker"] = "failed to start"
        else:
            status["worker"] = "running"
            worker_task = asyncio.create_task(worker.run())
    waiting: list[asyncio.Task[object]] = [asyncio.create_task(stop.wait())]
    if worker_task is not None:
        waiting.append(worker_task)
    await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
    if worker is not None and worker_task is not None and not worker_task.done():
        # Lets running activities finish (or heartbeat their cancel) before it exits.
        await worker.shutdown()
    if worker_task is not None:
        status["worker"] = "stopped"
        with contextlib.suppress(Exception):
            await worker_task
    server.close()
    await server.wait_closed()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
