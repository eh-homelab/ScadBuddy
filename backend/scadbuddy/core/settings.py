from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from scadbuddy.core.config import (
    DEFAULT_CHECK_CONCURRENCY,
    DEFAULT_DATA_DIR,
    DEFAULT_FONTS_CATALOGUE_TTL,
    DEFAULT_JOB_TTL,
    DEFAULT_OPENSCAD,
    DEFAULT_RENDER_CONCURRENCY,
    DEFAULT_RENDER_TIMEOUT,
    Config,
)

CONTAINER_SEED_MODELS_DIR = Path("/app/models")
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
    check_concurrency: int = DEFAULT_CHECK_CONCURRENCY
    job_ttl: float = DEFAULT_JOB_TTL

    # SCADBUDDY_GOOGLE_FONTS_API_KEY. Unset is supported: the catalogue then comes
    # from the keyless fonts.google.com metadata instead of the Developer API.
    google_fonts_api_key: str | None = None
    fonts_catalogue_ttl: float = DEFAULT_FONTS_CATALOGUE_TTL

    seed_models_dir: Path | None = None
    frontend_dir: Path | None = None

    # Initial values for data/settings.json; the stored file wins once written.
    bambuddy_url: str | None = None
    bambuddy_api_key: str | None = None
    # The URL Bambuddy should point its sidebar entry at; usually ScadBuddy's own
    # ingress, which the server cannot infer from a request behind a proxy.
    public_url: str | None = None
    # SCADBUDDY_DEFAULT_PLATE: the printer model ("H2C", "A1 mini") whose plate the
    # preview draws while no printer has been chosen (#81).
    default_plate: str | None = None

    log_level: str = Field(default="INFO")

    # Stamped into the image by .github/workflows/build-image.yml
    # (SCADBUDDY_REVISION / SCADBUDDY_VERSION build args): the commit and the
    # image tag this process was built from. /healthz reports both, and that is
    # what eh-homelab/clusters' post-deploy check reads to prove the image it
    # pinned is the one serving — see docs in README.md, "Deploying".
    revision: str = "unknown"
    version: str = "dev"

    def to_config(self) -> Config:
        return Config(
            openscad=self.openscad,
            data_dir=self.data_dir,
            render_timeout=self.render_timeout,
            render_concurrency=self.render_concurrency,
            check_concurrency=self.check_concurrency,
            job_ttl=self.job_ttl,
            google_fonts_api_key=self.google_fonts_api_key,
            fonts_catalogue_ttl=self.fonts_catalogue_ttl,
        )

    def resolve_seed_models_dir(self) -> Path | None:
        """The models bundled with the release: the repo's own in dev, /app/models in
        the container."""
        if self.seed_models_dir is not None:
            return self.seed_models_dir if self.seed_models_dir.is_dir() else None
        return _first_directory(REPO_ROOT / "models", CONTAINER_SEED_MODELS_DIR)

    def resolve_frontend_dir(self) -> Path | None:
        """The built SPA to serve, or None when no bundle is present."""
        if self.frontend_dir is not None:
            return self.frontend_dir if self.frontend_dir.is_dir() else None
        return _first_directory(REPO_ROOT / "frontend" / "dist", CONTAINER_FRONTEND_DIR)


def _first_directory(*candidates: Path) -> Path | None:
    return next((candidate for candidate in candidates if candidate.is_dir()), None)
