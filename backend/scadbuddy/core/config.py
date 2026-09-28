from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

DEFAULT_OPENSCAD = "openscad"
DEFAULT_DATA_DIR = Path("/data")
DEFAULT_RENDER_TIMEOUT = 120.0
# Jobs rendered at once by each process: each is one or more `openscad` processes.
DEFAULT_RENDER_CONCURRENCY = 2
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
# Each realtime socket (`WS /api/v1/ws`, #266) is a standing bus subscription: a
# queue of up to 256 events, walked on every publish. The cap bounds that cost; one
# tab holds one socket, so it is far above any real deployment's tabs.
DEFAULT_REALTIME_SOCKETS = 256
DEFAULT_FONTS_CATALOGUE_TTL = 86400.0
DEFAULT_GIT_TIMEOUT = 30.0
# A shallow clone is still unbounded in size, and every checkout shares the data
# volume (#213). NopSCADlib, the largest curated library, is about 60 MB.
DEFAULT_LIBRARY_MAX_BYTES = 200_000_000
# The files uploaded for `// file` parameters (#296). Each is small once stored (a PNG
# is downscaled to 256 px; an SVG upload is capped at 8 MiB), so these are about a
# runaway client, not normal use. 0 is no limit for either.
DEFAULT_ASSET_MAX_TOTAL_BYTES = 1_000_000_000
DEFAULT_ASSET_MAX_COUNT = 10_000
# An upload nothing references (no output, preset or job) is removed once it has not
# been uploaded again or used by a render or preset save for this long. The grace is
# what protects an upload whose render has not been submitted yet, so it has a floor.
DEFAULT_ASSET_SWEEP_GRACE = 7 * 86400.0
MIN_ASSET_SWEEP_GRACE = 3600.0
# How often the sweep runs after the one at boot; 0 turns the sweep off entirely.
DEFAULT_ASSET_SWEEP_INTERVAL = 86400.0


@dataclass(frozen=True)
class Config:
    openscad: str = DEFAULT_OPENSCAD
    data_dir: Path = DEFAULT_DATA_DIR
    render_timeout: float = DEFAULT_RENDER_TIMEOUT
    render_concurrency: int = DEFAULT_RENDER_CONCURRENCY
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
    realtime_sockets: int = DEFAULT_REALTIME_SOCKETS
    # The most one library's clone may take on the data volume (#213).
    library_max_bytes: int = DEFAULT_LIBRARY_MAX_BYTES
    asset_max_total_bytes: int = DEFAULT_ASSET_MAX_TOTAL_BYTES
    asset_max_count: int = DEFAULT_ASSET_MAX_COUNT
    asset_sweep_grace: float = DEFAULT_ASSET_SWEEP_GRACE
    asset_sweep_interval: float = DEFAULT_ASSET_SWEEP_INTERVAL

    def __post_init__(self) -> None:
        # Sizes the worker pool and the thumbnail executor, neither of which can be
        # empty; said here, by name, rather than as a ThreadPoolExecutor ValueError.
        if self.render_concurrency < 1:
            raise ValueError(
                f"SCADBUDDY_RENDER_CONCURRENCY must be at least 1, not {self.render_concurrency}"
            )
        for name, value in (
            ("SCADBUDDY_RENDER_QUEUE_MAX", self.render_queue_max),
            ("SCADBUDDY_RENDER_QUEUE_TIMEOUT", self.render_queue_timeout),
            ("SCADBUDDY_RENDER_QUEUE_DEPTH_SLO", self.render_queue_depth_slo),
            ("SCADBUDDY_RENDER_LATENCY_SLO", self.render_latency_slo),
            ("SCADBUDDY_ASSET_MAX_TOTAL_BYTES", self.asset_max_total_bytes),
            ("SCADBUDDY_ASSET_MAX_COUNT", self.asset_max_count),
            ("SCADBUDDY_ASSET_SWEEP_INTERVAL", self.asset_sweep_interval),
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
        # Also a semaphore; zero refuses every socket, and the UI polls.
        if self.realtime_sockets < 0:
            raise ValueError(
                f"SCADBUDDY_REALTIME_SOCKETS must be at least 0, not {self.realtime_sockets}"
            )
        # Zero or less would refuse every library, however small.
        if self.library_max_bytes < 1:
            raise ValueError(
                f"SCADBUDDY_LIBRARY_MAX_BYTES must be at least 1, not {self.library_max_bytes}"
            )
        # Below it, an upload waiting for its first render could be swept first.
        if self.asset_sweep_grace < MIN_ASSET_SWEEP_GRACE:
            raise ValueError(
                f"SCADBUDDY_ASSET_SWEEP_GRACE must be at least {MIN_ASSET_SWEEP_GRACE:g}, "
                f"not {self.asset_sweep_grace:g}"
            )


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
        realtime_sockets=int(source.get("SCADBUDDY_REALTIME_SOCKETS") or DEFAULT_REALTIME_SOCKETS),
        library_max_bytes=int(
            source.get("SCADBUDDY_LIBRARY_MAX_BYTES") or DEFAULT_LIBRARY_MAX_BYTES
        ),
        # Not `or`: 0 is a meaningful value for each (no limit, sweep off).
        asset_max_total_bytes=_int_or(
            source.get("SCADBUDDY_ASSET_MAX_TOTAL_BYTES"), DEFAULT_ASSET_MAX_TOTAL_BYTES
        ),
        asset_max_count=_int_or(source.get("SCADBUDDY_ASSET_MAX_COUNT"), DEFAULT_ASSET_MAX_COUNT),
        asset_sweep_grace=float(
            source.get("SCADBUDDY_ASSET_SWEEP_GRACE") or DEFAULT_ASSET_SWEEP_GRACE
        ),
        asset_sweep_interval=_float_or(
            source.get("SCADBUDDY_ASSET_SWEEP_INTERVAL"), DEFAULT_ASSET_SWEEP_INTERVAL
        ),
    )


def _int_or(value: str | None, default: int) -> int:
    return default if value is None or value == "" else int(value)


def _float_or(value: str | None, default: float) -> float:
    return default if value is None or value == "" else float(value)
