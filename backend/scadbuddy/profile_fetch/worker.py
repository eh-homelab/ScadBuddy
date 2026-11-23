"""The profile-fetch worker's process (#6625 Task 6, #6780): ``python -m scadbuddy.profile_fetch``.

It holds no database secret, no KEK, no store credential, no Bambuddy key and no 3DFP login,
and refuses to start with one (`settings.check_environment`). It runs no browser: each step on
3dfilamentprofiles.com goes to the browser service, a Deployment of its own
(`browser.py`), over its RPC (`remote.RemoteSite`, at ``SCADBUDDY_PROFILE_FETCH_BROWSER_URL``
with the RPC token). So no untrusted web content runs in the pod that holds the CRUD
credential and reaches Temporal (#6780). It exchanges its proof for a ``profile-fetch``
service credential (`exchange_source`), waits until the CRUD service serves its areas, then
polls `DEFAULT_QUEUE` for `ProfileFetch` runs and for 3DFP searches and creates (`TdfpSearch`,
`TdfpCreate`, #7025), one activity at a time on the one browser (Decision 16), and a
search's activity on a queue of its own (`workflow.search_queue`, #7062), whose one slot
never waits for an export (R-7062-1). It does
not sign in at start: the browser service does, and the first export asks it to again when it
has no signed-in browser. It imports no database code (`tests/profile_fetch/test_worker.py`).
It is not versioned (no worker deployment), so a change to the workflow goes behind
`workflow.patched`. ``/healthz`` on `HEALTH_PORT` answers 200 while it runs, and
``/metrics`` there its metrics (`activities.EXPORTS`).
"""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Callable
from contextlib import AsyncExitStack, suppress
from typing import Any

from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from temporalio.client import Client
from temporalio.contrib.opentelemetry import TracingInterceptor
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker

from scadbuddy.auth.service_auth import ServiceAuth, exchange_source
from scadbuddy.crud.client import AREAS_POLL, CrudClient, wait_for_areas
from scadbuddy.profile_fetch.activities import (
    ProfileFetchActivities,
    Site,
    SiteHolder,
    TdfpActivities,
)
from scadbuddy.profile_fetch.remote import RemoteSite
from scadbuddy.profile_fetch.settings import HEALTH_PORT, ProfileFetchSettings
from scadbuddy.profile_fetch.wire import AREAS
from scadbuddy.profile_fetch.workflow import ProfileFetch, TdfpCreate, TdfpSearch, search_queue

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


def temporal_workers(
    client: Client,
    queue: str,
    *,
    fetch: list[Callable[..., Any]],
    create: Callable[..., Any],
    search: Callable[..., Any],
    **options: Any,
) -> list[Worker]:
    """The process's two workers. One polls ``queue`` for the three workflows and runs a
    preset fetch's activities and a create, one activity at a time on the one browser
    (Decision 16): a create never runs beside an export. The other polls
    `workflow.search_queue` for a search's activity alone, one at a time, so a search never
    waits for a fetch's whole export (R-7062-1); on the browser service it takes its turn
    between the export's steps (R-7062-2). ``options`` go to both (a test's runner)."""
    return [
        Worker(
            client,
            task_queue=queue,
            workflows=[ProfileFetch, TdfpSearch, TdfpCreate],
            activities=[*fetch, create],
            # One browser, one login: one export at a time (Decision 16).
            max_concurrent_activities=1,
            **options,
        ),
        Worker(
            client,
            task_queue=search_queue(queue),
            activities=[search],
            max_concurrent_activities=1,
            **options,
        ),
    ]


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

    def browser_service() -> Site:
        return RemoteSite(settings.profile_fetch_browser_url, settings.browser_token)

    def search_browser_service() -> Site:
        return RemoteSite(
            settings.profile_fetch_browser_url, settings.browser_token, closes_browser=False
        )

    sites = SiteHolder(open_site or browser_service)
    activities = ProfileFetchActivities(crud, sites)
    tdfp = TdfpActivities(sites)
    # A search's own RPC client. Dropping it (a search cancelled, or failed `browser` or
    # `timeout`) closes only its connection, never the service's browser, which an export
    # may be using; only a fetch's or a create's own failure closes that (R-7062-3).
    search_sites = SiteHolder(open_site or search_browser_service)
    searches = TdfpActivities(search_sites)
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
        client = await Client.connect(
            settings.temporal_address,
            namespace=settings.temporal_namespace,
            data_converter=pydantic_data_converter,
            interceptors=[TracingInterceptor()],
        )
        async with AsyncExitStack() as running:
            for each in temporal_workers(
                client,
                settings.temporal_task_queue_profile_fetch,
                fetch=[activities.export, activities.import_, activities.failed],
                create=tdfp.create,
                search=searches.search,
            ):
                await running.enter_async_context(each)
            logger.info(
                "profile-fetch worker polling %s and %s",
                settings.temporal_task_queue_profile_fetch,
                search_queue(settings.temporal_task_queue_profile_fetch),
            )
            await stop.wait()
    finally:
        if health is not None:
            health.close()
        await sites.aclose()
        await search_sites.aclose()
        await crud.aclose()


def install_stop(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
