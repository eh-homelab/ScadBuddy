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

__all__ = [
    "CONTENT_TYPE_LATEST",
    "HttpMetrics",
    "Metrics",
    "RenderOutcome",
    "RenderStage",
    "TraceRelayOutcome",
]

#: How a job settled; ``superseded`` was replaced by a newer render first.
RenderOutcome = Literal["done", "failed", "superseded"]
RenderStage = Literal["source", "render", "split", "solids", "thumbnail", "write"]
#: What a `render_jobs` call that failed was doing: the per-scrape read of the
#: queue gauges, or starting a submitted job's workflow or cancelling a superseded one.
StoreOperation = Literal["read", "start_workflow", "cancel_workflow"]
#: Why the Postgres event bus did not publish an event: its payload was over the
#: NOTIFY cap, its outbox overflowed, or the database write failed.
EventDropReason = Literal["oversize", "outbox_full", "error"]
#: What became of a browser trace batch the relay accepted (spec 2026-10-01 §5.2): posted
#: to the collector, refused or unreachable there, dropped because the queue was full,
#: or still queued when the process stopped.
TraceRelayOutcome = Literal["forwarded", "failed", "queue_full", "shutdown"]

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
        self.store_info = Gauge(
            "scadbuddy_render_store_info",
            'Which job store holds the render jobs: backend="postgres" (the '
            "render_jobs projection); always 1.",
            ["backend"],
            registry=r,
        )
        self.store_up = Gauge(
            "scadbuddy_render_store_up",
            "1 when the last per-scrape read of the queue from its job store worked, 0 "
            "when it failed. While 0, the queue gauges hold their last good values.",
            registry=r,
        )
        self.store_errors = Counter(
            "scadbuddy_render_store_errors",
            "render_jobs calls that failed, by what they were doing.",
            ["operation"],
            registry=r,
        )
        self.listener_connected = Gauge(
            "scadbuddy_render_queue_listener_connected",
            "1 while this process's LISTEN connection (the event bus's, scadbuddy_events) "
            "is up; 0 while it is down and reconnecting.",
            registry=r,
        )
        self.listener_reconnects = Counter(
            "scadbuddy_render_queue_listener_reconnects",
            "Times this process's LISTEN connection was re-established after it "
            "dropped. A first connection is not counted.",
            registry=r,
        )
        # The Postgres event bus (spec §7): what went out, what came in, what was lost.
        self.events_published = Counter(
            "scadbuddy_events_published",
            "Events written to the event log and NOTIFYed on scadbuddy_events (Postgres).",
            registry=r,
        )
        self.events_dropped = Counter(
            "scadbuddy_events_dropped",
            "Events the Postgres bus did not publish, by reason.",
            ["reason"],
            registry=r,
        )
        self.events_received = Counter(
            "scadbuddy_events_received",
            "Events this process heard on scadbuddy_events and delivered to its subscribers.",
            registry=r,
        )
        self.events_resyncs = Counter(
            "scadbuddy_events_resyncs",
            "bus.resync markers delivered because the LISTEN connection came back after a drop.",
            registry=r,
        )
        self.event_log_pruned = Counter(
            "scadbuddy_event_log_pruned",
            "Rows removed from the event log by SCADBUDDY_EVENT_LOG_RETENTION_*.",
            registry=r,
        )
        self.render_rejected = Counter(
            "scadbuddy_render_jobs_rejected",
            "Render requests refused (503) because SCADBUDDY_RENDER_QUEUE_MAX were waiting.",
            registry=r,
        )
        self.render_retried = Counter(
            "scadbuddy_render_jobs_retried",
            "Running jobs requeued because their worker stopped heartbeating.",
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
        self.oldest_pending = Gauge(
            "scadbuddy_render_queue_oldest_seconds",
            "How long the longest-waiting job has waited for a worker; 0 with none.",
            registry=r,
        )
        self.queue_depth_slo = Gauge(
            "scadbuddy_render_queue_depth_slo",
            "SCADBUDDY_RENDER_QUEUE_DEPTH_SLO: the waiting-job count to alert above.",
            registry=r,
        )
        self.latency_slo = Gauge(
            "scadbuddy_render_latency_slo_seconds",
            "SCADBUDDY_RENDER_LATENCY_SLO: the submit-to-settled latency target.",
            registry=r,
        )
        self.running = Gauge(
            "scadbuddy_render_jobs_running",
            "Render jobs a worker is on right now.",
            registry=r,
        )
        self.queue_max = Gauge(
            "scadbuddy_render_queue_max",
            "SCADBUDDY_RENDER_QUEUE_MAX: waiting jobs past which a submit gets a 503; "
            "0 accepts everything.",
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

        # The content store under the blob store (spec 2026-09-27 §6.2, #426).
        self.store_ops = Counter(
            "scadbuddy_store_operations_total",
            "Blob store calls by operation (put, get, delete) and outcome"
            " (ok, full, corrupt, missing).",
            ["op", "outcome"],
            registry=r,
        )
        self.worker_cache = Counter(
            "scadbuddy_worker_cache_total",
            "Piece fetches answered from this process's local cache (hit) or downloaded (miss).",
            ["result"],
            registry=r,
        )
        self.store_blobs = Gauge(
            "scadbuddy_store_blobs", "Distinct objects in the blob store.", registry=r
        )
        self.store_bytes = Gauge(
            "scadbuddy_store_bytes", "Bytes in the blob store, by kind.", ["kind"], registry=r
        )
        self.store_max_blobs = Gauge(
            "scadbuddy_store_max_blobs", "SCADBUDDY_STORE_MAX_COUNT; 0 is no limit.", registry=r
        )
        self.store_max_bytes = Gauge(
            "scadbuddy_store_max_bytes",
            "SCADBUDDY_STORE_MAX_TOTAL_BYTES; 0 is no limit.",
            registry=r,
        )
        self.store_render_key_fallback = Gauge(
            "scadbuddy_store_render_key_fallback",
            "1 while render workers hold the full Bambuddy key (spec 2026-09-27 §9).",
            registry=r,
        )
        self.worker_cache_bytes = Gauge(
            "scadbuddy_worker_cache_bytes", "Bytes in this process's local piece cache.", registry=r
        )

        # Uploads for `// file` parameters (#296). The usage gauges are read from the
        # store per scrape, like the render queue's from the projection.
        self.assets_stored = Gauge(
            "scadbuddy_assets_stored",
            "Distinct files stored for `// file` parameters under data/assets/.",
            registry=r,
        )
        self.assets_bytes = Gauge(
            "scadbuddy_assets_bytes",
            "Total bytes of the files stored for `// file` parameters.",
            registry=r,
        )
        self.assets_max_count = Gauge(
            "scadbuddy_assets_max_count",
            "SCADBUDDY_ASSET_MAX_COUNT: stored files past which an upload is refused; "
            "0 is no limit.",
            registry=r,
        )
        self.assets_max_bytes = Gauge(
            "scadbuddy_assets_max_bytes",
            "SCADBUDDY_ASSET_MAX_TOTAL_BYTES: stored bytes past which an upload is "
            "refused; 0 is no limit.",
            registry=r,
        )
        self.assets_rejected = Counter(
            "scadbuddy_assets_rejected",
            "Uploads refused (413) because the store was at one of its caps.",
            registry=r,
        )
        self.assets_swept = Counter(
            "scadbuddy_assets_swept",
            "Stored files removed by the sweep because nothing referenced or used them.",
            registry=r,
        )

        # The browser trace relay (spec 2026-10-01 §5.2): it watches the tracing path
        # itself, and is the one metric the tracing design adds.
        self.trace_relay_batches = Counter(
            "scadbuddy_trace_relay_batches_total",
            "Browser trace batches the relay accepted, by what became of them.",
            ["outcome"],
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
        for operation in get_args(StoreOperation):
            self.store_errors.labels(operation)
        for reason in get_args(EventDropReason):
            self.events_dropped.labels(reason)
        for outcome in get_args(TraceRelayOutcome):
            self.trace_relay_batches.labels(outcome)

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
