from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, Literal
from urllib.parse import urlsplit

from pydantic import Field, ValidationError, ValidationInfo, field_validator
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
    DEFAULT_RENDER_LATENCY_SLO,
    DEFAULT_RENDER_QUEUE_DEPTH_SLO,
    DEFAULT_RENDER_QUEUE_MAX,
    DEFAULT_RENDER_TIMEOUT,
    DEFAULT_SOLID_CONCURRENCY,
    DEFAULT_STORE_MAX_COUNT,
    DEFAULT_STORE_MAX_TOTAL_BYTES,
    DEFAULT_TEMPORAL_NAMESPACE,
    DEFAULT_TEMPORAL_TASK_QUEUE_RENDER,
    DEFAULT_WORKER_CACHE_MAX_BYTES,
    Config,
    StoreBackend,
)
from scadbuddy.core.proxies import Network, parse_cidr_list

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
    # The largest media upload (#274). Env-seeded like the rest (#322): Settings can
    # change it, and the upload gate reads the value in effect on every request.
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
    # SCADBUDDY_BAMBUDDY_WEB_URLS: comma-separated URLs browsers reach Bambuddy at
    # (#775), when `bambuddy_url` is one only the server can (an in-cluster Service).
    # The first is where links point; the others are other hostnames of the same
    # Bambuddy, which a link follows when ScadBuddy is framed by one of them.
    # Unset, links use `bambuddy_url`.
    bambuddy_web_urls: str | None = None
    # SCADBUDDY_BAMBUDDY_RENDER_API_KEY / SCADBUDDY_STORE_BACKEND seed the stored values
    # (`library.settings_store`, ENV_SEEDED), like `bambuddy_api_key`: a value saved in
    # Settings wins. Render workers read them from there (spec §9).
    bambuddy_render_api_key: str | None = None
    store_backend: StoreBackend = "local"
    # SCADBUDDY_STORE_MAX_TOTAL_BYTES / _MAX_COUNT: the store's caps (spec §6.2); a put
    # past either is refused unless the content is already stored. 0 is no limit.
    store_max_total_bytes: int = DEFAULT_STORE_MAX_TOTAL_BYTES
    store_max_count: int = DEFAULT_STORE_MAX_COUNT
    worker_cache_max_bytes: int = DEFAULT_WORKER_CACHE_MAX_BYTES
    # The URL Bambuddy should point its sidebar entry at; usually ScadBuddy's own
    # ingress, which the server cannot infer from a request behind a proxy.
    public_url: str | None = None
    # SCADBUDDY_ALLOWED_ORIGINS: comma-separated origins the UI is ALSO served under,
    # besides the public URL's — the LAN hostname when the public URL is an SSO
    # proxy, say. A browser's `Origin` on the realtime socket (`api/realtime.py`, #266)
    # and on every write (`api/cross_site.py`, #962) must be one of them; with only
    # the public URL, whichever other hostname the same deployment answers on shows
    # "Live updates unavailable" and cannot save anything. Not a stored
    # setting: like the agent's SCADBUDDY_AGENT_TRUSTED_PROXIES, it decides which
    # pages may reach the server, so it belongs to the deployment.
    allowed_origins: str = ""

    # SCADBUDDY_TRUSTED_PROXIES: comma-separated CIDRs (a bare address is one host) whose
    # `X-Forwarded-For` is believed, and then only its last value (`core/proxies.py`, the
    # agent's SCADBUDDY_AGENT_TRUSTED_PROXIES rules). The browser trace relay's per-client
    # rate limit keys on it (spec 2026-10-01 §5.2). Empty, no forwarding header is
    # believed and every browser behind the gateway shares one client bucket (uvicorn
    # runs with --no-proxy-headers in the image, so it believes none either). Not a
    # stored setting: it decides who the server believes about who it is talking to.
    trusted_proxies: str = ""

    @field_validator("trusted_proxies")
    @classmethod
    def _trusted_proxies_are_cidrs(cls, value: str) -> str:
        parse_cidr_list(value)
        return value

    @field_validator("bambuddy_web_urls")
    @classmethod
    def _web_urls_are_http(cls, value: str | None) -> str | None:
        for url in split_urls(value):
            parts = urlsplit(url)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                raise ValueError(f"SCADBUDDY_BAMBUDDY_WEB_URLS: {url!r} is not an http(s) URL")
        return value

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

    @field_validator("temporal_address")
    @classmethod
    def _temporal_required(cls, value: str) -> str:
        if not value.strip():
            raise ValueError(
                "SCADBUDDY_TEMPORAL_ADDRESS is required: ScadBuddy renders on Temporal. Set"
                " it to the Temporal frontend's host:port, e.g. temporal-frontend:7233"
            )
        return value

    @field_validator("database_pool_size")
    @classmethod
    def _pool_size_at_least_one(cls, value: int) -> int:
        if value < 1:
            raise ValueError(f"SCADBUDDY_DATABASE_POOL_SIZE must be at least 1, not {value}")
        return value

    # SCADBUDDY_TEMPORAL_ADDRESS: host:port of the Temporal frontend. Required
    # (#546): every render runs on Temporal (spec 2026-09-27 §3.1).
    temporal_address: str = Field(default="", validate_default=True)
    temporal_namespace: str = DEFAULT_TEMPORAL_NAMESPACE
    temporal_task_queue_render: str = DEFAULT_TEMPORAL_TASK_QUEUE_RENDER
    # SCADBUDDY_TEMPORAL_TASK_QUEUE_BAMBUDDY: where print runs and the Bambuddy writes
    # run (#1052, spec 2026-10-01 §4.3), served by `python -m scadbuddy.worker --queue
    # bambuddy` (#1060).
    temporal_task_queue_bambuddy: str = "bambuddy"
    # SCADBUDDY_TEMPORAL_TASK_QUEUE_LIBRARY: where the housekeeping Schedule's sweeps
    # run (#1054, spec 2026-10-01 §4.3, §4.4); this process serves it.
    temporal_task_queue_library: str = "library"
    # SCADBUDDY_TEMPORAL_SEARCH_ATTRIBUTES: upsert the Scadbuddy* Search Attributes
    # (spec 2026-10-01 §4.2). Off until the namespace has them registered: an upsert of
    # an unregistered attribute fails the workflow task.
    temporal_search_attributes: bool = False
    # SCADBUDDY_TEMPORAL_WORKER_INPROCESS: run the render worker inside the API
    # process (one replica, dev and tests). Production runs `python -m
    # scadbuddy.worker` as its own Deployment and leaves this off.
    temporal_worker_inprocess: bool = False
    # SCADBUDDY_TEMPORAL_PRINT_WORKER_INPROCESS: serve the `bambuddy` queue inside the
    # API process (#1060), as SCADBUDDY_TEMPORAL_WORKER_INPROCESS does with every queue,
    # for a deployment without the `scadbuddy-print` worker.
    temporal_print_worker_inprocess: bool = False
    # SCADBUDDY_API_INTERNAL_URL: the API's cluster-internal URL, which the print worker
    # (`--queue bambuddy`) reads outputs through (#1060, spec 2026-10-01 §5.5). Only that
    # worker reads it.
    api_internal_url: str | None = None
    # SCADBUDDY_TEMPORAL_UI_URL: the Temporal web UI, which Settings → Administration
    # links to (#668). Empty (the default) shows no link.
    temporal_ui_url: str | None = None

    @field_validator("api_internal_url")
    @classmethod
    def _api_internal_url_is_http(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise ValueError(f"SCADBUDDY_API_INTERNAL_URL must be an http(s) URL, not {value!r}")
        return value

    @field_validator("temporal_ui_url")
    @classmethod
    def _temporal_ui_url_is_http(cls, value: str | None) -> str | None:
        # It becomes a link on the Settings page, so only an http(s) URL is one.
        if value is None or not value.strip():
            return None
        parts = urlsplit(value)
        # A host, and no whitespace anywhere: "https://" or "http:// x" is no link.
        if (
            parts.scheme not in {"http", "https"}
            or not parts.netloc
            or any(char.isspace() for char in value)
        ):
            raise ValueError(f"SCADBUDDY_TEMPORAL_UI_URL must be an http(s) URL, not {value!r}")
        return value

    @field_validator(
        "temporal_address",
        "temporal_namespace",
        "temporal_task_queue_render",
        "temporal_task_queue_bambuddy",
        "temporal_task_queue_library",
    )
    @classmethod
    def _temporal_without_whitespace(cls, value: str, info: ValidationInfo) -> str:
        # As `database_url`: a value that is only whitespace would read as "set" (the
        # Temporal path) with a garbage address. Only the address may be empty.
        name = f"SCADBUDDY_{(info.field_name or '').upper()}"
        if value != value.strip():
            raise ValueError(f"{name} must not start or end with whitespace: {value!r}")
        if not value and info.field_name != "temporal_address":
            raise ValueError(f"{name} must not be empty")
        return value

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

    @field_validator("log_level")
    @classmethod
    def _log_level_known(cls, value: str) -> str:
        level = value.strip().upper()
        if level not in LOG_LEVELS:
            raise ValueError(
                f"SCADBUDDY_LOG_LEVEL must be one of {', '.join(LOG_LEVELS)}, not {value!r}"
            )
        return level

    # Stamped into the image by .github/workflows/build-image.yml
    # (SCADBUDDY_REVISION / SCADBUDDY_VERSION build args): the commit and the
    # image tag this process was built from. /healthz reports both, and that is
    # what eh-homelab/clusters' post-deploy check reads to prove the image it
    # pinned is the one serving — see docs in README.md, "Deploying".
    revision: str = "unknown"
    version: str = "dev"

    @property
    def allowed_origin_list(self) -> list[str]:
        """`SCADBUDDY_ALLOWED_ORIGINS` split on commas, blanks dropped."""
        return [item.strip() for item in self.allowed_origins.split(",") if item.strip()]

    @property
    def trusted_proxy_networks(self) -> tuple[Network, ...]:
        """`SCADBUDDY_TRUSTED_PROXIES` parsed; the validator has already refused a bad one."""
        return parse_cidr_list(self.trusted_proxies)

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
            store_max_total_bytes=self.store_max_total_bytes,
            store_max_count=self.store_max_count,
            worker_cache_max_bytes=self.worker_cache_max_bytes,
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


#: What ``log_level`` takes: the standard library's own level names.
LOG_LEVELS: Final = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

#: The fields the UI cannot set (#322), each with the reason Settings shows beside it.
#: Every other field of :class:`Settings` is env-seeded: ``SCADBUDDY_<FIELD>`` seeds it,
#: a value saved in the UI wins, and a reset goes back to the environment, then the
#: default. ``tests/test_settings_coverage.py`` fails when a field is neither, so an
#: environment-only setting cannot slip in unnoticed.
BOOTSTRAP_FIELDS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "data_dir": "The data volume. It is needed before any stored setting can be read.",
        "database_url": (
            "Where the settings themselves are kept, so the UI cannot choose it; it is also a"
            " credential."
        ),
        "database_pool_size": "Sizes the connection pool the settings are read through.",
        "allowed_origins": (
            "Which pages may open the realtime socket and make writes. Like the agent's"
            " trusted proxies, it"
            " decides who can reach the server, so it belongs to the deployment."
        ),
        "trusted_proxies": (
            "Which peers are believed about the client they forward for. Like the allowed"
            " origins, it decides who the server believes it is talking to, so it belongs"
            " to the deployment."
        ),
        "seed_models_dir": "Image layout, fixed when the image is built.",
        "seed_libraries_dir": "Image layout, fixed when the image is built.",
        "frontend_dir": "Image layout, fixed when the image is built.",
        "openscad": (
            "The binary the server runs. Choosing it from a web form would let anyone who can"
            " reach the page run any program, and the verified OpenSCAD facts are tied to the"
            " image's own build."
        ),
        "openscad_lsp": (
            "The binary the server runs for the editor. Choosing it from a web form would let"
            " anyone who can reach the page run any program."
        ),
        "temporal_address": (
            "Where renders run (#424). The render workers (`python -m scadbuddy.worker`) take"
            " it from their own environment, so the deployment points the API and its workers"
            " together; a form only the API reads would split them."
        ),
        "temporal_namespace": "Paired with the Temporal address; set with it by the deployment.",
        "temporal_task_queue_render": (
            "Paired with the Temporal address: the API and the render workers must name the"
            " same queue, and only the deployment sets both."
        ),
        "temporal_task_queue_bambuddy": (
            "Paired with the Temporal address: the API starts print runs on it, and the"
            " worker that serves it must name the same queue."
        ),
        "temporal_task_queue_library": (
            "Paired with the Temporal address: the housekeeping Schedule starts its sweeps on"
            " it, and this process serves it."
        ),
        "temporal_search_attributes": (
            "Whether the namespace has ScadBuddy's Search Attributes registered, which the"
            " deployment's Temporal manifests decide."
        ),
        "temporal_worker_inprocess": (
            "Whether this process runs a render worker at all, decided by how the deployment"
            " is laid out (one replica, or a separate worker Deployment)."
        ),
        "temporal_print_worker_inprocess": (
            "Whether this process serves the print queue itself, decided by whether the"
            " deployment runs the `scadbuddy-print` worker."
        ),
        "api_internal_url": (
            "Where the print worker reaches the API inside the cluster: the deployment's"
            " Service, read only by that worker."
        ),
        "revision": "A build stamp that /healthz reports, not a setting.",
        "version": "A build stamp that /healthz reports, not a setting.",
    }
)

