"""`python -m scadbuddy_durable.worker`: the Temporal worker on `agent` (DurableSession and
the plugin's segment activity), the event projector, and `/healthz` (plan task 11)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import socket
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from psycopg_pool import AsyncConnectionPool
from temporalio.claude_agent_sdk import ClaudeAgentPlugin
from temporalio.client import Client
from temporalio.worker import Worker

from scadbuddy_durable.codec import data_converter
from scadbuddy_durable.config import Config, ConfigError, load_config
from scadbuddy_durable.credentials import CredentialSource
from scadbuddy_durable.models import TASK_QUEUE
from scadbuddy_durable.payload_keys import PayloadKeys
from scadbuddy_durable.projector import Projector
from scadbuddy_durable.runner import CWD, SessionRunner
from scadbuddy_durable.secrets import Kek, SecretKeyError, load_kek
from scadbuddy_durable.segments import Segments, Snapshots, make_save_snapshot
from scadbuddy_durable.store import PostgresSessionStore
from scadbuddy_durable.tools import MANIFEST_PATH, TOOLS
from scadbuddy_durable.workflow import DurableSession

log = logging.getLogger("scadbuddy_durable.worker")

PLUGIN_DIR = str(Path(__file__).resolve().parents[1] / "plugin")
# The agent build writes the prompt policy next to the tool manifest (dist/ there,
# /app/agent-durable/ in the image).
PROMPT_NAME = "durable-prompt.txt"
# The plugin's README puts a segment near 270 MB; 4 caps the sidecar near 1.1 GB.
MAX_CONCURRENT_ACTIVITIES = 4
GRACEFUL_SHUTDOWN = timedelta(seconds=10)
PROJECTOR_STOP_S = 10.0


@dataclass(frozen=True)
class WorkerDeps:
    pool: AsyncConnectionPool
    keks: Sequence[Kek]
    prompt_append: str
    plugin_dir: str = PLUGIN_DIR
    cwd: str = CWD


def client_with_codec(client: Client, pool: AsyncConnectionPool, keks: Sequence[Kek]) -> Client:
    """`client` with the per-subject payload codec over `ai_payload_keys`."""
    config = client.config()
    config["data_converter"] = data_converter(PayloadKeys(pool, keks))
    return Client(**config)


def build_worker(client: Client, deps: WorkerDeps) -> Worker:
    runner = SessionRunner(
        CredentialSource(deps.pool, deps.keks),
        Segments(deps.pool),
        PostgresSessionStore(deps.pool),
        plugin_dir=deps.plugin_dir,
        prompt_append=deps.prompt_append,
        cwd=deps.cwd,
    )
    return Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[DurableSession],
        # The tools' activities are served by the TypeScript agent-tools worker.
        activities=[make_save_snapshot(Snapshots(deps.pool))],
        plugins=[ClaudeAgentPlugin(runner)],
        max_concurrent_activities=MAX_CONCURRENT_ACTIVITIES,
        graceful_shutdown_timeout=GRACEFUL_SHUTDOWN,
    )


# ---- /healthz -------------------------------------------------------------------------


class Health:
    def __init__(
        self,
        *,
        database_probe: Callable[[], Awaitable[None]] | None = None,
        disabled: str | None = None,
    ) -> None:
        self.temporal = "connecting"  # then "ok", or "unavailable" once the worker ended
        self._probe = database_probe
        self._disabled = disabled

    async def report(self) -> tuple[int, dict[str, Any]]:
        if self._disabled is not None:
            return 200, {"status": "ok", "durable": self._disabled}
        database = "ok"
        if self._probe is not None:
            try:
                await asyncio.wait_for(self._probe(), 3)
            except Exception:
                database = "unavailable"
        if self.temporal == "connecting":
            status = "starting"
        elif self.temporal == "ok" and database == "ok":
            status = "ok"
        else:
            status = "unavailable"
        return (200 if status == "ok" else 503), {
            "status": status,
            "temporal": self.temporal,
            "database": database,
        }


async def serve_health(health: Health, port: int, *, host: str = "0.0.0.0") -> asyncio.Server:
    """A minimal HTTP/1.1 server: `GET /healthz`, anything else 404."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await asyncio.wait_for(reader.readline(), 10)
            while (line := await asyncio.wait_for(reader.readline(), 10)) not in (b"\r\n", b"\n", b""):
                del line
            parts = request.split()
            if len(parts) >= 2 and parts[0] == b"GET" and parts[1].split(b"?")[0] == b"/healthz":
                code, body = await health.report()
            else:
                code, body = 404, {"error": "not found"}
            data = json.dumps(body).encode()
            reason = {200: "OK", 404: "Not Found", 503: "Service Unavailable"}[code]
            writer.write(
                f"HTTP/1.1 {code} {reason}\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n".encode()
                + data
            )
            await writer.drain()
        except (TimeoutError, ConnectionError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    return await asyncio.start_server(handle, host, port)


# ---- the process ----------------------------------------------------------------------


async def connect_temporal(
    connect: Callable[[], Awaitable[Client]],
    health: Health,
    *,
    stop: asyncio.Event | None = None,
    first_delay: float = 0.5,
    max_delay: float = 10.0,
) -> Client | None:
    """Connects, retrying with backoff while /healthz says `starting`; None if stopped first."""
    delay = first_delay
    stop = stop or asyncio.Event()
    while not stop.is_set():
        try:
            client = await connect()
        except Exception as err:
            log.warning("Temporal is not reachable yet (retrying in %.1fs): %s", delay, err)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), delay)
            delay = min(delay * 2, max_delay)
            continue
        health.temporal = "ok"
        return client
    return None


