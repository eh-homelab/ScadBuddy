"""A Temporal for the tests: SCADBUDDY_TEST_TEMPORAL_ADDRESS (a running server, CI's
service or a dev server), else the `temporal` CLI's dev server started here
(SCADBUDDY_TEST_TEMPORAL_DEV_SERVER or `temporal` on PATH), else skip."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Iterator
from contextlib import asynccontextmanager, contextmanager, suppress
from pathlib import Path

from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.service import RPCError, RPCStatusCode
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


#: Where the dev server's store goes when tmpfs has room: on disk its first start
#: takes ~3 s, too near temporalio's fixed 5 s start window on a loaded machine.
#: A run killed with SIGTERM skips the cleanup and leaves its store behind in
#: /dev/shm (RAM) until reboot: `ls -d /dev/shm/temporal-*` finds them, and
#: `rm -rf /dev/shm/temporal-*` removes them once no test session is running.
TMPFS = Path("/dev/shm")
TMPFS_MIN_FREE = 512 * 2**20


def _store_dir() -> str | None:
    usable = TMPFS.is_dir() and os.access(TMPFS, os.W_OK)
    return str(TMPFS) if usable and shutil.disk_usage(TMPFS).free >= TMPFS_MIN_FREE else None


@contextmanager
def temporal_server(db_file: Path | None = None) -> Iterator[str]:
    """The address of a Temporal for a whole test session: SCADBUDDY_TEST_TEMPORAL_ADDRESS,
    or a dev server started here and stopped on exit. Its namespace is `default`. The
    dev server is a subprocess, so the loop that started it need not keep running.

    Every app a test starts runs its worker on a task queue of its own, and all of
    them register with the one deployment version: past Temporal's default of 100
    task queues per version a new queue is refused and its renders never start, so
    the dev server allows more. A server named by the address needs the same
    (`matching.maxTaskQueuesInDeploymentVersion`).

    Its store is a file (``db_file``, or one on tmpfs when it has room), not memory:
    the in-memory SQLite was lost mid-session under load (the log says "interrupted",
    then "Namespace default is not found"), and every test after that failed."""
    if TEST_TEMPORAL_ADDRESS:
        yield TEST_TEMPORAL_ADDRESS
        return
    with (
        tempfile.TemporaryDirectory(dir=_store_dir(), prefix="temporal-") as scratch,
        _dev_server(db_file or Path(scratch) / "temporal.db") as address,
    ):
        yield address


@contextmanager
def _dev_server(db_file: Path) -> Iterator[str]:
    loop = asyncio.new_event_loop()
    env = loop.run_until_complete(
        WorkflowEnvironment.start_local(
            dev_server_existing_path=TEST_TEMPORAL_DEV_SERVER or shutil.which("temporal"),
            data_converter=pydantic_data_converter,
            dev_server_database_filename=str(db_file),
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


async def terminate_open_workflows(client: Client, task_queue: str) -> None:
    """End every workflow still running on ``task_queue``."""
    query = f"TaskQueue = '{task_queue}' AND ExecutionStatus = 'Running'"
    async for execution in client.list_workflows(query):
        handle = client.get_workflow_handle(execution.id, run_id=execution.run_id)
        with suppress(RPCError):  # it closed in between
            await handle.terminate("the test that started it ended")


async def delete_schedules(client: Client, *schedule_ids: str) -> None:
    """Delete each Schedule a test's app made; one that never got made is fine."""
    for schedule_id in schedule_ids:
        try:
            await client.get_schedule_handle(schedule_id).delete()
        except RPCError as error:
            if error.status != RPCStatusCode.NOT_FOUND:
                raise


class WorkflowReaper:
    """Terminates what a test left open, for a whole session on ONE client: a
    temporalio `Client` has no close, so one per teardown would leak a connection per
    test. The client lives on a loop of its own in a thread, since the sync fixtures
    that call `terminate` have none."""

    def __init__(self, address: str, namespace: str) -> None:
        self._address, self._namespace = address, namespace
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="temporal-reaper", daemon=True
        )
        self.client: Client | None = None

    def __enter__(self) -> WorkflowReaper:
        self._thread.start()
        self.client = self._run(Client.connect(self._address, namespace=self._namespace))
        return self

    def __exit__(self, *_: object) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join()
        self._loop.close()

    def terminate(self, task_queue: str) -> None:
        assert self.client is not None, "use the reaper as a context manager"
        self._run(terminate_open_workflows(self.client, task_queue))

    def delete_schedules(self, *schedule_ids: str) -> None:
        assert self.client is not None, "use the reaper as a context manager"
        self._run(delete_schedules(self.client, *schedule_ids))

    def _run[T](self, coro: Coroutine[object, object, T]) -> T:
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=60)


def current_address(client: Client) -> str:
    """The host:port `client` is connected to: the dev server's, when one was started."""
    return client.service_client.config.target_host


class WorkerThread:
    """Run a Temporal `Worker` on its own loop in a thread, for sync TestClient tests."""

    def __init__(
        self, make_worker: Callable[[], Awaitable[Worker]], *, join_timeout: float = 30
    ) -> None:
        self._make_worker = make_worker
        self._join_timeout = join_timeout
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
        self._thread.join(timeout=self._join_timeout)
        if self._thread.is_alive():
            # Its loop and Worker live on: fail here, not in whichever test they disturb.
            raise RuntimeError(
                f"the Temporal worker thread is still running {self._join_timeout:g} s"
                " after it was told to stop"
            )
        self._loop.close()
        if self._error is None:
            return
        if exc_type is None:
            raise self._error
        # The test's own failure is the one to see; this one is logged beside it.
        logger.error("the Temporal worker thread failed too", exc_info=self._error)
