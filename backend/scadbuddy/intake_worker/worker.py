"""The intake worker's process (#6133, spec 2026-11-13 §7): ``python -m scadbuddy.intake_worker``.

It holds no database secret, no KEK and no store credential, and refuses to start with
one (`settings.check_environment`). It exchanges its proof for an ``intake`` service
credential (`exchange_source`), waits until the CRUD service serves its areas, then polls
``customer-files`` for `CustomerFileCheck` runs. It imports no database code
(`tests/intake_worker/test_worker.py`); each file is parsed only in a child process.
It is not versioned (no worker deployment), so a change to the workflow goes behind
`workflow.patched`. ``/healthz`` on `HEALTH_PORT` answers 200 while it runs.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import suppress

from temporalio.client import Client
from temporalio.contrib.opentelemetry import TracingInterceptor
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker

from scadbuddy.auth.service_auth import ServiceAuth, exchange_source
from scadbuddy.crud.client import CrudClient, wait_for_areas
from scadbuddy.intake_worker.activities import IntakeActivities
from scadbuddy.intake_worker.settings import HEALTH_PORT, IntakeSettings
from scadbuddy.intake_worker.wire import INTAKE_AREAS
from scadbuddy.intake_worker.workflow import CustomerFileCheck

logger = logging.getLogger(__name__)


async def _healthz(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Any request is answered 200: the process runs and its loop turns."""
    with suppress(Exception):
        await asyncio.wait_for(reader.readline(), 5)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        await writer.drain()
    writer.close()


async def run(
    settings: IntakeSettings,
    *,
    stop: asyncio.Event | None = None,
    health_port: int | None = HEALTH_PORT,
) -> None:
    stop = stop or asyncio.Event()
    auth = ServiceAuth(exchange_source("intake", settings))
    crud = CrudClient(settings.api_internal_url, "intake", auth)
    sandbox = settings.intake_sandbox == "true"
    if not sandbox:
        logger.warning(
            "SCADBUDDY_INTAKE_SANDBOX=unsafe-local: checks run in this container; dev only"
        )
    activities = IntakeActivities(crud, settings.intake_work_dir, sandbox=sandbox)
    health = (
        await asyncio.start_server(_healthz, "0.0.0.0", health_port)
        if health_port is not None
        else None
    )
    try:
        # During a roll the API pod may be older than the worker: poll only once it
        # serves every area the worker calls.
        if not await wait_for_areas(crud, "intake", INTAKE_AREAS, stop):
            logger.info("stopped before the CRUD service served the intake areas")
            return
        client = await Client.connect(
            settings.temporal_address,
            namespace=settings.temporal_namespace,
            data_converter=pydantic_data_converter,
            interceptors=[TracingInterceptor()],
        )
        worker = Worker(
            client,
            task_queue=settings.temporal_task_queue_customer_files,
            workflows=[CustomerFileCheck],
            activities=[activities.check, activities.failed],
            max_concurrent_activities=settings.intake_concurrency,
        )
        async with worker:
            logger.info("intake worker polling %s", settings.temporal_task_queue_customer_files)
            await stop.wait()
    finally:
        if health is not None:
            health.close()
        await activities.aclose()
        await crud.aclose()


def install_stop(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
