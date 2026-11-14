"""Prometheus metrics, served at ``GET /metrics``.

One registry per app rather than prometheus_client's process-global one: the
tests build many apps in one process, and a second registration of the same name
on the global registry raises. Labels are kept to small closed sets (a stage, an
outcome, a route template) -- never a slug, a job id or a raw path.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Iterator
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
from prometheus_client.core import GaugeMetricFamily, Metric
from prometheus_client.metrics import MetricWrapperBase
from prometheus_client.registry import Collector
from starlette.types import ASGIApp, Message, Receive, Scope, Send

__all__ = [
    "CONTENT_TYPE_LATEST",
    "ROLE_SERIES",
    "HttpMetrics",
    "Metrics",
    "MetricsRole",
    "RenderOutcome",
    "RenderStage",
    "SettlePass",
    "TraceRelayOutcome",
]

#: How a job settled; ``superseded`` was replaced by a newer render first.
RenderOutcome = Literal["done", "failed", "superseded", "cancelled"]

#: Which workflow a queue job runs, ``render.job_models.JobTableKind`` (spec 2026-09-27
#: §7): spelled out here, not imported, so the metrics never load the render stack.
RenderKind = Literal["render", "arrange"]
RenderStage = Literal[
    "source", "render", "split", "solids", "paint", "thumbnail", "write", "recolour"
]
#: How a native slice ended (`native_slices.status`).
NativeSliceOutcome = Literal["completed", "failed", "cancelled"]
#: What a `render_jobs` call that failed was doing: the per-scrape read of the
#: queue gauges, a render's start (Temporal unavailable, or the start refused or
#: unstartable; one still accepting is `render_accept_pending`), or releasing or
#: cancelling a superseded job.
StoreOperation = Literal["read", "start_workflow", "cancel_workflow", "release_not_ready"]
#: Which settle pass failed a row nothing would settle: ``closed``, a row whose
#: ``render-<render_key>`` run closed without settling it (terminated, timed out). The
#: ``legacy`` pass went with #1142, whose migration failed the rows it was for.
SettlePass = Literal["closed"]
#: Why the Postgres event bus did not publish an event: its payload was over the
#: NOTIFY cap, its outbox overflowed, or the database write failed.
EventDropReason = Literal["oversize", "outbox_full", "error"]
#: What became of a browser trace batch the relay received from a page (spec 2026-10-01
#: §5.2): posted to the collector, refused or unreachable there (or the forwarder had
#: died), dropped because the queue was full, refused or still queued once the process
#: was stopping, over a rate limit (429), or refused as malformed, too large or not
#: OTLP/JSON (400, 413, 415). Every outcome but ``forwarded`` is a lost batch (#1161).
TraceRelayOutcome = Literal[
    "forwarded", "failed", "queue_full", "shutdown", "rate_limited", "rejected"
]

# A render is bounded by SCADBUDDY_RENDER_TIMEOUT (120 s by default) per openscad
# pass, and a multi-colour job makes one pass per colour, so the tail runs long.
RENDER_BUCKETS = (0.25, 0.5, 1, 2, 5, 10, 20, 30, 60, 120, 240, 480, 900)
#: A native slice: the slicer queue's wait (up to two hours) and four `slice_plate`
#: attempts of up to 30 minutes, past `RENDER_BUCKETS`' 15 minutes (Task 14/15 review M3).
SLICE_BUCKETS = (5, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 16200)
#: Up to 120 s (#2293): list routes took 10 to 16 s and a save with its polling ~37 s,
#: and past the top finite bucket a quantile reads as that bucket.
HTTP_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 15, 30, 60, 120)
#: An operation, from its record to its end (#2295): most answer in a second, a save with
#: its render ~37 s, a run up to its 5-minute ``RUN_TIMEOUT``, a slice quote 30 minutes.
OPERATION_BUCKETS = (0.1, 0.25, 0.5, 1, 2.5, 5, 10, 15, 30, 60, 120, 300, 600, 1800)

#: How an operation ended: its record's final status (`operations/store.py`
#: `OperationStatus`), spelled out here so the metrics never load the operations stack.
OperationOutcome = Literal["succeeded", "failed"]

#: Which process the metrics are for: the API, or one of `python -m scadbuddy.worker`'s
#: queues (the render worker, ``--queue bambuddy``, ``--queue projects``).
MetricsRole = Literal["api", "render", "print", "projects"]

#: Every worker: its build, the events it publishes and the event-loop watch.
_WORKER_COMMON = frozenset(
    {
        "build_info",
        "events_published",
        "events_dropped",
        "event_loop_lag",
        "event_loop_stalls",
        "default_executor_wait",
        "default_executor_stalls",
    }
)
#: No worker listens on the event bus or ends an operation's record (#5298): the API
#: logs their events, ends the records and counts them (`OperationStore.finish`, #2295),
#: so `events_received`, `operations_finished` and `operation_duration` are the API's.
#: The dashboards pick the workers' operation kinds by name
#: (`backend/tests/test_dashboard_operation_kinds.py`).

#: The attributes each worker role registers (#664): only what that worker sets, so its
#: /metrics never carries the API's series as constant zeros (`scadbuddy_render_store_up
#: 0` fires ScadBuddyRenderStoreUnreadable). The API registers every series. When a
#: worker starts setting a ``metrics.<attribute>``, add the attribute to its role here, or
#: that worker never exports it.
ROLE_SERIES: dict[MetricsRole, frozenset[str]] = {
    "render": _WORKER_COMMON
    | {
        "activity_write_failures",
        "render_finished",
        "render_duration",
        "job_latency",
        "queue_wait",
        "project_retries",
        "stage_duration",
        "store_ops",
        "worker_cache",
        "worker_cache_bytes",
        "render_data_bytes",
        "store_render_key_fallback",
        "crud_client_seconds",
        "asset_prune_failures",
    },
    "print": _WORKER_COMMON
    | {
        "print_follows_running",
        "rack_watch_settle_failed",
        "printer_watch_write_failed",
        "crud_client_seconds",
    },
    "projects": _WORKER_COMMON | {"crud_client_seconds"},
}


class Metrics:
    def __init__(
        self, registry: CollectorRegistry | None = None, *, role: MetricsRole = "api"
    ) -> None:
        """``role`` other than ``api`` keeps only `ROLE_SERIES`' series in ``registry``;
        the others stay as attributes, counted but never exported."""
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
            "Render requests accepted onto the queue as a new job."
            " A request answered 202 accepting is counted by the follow that gets its"
            " answer; a follow sent again after a dropped answer counts it again (#2340).",
            ["kind"],
            registry=r,
        )
        self.render_coalesced = Counter(
            "scadbuddy_render_jobs_coalesced",
            "Render requests answered with an identical job already waiting, or one"
            " already done whose result is still held (#4870)."
            " A request answered 202 accepting is counted by the follow that gets its"
            " answer; a follow sent again after a dropped answer counts it again (#2340).",
            ["kind"],
            registry=r,
        )
        self.render_accept_pending = Counter(
            "scadbuddy_render_accept_pending",
            "Render requests whose job was not answered in time: answered 503"
            " command-still-accepting (the client sends the same request again), or 202"
            " accepting to a client that follows it (#2340); once per request, by kind.",
            ["kind"],
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
        self.settle_failed = Counter(
            "scadbuddy_render_settle_failed",
            "Render jobs a settle pass failed because nothing would settle them, by pass."
            " Each one also counts as a failed job.",
            ["pass"],
            registry=r,
        )
        self.settle_errors = Counter(
            "scadbuddy_render_settle_errors",
            "Settle passes that stopped before judging every row (Temporal did not"
            " answer, or the pass raised), by pass: their rows wait for the next one.",
            ["pass"],
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
        self.activity_write_failures = Counter(
            "scadbuddy_activity_write_failures",
            "Activity rows a source could not write; the repair pass restores them.",
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
            "Render requests refused (503) because SCADBUDDY_RENDER_QUEUE_MAX were waiting."
            " A request answered 202 accepting and then refused is not counted: its follow"
            " cannot tell a render's queue-full from an Arrange's (#2340, #3575).",
            ["kind"],
            registry=r,
        )
        # `kind` is "render" or "arrange" (spec 2026-09-27 §7): an arrange shares the
        # queue but is no render, so the render rate leaves it out.
        for kind in get_args(RenderKind):
            for counter in (
                self.render_submitted,
                self.render_coalesced,
                self.render_rejected,
                self.render_accept_pending,
            ):
                counter.labels(kind)
        self.render_retried = Counter(
            "scadbuddy_render_jobs_retried",
            "Running jobs requeued because their worker stopped heartbeating.",
            registry=r,
        )
        self.render_finished = Counter(
            "scadbuddy_render_jobs_finished",
            "Render jobs that left the queue, by kind and outcome.",
            ["kind", "outcome"],
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
        self.print_follows_running = Gauge(
            "scadbuddy_print_follows_running",
            "Prints this process is following (FollowPrint attempts); at FOLLOW_SLOTS "
            "the next prints wait on the follow queue, unfollowed.",
            registry=r,
        )
        self.rack_watch_settle_failed = Counter(
            "scadbuddy_rack_watch_settle_failed",
            "LAN prints whose rack picks the printer watch could not settle as the printer "
            "ended them (#5138). Not retried: lan_wait or the run's follow may settle them, "
            "else they stay pending until PENDING_PICK_MAX_AGE.",
            registry=r,
        )
        self.printer_watch_write_failed = Counter(
            "scadbuddy_printer_watch_write_failed",
            "LAN reports the printer watch could not write, or touches it could not make "
            "(#5298), and owed reports given up after their resends. The watch goes on: an "
            "unwritten report is written at the next quiet tick, a refused one (a 4xx other "
            "than 429) is dropped, and the row's seen_at ages until a write succeeds.",
            registry=r,
        )
        self.queue_wait = Histogram(
            "scadbuddy_render_queue_wait_seconds",
            "Time from submit until its workflow began the render, by kind.",
            ["kind"],
            buckets=RENDER_BUCKETS,
            registry=r,
        )
        self.project_retries = Counter(
            "scadbuddy_render_project_retries_total",
            "Retried writes of a render job's row (the `project` activity), which retries "
            "without limit: counted as each retry starts, so one per failed write. A "
            "steady rise is a write that never lands (#857). Alert on this, not the log: "
            "the warning is logged only at the 12th attempt and every 100th after.",
            registry=r,
        )
        self.render_duration = Histogram(
            "scadbuddy_render_duration_seconds",
            "Time a worker spent on a job, by kind and outcome.",
            ["kind", "outcome"],
            buckets=RENDER_BUCKETS,
            registry=r,
        )
        self.job_latency = Histogram(
            "scadbuddy_render_job_latency_seconds",
            "Time from submit to a settled job, by kind and outcome: what the user waits for.",
            ["kind", "outcome"],
            buckets=RENDER_BUCKETS,
            registry=r,
        )
        self.asset_prune_failures = Counter(
            "scadbuddy_render_asset_prune_failures",
            "Batches of a render worker's prune of its local upload copies whose last uses"
            " the API did not answer (#5298): their copies wait for the next prune.",
            registry=r,
        )
        self.crud_client_seconds = Histogram(
            "scadbuddy_crud_client_seconds",
            "Each attempt of a worker's call to the API's CRUD routes (#5298), by caller, "
            "area (the first path segment after the caller) and outcome: ok, refused, "
            "route_missing (an old API pod), unavailable (5xx), transport or reexchange "
            "(a 401 or out-of-scope 403: the credential is stale, so the call retries with "
            "a backoff until a new one is exchanged or its 30 s budget ends). A streamed answer "
            "(the storefront's preview) is counted when its body ends: ok once relayed to "
            "the end, relay_failed when it broke or was closed before.",
            ["caller", "area", "outcome"],
            buckets=HTTP_BUCKETS,
            registry=r,
        )
        self.stage_duration = Histogram(
            "scadbuddy_render_stage_seconds",
            "Time spent in each step of a render.",
            ["stage"],
            buckets=RENDER_BUCKETS,
            registry=r,
        )

        # A native slice, from its start to its record (plan 2026-10-16 Task 15): what
        # the print dialog waits for on `slicer_backend` `native`.
        self.native_slice_duration = Histogram(
            "scadbuddy_native_slice_seconds",
            "Time from a native slice's start to its record, by outcome"
            " (completed, failed, cancelled).",
            ["outcome"],
            buckets=SLICE_BUCKETS,
            registry=r,
        )

        # Operations (#1053, #2295): `kind` is a registered `OperationKind`'s name, a
        # closed set each process pre-creates (`operation_kinds`). Counted where the
        # record ends (`OperationStore.finish`): in the process whose worker ran it, or
        # in the API for a worker that ends it through the CRUD routes (#5298).
        self.operation_duration = Histogram(
            "scadbuddy_operation_duration_seconds",
            "Time from an operation's record to its end, by kind and outcome (succeeded, failed).",
            ["kind", "outcome"],
            buckets=OPERATION_BUCKETS,
            registry=r,
        )
        self.operations_finished = Counter(
            "scadbuddy_operations_finished",
            "Operations whose record ended, by kind and outcome (succeeded, failed).",
            ["kind", "outcome"],
            registry=r,
        )
        self.operations_deferred = Counter(
            "scadbuddy_operations_deferred",
            "Operation requests not answered within the deadline: a 202 to follow (or a"
            " 503 still-accepting for a key the follow URL cannot carry), by kind; once"
            " per request, so a re-send counts again.",
            ["kind"],
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
        # The worker's event-loop watch (#3788, `core.loop_watch`).
        self.event_loop_lag = Histogram(
            "scadbuddy_event_loop_lag_seconds",
            "How late a no-op posted to the worker's event loop ran.",
            buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60),
            registry=r,
        )
        self.event_loop_stalls = Counter(
            "scadbuddy_event_loop_stalls",
            "Times the worker's event loop did not run for over a second; each is logged"
            " with every thread's stack.",
            registry=r,
        )
        self.default_executor_wait = Histogram(
            "scadbuddy_default_executor_wait_seconds",
            "How long a no-op handed to the event loop's default executor (asyncio.to_thread)"
            " waited for a thread.",
            buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60),
            registry=r,
        )
        self.default_executor_stalls = Counter(
            "scadbuddy_default_executor_stalls",
            "Times the default executor had no free thread for over a second; each is logged"
            " with every thread's stack.",
            registry=r,
        )
        self.render_data_bytes = Gauge(
            "scadbuddy_render_data_bytes",
            "Bytes allocated under the render worker's data directory, as kubelet counts an"
            " emptyDir against its sizeLimit.",
            registry=r,
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
            "Browser trace batches the relay received from a page, by what became of them.",
            ["outcome"],
            registry=r,
        )

        # #4975 (spec 2026-11-01 §9.1): what SCADBUDDY_AUTH_MODE=enforce would refuse,
        # served in `observe`. Both labels are closed sets (`auth/routes.py`).
        self.auth_would_refuse = Counter(
            "scadbuddy_auth_would_refuse",
            "Requests enforce would refuse, served in observe, by route class and reason.",
            ["class", "reason"],
            registry=r,
        )
        # #5494 (spec 2026-11-05 §8 step 3): the service-token exchange, by caller
        # (`unknown` for a proof that named none), proof and result. Closed sets
        # (`api/service_token.py`).
        self.service_token_exchanges = Counter(
            "scadbuddy_service_token_exchanges",
            "Service-token exchanges, by caller, proof and result.",
            ["caller", "proof", "result"],
            registry=r,
        )
        self.service_jwks_stale_uses = Counter(
            "scadbuddy_service_jwks_stale_uses",
            "Service tokens checked against a cluster JWKS that could not be refreshed.",
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
        # `kind` is the job's (spec 2026-09-27 §7), so Arrange is charted apart (#2294).
        for kind in get_args(RenderKind):
            self.queue_wait.labels(kind)
            for outcome in get_args(RenderOutcome):
                self.render_finished.labels(kind, outcome)
                self.job_latency.labels(kind, outcome)
            for outcome in ("done", "failed"):
                self.render_duration.labels(kind, outcome)
        for stage in get_args(RenderStage):
            self.stage_duration.labels(stage)
        for slice_outcome in get_args(NativeSliceOutcome):
            self.native_slice_duration.labels(slice_outcome)
        for operation in get_args(StoreOperation):
            self.store_errors.labels(operation)
        for settle_pass in get_args(SettlePass):
            self.settle_failed.labels(settle_pass)
            self.settle_errors.labels(settle_pass)
        for reason in get_args(EventDropReason):
            self.events_dropped.labels(reason)
        for outcome in get_args(TraceRelayOutcome):
            self.trace_relay_batches.labels(outcome)

        # Postgres connections (#5226): read from `core/pg_pool.py`'s registry per scrape,
        # in every role (the loop below drops only the metric wrappers).
        r.register(_PoolCollector())

        if role != "api":
            keep = ROLE_SERIES[role]
            for name, value in vars(self).items():
                if isinstance(value, MetricWrapperBase) and name not in keep:
                    r.unregister(value)

    def operation_kinds(self, kinds: Iterable[str]) -> None:
        """Make each kind's operation series at zero, as the other labelled series are
        made above, once the registered kinds are known."""
        for kind in kinds:
            self.operations_deferred.labels(kind)
            for outcome in get_args(OperationOutcome):
                self.operations_finished.labels(kind, outcome)
                self.operation_duration.labels(kind, outcome)

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


class _PoolCollector(Collector):
    """This process's Postgres pools and its ``LISTEN`` connection, by pool name: the
    connections each holds now, the most it may, and the callers waiting for one.
    ``sum by (pod)`` of the most is what a pod's pools may take of ``max_connections``;
    the short-lived lock and claim connections opened outside any pool come on top
    (README "Running it")."""

    def describe(self) -> list[Metric]:
        return list(self._families())

    def collect(self) -> Iterator[Metric]:
        from scadbuddy.core.pg_pool import pools

        open_now, most, waiting = self._families()
        totals: dict[str, list[int]] = {}
        for use in pools():
            total = totals.setdefault(use.name, [0, 0, 0])
            total[0] += use.open
            total[1] += use.max_size
            total[2] += use.waiting
        for name, (now, top, queued) in sorted(totals.items()):
            open_now.add_metric([name], now)
            most.add_metric([name], top)
            waiting.add_metric([name], queued)
        yield open_now
        yield most
        yield waiting

    @staticmethod
    def _families() -> tuple[GaugeMetricFamily, GaugeMetricFamily, GaugeMetricFamily]:
        return (
            GaugeMetricFamily(
                "scadbuddy_db_pool_connections",
                "Postgres connections this process's pool holds now (in use, idle or"
                " connecting), by pool; the LISTEN connection is the pool"
                " scadbuddy-listener.",
                labels=["pool"],
            ),
            GaugeMetricFamily(
                "scadbuddy_db_pool_max_connections",
                "The most Postgres connections this process's pool may hold, by pool.",
                labels=["pool"],
            ),
            GaugeMetricFamily(
                "scadbuddy_db_pool_requests_waiting",
                "Callers waiting for a connection from this process's pool now, by pool:"
                " above 0 for long, the pool is too small for its load.",
                labels=["pool"],
            ),
        )


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
