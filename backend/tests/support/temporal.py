"""A Temporal for the tests: SCADBUDDY_TEST_TEMPORAL_ADDRESS (a running server, CI's
service or a dev server), else the `temporal` CLI's dev server started here
(SCADBUDDY_TEST_TEMPORAL_DEV_SERVER or `temporal` on PATH), else skip."""

from __future__ import annotations

import asyncio
import os
import shutil
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

TEST_TEMPORAL_ADDRESS_ENV = "SCADBUDDY_TEST_TEMPORAL_ADDRESS"
TEST_TEMPORAL_DEV_SERVER_ENV = "SCADBUDDY_TEST_TEMPORAL_DEV_SERVER"


def temporal_available() -> bool:
    return bool(
        os.environ.get(TEST_TEMPORAL_ADDRESS_ENV)
        or os.environ.get(TEST_TEMPORAL_DEV_SERVER_ENV)
        or shutil.which("temporal")
    )


@asynccontextmanager
async def temporal_client() -> AsyncIterator[Client]:
    address = os.environ.get(TEST_TEMPORAL_ADDRESS_ENV)
    if address:
        yield await Client.connect(address, data_converter=pydantic_data_converter)
        return
    binary = os.environ.get(TEST_TEMPORAL_DEV_SERVER_ENV) or shutil.which("temporal")
    env = await WorkflowEnvironment.start_local(
        dev_server_existing_path=binary, data_converter=pydantic_data_converter
    )
    try:
        yield env.client
    finally:
        await env.shutdown()


def current_address(client: Client) -> str:
    """The host:port `client` is connected to: the dev server's, when one was started."""
    return client.service_client.config.target_host


class WorkerThread:
    """Run a Temporal `Worker` on its own loop in a thread, for sync TestClient tests."""

    def __init__(self, make_worker: Callable[[], Awaitable[Worker]]) -> None:
        self._make_worker = make_worker
        self._stop = asyncio.Event()
        self._thread = threading.Thread(target=self._run, name="temporal-worker", daemon=True)
        self._loop: asyncio.AbstractEventLoop | None = None

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._main())

    async def _main(self) -> None:
        worker: Worker = await self._make_worker()
        async with worker:
            await self._stop.wait()

    def __enter__(self) -> WorkerThread:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        assert self._loop is not None
        self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(timeout=30)
