from __future__ import annotations

from pathlib import Path

from pydantic import Field, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from scadbuddy.core.config import (
    DEFAULT_ASSET_MAX_COUNT,
    DEFAULT_ASSET_MAX_TOTAL_BYTES,
    DEFAULT_ASSET_SWEEP_GRACE,
    DEFAULT_ASSET_SWEEP_INTERVAL,
    DEFAULT_CHECK_CONCURRENCY,
    DEFAULT_DATA_DIR,
    DEFAULT_DATABASE_POOL_SIZE,
    DEFAULT_DUPLICATE_STAGING_MAX_AGE,
    DEFAULT_EVENT_LOG_RETENTION_ROWS,
    DEFAULT_EVENT_LOG_RETENTION_SECONDS,
    DEFAULT_FONTS_CATALOGUE_TTL,
    DEFAULT_JOB_TTL,
    DEFAULT_LIBRARY_MAX_BYTES,
    DEFAULT_LSP_SESSIONS,
    DEFAULT_MEDIA_UPLOAD_MAX_BYTES,
    DEFAULT_OPENSCAD,
    DEFAULT_OPENSCAD_LSP,
    DEFAULT_REALTIME_SOCKETS,
    DEFAULT_RENDER_CONCURRENCY,
    DEFAULT_RENDER_FALLBACK_POLL_INTERVAL,
    DEFAULT_RENDER_LATENCY_SLO,
    DEFAULT_RENDER_LEASE_TIMEOUT,
    DEFAULT_RENDER_MAX_ATTEMPTS,
    DEFAULT_RENDER_POLL_INTERVAL,
    DEFAULT_RENDER_QUEUE_DEPTH_SLO,
    DEFAULT_RENDER_QUEUE_MAX,
    DEFAULT_RENDER_QUEUE_TIMEOUT,
    DEFAULT_RENDER_TIMEOUT,
    DEFAULT_SOLID_CONCURRENCY,
    DEFAULT_TEMPORAL_NAMESPACE,
    DEFAULT_TEMPORAL_TASK_QUEUE_RENDER,
    Config,
)

CONTAINER_SEED_MODELS_DIR = Path("/app/models")
# The curated libraries the image bakes in (#169); no dev equivalent.
CONTAINER_SEED_LIBRARIES_DIR = Path("/app/libraries")
CONTAINER_FRONTEND_DIR = Path("/app/frontend/dist")