#: The env-seeded fields, in declaration order.
ENV_SEEDED: Final = tuple(name for name in Settings.model_fields if name not in BOOTSTRAP_FIELDS)

#: Env-seeded fields that are credentials: written, never read back (only "set").
SECRET_FIELDS: Final = frozenset(
    {"bambuddy_api_key", "bambuddy_render_api_key", "google_fonts_api_key"}
)

Applies = Literal["live", "restart"]

#: When a changed value takes effect in a running process (#322). ``live`` is at once,
#: on every replica (``api/runtime.py`` applies it on ``settings.changed``); ``restart``
#: is at the next start, and until then ``GET /settings`` lists the field in
#: ``restart_required``. A new env-seeded field must say which it is.
APPLIES: Final[Mapping[str, Applies]] = MappingProxyType(
    {
        # Read from the store on every use.
        "bambuddy_url": "live",
        "bambuddy_api_key": "live",
        "bambuddy_web_urls": "live",
        "public_url": "live",
        # Read from the store by every GET /settings.
        "temporal_ui_url": "live",
        "default_plate": "live",
        # The upload gate asks for the value in effect on every request.
        "media_upload_max_bytes": "live",
        # Read from the queue's config by each job, poll or admission check.
        "render_timeout": "live",
        "job_ttl": "live",
        "solid_concurrency": "live",
        "render_queue_max": "live",
        "render_queue_depth_slo": "live",
        "render_latency_slo": "live",
        # Sizes the worker tasks and the thumbnail pool, which are built at start.
        "render_concurrency": "restart",
        # Semaphores: resizing one with permits out would over- or under-admit.
        "check_concurrency": "restart",
        "lsp_sessions": "restart",
        "realtime_sockets": "restart",
        # Decides at start whether the preview scheduler runs at all.
        "preview_renders": "restart",
        # Decides at start whether the periodic sweep runs at all.
        "asset_sweep_interval": "restart",
        # Attributes of the stores that read them on every use.
        "library_max_bytes": "live",
        "asset_max_total_bytes": "live",
        "asset_max_count": "live",
        "asset_sweep_grace": "live",
        "duplicate_staging_max_age": "live",
        "event_log_retention_seconds": "live",
        "event_log_retention_rows": "live",
        # A key change swaps the client and refetches the catalogue.
        "google_fonts_api_key": "live",
        "fonts_catalogue_ttl": "live",
        "log_level": "live",
        # Read at start, by the API and by every render worker (spec 2026-09-27 §6.2, §9):
        # the store each process opens, its caps, and the key workers fetch with.
        "store_backend": "restart",
        "bambuddy_render_api_key": "restart",
        "store_max_total_bytes": "restart",
        "store_max_count": "restart",
        "worker_cache_max_bytes": "restart",
    }
)


def split_urls(value: str | None) -> list[str]:
    """A comma-separated URL list, blanks dropped, each without its trailing slash."""
    return [url.strip().rstrip("/") for url in (value or "").split(",") if url.strip()]


def env_var(name: str) -> str:
    """The environment variable that seeds ``name``."""
    return f"SCADBUDDY_{name.upper()}"


def check_value(name: str, value: Any) -> Any:
    """``value`` for the env-seeded field ``name``, coerced, or ``ValueError`` naming it.

    The bounds are the ones a deployment's ``SCADBUDDY_<FIELD>`` meets: the field's own
    type and validators on :class:`Settings`, then :class:`Config`'s checks. Every other
    field is left at its default for the check, so a failure is this field's.
    """
    probe = Settings.model_construct()
    try:
        Settings.__pydantic_validator__.validate_assignment(probe, name, value)
    except ValidationError as error:
        message = error.errors()[0]["msg"].removeprefix("Value error, ")
        if not message.startswith(env_var(name)):
            message = f"{env_var(name)}: {message}"
        raise ValueError(message) from None
    probe.to_config()
    return getattr(probe, name)


def _first_directory(*candidates: Path) -> Path | None:
    return next((candidate for candidate in candidates if candidate.is_dir()), None)
