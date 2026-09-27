"""Prometheus metrics, served at ``GET /metrics``.

One registry per app rather than prometheus_client's process-global one: the
tests build many apps in one process, and a second registration of the same name
on the global registry raises. Labels are kept to small closed sets (a stage, an
outcome, a route template) -- never a slug, a job id or a raw path.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Literal, get_args

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from starlette.types import ASGIApp, Message, Receive, Scope, Send

__all__ = ["CONTENT_TYPE_LATEST", "HttpMetrics", "Metrics", "RenderOutcome", "RenderStage"]

#: How a job left the queue. ``expired`` waited past SCADBUDDY_RENDER_QUEUE_TIMEOUT
#: and never reached a worker; ``superseded`` was replaced by a newer render first.
RenderOutcome = Literal["done", "failed", "expired", "superseded"]
RenderStage = Literal["source", "render", "split", "solids", "thumbnail", "write"]

# A render is bounded by SCADBUDDY_RENDER_TIMEOUT (120 s by default) per openscad
# pass, and a multi-colour job makes one pass per colour, so the tail runs long.
RENDER_BUCKETS = (0.25, 0.5, 1, 2, 5, 10, 20, 30, 60, 120, 240, 480, 900)
HTTP_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)


class Metrics:
    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry if registry is not None else CollectorRegistry()
        r = self.registry

        self.build_info = Gauge(
            "scadbuddy_build_info",
            "The image this process runs; always 1.",
            ["version", "revision"],
            registry=r,
        )

        # Render queue: admission, then the job's life.
        self.render_submitted = Counter(
            "scadbuddy_render_jobs_submitted",
            "Render requests accepted onto the queue as a new job.",
            registry=r,
        )
        self.render_coalesced = Counter(
            "scadbuddy_render_jobs_coalesced",
            "Render requests answered with an identical job already waiting.",
            registry=r,
        )
        self.render_rejected = Counter(
            "scadbuddy_render_jobs_rejected",
            "Render requests refused because the queue was full.",
            registry=r,
        )
        self.render_finished = Counter(
            "scadbuddy_render_jobs_finished",
            "Render jobs that left the queue, by outcome.",
            ["outcome"],
            registry=r,
        )
        self.queue_depth = Gauge(
            "scadbuddy_render_queue_depth",
            "Render jobs waiting for a worker.",
            registry=r,
        )
        self.queue_capacity = Gauge(
            "scadbuddy_render_queue_capacity",
            "SCADBUDDY_RENDER_QUEUE_MAX: waiting jobs past which a submit is refused.",
            registry=r,
        )
        self.running = Gauge(
            "scadbuddy_render_jobs_running",
            "Render jobs a worker is on right now.",
            registry=r,
        )
        self.workers = Gauge(
            "scadbuddy_render_workers",
            "SCADBUDDY_RENDER_CONCURRENCY: render workers in this process.",
            registry=r,
        )
        self.queue_wait = Histogram(
            "scadbuddy_render_queue_wait_seconds",
            "Time from submit until a worker took the job (or expired it).",
            buckets=RENDER_BUCKETS,
            registry=r,
        )
        self.render_duration = Histogram(
            "scadbuddy_render_duration_seconds",
            "Time a worker spent on a job, by outcome.",
            ["outcome"],
            buckets=RENDER_BUCKETS,
            registry=r,
        )
        self.job_latency = Histogram(
            "scadbuddy_render_job_latency_seconds",
            "Time from submit to a settled job, by outcome: what the user waits for.",
            ["outcome"],
            buckets=RENDER_BUCKETS,
            registry=r,
        )
        self.stage_duration = Histogram(
            "scadbuddy_render_stage_seconds",
            "Time spent in each step of a render.",
            ["stage"],
            buckets=RENDER_BUCKETS,
            registry=r,
        )

        self.http_requests = Counter(
            "scadbuddy_http_requests",
            "HTTP requests served, by method, route template and status.",
            ["method", "route", "status"],
            registry=r,
        )
        self.http_duration = Histogram(
            "scadbuddy_http_request_duration_seconds",
            "HTTP request latency, by method and route template.",
            ["method", "route"],
            buckets=HTTP_BUCKETS,
            registry=r,
        )

        # Every series a dashboard or alert divides by exists from the first scrape,
        # at zero, rather than appearing with the first job to reach it.
        for outcome in get_args(RenderOutcome):
            self.render_finished.labels(outcome)
            self.job_latency.labels(outcome)
        for outcome in ("done", "failed"):
            self.render_duration.labels(outcome)
        for stage in get_args(RenderStage):
            self.stage_duration.labels(stage)

    @contextmanager
    def stage(self, stage: RenderStage) -> Iterator[None]:
        """Time one render step. Observed whether the step succeeds or raises: a
        step that times out is exactly the one worth seeing in the histogram."""
        started = time.perf_counter()
        try:
            yield
        finally:
            self.stage_duration.labels(stage).observe(time.perf_counter() - started)

    def exposition(self) -> bytes:
        return generate_latest(self.registry)


#: Requests that matched no API route: the SPA's static files and 404s. One label
#: for all of them, or every probed path would become its own series.
UNMATCHED_ROUTE = "other"


class HttpMetrics:
    """Pure ASGI middleware counting and timing HTTP requests by route template.

    Not a `BaseHTTPMiddleware`: that buffers streaming responses (the downloads
    are files). The template is read after the app ran, from the `APIRoute` FastAPI's
    router put on the scope. With routers included into routers (FastAPI >= 0.140
    no longer flattens them) that is the path as its own router declares it, so
    `/api/v1/models/{slug}` is labelled `/models/{slug}`: still a template, and
    every such route sits under the one API prefix.
    """

    def __init__(self, app: ASGIApp, *, metrics: Metrics) -> None:
        self.app = app
        self.metrics = metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        status = 500
        started = time.perf_counter()

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            template = getattr(scope.get("route"), "path", None) or UNMATCHED_ROUTE
            method = scope.get("method", "")
            self.metrics.http_requests.labels(method, template, str(status)).inc()
            self.metrics.http_duration.labels(method, template).observe(
                time.perf_counter() - started
            )
