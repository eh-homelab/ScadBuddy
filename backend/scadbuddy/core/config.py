from __future__ import annotations

import functools
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

DEFAULT_OPENSCAD = "openscad"
DEFAULT_DATA_DIR = Path("/data")
DEFAULT_RENDER_TIMEOUT = 120.0
# Jobs rendered at once by each process: each is one or more `openscad` processes.
DEFAULT_RENDER_CONCURRENCY = 2
# Per-colour wrapper renders one job runs at once (spec §6.3). 0 (the default) derives
# it from the CPUs this process may use -- see `default_solid_concurrency`.
DEFAULT_SOLID_CONCURRENCY = 0
# The derived default never goes past this, however many cores the host has: past a
# handful of colours at once the gain is gone and the memory is not.
MAX_DEFAULT_SOLID_CONCURRENCY = 8
CGROUP_ROOT = Path("/sys/fs/cgroup")
# Admission control, OFF by default: 0 accepts every render. Set, a render that would
# be a new job while this many already wait is refused with 503 + Retry-After.
# Superseded and coalesced submits never count against it.
DEFAULT_RENDER_QUEUE_MAX = 0
# How long a job may wait for a worker before it is failed unrendered; 0 (the
# default) never expires one -- every submit is accepted and, in time, rendered.
DEFAULT_RENDER_QUEUE_TIMEOUT = 0.0
# How often an idle worker looks for work it was not woken for: jobs another replica
# submitted, or ones a reaped lease put back. With Postgres this is the poll only
# while the LISTEN connection is down; it is also a failed claim's back-off.
DEFAULT_RENDER_POLL_INTERVAL = 1.0
# Postgres only: while the LISTEN connection is up, a NOTIFY wakes the workers for
# every job any replica queues, and the poll only has to catch a notification lost
# around a reconnect -- so it can be long.
DEFAULT_RENDER_FALLBACK_POLL_INTERVAL = 30.0
# A running job whose worker has not heartbeated for this long is presumed lost and
# requeued (Postgres only; heartbeats go every third of it).
DEFAULT_RENDER_LEASE_TIMEOUT = 60.0
# Tries a job gets before a lost worker fails it for good.
DEFAULT_RENDER_MAX_ATTEMPTS = 2
# SLO targets. Not limits -- nothing is refused or dropped on reaching them. They are
# exported beside the measurements (`scadbuddy_render_queue_depth_slo`,
# `scadbuddy_render_latency_slo_seconds`) for alerts to compare against.
DEFAULT_RENDER_QUEUE_DEPTH_SLO = 16
DEFAULT_RENDER_LATENCY_SLO = 60.0
DEFAULT_DATABASE_POOL_SIZE = 10
# The editor's parse check does not go through the render queue, so it carries its own
# budget rather than borrowing the render one: the pod's worst case is the two added
# together, and that is a number worth declaring rather than discovering. One is
# plenty — a check parses and exports parameters, it renders no geometry.
DEFAULT_CHECK_CONCURRENCY = 1
DEFAULT_JOB_TTL = 86400.0
DEFAULT_OPENSCAD_LSP = "openscad-lsp"
# Each open editor is one openscad-lsp process for as long as the tab stays open, so
# the cap is on sessions rather than on work: past it an editor simply goes without
# completion and hover, which it already has to cope with when no server is installed.
DEFAULT_LSP_SESSIONS = 4
DEFAULT_FONTS_CATALOGUE_TTL = 86400.0
DEFAULT_GIT_TIMEOUT = 30.0
# A shallow clone is still unbounded in size, and every checkout shares the data
# volume (#213). NopSCADlib, the largest curated library, is about 60 MB.
DEFAULT_LIBRARY_MAX_BYTES = 200_000_000


