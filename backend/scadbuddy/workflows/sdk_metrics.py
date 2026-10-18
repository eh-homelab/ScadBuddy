"""The Temporal SDK's own metrics on a worker's ``/metrics`` (#3788).

The SDK's Rust core records workflow-task and activity latencies
(``temporal_workflow_task_execution_latency``, ``temporal_activity_schedule_to_start_latency``
and the rest), but only into a Runtime that is given a metrics exporter, and a client
built without one records nothing. So each worker process that connects for itself
gets one Runtime whose Prometheus exporter listens on loopback only, and the worker's
own ``/metrics`` appends that exposition to its own: one scrape target, as before, and
the exporter is never reachable from outside the pod.

The exporter's names all start ``temporal_``; none of ScadBuddy's own do, so the two
expositions never declare the same family.
"""

from __future__ import annotations

import functools
import logging

import httpx
from temporalio.runtime import PrometheusConfig, Runtime, TelemetryConfig

logger = logging.getLogger(__name__)

#: How long a scrape waits for the exporter before it answers without it.
SDK_READ_TIMEOUT = 2.0


@functools.cache
def sdk_runtime(port: int) -> Runtime:
    """The process's Runtime, exporting on ``127.0.0.1:port``. One per process: a
    second exporter on the same port would fail to bind, so it is built once."""
    return Runtime(
        telemetry=TelemetryConfig(
            metrics=PrometheusConfig(bind_address=f"127.0.0.1:{port}", durations_as_seconds=True)
        )
    )


async def sdk_exposition(port: int, client: httpx.AsyncClient) -> bytes:
    """The exporter's text, read with ``client`` (one per health server, so a scrape
    reuses its connection), or nothing when it does not answer: a scrape never fails
    for it, it just lacks the SDK's series that once."""
    try:
        response = await client.get(f"http://127.0.0.1:{port}/metrics")
        response.raise_for_status()
    except httpx.HTTPError as err:
        logger.debug("the Temporal SDK's metrics could not be read: %s", err)
        return b""
    body = response.content
    return body if body.endswith(b"\n") or not body else body + b"\n"
