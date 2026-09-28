"""A Temporal for the tests: SCADBUDDY_TEST_TEMPORAL_ADDRESS (a running server, CI's
service or a dev server), else the `temporal` CLI's dev server started here
(SCADBUDDY_TEST_TEMPORAL_DEV_SERVER or `temporal` on PATH), else skip."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager, suppress

from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.service import RPCError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

logger = logging.getLogger(__name__)

TEST_TEMPORAL_ADDRESS_ENV = "SCADBUDDY_TEST_TEMPORAL_ADDRESS"
TEST_TEMPORAL_DEV_SERVER_ENV = "SCADBUDDY_TEST_TEMPORAL_DEV_SERVER"

# Read once, at import, as `tests.conftest` reads the test database URL: the API tests'
# `_clean_env` scrubs every SCADBUDDY_* variable before each test.
TEST_TEMPORAL_ADDRESS = os.environ.get(TEST_TEMPORAL_ADDRESS_ENV) or None
TEST_TEMPORAL_DEV_SERVER = os.environ.get(TEST_TEMPORAL_DEV_SERVER_ENV) or None
#: One task queue per API test (see `temporal_server`), with room to spare.
MAX_TASK_QUEUES_PER_VERSION = 100_000


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


@contextmanager
def temporal_server() -> Iterator[str]:
    """The address of a Temporal for a whole test session: SCADBUDDY_TEST_TEMPORAL_ADDRESS,
    or a dev server started here and stopped on exit. Its namespace is `default`. The
    dev server is a subprocess, so the loop that started it need not keep running.

    Every app a test starts runs its worker on a task queue of its own, and all of
    them register with the one deployment version: past Temporal's default of 100
    task queues per version a new queue is refused and its renders never start, so
    the dev server allows more. A server named by the address needs the same
    (`matching.maxTaskQueuesInDeploymentVersion`)."""
    if TEST_TEMPORAL_ADDRESS:
        yield TEST_TEMPORAL_ADDRESS
        return
    loop = asyncio.new_event_loop()
    env = loop.run_until_complete(
        WorkflowEnvironment.start_local(
            dev_server_existing_path=TEST_TEMPORAL_DEV_SERVER or shutil.which("temporal"),
            data_converter=pydantic_data_converter,
            dev_server_extra_args=[
                "--dynamic-config-value",
                f"matching.maxTaskQueuesInDeploymentVersion={MAX_TASK_QUEUES_PER_VERSION}",
            ],
        )
    )
    try:
        yield current_address(env.client)
    finally:
        loop.run_until_complete(env.shutdown())
        loop.close()


async def terminate_open_workflows(address: str, namespace: str, task_queue: str) -> None:
    """End every workflow still running on ``task_queue``."""
    client = await Client.connect(address, namespace=namespace)
    query = f"TaskQueue = '{task_queue}' AND ExecutionStatus = 'Running'"
    async for execution in client.list_workflows(query):
        handle = client.get_workflow_handle(execution.id, run_id=execution.run_id)
        with suppress(RPCError):  # it closed in between
            await handle.terminate("the test that started it ended")


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

    def __exit__(self, exc_type: type[BaseException] | None, *_: object) -> None:
        assert self._loop is not None
        self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(timeout=30)
        if not self._thread.is_alive():
            self._loop.close()
        if self._error is None:
            return
        if exc_type is None:
            raise self._error
        # The test's own failure is the one to see; this one is logged beside it.
        logger.error("the Temporal worker thread failed too", exc_info=self._error)