@dataclass(frozen=True)
class Config:
    openscad: str = DEFAULT_OPENSCAD
    data_dir: Path = DEFAULT_DATA_DIR
    render_timeout: float = DEFAULT_RENDER_TIMEOUT
    render_concurrency: int = DEFAULT_RENDER_CONCURRENCY
    solid_concurrency: int = DEFAULT_SOLID_CONCURRENCY
    render_queue_max: int = DEFAULT_RENDER_QUEUE_MAX
    render_queue_timeout: float = DEFAULT_RENDER_QUEUE_TIMEOUT
    render_poll_interval: float = DEFAULT_RENDER_POLL_INTERVAL
    render_fallback_poll_interval: float = DEFAULT_RENDER_FALLBACK_POLL_INTERVAL
    render_lease_timeout: float = DEFAULT_RENDER_LEASE_TIMEOUT
    render_max_attempts: int = DEFAULT_RENDER_MAX_ATTEMPTS
    render_queue_depth_slo: int = DEFAULT_RENDER_QUEUE_DEPTH_SLO
    render_latency_slo: float = DEFAULT_RENDER_LATENCY_SLO
    check_concurrency: int = DEFAULT_CHECK_CONCURRENCY
    job_ttl: float = DEFAULT_JOB_TTL
    # Never sent to the browser: the catalogue is fetched server-side (issue #82).
    google_fonts_api_key: str | None = None
    fonts_catalogue_ttl: float = DEFAULT_FONTS_CATALOGUE_TTL
    # Bounds every git call and the wait for the model repository's write lock.
    git_timeout: float = DEFAULT_GIT_TIMEOUT
    # Per model, never per process (#93): the checkouts of the libraries ONE model
    # pins, and so the whole of OPENSCADPATH. Empty here; whoever resolved a
    # model's source sets it with `dataclasses.replace`, which carries it through
    # every openscad call for that model -- schema, render and solids alike.
    library_path: tuple[Path, ...] = ()
    openscad_lsp: str = DEFAULT_OPENSCAD_LSP
    lsp_sessions: int = DEFAULT_LSP_SESSIONS
    # The most one library's clone may take on the data volume (#213).
    library_max_bytes: int = DEFAULT_LIBRARY_MAX_BYTES

    def __post_init__(self) -> None:
        # Sizes the worker pool and the thumbnail executor, neither of which can be
        # empty; said here, by name, rather than as a ThreadPoolExecutor ValueError.
        if self.render_concurrency < 1:
            raise ValueError(
                f"SCADBUDDY_RENDER_CONCURRENCY must be at least 1, not {self.render_concurrency}"
            )
        for name, value in (
            ("SCADBUDDY_SOLID_CONCURRENCY", self.solid_concurrency),
            ("SCADBUDDY_RENDER_QUEUE_MAX", self.render_queue_max),
            ("SCADBUDDY_RENDER_QUEUE_TIMEOUT", self.render_queue_timeout),
            ("SCADBUDDY_RENDER_QUEUE_DEPTH_SLO", self.render_queue_depth_slo),
            ("SCADBUDDY_RENDER_LATENCY_SLO", self.render_latency_slo),
        ):
            if value < 0:
                raise ValueError(f"{name} must be at least 0, not {value}")
        for name, value in (
            ("SCADBUDDY_RENDER_POLL_INTERVAL", self.render_poll_interval),
            ("SCADBUDDY_RENDER_FALLBACK_POLL_INTERVAL", self.render_fallback_poll_interval),
            ("SCADBUDDY_RENDER_LEASE_TIMEOUT", self.render_lease_timeout),
        ):
            if value <= 0:
                raise ValueError(f"{name} must be more than 0, not {value}")
        if self.render_max_attempts < 1:
            raise ValueError(
                f"SCADBUDDY_RENDER_MAX_ATTEMPTS must be at least 1, not {self.render_max_attempts}"
            )
        # Sizes a semaphore, which refuses a negative count; zero refuses every editor.
        if self.lsp_sessions < 0:
            raise ValueError(f"SCADBUDDY_LSP_SESSIONS must be at least 0, not {self.lsp_sessions}")
        # Zero or less would refuse every library, however small.
        if self.library_max_bytes < 1:
            raise ValueError(
                f"SCADBUDDY_LIBRARY_MAX_BYTES must be at least 1, not {self.library_max_bytes}"
            )

    def solid_slots(self) -> int:
        """How many of one job's per-colour wrapper renders may run at once."""
        if self.solid_concurrency:
            return self.solid_concurrency
        return default_solid_concurrency(available_cpus(), self.render_concurrency)


