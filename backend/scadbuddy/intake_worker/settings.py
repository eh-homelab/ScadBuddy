"""The intake worker's own settings (#6133, spec 2026-11-13 §7). It has no database, key or
store field: it reaches the CRUD service and Temporal only."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Final, Literal
from urllib.parse import urlsplit

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from scadbuddy.intake_worker.wire import DEFAULT_CHECK_QUEUE

#: The worker refuses to start when any of these is set: it must never hold a database
#: secret or the KEK's file (#5295, #5296), since it parses untrusted files.
FORBIDDEN_ENV: Final = (
    "SCADBUDDY_DATABASE_URL",
    "PGPASSWORD",
    "PGPASSFILE",
    "SCADBUDDY_SECRET_KEY_FILE",
)
LOG_LEVELS: Final = ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG")
HEALTH_PORT: Final = 9090


class IntakeRefusal(Exception):  # noqa: N818  (a refusal to start, not an error)
    def __init__(self, reasons: list[str]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = reasons


def check_environment(environ: Mapping[str, str]) -> None:
    reasons = [
        f"{name} is set; the intake worker holds no database secret or key"
        for name in FORBIDDEN_ENV
        if name in environ
    ]
    if reasons:
        raise IntakeRefusal(reasons)


class IntakeSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SCADBUDDY_", extra="ignore")

    #: The CRUD service (the API process).
    api_internal_url: str
    service_token_file: Path | None = None
    service_bootstrap_file: Path | None = None
    temporal_address: str
    temporal_namespace: str = "default"
    temporal_task_queue_customer_files: str = DEFAULT_CHECK_QUEUE
    #: Where downloads and the child's files go: a writable volume in a pod whose root
    #: file system is read-only.
    intake_work_dir: Path = Path("/tmp")
    #: Required, with no default, so a missing variable never falls back to the weaker
    #: mode: ``true`` hands each check to the pod's sandbox container over
    #: `intake_work_dir`, an emptyDir the two share (`runner.py`, `sandbox.py`); the
    #: deployment sets it. ``unsafe-local`` starts the child in this process's container,
    #: for development only.
    intake_sandbox: Literal["true", "unsafe-local"]
    #: Checks at once; each holds a download and a child process.
    intake_concurrency: int = 2
    log_level: str = "INFO"

    @field_validator("api_internal_url")
    @classmethod
    def _api_internal_url_is_http(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise ValueError(f"SCADBUDDY_API_INTERNAL_URL must be an http(s) URL, not {value!r}")
        return value

    @field_validator("intake_concurrency")
    @classmethod
    def _concurrency_in_range(cls, value: int) -> int:
        if not 1 <= value <= 16:
            raise ValueError("SCADBUDDY_INTAKE_CONCURRENCY must be between 1 and 16")
        return value

    @field_validator("log_level")
    @classmethod
    def _log_level_known(cls, value: str) -> str:
        level = value.strip().upper()
        if level not in LOG_LEVELS:
            raise ValueError(
                f"SCADBUDDY_LOG_LEVEL must be one of {', '.join(LOG_LEVELS)}, not {value!r}"
            )
        return level

    @model_validator(mode="after")
    def _one_proof(self) -> IntakeSettings:
        if (self.service_token_file is None) == (self.service_bootstrap_file is None):
            raise ValueError(
                "set exactly one of SCADBUDDY_SERVICE_TOKEN_FILE and"
                " SCADBUDDY_SERVICE_BOOTSTRAP_FILE"
            )
        return self