async def run_worker(worker: Worker, projector: Projector, stop: asyncio.Event, health: Health) -> None:
    """Runs the worker and the projector until `stop` (or either ends), then shuts down."""
    running = asyncio.create_task(worker.run())
    projecting = asyncio.create_task(projector.run(stop))
    stopping = asyncio.create_task(stop.wait())
    await asyncio.wait({running, projecting, stopping}, return_when=asyncio.FIRST_COMPLETED)
    stop.set()
    if not running.done():
        await worker.shutdown()
    health.temporal = "unavailable"
    log.info("worker shut down")
    try:
        await asyncio.wait_for(projecting, PROJECTOR_STOP_S)
        log.info("projector stopped")
    except TimeoutError:
        log.error("projector did not stop within %.0fs", PROJECTOR_STOP_S)
    await stopping
    failure = running.exception() if running.done() and not running.cancelled() else None
    if failure is not None:
        raise failure


async def _ping(pool: AsyncConnectionPool) -> None:
    async with pool.connection(timeout=3) as conn:
        await conn.execute("SELECT 1")


def _disabled_reason(config: Config) -> str | None:
    if config.database_url is None:
        return "no database"
    if config.temporal_address is None:
        return "no Temporal address"
    return None


async def serve(config: Config) -> int:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    reason = _disabled_reason(config)
    if reason is not None:
        log.warning("durable sessions are disabled: %s", reason)
        server = await serve_health(Health(disabled=f"disabled ({reason})"), config.health_port)
        await stop.wait()
        server.close()
        await server.wait_closed()
        return 0
    assert config.database_url is not None and config.temporal_address is not None

    if config.secret_key_file is None:
        log.error("SCADBUDDY_SECRET_KEY_FILE is required with a database")
        return 1
    try:
        keks = [load_kek(config.secret_key_file)]
        if config.secret_key_previous_file is not None:
            keks.append(load_kek(config.secret_key_previous_file))
    except (OSError, SecretKeyError) as err:
        log.error("cannot load the secret key: %s", err)
        return 1
    prompt_path = Path(MANIFEST_PATH).with_name(PROMPT_NAME)
    if not prompt_path.is_file():
        log.error("no prompt policy at %s", prompt_path)
        return 1
    prompt_append = prompt_path.read_text(encoding="utf-8")

    address, namespace = config.temporal_address, config.temporal_namespace
    async with AsyncConnectionPool(config.database_url, open=False, max_size=10) as pool:
        health = Health(database_probe=lambda: _ping(pool))
        server = await serve_health(health, config.health_port)
        try:
            client = await connect_temporal(
                lambda: Client.connect(
                    address, namespace=namespace, data_converter=data_converter(PayloadKeys(pool, keks))
                ),
                health,
                stop=stop,
            )
            if client is None:
                return 0
            log.info("connected to Temporal at %s (namespace %s)", address, namespace)
            worker = build_worker(client, WorkerDeps(pool=pool, keks=keks, prompt_append=prompt_append))
            holder = f"agent-durable:{socket.gethostname()}:{os.getpid()}"
            await run_worker(worker, Projector(pool, client, holder=holder), stop, health)
        finally:
            server.close()
            await server.wait_closed()
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not TOOLS:
        print(f"agent-durable: no tool manifest at {MANIFEST_PATH}", file=sys.stderr)
        return 1
    try:
        config = load_config(os.environ)
    except ConfigError as err:
        print(f"agent-durable: {err}", file=sys.stderr)
        return 1
    return asyncio.run(serve(config))


if __name__ == "__main__":
    sys.exit(main())
