"""The profile-fetch worker's process (#6625 Task 6): ``python -m scadbuddy.profile_fetch``.

It holds no database secret, no KEK, no store credential and no Bambuddy key, and refuses
to start with one (`settings.check_environment`). It exchanges its proof for a
``profile-fetch`` service credential (`exchange_source`), waits until the CRUD service
serves its areas, signs in to 3dfilamentprofiles.com once (a refused login ends the start,
plan Decision 5), then polls `DEFAULT_QUEUE` for `ProfileFetch` runs, one activity at a
time (Decision 16). It imports no database code (`tests/profile_fetch/test_worker.py`).
It is not versioned (no worker deployment), so a change to the workflow goes behind
`workflow.patched`. ``/healthz`` on `HEALTH_PORT` answers 200 while it runs, and
``/metrics`` there its metrics (`activities.EXPORTS`).
"""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Callable
from contextlib import suppress

from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from temporalio.client import Client
from temporalio.contrib.opentelemetry import TracingInterceptor
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker

from scadbuddy.auth.service_auth import ServiceAuth, exchange_source
from scadbuddy.crud.client import AREAS_POLL, CrudClient, wait_for_areas
from scadbuddy.profile_fetch.activities import ProfileFetchActivities, Site, SiteHolder
from scadbuddy.profile_fetch.settings import HEALTH_PORT, ProfileFetchSettings
from scadbuddy.profile_fetch.site import SITE, SiteError, ThreeDfpSite
from scadbuddy.profile_fetch.wire import AREAS
from scadbuddy.profile_fetch.workflow import ProfileFetch

logger = logging.getLogger(__name__)


async def _healthz(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """``GET /metrics`` is answered with the process's metrics (`activities.EXPORTS` among
    them); any other request 200: the process runs and its loop turns. As the intake
    worker's, whose own metrics this process does not import."""
    with suppress(Exception):
        line = await asyncio.wait_for(reader.readline(), 5)
        if line.split(b" ")[1:2] == [b"/metrics"]:
            body, kind = generate_latest(), CONTENT_TYPE_LATEST.encode()
        else:
            body, kind = b"ok", b"text/plain"
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: "
            + kind
            + b"\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\nConnection: close\r\n\r\n"
            + body
        )
        await writer.drain()
    writer.close()


class StartRefusedError(Exception):
    """The worker cannot start: the 3DFP sign-in failed. Its text holds no secret."""


async def run(
    settings: ProfileFetchSettings,
    *,
    stop: asyncio.Event | None = None,
    health_port: int | None = HEALTH_PORT,
    crud: CrudClient | None = None,
    open_site: Callable[[], Site] | None = None,
    areas_poll: float = AREAS_POLL,
) -> None:
    stop = stop or asyncio.Event()
    if crud is None:
        auth = ServiceAuth(exchange_source("profile-fetch", settings))
        crud = CrudClient(settings.api_internal_url, "profile-fetch", auth)

    def browser() -> Site:
        return ThreeDfpSite(SITE, settings.tdfp_email, settings.tdfp_password, settings.profile_dir)

    sites = SiteHolder(open_site or browser)
    activities = ProfileFetchActivities(crud, sites)
    health = (
        await asyncio.start_server(_healthz, "0.0.0.0", health_port)
        if health_port is not None
        else None
    )
    try:
        # During a roll the API pod may be older than the worker: poll only once it
        # serves every area the worker calls.
        if not await wait_for_areas(crud, "profile-fetch", AREAS, stop, poll=areas_poll):
            logger.info("stopped before the CRUD service served the profile-fetch areas")
            return
        failed: SiteError | None = None
        try:
            await sites.get()
        except SiteError as error:
            failed = error
        if failed is not None:
            # Raised outside the handler: the text is fixed, and no chain rides along.
            if failed.kind == "login_failed":
                raise StartRefusedError("3DFP login failed")
            raise StartRefusedError(f"3DFP sign-in at start failed ({failed.kind}): {failed}")
        client = await Client.connect(
            settings.temporal_address,
            namespace=settings.temporal_namespace,
            data_converter=pydantic_data_converter,
            interceptors=[TracingInterceptor()],
        )
        worker = Worker(
            client,
            task_queue=settings.temporal_task_queue_profile_fetch,
            workflows=[ProfileFetch],
            activities=[activities.export, activities.import_, activities.failed],
            # One browser, one login: one export at a time (Decision 16).
            max_concurrent_activities=1,
        )
        async with worker:
            logger.info(
                "profile-fetch worker polling %s", settings.temporal_task_queue_profile_fetch
            )
            await stop.wait()
    finally:
        if health is not None:
            health.close()
        await sites.aclose()
        await crud.aclose()


def install_stop(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
