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

# Read once, at import, as `tests.conftest` reads the test database URL: the API tests'
# `_clean_env` scrubs every SCADBUDDY_* variable before each test.
TEST_TEMPORAL_ADDRESS = os.environ.get(TEST_TEMPORAL_ADDRESS_ENV) or None
TEST_TEMPORAL_DEV_SERVER = os.environ.get(TEST_TEMPORAL_DEV_SERVER_ENV) or None


def temporal_available() -> bool:
    return bool(TEST_TEMPORAL_ADDRESS or TEST_TEMPORAL_DEV_SERVER or shutil.which("temporal"))


@asynccontextmanager
async def temporal_client() -> AsyncIterator[Client]:
    if TEST_TEMPORAL_ADDRESS:
        yield await Client.connect(TEST_TEMPORAL_ADDRESS, data_converter=pydantic_data_converter)
        return
    binary = TEST_TEMPORAL_DEV_SERVER or shutil.which("temporal")
    env = await WorkflowEnvironment.start_local(
        dev_server_existing_path=binary, data_converter=pydantic_data_converter
    )
    try:
        yield env.client
    finally:
        await env.shutdown()


class WorkerThread:
    """Run a Temporal `Worker` on its own loop in a thread, for sync TestClient tests."""

    def __init__(self, make_worker: Callable[[], Awaitable[Worker]]) -> None:
        self._make_worker = make_worker
        self._stop = asyncio.Event()
        self._thread = threading.Thread(target=self._run, name="temporal-worker", daemon=True)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._started = threading.Event()
        self._error: BaseException | None = None

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._started.set()
        try:
            self._loop.run_until_complete(self._main())
        except BaseException as error:  # re-raised by __exit__ on the test's thread
            self._error = error

    async def _main(self) -> None:
        worker: Worker = await self._make_worker()
        async with worker:
            await self._stop.wait()

    def __enter__(self) -> WorkerThread:
        self._thread.start()
        self._started.wait()
        return self

    def __exit__(self, *_: object) -> None:
        assert self._loop is not None
        self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(timeout=30)
        if not self._thread.is_alive():
            self._loop.close()
        if self._error is not None:
            raise self._error