def default_solid_concurrency(cpus: int, render_concurrency: int) -> int:
    """The CPUs this process may use, shared out between its render workers.

    Every worker can be in its solids stage at once, so the pod's worst case is
    ``render_concurrency`` x this many `openscad` processes. Splitting the CPUs keeps
    that at one process per core rather than oversubscribing them, and the timeout is
    why that matters: each wrapper render gets its own `SCADBUDDY_RENDER_TIMEOUT`, so
    a colour slowed by contention is one that times out and falls back to its open
    split mesh. Never below one (sequential, as before #282), never above
    `MAX_DEFAULT_SOLID_CONCURRENCY`.
    """
    return max(1, min(MAX_DEFAULT_SOLID_CONCURRENCY, cpus // max(1, render_concurrency)))


@functools.cache
def available_cpus(cgroup_root: Path = CGROUP_ROOT) -> int:
    """The CPUs this process may actually use: its affinity mask, capped by a cgroup
    v2 CPU limit (a Kubernetes `limits.cpu`) where there is one.

    `os.cpu_count()` alone is the node's core count, which inside a pod limited to two
    CPUs would size the pool for the whole host. A fractional limit rounds up: 1.5
    CPUs can keep two processes busy for most of a period.
    """
    cpus = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else os.cpu_count()
    cpus = cpus or 1
    try:
        quota, period = (cgroup_root / "cpu.max").read_text(encoding="utf-8").split()[:2]
    except (OSError, ValueError):
        return cpus
    if quota == "max":
        return cpus
    try:
        limit = math.ceil(int(quota) / int(period))
    except (ValueError, ZeroDivisionError):
        return cpus
    return max(1, min(cpus, limit))


def load_config(env: Mapping[str, str] | None = None) -> Config:
    source: Mapping[str, str] = os.environ if env is None else env
    data_dir = source.get("SCADBUDDY_DATA_DIR")
    return Config(
        openscad=source.get("SCADBUDDY_OPENSCAD") or DEFAULT_OPENSCAD,
        data_dir=Path(data_dir) if data_dir else DEFAULT_DATA_DIR,
        render_timeout=float(source.get("SCADBUDDY_RENDER_TIMEOUT") or DEFAULT_RENDER_TIMEOUT),
        render_concurrency=int(
            source.get("SCADBUDDY_RENDER_CONCURRENCY") or DEFAULT_RENDER_CONCURRENCY
        ),
        solid_concurrency=int(
            source.get("SCADBUDDY_SOLID_CONCURRENCY") or DEFAULT_SOLID_CONCURRENCY
        ),
        render_queue_max=int(source.get("SCADBUDDY_RENDER_QUEUE_MAX") or DEFAULT_RENDER_QUEUE_MAX),
        render_queue_timeout=float(
            source.get("SCADBUDDY_RENDER_QUEUE_TIMEOUT") or DEFAULT_RENDER_QUEUE_TIMEOUT
        ),
        render_poll_interval=float(
            source.get("SCADBUDDY_RENDER_POLL_INTERVAL") or DEFAULT_RENDER_POLL_INTERVAL
        ),
        render_fallback_poll_interval=float(
            source.get("SCADBUDDY_RENDER_FALLBACK_POLL_INTERVAL")
            or DEFAULT_RENDER_FALLBACK_POLL_INTERVAL
        ),
        render_lease_timeout=float(
            source.get("SCADBUDDY_RENDER_LEASE_TIMEOUT") or DEFAULT_RENDER_LEASE_TIMEOUT
        ),
        render_max_attempts=int(
            source.get("SCADBUDDY_RENDER_MAX_ATTEMPTS") or DEFAULT_RENDER_MAX_ATTEMPTS
        ),
        render_queue_depth_slo=int(
            source.get("SCADBUDDY_RENDER_QUEUE_DEPTH_SLO") or DEFAULT_RENDER_QUEUE_DEPTH_SLO
        ),
        render_latency_slo=float(
            source.get("SCADBUDDY_RENDER_LATENCY_SLO") or DEFAULT_RENDER_LATENCY_SLO
        ),
        check_concurrency=int(
            source.get("SCADBUDDY_CHECK_CONCURRENCY") or DEFAULT_CHECK_CONCURRENCY
        ),
        job_ttl=float(source.get("SCADBUDDY_JOB_TTL") or DEFAULT_JOB_TTL),
        google_fonts_api_key=source.get("SCADBUDDY_GOOGLE_FONTS_API_KEY") or None,
        fonts_catalogue_ttl=float(
            source.get("SCADBUDDY_FONTS_CATALOGUE_TTL") or DEFAULT_FONTS_CATALOGUE_TTL
        ),
        git_timeout=float(source.get("SCADBUDDY_GIT_TIMEOUT") or DEFAULT_GIT_TIMEOUT),
        openscad_lsp=source.get("SCADBUDDY_OPENSCAD_LSP") or DEFAULT_OPENSCAD_LSP,
        lsp_sessions=int(source.get("SCADBUDDY_LSP_SESSIONS") or DEFAULT_LSP_SESSIONS),
        library_max_bytes=int(
            source.get("SCADBUDDY_LIBRARY_MAX_BYTES") or DEFAULT_LIBRARY_MAX_BYTES
        ),
    )