# scadbuddy/core/settings.py -> scadbuddy -> backend -> repo root
REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    """Environment configuration. Every field is overridable as ``SCADBUDDY_<FIELD>``."""

    model_config = SettingsConfigDict(env_prefix="SCADBUDDY_", extra="ignore")

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
    # SCADBUDDY_PREVIEW_RENDERS: render a model with no thumbnail and no output at
    # its default parameters, in the background, and show that as its thumbnail.
    preview_renders: bool = True
    openscad_lsp: str = DEFAULT_OPENSCAD_LSP
    lsp_sessions: int = DEFAULT_LSP_SESSIONS
    realtime_sockets: int = DEFAULT_REALTIME_SOCKETS
    library_max_bytes: int = DEFAULT_LIBRARY_MAX_BYTES
    asset_max_total_bytes: int = DEFAULT_ASSET_MAX_TOTAL_BYTES
    asset_max_count: int = DEFAULT_ASSET_MAX_COUNT
    asset_sweep_grace: float = DEFAULT_ASSET_SWEEP_GRACE
    asset_sweep_interval: float = DEFAULT_ASSET_SWEEP_INTERVAL
    duplicate_staging_max_age: float = DEFAULT_DUPLICATE_STAGING_MAX_AGE
    # The largest media upload (#274). Environment only: GET /settings reports it
    # read-only, and nothing stores an override.
    media_upload_max_bytes: int = Field(default=DEFAULT_MEDIA_UPLOAD_MAX_BYTES, gt=0)

    # SCADBUDDY_GOOGLE_FONTS_API_KEY. Unset is supported: the catalogue then comes
    # from the keyless fonts.google.com metadata instead of the Developer API.
    google_fonts_api_key: str | None = None
    fonts_catalogue_ttl: float = DEFAULT_FONTS_CATALOGUE_TTL

    seed_models_dir: Path | None = None
    seed_libraries_dir: Path | None = None
    frontend_dir: Path | None = None

    # Initial values for the stored settings (`library.settings_store`, in Postgres);
    # a value stored from the UI wins once written.
    bambuddy_url: str | None = None
    bambuddy_api_key: str | None = None
    # The URL Bambuddy should point its sidebar entry at; usually ScadBuddy's own
    # ingress, which the server cannot infer from a request behind a proxy.
    public_url: str | None = None
    # SCADBUDDY_DEFAULT_PLATE: the printer model ("H2C", "A1 mini") whose plate the
    # preview draws while no printer has been chosen (#81).
    default_plate: str | None = None

    # SCADBUDDY_DATABASE_URL: a libpq URL or DSN. Required (#401): the settings live
    # only in Postgres, and so do the render queue and an output's Bambuddy upload
    # records (#455). Server-side only, like the Bambuddy key.
    database_url: str = Field(default="", validate_default=True)
    database_pool_size: int = DEFAULT_DATABASE_POOL_SIZE

    @field_validator("database_url")
    @classmethod
    def _database_required(cls, value: str) -> str:
        if not value.strip():
            raise ValueError(
                "SCADBUDDY_DATABASE_URL is required: ScadBuddy keeps its settings and render"
                " queue in Postgres. Set it to a libpq URL, e.g."
                " postgresql://user:password@host:5432/scadbuddy"
            )
        return value

    @field_validator("database_pool_size")
    @classmethod
    def _pool_size_at_least_one(cls, value: int) -> int:
        if value < 1:
            raise ValueError(f"SCADBUDDY_DATABASE_POOL_SIZE must be at least 1, not {value}")
        return value

    # SCADBUDDY_TEMPORAL_ADDRESS: host:port of the Temporal frontend. Empty (for now)
    # keeps renders on the legacy queue; set, they run on Temporal. The final phase-1
    # PR makes it required and removes the legacy queue.
    temporal_address: str = ""
    temporal_namespace: str = DEFAULT_TEMPORAL_NAMESPACE
    temporal_task_queue_render: str = DEFAULT_TEMPORAL_TASK_QUEUE_RENDER
    # SCADBUDDY_TEMPORAL_WORKER_INPROCESS: run the render worker inside the API
    # process (one replica, dev and tests). Production runs `python -m
    # scadbuddy.worker` as its own Deployment and leaves this off.
    temporal_worker_inprocess: bool = False

    # SCADBUDDY_EVENT_LOG_RETENTION_SECONDS / _ROWS, Postgres only: how much of the
    # event log (Last-Event-ID replay, spec §7) each replica's pruning keeps. 0 is no
    # limit on that dimension.
    event_log_retention_seconds: float = DEFAULT_EVENT_LOG_RETENTION_SECONDS
    event_log_retention_rows: int = DEFAULT_EVENT_LOG_RETENTION_ROWS

    @field_validator("event_log_retention_seconds", "event_log_retention_rows")
    @classmethod
    def _retention_not_negative(cls, value: float, info: ValidationInfo) -> float:
        if value < 0:
            name = f"SCADBUDDY_{(info.field_name or '').upper()}"
            raise ValueError(f"{name} must be at least 0, not {value}")
        return value

    log_level: str = Field(default="INFO")

    # Stamped into the image by .github/workflows/build-image.yml
    # (SCADBUDDY_REVISION / SCADBUDDY_VERSION build args): the commit and the
    # image tag this process was built from. /healthz reports both, and that is
    # what eh-homelab/clusters' post-deploy check reads to prove the image it
    # pinned is the one serving — see docs in README.md, "Deploying".
    revision: str = "unknown"
    version: str = "dev"

    @field_validator("revision")
    @classmethod
    def _revision_fits_a_visibility_query(cls, value: str) -> str:
        # It is the worker's build id, which the drain puts inside a quoted visibility
        # query (workflows/client.py `drained`).
        if '"' in value or any(c.isspace() for c in value):
            raise ValueError(
                f"SCADBUDDY_REVISION must not contain a double quote or whitespace: {value!r}"
            )
        return value

    def to_config(self) -> Config:
        return Config(
            openscad=self.openscad,
            data_dir=self.data_dir,
            render_timeout=self.render_timeout,
            render_concurrency=self.render_concurrency,
            solid_concurrency=self.solid_concurrency,
            render_queue_max=self.render_queue_max,
            render_queue_timeout=self.render_queue_timeout,
            render_poll_interval=self.render_poll_interval,
            render_fallback_poll_interval=self.render_fallback_poll_interval,
            render_lease_timeout=self.render_lease_timeout,
            render_max_attempts=self.render_max_attempts,
            render_queue_depth_slo=self.render_queue_depth_slo,
            render_latency_slo=self.render_latency_slo,
            check_concurrency=self.check_concurrency,
            job_ttl=self.job_ttl,
            google_fonts_api_key=self.google_fonts_api_key,
            fonts_catalogue_ttl=self.fonts_catalogue_ttl,
            openscad_lsp=self.openscad_lsp,
            lsp_sessions=self.lsp_sessions,
            realtime_sockets=self.realtime_sockets,
            library_max_bytes=self.library_max_bytes,
            asset_max_total_bytes=self.asset_max_total_bytes,
            asset_max_count=self.asset_max_count,
            asset_sweep_grace=self.asset_sweep_grace,
            asset_sweep_interval=self.asset_sweep_interval,
            duplicate_staging_max_age=self.duplicate_staging_max_age,
            temporal_address=self.temporal_address,
            temporal_namespace=self.temporal_namespace,
            temporal_task_queue_render=self.temporal_task_queue_render,
        )

    def resolve_seed_models_dir(self) -> Path | None:
        """The models bundled with the release: the repo's own in dev, /app/models in
        the container."""
        if self.seed_models_dir is not None:
            return self.seed_models_dir if self.seed_models_dir.is_dir() else None
        return _first_directory(REPO_ROOT / "models", CONTAINER_SEED_MODELS_DIR)

    def resolve_seed_libraries_dir(self) -> Path | None:
        """The library checkouts bundled with the release: /app/libraries in the
        container, none in dev."""
        if self.seed_libraries_dir is not None:
            return self.seed_libraries_dir if self.seed_libraries_dir.is_dir() else None
        return _first_directory(CONTAINER_SEED_LIBRARIES_DIR)

    def resolve_frontend_dir(self) -> Path | None:
        """The built SPA to serve, or None when no bundle is present."""
        if self.frontend_dir is not None:
            return self.frontend_dir if self.frontend_dir.is_dir() else None
        return _first_directory(REPO_ROOT / "frontend" / "dist", CONTAINER_FRONTEND_DIR)


def _first_directory(*candidates: Path) -> Path | None:
    return next((candidate for candidate in candidates if candidate.is_dir()), None)
